#!/usr/bin/env python3
"""Raises a macOS wheel tag so it covers the Mach-O files the wheel carries.

A wheel tag is a promise: "this installs on macOS X.Y and works". Nothing in
the build chain checks that promise against the binary it wraps, and the two
disagree today:

  * the Core dylib is built by Chromium, whose mac_deployment_target and
    mac_min_system_version are both 13.0 (build/config/mac/mac_sdk.gni:29,38),
    so its LC_BUILD_VERSION says minos 13.0;
  * maturin tags the wheel from its own default deployment target, which is
    macosx_10_12 for x86_64 and macosx_11_0 for arm64;
  * delocate bundles the dylib through install_name_tool, which rewrites the
    install name and nothing else -- the 13.0 survives.

The result installs on macOS 10.12-12 and then fails at import time, because
dlopen refuses a dylib whose minimum OS is newer than the running system.
Those two versions cannot import the module and cannot be told why, so the tag
has to tell the truth instead: this script raises it to the highest minimum
declared by any Mach-O in the wheel, and rewrites both the filename and the
WHEEL metadata entry, because pip dispatches on the filename and verifies
nothing, while every tool that reads the metadata believes what it says.

    python3 tools/macos-wheel-floor.py dist/*.whl
    python3 tools/macos-wheel-floor.py --check dist/*.whl   # assert only

--check is for tests and for anyone who would rather fail the build than move
the tag; the default rewrites the wheel in place.
"""

import argparse
import base64
import hashlib
import pathlib
import re
import struct
import sys
import zipfile

#: Mach-O load commands that carry a minimum-OS version.
LC_VERSION_MIN_MACOSX = 0x24
LC_BUILD_VERSION = 0x32
MACHO_HEADER_64 = 32
#: 0xcafebabe is big-endian in the file. Reading it little-endian makes every
#: universal binary look thin and silently skips the per-slice check, which is
#: the bug this comment is here to stop coming back.
FAT_MAGIC = 0xCAFEBABE
FAT_ARCH_SIZE = 20

#: cputype -> the architecture name a wheel platform tag uses.
CPU_TO_ARCH = {0x01000007: "x86_64", 0x0100000C: "arm64", 0x00000007: "i386"}

PLATFORM_RE = re.compile(r"^macosx_(\d+)_(\d+)_(.+)$")


def decode_version(value):
    return (value >> 16, (value >> 8) & 0xFF, value & 0xFF)


def thin_min_os(data):
    """Highest minimum macOS version declared by a single-architecture Mach-O."""
    ncmds = struct.unpack_from("<I", data, 16)[0]
    offset = MACHO_HEADER_64
    floors = []
    for _ in range(ncmds):
        cmd, size = struct.unpack_from("<II", data, offset)
        if cmd == LC_BUILD_VERSION:
            floors.append(decode_version(struct.unpack_from("<I", data, offset + 12)[0]))
        elif cmd == LC_VERSION_MIN_MACOSX:
            floors.append(decode_version(struct.unpack_from("<I", data, offset + 8)[0]))
        offset += size
    return max(floors) if floors else None


def slices_of(data):
    """Yields (cputype, bytes) for a thin or universal Mach-O."""
    if struct.unpack_from(">I", data, 0)[0] == FAT_MAGIC:
        count = struct.unpack_from(">I", data, 4)[0]
        for index in range(count):
            base = 8 + index * FAT_ARCH_SIZE
            cputype = struct.unpack_from(">i", data, base)[0]
            offset, size = struct.unpack_from(">II", data, base + 8)
            yield cputype, data[offset:offset + size]
    else:
        yield struct.unpack_from("<i", data, 4)[0], data


def split_platform(tag):
    match = PLATFORM_RE.match(tag)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), match.group(3)


def split_wheel_name(path):
    parts = path.name[:-len(".whl")].split("-")
    if len(parts) not in (5, 6):
        raise SystemExit("%s: not a PEP 427 wheel filename" % path)
    return parts


def read_members(path):
    with zipfile.ZipFile(path) as archive:
        return {info.filename: archive.read(info)
                for info in archive.infolist() if not info.is_dir()}


