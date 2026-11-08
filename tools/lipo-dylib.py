#!/usr/bin/env python3
"""Builds a universal (fat) Mach-O dylib out of per-architecture slices.

Chromium is cross-built for macOS one architecture at a time, so `core/binaries/`
ships `macos-x86_64/libminicronet.dylib` and `macos-arm64/libminicronet.dylib`
and nothing else. A universal2 wheel needs one dylib that the loader can open
on both, and the only tool that stitches them -- `lipo` -- is part of the
Xcode toolchain and of osxcross, neither of which exists on a Linux build host.

The container format is small enough to write directly: an 0xcafebabe header,
one `fat_arch` record per slice, and each slice aligned to the maximum
p_align its load commands ask for (2^14 for every Mach-O clang produces).
Writing it here rather than shelling out also makes the result reproducible:
`lipo` picks alignment from what it reads, this picks it from the file.

    tools/lipo-dylib.py \
        --output /tmp/libminicronet-universal.dylib \
        core/binaries/macos-x86_64/libminicronet.dylib \
        core/binaries/macos-arm64/libminicronet.dylib

The result is checked before it is written: every slice must be a Mach-O
dylib, and no two slices may share a CPU type/subtype, because a duplicate
slice loads the wrong half of the file on one of the two machines.
"""

import argparse
import struct
import sys
from pathlib import Path

#: 0xcafebabe means "universal, big-endian header, 32-bit arch records".
#: 0xcafebabf is the 64-bit form, which no current slice needs: the largest
#: offset any of these dylibs can reach is far below 4 GiB.
FAT_MAGIC = 0xcafebabe
FAT_HEADER = ">II"
FAT_ARCH = ">IIIII"
MACHO_MAGIC_64 = 0xfeedfacf
MH_DYLIB = 6
#: Mach-O segment alignment for the slices this repository produces. 16 KiB is
#: what `lipo` itself writes for arm64 and x86_64 dylibs, and what the dynamic
#: loader requires of the file offset of a segment. Aligning more only wastes
#: file space; aligning less produces a file `dlopen` refuses.
ALIGN = 1 << 14


def read_slice(path, target_cpu):
    """Returns (cputype, cpusubtype, bytes) for one Mach-O dylib."""
    data = path.read_bytes()
    if len(data) < 32:
        raise SystemExit(f"{path}: too short to hold a Mach-O header")
    magic, cputype, cpusubtype, filetype = struct.unpack_from("<IiiI", data, 0)
    if magic != MACHO_MAGIC_64:
        raise SystemExit(
            f"{path}: not a little-endian 64-bit Mach-O (magic {magic:#010x}); "
            "fat slices must all be 64-bit"
        )
    if filetype != MH_DYLIB:
        raise SystemExit(f"{path}: Mach-O file type {filetype} is not a dylib")
    return cputype, cpusubtype, data


def cpu_name(cputype):
    return {0x01000007: "x86_64", 0x0100000C: "arm64"}.get(cputype, hex(cputype))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("slices", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    slices = [read_slice(path, path) for path in args.slices]
    seen = {}
    for path, (cputype, cpusubtype, _) in zip(args.slices, slices):
        key = (cputype, cpusubtype & 0x00FFFFFF)
        if key in seen:
            raise SystemExit(
                f"{path} and {seen[key]} both hold a {cpu_name(cputype)} slice; "
                "a universal binary may carry each architecture once"
            )
        seen[key] = path

    header_size = struct.calcsize(FAT_HEADER) + struct.calcsize(FAT_ARCH) * len(slices)
    offsets = []
    cursor = header_size
    for _, _, data in slices:
        cursor = (cursor + ALIGN - 1) // ALIGN * ALIGN
        offsets.append(cursor)
        cursor += len(data)

    out = bytearray(cursor)
    struct.pack_into(FAT_HEADER, out, 0, FAT_MAGIC, len(slices))
    position = struct.calcsize(FAT_HEADER)
    for (cputype, cpusubtype, data), offset in zip(slices, offsets):
        # The last field is log2(alignment), not the alignment itself.
        struct.pack_into(FAT_ARCH, out, position, cputype, cpusubtype,
                         offset, len(data), ALIGN.bit_length() - 1)
        position += struct.calcsize(FAT_ARCH)
        out[offset:offset + len(data)] = data

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(bytes(out))
    total = sum(len(data) for _, _, data in slices)
    print("%s: %d slices (%s), %d bytes of slices in %d bytes" % (
        args.output, len(slices),
        ", ".join(cpu_name(cputype) for cputype, _, _ in slices),
        total, len(out)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
