#!/usr/bin/env python3
"""Prove the ABI header declares exactly the frozen export set for its ABI.

`tools/audit-abi.sh` checks one direction: every symbol in its list appears in
the header, the Rust FFI, the Core implementation and the three export tables.
That cannot see the opposite direction. An export added to the header, the FFI
and the Core while the tables kept 20 entries passed every check and still
printed "20 symbols" -- a frozen contract that had quietly grown.

This reads the header alone and compares the set it actually declares against
the expected list, so both directions are covered.

    tools/abi-exports.py core/abi/minicronet.h <expected-symbols-file>

The ABI number in every message is read from the header rather than written
here. The messages said "v9" for a release after MN_ABI_VERSION had moved to 10,
and a failure that names the wrong version sends the reader to the wrong header.
"""

import re
import sys
from pathlib import Path


def declared_exports(header: Path):
    # Preprocessor lines carry the MN_EXPORT macro definition, which is not a
    # declaration; drop them before scanning. Newlines are collapsed so a
    # prototype wrapped across lines (the header has several) still parses as
    # one declaration.
    text = re.sub(r"^#.*$", "", header.read_text(), flags=re.M)
    # Comments go too. A declaration inside /* */ or after // is not a
    # declaration, and scanning the raw text counted it: commenting out the only
    # MN_EXPORT line of a symbol still produced "declares exactly the N v8
    # exports" -- a stale version in the one message that reports the count. The header holds no string literal containing a comment marker,
    # so a plain textual strip cannot remove a declaration that is really there.
    text = re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S)
    return set(re.findall(r"MN_EXPORT\b[^;]*?\b(mn_[a-z_]+)\s*\(", text, re.S))


def abi_version(header: Path) -> str:
    """The MN_ABI_VERSION the header declares, as a string."""
    match = re.search(r"^#define MN_ABI_VERSION (\d+)u$", header.read_text(), re.M)
    if match is None:
        raise SystemExit(f"{header} has no '#define MN_ABI_VERSION <n>u'")
    return match.group(1)


def main():
    if len(sys.argv) != 3:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} HEADER EXPECTED_LIST")
    header = Path(sys.argv[1])
    expected = set(Path(sys.argv[2]).read_text().split())
    version = abi_version(header)

    declared = declared_exports(header)
    extra = sorted(declared - expected)
    missing = sorted(expected - declared)
    if extra:
        raise SystemExit(
            f"header declares exports that are not part of ABI v{version}: "
            + ", ".join(extra))
    if missing:
        raise SystemExit(f"header is missing ABI v{version} exports: " + ", ".join(missing))
    if len(declared) != len(expected):
        raise SystemExit(
            f"header declares {len(declared)} exports, ABI v{version} has {len(expected)}")
    print(f"ABI header declares exactly the {len(expected)} v{version} exports")


if __name__ == "__main__":
    main()