def record_hash(data):
    digest = hashlib.sha256(data).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def rewrite_record(members):
    """Recomputes RECORD from what is actually in the archive."""
    name = next(n for n in members if n.endswith(".dist-info/RECORD"))
    lines = members[name].decode("utf-8").splitlines()
    rows = {line.rsplit(",", 2)[0]: line for line in lines if line}
    for member, data in members.items():
        if member == name:
            continue
        rows[member] = "%s,%s,%d" % (member, record_hash(data), len(data))
    rows[name] = name + ",,"
    members[name] = ("\n".join(rows[key] for key in sorted(rows)) + "\n").encode("utf-8")


def rewrite_tags(members, old_tag, new_tag):
    """Repoints every macosx Tag: line at the raised platform, keeping the ABI."""
    name = next(n for n in members if n.endswith(".dist-info/WHEEL"))
    out = []
    replaced = 0
    for line in members[name].decode("utf-8").splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("Tag:") and stripped.rsplit("-", 1)[-1] == old_tag:
            abi = stripped.split("Tag:", 1)[1].strip()[:-len(old_tag) - 1]
            out.append("Tag: %s-%s\n" % (abi, new_tag))
            replaced += 1
        else:
            out.append(line)
    if replaced == 0:
        raise SystemExit("%s: WHEEL declares no Tag: line ending in %s"
                         % (name, old_tag))
    members[name] = "".join(out).encode("utf-8")
    return replaced


def floor_of(members, tag_arch):
    """Returns the highest minimum macOS version across every Mach-O member."""
    natives = sorted(n for n in members
                     if n.endswith(".so") or n.endswith(".dylib"))
    if not natives:
        raise SystemExit("wheel holds no .so or .dylib; the Core dylib never made "
                         "it in, so delocate did not bundle it")
    floor = (0, 0)
    matched = 0
    for name in natives:
        for cputype, blob in slices_of(members[name]):
            arch = CPU_TO_ARCH.get(cputype)
            if arch is None:
                raise SystemExit("%s: unknown Mach-O cputype %#x" % (name, cputype))
            # A slice of another architecture cannot affect what this wheel does
            # on the architecture it names: the loader picks the matching slice
            # and ignores the rest. Skipping it (rather than failing) keeps a
            # universal dylib inside a per-arch wheel from breaking the build,
            # which is a benign file layout, not a defect.
            if arch != tag_arch:
                print("%s: ignoring the %s slice; the tag says %s"
                      % (name, arch, tag_arch), file=sys.stderr)
                continue
            found = thin_min_os(blob)
            if found is None:
                raise SystemExit("%s: Mach-O declares no minimum macOS version" % name)
            matched += 1
            floor = max(floor, found[:2])
    if matched == 0:
        raise SystemExit("no %s slice in any .so or .dylib; the tag names an "
                         "architecture this wheel does not carry" % tag_arch)
    return floor


def raise_wheel(path, check_only):
    parts = split_wheel_name(path)
    platform_tag = parts[-1]
    parsed = split_platform(platform_tag)
    if parsed is None:
        raise SystemExit("%s: platform tag %r is not macosx_<major>_<minor>_<arch>"
                         % (path, platform_tag))
    tag_floor = (parsed[0], parsed[1])
    tag_arch = parsed[2]

    members = read_members(path)
    floor = floor_of(members, tag_arch)

    if floor <= tag_floor:
        print("%s: tag %s covers every Mach-O (highest minimum %d.%d)"
              % (path.name, platform_tag, floor[0], floor[1]))
        return 0

    if check_only:
        print("%s: tag says macOS %d.%d but a Mach-O inside requires %d.%d"
              % (path.name, tag_floor[0], tag_floor[1], floor[0], floor[1]),
              file=sys.stderr)
        return 1

    new_platform = "macosx_%d_%d_%s" % (floor[0], floor[1], tag_arch)
    rewrite_tags(members, platform_tag, new_platform)
    rewrite_record(members)

    destination = path.with_name("-".join(parts[:-1] + [new_platform]) + ".whl")
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for member in sorted(members):
            archive.writestr(member, members[member])
    print("%s -> %s: a Mach-O inside requires macOS %d.%d, above the %d.%d the "
          "tag claimed" % (path.name, destination.name, floor[0], floor[1],
                           tag_floor[0], tag_floor[1]))
    path.unlink()
    return 0


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("wheels", nargs="+", type=pathlib.Path)
    parser.add_argument("--check", action="store_true",
                        help="fail instead of raising the tag")
    args = parser.parse_args(argv)
    status = 0
    for path in args.wheels:
        status |= raise_wheel(path, args.check)
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
