#!/usr/bin/env python3
"""Assert the advertised TLS version range matches each release's own default floor.

Chrome kept `kDefaultSSLVersionMin = SSL_PROTOCOL_VERSION_TLS1` up to 97, which
is why those releases list TLS 1.3/1.2/1.1/1.0 in supported_versions; 98 raised
it to TLS1_2 and the two legacy entries disappeared. That is ten bytes of
ClientHello, and it is the difference this gate protects.

Two things are checked, because either alone can lie:

  1. the generated table carries the right floor per release (86..97 -> 0x0301,
     98.. -> 0x0303), read from the compiled header rather than the JSON input,
     so a stale input cannot make it pass;
  2. the captured ClientHello advertises exactly the versions that floor
     implies, so a table that says one thing while the wire says another fails.

The second half used to be impossible: the alignment reports this gate read
before did not record the supported_versions payload, so the 86..97 conclusion
was argued from the ClientHello being ten bytes longer than 98..118. The
captures carry the version list directly, so it is asserted as a list now.

    python3 tools/audit-tls-version-range.py
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import profile_captures as pc

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_HEADER = ROOT / "core/source/minicronet/profile_table_generated.h"

LEGACY_FLOOR_LAST_MAJOR = 97
TLS1 = 0x0301
TLS1_2 = 0x0303

# The versions each floor produces on the wire, GREASE excluded. The GREASE
# entry is per-connection by design and is checked for presence, not value.
LEGACY_VERSIONS = ["TLS 1.3", "TLS 1.2", "TLS 1.1", "TLS 1.0"]
MODERN_VERSIONS = ["TLS 1.3", "TLS 1.2"]


def expected_versions(major):
    return LEGACY_VERSIONS if major <= LEGACY_FLOOR_LAST_MAJOR else MODERN_VERSIONS


def degrease(versions):
    return [v for v in versions if "GREASE" not in v]


def audit_table(header):
    failures = []
    if not header.exists():
        return [f"{header}: missing generated profile table"]
    text = header.read_text(encoding="utf-8")
    entries = {}
    for match in re.finditer(r'"chrome_(\d+)",\n(.*?)\n  \},', text, re.DOTALL):
        entries[int(match.group(1))] = match.group(2)
    if not entries:
        return [f"{header}: no profile entries in the generated table"]

    for major, body in sorted(entries.items()):
        values = [line.strip().rstrip(",") for line in body.splitlines()
                  if line.strip()]
        # The two version words are the only 0x03xx literals in an entry; find
        # them by value so a field reorder cannot silently pass.
        words = [int(v, 16) for v in values if re.fullmatch(r"0x03[0-9a-f]{2}", v)]
        if not words:
            failures.append(f"chrome_{major}: no TLS version words in the entry")
            continue
        expected_floor = TLS1 if major <= LEGACY_FLOOR_LAST_MAJOR else TLS1_2
        if words[0] != expected_floor:
            failures.append(
                f"chrome_{major}: tls_version_min=0x{words[0]:04x}, "
                f"expected 0x{expected_floor:04x}")
    return failures


def audit_captures():
    """The wire must advertise exactly what the floor implies, per release."""
    failures = []
    checked = 0
    for major in pc.iter_majors():
        records = pc.load_protocol(major, "h2")
        if records is None:
            failures.append(f"chrome_{major}: no h2 captures")
            continue
        expected = expected_versions(major)
        for index, record in enumerate(records, 1):
            advertised = pc.supported_versions(record)
            if advertised is None:
                failures.append(
                    f"chrome_{major} connection {index}: no supported_versions "
                    f"extension")
                continue
            grease = [v for v in advertised if "GREASE" in v]
            if len(grease) != 1:
                failures.append(
                    f"chrome_{major} connection {index}: {len(grease)} GREASE "
                    f"entries in supported_versions, expected exactly 1")
            actual = degrease(advertised)
            if actual != expected:
                failures.append(
                    f"chrome_{major} connection {index}: supported_versions is "
                    f"{actual}, expected {expected}")
        checked += 1
    return failures, checked


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--header", default=str(DEFAULT_HEADER))
    args = parser.parse_args(argv)

    failures = audit_table(pathlib.Path(args.header))
    capture_failures, checked = audit_captures()
    failures += capture_failures

    if failures:
        print("TLS version range audit FAILED:", file=sys.stderr)
        for failure in failures[:20]:
            print("  " + failure, file=sys.stderr)
        if len(failures) > 20:
            print(f"  ... and {len(failures) - 20} more", file=sys.stderr)
        return 1
    print(f"TLS version range audit passed: {checked} profiles; "
          f"86..{LEGACY_FLOOR_LAST_MAJOR} advertise TLS 1.3/1.2/1.1/1.0 on the "
          f"wire, {LEGACY_FLOOR_LAST_MAJOR + 1}.. advertise TLS 1.3/1.2, and the "
          f"compiled table carries the matching floor")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
