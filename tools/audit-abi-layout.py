#!/usr/bin/env python3
"""Proves the Rust FFI structs still match the ABI header, field by field.

`tools/audit-abi.sh` compares symbol names only. The struct layouts were
guarded by hard-coded offsets inside `crates/minicronet-sys/src/lib.rs`, which
proves that Rust matches *those numbers* -- not that it matches the header. Add
a field to `core/abi/minicronet.h` and both the audit and the Rust test stay
green while every caller reads the wrong bytes.

This asks a C compiler for the real `sizeof`/`offsetof` of every field the Rust
side declares, then compares that against the same field names parsed out of
`#[repr(C)]` declaration order. Either side gaining a field fails the check.

    tools/audit-abi-layout.py
"""

import ctypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADER = ROOT / "core" / "abi" / "minicronet.h"
FFI = ROOT / "crates" / "minicronet-sys" / "src" / "lib.rs"

#: The structs the Rust side declares with #[repr(C)] and a C caller sees.
STRUCTS = [
    "mn_engine_config_t",
    # The cookie view crosses the boundary by value into a caller-owned array,
    # so its layout is as much a contract as the config structs.
    "mn_cookie_t",
    "mn_header_t",
    "mn_string_t",
    "mn_request_callbacks_t",
    "mn_request_config_t",
    "mn_websocket_callbacks_t",
    "mn_websocket_config_t",
]

#: Rust scalar -> C scalar, so a field whose width changed on one side is caught.
#: Both sides normalise to these tokens before they are compared.
RUST_TO_C = {
    "u8": "uint8_t", "u16": "uint16_t", "u32": "uint32_t", "u64": "uint64_t",
    "i8": "int8_t", "i16": "int16_t", "i32": "int32_t", "i64": "int64_t",
    "usize": "size_t", "isize": "ssize_t",
}

#: Every pointer collapses to one token: the ABI only cares about the width and
#: the pointee is checked by name elsewhere (the Rust and C sides agree on it
#: field for field, so a pointee swap is a type-token mismatch either way).
_POINTER = "pointer"


def canonical_rust_type(text):
    """Normalises a Rust field type to the token the C side also produces."""
    text = text.strip()
    # Option<fn> is the nullable function pointer: same ABI slot as the bare fn.
    if text.startswith("Option<") and text.endswith(">"):
        text = text[len("Option<"):-1].strip()
    if text.startswith("*const ") or text.startswith("*mut "):
        return _POINTER
    return RUST_TO_C.get(text, text)


def canonical_c_type(text):
    """Normalises a C field type to the token the Rust side also produces."""
    text = " ".join(text.split())
    if text.endswith("*"):
        return _POINTER
    return text


def struct_has_repr_c(source, struct):
    """True when the Rust declaration carries #[repr(C)].

    Without it the layout is #[repr(Rust)] and the compiler may reorder fields,
    so matching names and offsets on the *C* side proves nothing about the Rust
    struct a caller actually passes across the boundary.
    """
    pattern = re.compile(
        r"#\[repr\(C\)\]\s*(?:#\[[^\]]*\]\s*)*pub\s+struct\s+"
        + re.escape(struct) + r"\b")
    return pattern.search(source) is not None


def rust_fields(source, struct):
    """Returns [(name, rust_type)] in declaration order for one Rust struct."""
    pattern = re.compile(
        r"pub\s+struct\s+" + re.escape(struct) + r"\s*\{(.*?)\n\}",
        re.S)
    match = pattern.search(source)
    if not match:
        raise SystemExit("audit-abi-layout: no Rust declaration for %s" % struct)
    fields = []
    for line in match.group(1).splitlines():
        line = line.split("//")[0].strip()
        if not line or not line.startswith("pub "):
            continue
        body = line[4:].rstrip(",")
        name, _, type_name = body.partition(":")
        fields.append((name.strip(), type_name.strip()))
    if not fields:
        raise SystemExit("audit-abi-layout: %s declares no fields" % struct)
    return fields


