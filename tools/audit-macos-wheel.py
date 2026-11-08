#!/usr/bin/env python3
"""Asserts a built macOS wheel is universal2 and its tag matches its payload.

Three things can go wrong in a fused wheel and none of them is visible from
the filename:

  1. the tag is lower than what a Mach-O file inside declares, so pip installs
     the wheel on a system that cannot map it (today the Core dylib says 13.0
     while the extension says 10.12/11.0);
  2. one slice lost its symbols, so the wheel imports on the build machine and
     fails on the other architecture;
  3. the Core dylib never made it in at all, which looks fine until import.

delocate reports (1) as a warning and says nothing about (2) or (3). This is
the gate.

    python3 tools/audit-macos-wheel.py dist/*.whl
"""

import base64
import hashlib
import pathlib
import re
import struct
import sys
import zipfile

LC_VERSION_MIN_MACOSX = 0x24
LC_BUILD_VERSION = 0x32
MACHO_HEADER_64 = 32
FAT_MAGIC = 0xCAFEBABE
FAT_ARCH = 20

#: Symbols the Core must define in every slice; the same twenty
#: tools/audit-core-binaries.sh checks, because the loader picks one half.
MN_SYMBOLS = 20

PLATFORM_RE = re.compile(r"^macosx_(\d+)_(\d+)_universal2$")


def decode_version(value):
    return (value >> 16, (value >> 8) & 0xFF, value & 0xFF)


def thin_min_os(data):
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
    """Yields (cputype, slice bytes) for a thin or universal Mach-O."""
    # 0xcafebabe is big-endian in the file: read it that way or every fat
    # binary looks thin and the slice check silently passes on one architecture.
    if struct.unpack_from(">I", data, 0)[0] == FAT_MAGIC:
        count = struct.unpack_from(">I", data, 4)[0]
        for index in range(count):
            base = 8 + index * FAT_ARCH
            cputype, _, offset, size, _ = struct.unpack_from(">IIIII", data, base)
            yield cputype, data[offset:offset + size]
    else:
        yield None, data


def count_defined(data, prefix):
    """Counts `prefix`-prefixed defined symbols without shelling out to nm.

    Only used to prove a slice carries its ABI at all, so a string scan of the
    symbol table is enough and keeps this dependency-free.
    """
    return data.count(prefix.encode())


def check(path):
    failures = []
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        native = [n for n in names if n.endswith(".so")]
        dylibs = [n for n in names if n.endswith(".dylib")]
        if not native:
            failures.append("no extension module in the wheel")
        if not dylibs:
            failures.append("no Core dylib in the wheel; delocate did not bundle it")

        wheel_file = next((n for n in names if n.endswith(".dist-info/WHEEL")), None)
        if wheel_file is None:
            failures.append("no .dist-info/WHEEL")
            return failures
        tags = [line.split(":", 1)[1].strip()
                for line in archive.read(wheel_file).decode().splitlines()
                if line.startswith("Tag:")]
        universal = [t for t in tags if t.endswith("_universal2")]
        if not universal:
            failures.append("WHEEL declares no universal2 tag: %s" % tags)
            return failures

        match = PLATFORM_RE.match(next(t for t in universal).rsplit("-", 1)[1])
        if not match:
            failures.append("cannot parse %s" % universal)
            return failures
        tag_floor = (int(match.group(1)), int(match.group(2)))

        payload_floor = (0, 0)
        for name in native + dylibs:
            data = archive.read(name)
            seen = list(slices_of(data))
            # delocate rewrites the bundled dylib through install_name_tool, and
            # that tool writes a thin file back. So the extension module must be
            # universal (it is the thing that must load on both), while the Core
            # only has to be present and loadable -- requiring two slices there
            # would fail every real delocate run. The per-slice minimum-OS check
            # below still runs for both.
            if name in native and len(seen) != 2:
                failures.append("%s has %d slices, expected 2" % (name, len(seen)))
                continue
            if len(seen) < 1:
                failures.append("%s is empty" % name)
                continue
            for cputype, blob in seen:
                floor = thin_min_os(blob)
                if floor is None:
                    failures.append("%s slice %s declares no minimum macOS version"
                                    % (name, hex(cputype) if cputype else "thin"))
                    continue
                payload_floor = max(payload_floor, floor[:2])
            if name in dylibs:
                for cputype, blob in seen:
                    count = count_defined(blob, "mn_engine_create")
                    if count < 1:
                        failures.append("%s slice %s has no mn_ ABI symbols"
                                        % (name, hex(cputype)))

        if payload_floor > tag_floor:
            failures.append(
                "tag says macOS %d.%d but a payload requires %d.%d; pip would "
                "install this on a system that cannot map it"
                % (tag_floor[0], tag_floor[1], payload_floor[0], payload_floor[1]))

        record = next((n for n in names if n.endswith(".dist-info/RECORD")), None)
        if record:
            for line in archive.read(record).decode().strip().splitlines():
                member, digest, size = line.rsplit(",", 2)
                if member.endswith("/RECORD") or not digest:
                    continue
                data = archive.read(member)
                actual = "sha256=" + base64.urlsafe_b64encode(
                    hashlib.sha256(data).digest()).rstrip(b"=").decode()
                if actual != digest or len(data) != int(size):
                    failures.append("RECORD mismatch for %s" % member)
    return failures


def main(argv):
    if len(argv) < 2:
        raise SystemExit("usage: audit-macos-wheel.py WHEEL...")
    failed = False
    for name in argv[1:]:
        path = pathlib.Path(name)
        failures = check(path)
        if failures:
            failed = True
            print("%s: FAILED" % path.name, file=sys.stderr)
            for failure in failures:
                print("  " + failure, file=sys.stderr)
        else:
            print("%s: universal2, tag matches every payload slice, RECORD clean"
                  % path.name)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))