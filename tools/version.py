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
#
# The table is located first and its body bounded at the next table header, so
# the `version = ` key cannot be picked up from a later section. Matching the
# whole file let `[profile.release] version = "9.9.9"` stand in for a missing
# workspace version, which turned "the source of truth is gone" into "the source
# of truth is 9.9.9" -- the one failure this module exists to prevent.
_SECTION = re.compile(r'^\[workspace\.package\]\s*$', re.MULTILINE)
_NEXT_TABLE = re.compile(r'^\[', re.MULTILINE)
_VERSION_KEY = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)


def version() -> str:
    text = CARGO_TOML.read_text(encoding="utf-8")
    section = _SECTION.search(text)
    if section is None:
        raise SystemExit(
            "version: no [workspace.package] table in %s; the release version "
            "has no source of truth" % CARGO_TOML)
    body_start = section.end()
    following = _NEXT_TABLE.search(text, body_start)
    body = text[body_start:following.start() if following else len(text)]
    match = _VERSION_KEY.search(body)
    if match is None:
        raise SystemExit(
            "version: [workspace.package] in %s has no version key; the release "
            "version has no source of truth" % CARGO_TOML)
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
