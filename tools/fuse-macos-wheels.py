#!/usr/bin/env python3
"""Fuses one x86_64 and one arm64 macOS wheel into a single universal2 wheel.

maturin can emit a universal2 wheel only by invoking `lipo`, which lives in the
Xcode toolchain; on a runner that has it the flag is `--target
universal2-apple-darwin`, and rustc has no such target, so maturin builds the two
halves itself and fuses them with lipo. This script is that step with the fusing
done by tools/lipo-dylib.py instead, so the workflow does not depend on a tool
location and the fused file is checked rather than assumed.

    python3 tools/fuse-macos-wheels.py --out dist \
        dist/chrome_client-0.2.8-cp39-abi3-macosx_10_12_x86_64.whl \
        dist/chrome_client-0.2.8-cp39-abi3-macosx_11_0_arm64.whl

Both inputs must agree on everything except the platform tag and the native
extension. A wheel whose pure-Python half differed between the two builds is not
a wheel this script can fuse, and merging them would pick one half and drop the
other without saying so -- so that is a failure, not a merge.
"""

import argparse
import base64
import hashlib
import os
import pathlib
import re
import shutil
import sys
import tempfile
import zipfile

import importlib.util

#: tools/ names its scripts with hyphens, so the fusor cannot be imported by
#: name. Load it from next to this file instead of renaming it: the hyphen is
#: the convention every other tool here follows.
_LIPO = pathlib.Path(__file__).resolve().parent / "lipo-dylib.py"
_spec = importlib.util.spec_from_file_location("lipo_dylib", _LIPO)
lipo_dylib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lipo_dylib)

#: Wheel platform tags this script understands: macosx_<major>_<minor>_<arch>.
PLATFORM_RE = re.compile(r"^macosx_(\d+)_(\d+)_([a-z0-9_]+)$")
#: The extension module maturin puts at the root of every wheel.
NATIVE_SUFFIX = ".so"


def split_wheel_name(path):
    """Returns (stem, python tag, abi tag, platform tag) for a wheel filename."""
    parts = path.name[:-len(".whl")].split("-")
    if len(parts) not in (5, 6):
        raise SystemExit("%s: not a PEP 427 wheel filename" % path)
    stem = "-".join(parts[:-3])
    python_tag, abi_tag, platform_tag = parts[-3:]
    return stem, python_tag, abi_tag, platform_tag


def record_hash(data):
    digest = hashlib.sha256(data).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def read_wheel(path):
    """Returns {name: bytes} for every member, rejecting directory entries."""
    members = {}
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            members[info.filename] = archive.read(info)
    return members


def native_members(members):
    """The extension modules and the dylibs the wheel ships, by relative path."""
    return sorted(name for name in members
                  if name.endswith(".so") or name.endswith(".dylib"))


def rewrite_wheel_metadata(members, universal_tag):
    """Points the WHEEL file at the fused platform tag.

    The Tag lines are what pip reads; the filename is what it dispatches on.
    A wheel whose filename says universal2 and whose metadata says x86_64
    installs on arm64 and then fails to import, so both have to move together.
    """
    wheel_name = next(name for name in members
                      if name.endswith(".dist-info/WHEEL"))
    text = members[wheel_name].decode("utf-8")
    lines = text.splitlines(keepends=True)
    out = []
    for line in lines:
        if line.startswith("Tag:"):
            _, _, value = line.partition(":")
            python_tag, abi_tag, _ = value.strip().split("-")
            out.append("Tag: %s-%s-%s\n" % (python_tag, abi_tag, universal_tag))
        else:
            out.append(line)
    members[wheel_name] = "".join(out).encode("utf-8")


def rewrite_record(members):
    """Rebuilds RECORD from what is actually in the archive.

    New lines are appended rather than rewriting every existing line: the
    original RECORD already has the right hash for every file this script does
    not touch, and recomputing all of them would hide a corruption in one of
    those files instead of preserving it for the audit to find.
    """
    record_name = next(name for name in members
                       if name.endswith(".dist-info/RECORD"))
    lines = members[record_name].decode("utf-8").splitlines()
    hashed = {line.rsplit(",", 2)[0]: line for line in lines if line}
    for name, data in members.items():
        if name == record_name:
            continue
        hashed[name] = "%s,%s,%d" % (name, record_hash(data), len(data))
    hashed[record_name] = record_name + ",,"
    members[record_name] = ("\n".join(hashed[name] for name in sorted(hashed))
                            + "\n").encode("utf-8")