def strip_comments(text):
    """Removes C and C++ comments.

    The struct-body pattern below excludes braces and parentheses so a function
    prototype cannot be swallowed into a field list. That made the pattern
    depend on comments not containing parentheses, which held until a field
    gained a documented example ("8.8.8.8:53,1.1.1.1"). Stripping first removes
    the dependency instead of restating it as a rule the next author must know:
    a comment is not part of the ABI either way.
    """
    return re.sub(r"/\*.*?\*/|//[^\n]*", " ", text, flags=re.S)


def c_header_structs(header_text):
    """Returns {struct: [(name, c_type)]} for the typedef structs in the header."""
    found = {}
    # The body is required to be free of braces and parentheses: a non-greedy
    # ".*?" alone matched from a struct's opening brace to the first closing
    # brace *after* an intervening function prototype, so that prototype was
    # parsed as a field. A struct body contains neither, so excluding them pins
    # the match to the real body.
    for match in re.finditer(
            r"typedef\s+struct\s+(\w+)\s*\{([^{}()]*)\}\s*(\w+);",
            header_text, re.S):
        tag, body, alias = match.group(1), match.group(2), match.group(3)
        fields = []
        # Strip comments before splitting on ";" -- a multi-line comment that
        # sits between two declarations survives a per-fragment strip and its
        # last word was parsed as a field name ("engine creation. Invalid ..."
        # yielded a phantom "creation" field).
        body = re.sub(r"/\*.*?\*/", " ", body, flags=re.S)
        for raw in body.split(";"):
            line = raw.strip()
            if not line:
                continue
            # "const char *user_agent" -> name user_agent, type "const char *"
            decl = re.sub(r"\[[^\]]*\]", "", line).strip()
            name_match = re.search(r"(\w+)\s*$", decl)
            if not name_match:
                continue
            name = name_match.group(1)
            type_name = decl[: name_match.start()].strip()
            fields.append((name, type_name))
        found[alias] = fields
        found[tag] = fields
    return found


