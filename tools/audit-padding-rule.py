#!/usr/bin/env python3
"""Assert the RFC 7685 padding behaviour, on both sides of the wire.

Chrome's rule has been the same the whole time: pad the ClientHello to a
512-byte total while it sits in the 256..511 window. The per-release widths the
captures show are the OUTPUT of that rule, not an input to it:

  * 86..90   h2 418  h1 424
  * 91..97   h2 400  h1 424
  * 98..118  h2 408  h1 432
  * 119..123 conditional: the extension appears only when the per-connection ECH
             GREASE payload happens to leave the ClientHello inside the window
             (28 bytes on h2, 52 on h1), and is absent otherwise
  * 124+     never: the X25519Kyber768 key share alone pushes the ClientHello
             past 511 bytes, so padding cannot fire again

The h1/h2 difference is the ALPS extension h1 does not send; the 86..97 vs
98..118 difference is the ten bytes of TLS 1.0/1.1 in supported_versions.

The gate has TWO halves, because the captures alone cannot see the table:

  1. the compiled profile table must carry client_hello_padding_length == 0 for
     every release. Writing any observed width into the table would freeze a
     payload constant that cannot hold for both protocols, and a later
     ClientHello size regression would then be hidden behind the override
     instead of showing up. Before this half existed the gate read only the
     recorded reports, so a table that pinned 418 was invisible to it.
  2. the captured ClientHello must show the width its era reports.

    python3 tools/audit-padding-rule.py
    python3 tools/audit-padding-rule.py --header P   # an alternative table
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

# Widths the captures report, keyed by era. These are expectations about the
# OUTPUT; the table above must stay at 0.
FIXED = {
    (86, 90): {"h2": 418, "h1": 424},
    (91, 97): {"h2": 400, "h1": 424},
    (98, 118): {"h2": 408, "h1": 432},
}
CONDITIONAL = {"h2": 28, "h1": 52}
CONDITIONAL_LAST = 123
NEVER_PADS_FROM = 124


def fixed_width(major, protocol):
    for (first, last), widths in FIXED.items():
        if first <= major <= last:
            return widths[protocol]
    return None


def audit_table(header):
    """The compiled table must not pin the widths the captures report.

    client_hello_padding_length == 0 means "apply BoringSSL's 512-total rule".
    A non-zero value freezes one payload constant, and since h1 and h2 report
    different totals for the same release (ALPS) no single constant can satisfy
    both.
    """
    failures = []
    if not header.exists():
        return [f"{header}: missing generated profile table"]
    text = header.read_text(encoding="utf-8")
    entries = re.findall(r'"chrome_(\d+)",\n(.*?)\n  \},', text, re.DOTALL)
    if not entries:
        return [f"{header}: no profile entries parsed"]

    for major_text, body in entries:
        major = int(major_text)
        values = [line.strip().rstrip(",") for line in body.splitlines()
                  if line.strip()]
        # The struct is positional, so locate the override by its position
        # relative to the two TLS version words rather than by guessing an
        # index: those words are the only 0x03xx literals in the entry.
        try:
            version_index = next(
                index for index, value in enumerate(values)
                if re.fullmatch(r"0x03[0-9a-f]{2}", value))
        except StopIteration:
            failures.append(f"chrome_{major}: no TLS version words in the entry")
            continue
        # tls_version_min, tls_version_max, enable_quic, try_quic_by_default,
        # maintain_ipv6_temp_addr, client_hello_padding_enabled,
        # client_hello_padding_length
        length_index = version_index + 6
        if length_index >= len(values):
            failures.append(f"chrome_{major}: entry is too short for the padding fields")
            continue
        try:
            length = int(values[length_index])
        except ValueError:
            failures.append(
                f"chrome_{major}: client_hello_padding_length is not an integer "
                f"({values[length_index]!r})")
            continue
        if length != 0:
            failures.append(
                f"chrome_{major}: client_hello_padding_length={length} pins a "
                "payload constant; the widths the captures show are the output "
                "of the 512-total rule and must not be written into the table")
    return failures


def audit_captures():
    """Every release and protocol must show the width its era reports."""
    failures = []
    checked = 0
    for major in pc.iter_majors():
        for protocol in ("h2", "h1"):
            records = pc.load_protocol(major, protocol)
            if records is None:
                failures.append(f"chrome_{major}/{protocol}: no captures")
                continue
            if len(records) != 6:
                failures.append(
                    f"chrome_{major}/{protocol}: {len(records)} connections")
                continue
            where = f"chrome_{major}/{protocol}"
            width = fixed_width(major, protocol)
            for index, record in enumerate(records, 1):
                length = pc.padding_length(record)
                if major >= NEVER_PADS_FROM:
                    if length is not None:
                        failures.append(
                            f"{where} connection {index}: padded ({length}) "
                            f"although key_share makes the 256..511 window "
                            f"unreachable from {NEVER_PADS_FROM}")
                    continue
                if width is not None:
                    if length is None:
                        failures.append(
                            f"{where} connection {index}: missing the fixed "
                            f"padding this era always carries ({width})")
                    elif length != width:
                        failures.append(
                            f"{where} connection {index}: padding {length}, "
                            f"captures say {width}")
                else:
                    expected = CONDITIONAL[protocol]
                    if length is not None and length != expected:
                        failures.append(
                            f"{where} connection {index}: padding {length}, "
                            f"the only conditional form is {expected}")
            checked += 1
    return failures, checked



#: The release whose ClientHello first carries the ECH extension. The wire
#: side is asserted in tools/verify-wire-capture.py; this constant is the same
#: fact in the other direction, so the two cannot drift apart.
ECH_FIRST_MAJOR = 119


def check_ech_boundary_documented():
    """Fails when a document states a different ECH starting major.

    Three independent carriers claim the same number: the captures
    (tools/verify-wire-capture.py), the profile table (ech_enabled), and the
    prose a reader actually sees. The first two are already checked; without
    this one the README could say 118 and every gate would stay green.

    Returns:
        A list of error strings, empty when every carrier agrees.
    """
    errors = []
    pattern = re.compile(r"chrome_(\d+)`?\s*(?:起|and later|及以后)")
    for relative in ("README.md", "README.en.md", "docs/ABI.md"):
        path = pathlib.Path(__file__).resolve().parents[1] / relative
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if "ECH" not in line and "ech" not in line:
                continue
            for major in pattern.findall(line):
                if int(major) != ECH_FIRST_MAJOR:
                    errors.append(
                        "%s: ECH is claimed to start at chrome_%s, but the "
                        "captures put it at chrome_%d" % (relative, major,
                                                       ECH_FIRST_MAJOR))
    return errors

def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--header", default=str(DEFAULT_HEADER),
                        help="generated profile table to audit")
    args = parser.parse_args(argv)

    failures = audit_table(pathlib.Path(args.header))
    capture_failures, checked = audit_captures()
    failures += capture_failures
    failures += check_ech_boundary_documented()

    if failures:
        print("padding rule audit FAILED:", file=sys.stderr)
        for failure in failures[:20]:
            print("  " + failure, file=sys.stderr)
        if len(failures) > 20:
            print(f"  ... and {len(failures) - 20} more", file=sys.stderr)
        return 1
    print(f"padding rule audit passed: {checked} profile/protocol pairs; "
          f"86..118 fixed widths, 119..{CONDITIONAL_LAST} conditional, "
          f"{NEVER_PADS_FROM}+ never; compiled table pins no override")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
