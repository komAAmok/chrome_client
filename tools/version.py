#!/usr/bin/env python3
"""Single source of truth for the release version.

The workspace version lives in the root Cargo.toml and nowhere else. This module
is the one place other tooling reads it from, so a bump cannot leave a stale copy
behind in a second carrier.

It exists because that already happened once: core/source/minicronet.cc carried a
hand-written "0.4.0" that chrome_client.core_version() kept reporting long after
the workspace had moved to 0.2.5, while tools/audit-pypi-metadata.sh only checked
the READMEs, the stub docs and __init__.py.

Usage:
    python3 tools/version.py            # prints the version
    python3 tools/version.py --check    # exits non-zero if it cannot be read
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
CARGO_TOML = ROOT / "Cargo.toml"

# [workspace.package] version = "x.y.z" -- the only carrier.
_WORKSPACE_VERSION = re.compile(
    r'^\[workspace\.package\]\s*$.*?^version\s*=\s*"([^"]+)"',
    re.MULTILINE | re.DOTALL)


def version() -> str:
    text = CARGO_TOML.read_text(encoding="utf-8")
    match = _WORKSPACE_VERSION.search(text)
    if match is None:
        raise SystemExit(
            "version: no [workspace.package] version in %s; the release version "
            "has no source of truth" % CARGO_TOML)
    return match.group(1)


def main(argv: list[str]) -> int:
    found = version()
    if "--check" in argv:
        if not re.fullmatch(r"\d+\.\d+\.\d+", found):
            raise SystemExit("version: %r is not a three-component release" % found)
        print("ok workspace version %s" % found)
        return 0
    print(found)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