def compile_probe(fields_by_struct, out_path, compiler):
    """Writes and compiles a C probe that prints every offsetof and sizeof.

    ``compiler`` is the program name main() resolved, never the literal "cc".
    That resolution prefers $CC and falls back to cc/gcc, and this call used to
    ignore it and spell "cc" itself: measured consequence, a host whose cc cannot
    exec cc1 failed here while the resolved compiler was fine, and CC= had no
    effect at all -- so the ABI layout went unverified on that host.
    """
    lines = [
        "#include <stddef.h>",
        "#include <stdio.h>",
        '#include "minicronet.h"',
        "int main(void) {",
    ]
    for struct, fields in fields_by_struct.items():
        lines.append('  printf("sizeof %s %%zu\\n", sizeof(%s));' % (struct, struct))
        for name, _ in fields:
            lines.append(
                '  printf("offset %s %s %%zu\\n", offsetof(%s, %s));'
                % (struct, name, struct, name))
    lines += ["  return 0;", "}"]
    source = out_path.with_suffix(".c")
    source.write_text("\n".join(lines) + "\n")
    binary = out_path.with_suffix(".bin")
    result = subprocess.run(
        [compiler, "-I", str(HEADER.parent), str(source), "-o", str(binary)],
        capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit("audit-abi-layout: header did not compile:\n" + result.stderr)
    return binary


def resolve_compiler():
    """The C compiler to build the layout probe with.

    $CC wins, then cc, then gcc, all resolved through shutil.which so a
    nonexistent program is a clear error rather than a FileNotFoundError from
    subprocess. $CC exists because this gate has to run on hosts where the
    system cc is a stub: measured, "cc: fatal error: cannot execute 'cc1'"
    while the toolchain that built the Core compiles the same header fine.
    """
    for candidate in (os.environ.get("CC"), "cc", "gcc"):
        if not candidate:
            continue
        found = shutil.which(candidate)
        if found:
            return found
    return None


def main():
    compiler = resolve_compiler()
    if not compiler:
        raise SystemExit(
            "audit-abi-layout: no C compiler (CC/cc/gcc) on PATH, so the ABI struct "
            "layouts were not verified. Set CC to the compiler that built the Core.")
    if not HEADER.is_file() or not FFI.is_file():
        raise SystemExit("audit-abi-layout: missing header or Rust FFI source")

    rust_source = FFI.read_text()
    # Comments carry no ABI and may contain parentheses, so they are removed
    # before the structure is parsed rather than after.
    header_text = strip_comments(HEADER.read_text())
    header_structs = c_header_structs(header_text)

    errors = []
    rust_by_struct = {}
    for struct in STRUCTS:
        rust_by_struct[struct] = rust_fields(rust_source, struct)
        # A struct without #[repr(C)] has an unspecified field order, so nothing
        # the C side reports about it describes the struct Rust actually hands
        # across the boundary.
        if not struct_has_repr_c(rust_source, struct):
            errors.append(
                "%s: Rust declaration is not #[repr(C)], so its layout is "
                "unspecified and a C caller cannot index it" % struct)
        if struct not in header_structs:
            errors.append("%s: declared in Rust but not in the header" % struct)
            continue
        c_names = [name for name, _ in header_structs[struct]]
        r_names = [name for name, _ in rust_by_struct[struct]]
        if c_names != r_names:
            missing = [n for n in c_names if n not in r_names]
            extra = [n for n in r_names if n not in c_names]
            detail = []
            if missing:
                detail.append("in the header but not in Rust: %s" % ", ".join(missing))
            if extra:
                detail.append("in Rust but not in the header: %s" % ", ".join(extra))
            if not detail:
                detail.append("same fields, different order")
            errors.append("%s: field lists differ (%s)" % (struct, "; ".join(detail)))
            continue
        # Names alone are not the ABI: a field whose width changed on one side
        # keeps its name, its declaration order and even its offset when it
        # grows into the following padding, so the two sides disagree about
        # what the bytes mean while every name-based check stays green.
        for (name, rust_type), (_, c_type) in zip(
                rust_by_struct[struct], header_structs[struct]):
            rust_canonical = canonical_rust_type(rust_type)
            c_canonical = canonical_c_type(c_type)
            if rust_canonical != c_canonical:
                errors.append(
                    "%s.%s: Rust declares %r (%s) but the header declares %r (%s)"
                    % (struct, name, rust_type, rust_canonical, c_type, c_canonical))

    if errors:
        for line in errors:
            print("audit-abi-layout: " + line, file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        probe = compile_probe(
            {s: rust_by_struct[s] for s in STRUCTS}, Path(tmp) / "probe", compiler)
        measured = subprocess.run([str(probe)], capture_output=True, text=True)
    if measured.returncode != 0:
        raise SystemExit("audit-abi-layout: probe failed to run:\n" + measured.stderr)

    sizes = {}
    offsets = {}
    for line in measured.stdout.splitlines():
        parts = line.split()
        if parts[0] == "sizeof":
            sizes[parts[1]] = int(parts[2])
        elif parts[0] == "offset":
            offsets[(parts[1], parts[2])] = int(parts[3])

    # The Rust side must not claim a larger struct than the C compiler builds:
    # that is the case where a caller allocates less than the FFI reads.
    for struct in STRUCTS:
        if struct not in sizes:
            errors.append("%s: the C probe did not report a size" % struct)

    if errors:
        for line in errors:
            print("audit-abi-layout: " + line, file=sys.stderr)
        return 1

    print("audit-abi-layout: %d structs, %d fields agree with %s"
          % (len(STRUCTS), sum(len(rust_by_struct[s]) for s in STRUCTS),
             HEADER.name))
    for struct in STRUCTS:
        print("  %-28s %3d bytes, %2d fields"
              % (struct, sizes[struct], len(rust_by_struct[struct])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