def fuse(inputs, output_dir):
    if len(inputs) != 2:
        raise SystemExit("expected exactly two wheels, got %d" % len(inputs))
    wheels = []
    for path in inputs:
        stem, python_tag, abi_tag, platform_tag = split_wheel_name(path)
        match = PLATFORM_RE.match(platform_tag)
        if not match:
            raise SystemExit("%s: platform tag %r is not macosx_<v>_<arch>"
                             % (path, platform_tag))
        major, minor, arch = int(match.group(1)), int(match.group(2)), match.group(3)
        if arch not in ("x86_64", "arm64"):
            raise SystemExit("%s: %s is not one of the two slices to fuse"
                             % (path, arch))
        members = read_wheel(path)
        natives = native_members(members)
        if not natives:
            raise SystemExit("%s: holds no .so or .dylib to fuse" % path)
        wheels.append(dict(path=path, stem=stem, python_tag=python_tag,
                           abi_tag=abi_tag, arch=arch, version=(major, minor),
                           members=members, natives=natives))

    first, second = wheels
    for key in ("stem", "python_tag", "abi_tag", "natives"):
        if first[key] != second[key]:
            raise SystemExit(
                "the two wheels disagree on %s: %r vs %r; they were not built "
                "from the same source, so fusing them would ship one half "
                "silently" % (key, first[key], second[key]))
    if first["arch"] == second["arch"]:
        raise SystemExit("both wheels carry %s; nothing to fuse" % first["arch"])
    # The deployment target is deliberately *not* required to match: building for
    # x86_64 and for arm64 legitimately picks different macOS floors, and both
    # wheels are valid inputs. The fused wheel takes the higher of the two, which
    # is the only floor satisfied by both slices, and the difference is reported
    # rather than absorbed, because raising an arm64 wheel from 11.0 to 12.0 plus
    # keep the tag honest is a decision a reader should be able to see.
    # Lexicographic, not element-wise: 11.0 is newer than 10.12, but an
    # element-wise max would produce the non-existent 11.12.
    floor = max(first["version"], second["version"])
    if first["version"] != second["version"]:
        print("deployment target: %d.%d and %d.%d -> %d.%d" % (
            first["version"][0], first["version"][1],
            second["version"][0], second["version"][1], floor[0], floor[1]))

    unified = dict(first)
    unified_members = dict(first["members"])
    fused_bytes = 0
    with tempfile.TemporaryDirectory() as scratch:
        scratch = pathlib.Path(scratch)
        for wheel in wheels:
            for name in wheel["natives"]:
                target = scratch / wheel["arch"] / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(wheel["members"][name])
        for name in first["natives"]:
            slices = [scratch / wheel["arch"] / name for wheel in wheels]
            output = scratch / "fused" / name
            output.parent.mkdir(parents=True, exist_ok=True)
            status = lipo_dylib.main(["--output", str(output)]
                                     + [str(slice_) for slice_ in slices])
            if status != 0:
                raise SystemExit("%s: fusing failed" % name)
            unified_members[name] = output.read_bytes()
            fused_bytes += len(unified_members[name])

    universal = "macosx_%d_%d_universal2" % floor
    rewrite_wheel_metadata(unified_members, universal)
    rewrite_record(unified_members)

    name = "%s-%s-%s-%s.whl" % (first["stem"], first["python_tag"],
                                first["abi_tag"], universal)
    destination = output_dir / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Sort the members so two runs over the same inputs produce the same bytes;
    # an archive ordered by dict iteration would differ per Python version and
    # make the wheel unattributable to its inputs.
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for member in sorted(unified_members):
            archive.writestr(member, unified_members[member])
    print("%s: %s + %s -> %s (%d fused bytes)"
          % (destination, first["arch"], second["arch"], universal, fused_bytes))
    return 0


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("wheels", nargs="+", type=pathlib.Path)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    args = parser.parse_args(argv)
    return fuse(args.wheels, args.out)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
