#!/usr/bin/env python3
"""Print the exported symbol names of a Core artifact, one per line.

The audit has to read each artifact's real export table, and the obvious tools
are each wrong for at least one of the three formats this repository ships:

  * `nm -D` reads ELF only.
  * `llvm-nm` answers "no symbols" for a PE image and still exits 0, so with
    LLVM on PATH every Windows target looked like it exported nothing.
  * `strings` finds the export names as plain literals and keeps finding them
    after the export table is gone, so it cannot tell an export from a string.
    Zeroing the x86_64 DLL's export directory left all twenty names in .rdata.
  * binutils `objdump` reads x86/x86_64 PE but not the ARM64 image, and cannot
    read Mach-O at all.

Python is already a hard dependency of the audit (it parses the manifests), so
parsing the three table formats here keeps the check identical in CI and on a
developer machine, with no external tool to install or to be silently missing.

Usage: core-exports.py <binary>
Exit 0 and print the names, or exit 1 with a message on stderr.
"""

from __future__ import annotations

import pathlib
import struct
import sys


class Unsupported(Exception):
    """The file could not be parsed as this container format."""


def _uleb(data: bytes, offset: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, offset
        shift += 7


def _cstr(data: bytes, offset: int) -> str:
    end = data.index(b"\x00", offset)
    return data[offset:end].decode("utf-8", "replace")


def elf_exports(data: bytes) -> list[str]:
    if data[:4] != b"\x7fELF":
        raise Unsupported("not ELF")
    is64 = data[4] == 2
    if data[5] != 1:
        raise Unsupported("big-endian ELF is not expected for any shipped target")

    if is64:
        e_shoff, = struct.unpack_from("<Q", data, 0x28)
        e_shentsize, e_shnum, _shstrndx = struct.unpack_from("<HHH", data, 0x3A)
    else:
        e_shoff, = struct.unpack_from("<I", data, 0x20)
        e_shentsize, e_shnum, _shstrndx = struct.unpack_from("<HHH", data, 0x2E)

    # ELF32 and ELF64 order the symbol fields differently: 64-bit is
    # st_name, st_info, st_other, st_shndx, st_value, st_size; 32-bit is
    # st_name, st_value, st_size, st_info, st_other, st_shndx. Reading one
    # layout with the other's indices silently selects on st_value and rejects
    # every symbol, which is why 32-bit ELF produced an empty export list.
    if is64:
        sym_fmt, sym_entry, info_at, shndx_at = "<IBBHQQ", 24, 1, 3
    else:
        sym_fmt, sym_entry, info_at, shndx_at = "<IIIBBH", 16, 3, 5

    def section(index: int) -> tuple[int, int, int]:
        base = e_shoff + index * e_shentsize
        if is64:
            _name, kind, _flags, _addr, offset, size, link = struct.unpack_from(
                "<IIQQQQI", data, base)
        else:
            _name, kind, _flags, _addr, offset, size, link = struct.unpack_from(
                "<IIIIIII", data, base)
        return kind, offset, link

    def section_size(index: int) -> int:
        base = e_shoff + index * e_shentsize
        if is64:
            size, = struct.unpack_from("<Q", data, base + 32)
        else:
            size, = struct.unpack_from("<I", data, base + 20)
        return size

    names: list[str] = []
    for index in range(e_shnum):
        kind, offset, link = section(index)
        if kind != 11:  # SHT_DYNSYM
            continue
        _lk, sym_strtab, _ll = section(link)
        for i in range(section_size(index) // sym_entry):
            fields = struct.unpack_from(sym_fmt, data, offset + i * sym_entry)
            if fields[shndx_at] == 0:  # SHN_UNDEF: imported, not exported
                continue
            if (fields[info_at] >> 4) not in (1, 2):  # GLOBAL, WEAK
                continue
            names.append(_cstr(data, sym_strtab + fields[0]))
    if not names:
        raise Unsupported("no exported dynamic symbols")
    return names


def pe_exports(data: bytes) -> list[str]:
    if data[:2] != b"MZ":
        raise Unsupported("not PE")
    e_lfanew, = struct.unpack_from("<I", data, 0x3C)
    if data[e_lfanew:e_lfanew + 4] != b"PE\x00\x00":
        raise Unsupported("no PE signature")
    coff = e_lfanew + 4
    nsec, = struct.unpack_from("<H", data, coff + 2)
    opt_size, = struct.unpack_from("<H", data, coff + 16)
    opt = coff + 20
    magic, = struct.unpack_from("<H", data, opt)
    if magic == 0x20B:
        dd = opt + 112
    elif magic == 0x10B:
        dd = opt + 96
    else:
        raise Unsupported(f"unknown PE optional header magic {magic:#x}")
    export_rva, export_size = struct.unpack_from("<II", data, dd)
    if export_rva == 0 or export_size == 0:
        raise Unsupported("the image has no export directory")

    sections = []
    so = opt + opt_size
    for i in range(nsec):
        base = so + i * 40
        vsize, vaddr, _rawsize, rawptr = struct.unpack_from("<IIII", data, base + 8)
        sections.append((vaddr, max(vsize, 1), rawptr))

    def rva_to_offset(rva: int) -> int:
        for vaddr, vsize, rawptr in sections:
            if vaddr <= rva < vaddr + vsize:
                return rawptr + (rva - vaddr)
        raise Unsupported(f"RVA {rva:#x} is not mapped by any section")

    base = rva_to_offset(export_rva)
    nnames, = struct.unpack_from("<I", data, base + 24)
    names_rva, = struct.unpack_from("<I", data, base + 32)
    if nnames == 0:
        raise Unsupported("the export directory names no symbols")
    names_off = rva_to_offset(names_rva)
    out = []
    for i in range(nnames):
        name_rva, = struct.unpack_from("<I", data, names_off + i * 4)
        out.append(_cstr(data, rva_to_offset(name_rva)))
    return out


def _trie_exports(data: bytes, start: int, size: int) -> list[str]:
    """Walk a dyld export trie, the authoritative export list of a Mach-O image.

    Child offsets are relative to the start of the trie, not to the current
    node; using the node as the base walks into the middle of an edge string.
    """
    end = start + size
    found: list[str] = []
    stack = [(start, "")]
    seen: set[int] = set()
    while stack:
        node, prefix = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        if not start <= node < end:
            raise Unsupported(f"export trie node {node} is outside the trie")
        terminal, offset = _uleb(data, node)
        if terminal:
            # A terminal node carries a flags ULEB and, for a regular export,
            # an address ULEB. The child list starts AFTER that payload.
            # Reading it from `offset` walked into the flags byte and lost
            # every node that is both a terminal and a prefix of a longer
            # name -- measured on libminicronet.dylib, where "release" is a
            # prefix of "release_buffer" and the walk reported 28 exports
            # where the trie held 29.
            flags, after_flags = _uleb(data, offset)
            offset = after_flags
            if not flags & 0x08:  # not REEXPORT: the name is exported here
                found.append(prefix)
                if not flags & 0x10:  # not a stub: an address ULEB follows
                    _address, offset = _uleb(data, offset)
        if offset >= end:
            continue
        children = data[offset]
        offset += 1
        for _ in range(children):
            edge = _cstr(data, offset)
            offset += len(edge) + 1
            child, offset = _uleb(data, offset)
            stack.append((start + child, prefix + edge))
    return found


def macho_exports(data: bytes) -> list[str]:
    magic = data[:4]
    if magic == b"\xcf\xfa\xed\xfe":
        is64 = True
    elif magic == b"\xce\xfa\xed\xfe":
        is64 = False
    else:
        raise Unsupported("not a little-endian Mach-O image")

    ncmds, = struct.unpack_from("<I", data, 16)
    off = 32
    symtab = None
    trie = None
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from("<II", data, off)
        if cmd == 0x2:  # LC_SYMTAB
            symtab = struct.unpack_from("<IIII", data, off + 8)
        elif cmd == 0x80000033:  # LC_DYLD_EXPORTS_TRIE
            trie = struct.unpack_from("<II", data, off + 8)
        elif cmd == 0x80000022:  # LC_DYLD_INFO_ONLY
            trie = struct.unpack_from("<II", data, off + 40)
        off += cmdsize

    if trie is not None and trie[1] > 0:
        names = _trie_exports(data, trie[0], trie[1])
        if names:
            return names

    if symtab is None:
        raise Unsupported("no LC_SYMTAB and no usable export trie")
    symoff, nsyms, stroff, _strsize = symtab
    if nsyms == 0:
        raise Unsupported("LC_SYMTAB lists no symbols")
    entry = 16 if is64 else 12
    out = []
    for i in range(nsyms):
        base = symoff + i * entry
        n_strx, n_type = struct.unpack_from("<IB", data, base)
        if n_type & 0x01:  # N_EXT
            out.append(_cstr(data, stroff + n_strx))
    if not out:
        raise Unsupported("no external symbols")
    return out


PARSERS = (elf_exports, pe_exports, macho_exports)


def main() -> int:
    if "--self-test" in sys.argv[1:]:
        return _self_test()
    if len(sys.argv) != 2:
        print(f"usage: {pathlib.Path(sys.argv[0]).name} <binary>", file=sys.stderr)
        return 1
    path = pathlib.Path(sys.argv[1])
    if not path.is_file():
        print(f"core-exports: {path} is not a file", file=sys.stderr)
        return 1
    data = path.read_bytes()
    errors = []
    for parser in PARSERS:
        try:
            names = parser(data)
        except (Unsupported, struct.error, IndexError, ValueError) as exc:
            errors.append(f"{parser.__name__}: {exc}")
            continue
        if not names:
            errors.append(f"{parser.__name__}: parsed but found no names")
            continue
        for name in sorted(set(names)):
            print(name)
        return 0
    print(f"core-exports: cannot read the export table of {path}", file=sys.stderr)
    for error in errors:
        print(f"  {error}", file=sys.stderr)
    return 1




def _self_test() -> int:
    """Checks the trie walker against a hand-built trie.

    The case that matters is the one this walker got wrong: a node that is both
    an export and the prefix of a longer one. "release" is a prefix of
    "release_buffer" in the real library, and reading the child count from the
    flags byte instead of after the address payload silently dropped it, so the
    gate reported 28 exports where the image held 29 -- on macOS only, because
    that is the only container format that uses a trie.

    Returns:
        A process exit status.
    """
    def uleb(value: int) -> bytes:
        out = bytearray()
        while True:
            byte = value & 0x7F
            value >>= 7
            if value:
                out.append(byte | 0x80)
            else:
                out.append(byte)
                return bytes(out)

    # Leaf offsets first, then parents, so every child offset is known when the
    # parent is written. The root is at offset 0.
    n = {}

    def size_of(terminal, address, children):
        body = len(uleb(1)) + len(uleb(0)) + len(uleb(address)) if terminal else 1
        body += len(uleb(len(children)))
        for edge, _child in children:
            body += len(edge) + 1 + len(uleb(0)) + 1
        return body

    # Walk worst case: children references, so compute sizes then lay out.
    specs = {}
    specs["yz"] = (True, 0x3000, [])
    specs["x"] = (True, 0x1000, [])
    specs["y"] = (True, 0x2000, [("z", "yz")])
    specs["mn"] = (False, 0, [("x", "x"), ("y", "y")])
    specs["root"] = (False, 0, [("_mn_", "mn")])

    order = ["root", "mn", "x", "y", "yz"]
    cursor = 0
    for key in order:
        terminal, address, children = specs[key]
        n[key] = cursor
        if terminal:
            cursor += len(uleb(1)) + len(uleb(0)) + len(uleb(address))
        else:
            cursor += 1
        cursor += len(uleb(len(children)))
        for edge, _child in children:
            cursor += len(edge) + 1 + len(uleb(0))

    blob = bytearray()
    for key in order:
        terminal, address, children = specs[key]
        if terminal:
            blob.extend(uleb(1))
            blob.extend(uleb(0))
            blob.extend(uleb(address))
        else:
            blob.append(0)
        blob.extend(uleb(len(children)))
        for edge, child in children:
            blob.extend(edge.encode() + b"\0")
            blob.extend(uleb(n[child]))

    found = _trie_exports(bytes(blob), 0, len(blob))
    wanted = {"_mn_x", "_mn_y", "_mn_yz"}
    if set(found) != wanted:
        print("core-exports self-test FAILED: a terminal that is also a prefix",
              file=sys.stderr)
        print("  expected %s" % sorted(wanted), file=sys.stderr)
        print("  got      %s" % sorted(found), file=sys.stderr)
        return 1
    print("core-exports self-test passed: a terminal that is also a prefix "
          "survives the walk")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
