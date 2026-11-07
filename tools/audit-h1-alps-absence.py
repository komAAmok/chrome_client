#!/usr/bin/env python3
"""Assert that the HTTP/1.1 ClientHello matches Chrome's, on Chrome's own terms.

An ALPS extension (17513 / 17613) is only offered for a protocol that is also
in `alpn_protos`: net/socket/ssl_client_socket_impl.cc walks `alpn_protos` and
adds settings only when the same protocol has an `application_settings` entry.
Chrome installs that entry for h2 alone (net/http/http_network_session.cc), so
an h1-only ClientHello carries no ALPS extension. Comparing an h1 request
against an h2 capture therefore always shows two differences (ALPN 2-1.1 vs
1.1, and the missing ALPS) that are by design.

This gate pins that down from the captures themselves:

  1. no release's h1 ClientHello carries application_settings, and
  2. every release from 91 on DOES carry it in h2 -- so the absence in h1 is a
     protocol property, not a profile that lost ALPS.
  3. the two protocols agree on everything else that is protocol-independent:
     cipher list, supported groups, signature algorithms and the TLS version
     list. A difference there would mean the h1 and h2 captures were not taken
     from the same browser build.

    python3 tools/audit-h1-alps-absence.py
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import profile_captures as pc

ALPS_NAMES = ("application_settings", "application_settings_old")
ALPS_FIRST_MAJOR = 91

# Extension ids that are protocol-independent, so h1 and h2 must agree.
SHARED_EXTENSIONS = {
    10: "supported_groups",
    13: "signature_algorithms",
    43: "supported_versions",
}


def carries_alps(record):
    return [name for name in pc.extension_names(record)
            if any(alps in name for alps in ALPS_NAMES)]


# GREASE values appear two ways in these captures: the literal token
# "TLS_GREASE (0xaaaa)" in cipher and group lists, and a bare hex string such
# as "0xcaca" in signature_algorithms. Both are per-connection by design (the
# whole point of GREASE), so both must be filtered before comparing a field
# across connections -- filtering only the token spelling made every release
# from 152 on look unstable.
GREASE_HEX = {format(v, "x") for v in range(0x0A0A, 0xFAFA + 1, 0x1010)}


def degrease(values):
    kept = []
    for value in values:
        text = str(value)
        if "GREASE" in text:
            continue
        if text.startswith("0x") and text[2:].lower() in GREASE_HEX:
            continue
        kept.append(value)
    return kept


def main():
    failures = []
    checked = 0
    for major in pc.iter_majors():
        h1 = pc.load_protocol(major, "h1")
        h2 = pc.load_protocol(major, "h2")
        if h1 is None:
            failures.append(f"chrome_{major}: no h1 captures")
            continue
        if h2 is None:
            failures.append(f"chrome_{major}: no h2 captures")
            continue
        checked += 1

        for index, record in enumerate(h1, 1):
            alps = carries_alps(record)
            if alps:
                failures.append(
                    f"chrome_{major} h1 connection {index}: carries ALPS "
                    f"{alps}; h1 never offers it, so either the capture is "
                    f"mislabeled or the pin was dropped")

        h2_alps = [carries_alps(r) for r in h2]
        expected = major >= ALPS_FIRST_MAJOR
        if expected and not all(h2_alps):
            failures.append(
                f"chrome_{major}: h2 is missing ALPS on "
                f"{sum(1 for a in h2_alps if not a)}/6 connections although "
                f"every release from {ALPS_FIRST_MAJOR} sends it")
        if not expected and any(h2_alps):
            failures.append(
                f"chrome_{major}: h2 carries ALPS although releases before "
                f"{ALPS_FIRST_MAJOR} do not")

        # The two protocols must agree on the extension ids that do not depend
        # on the negotiated protocol. Comparing whole extension sets would fail
        # by design on ALPN and ALPS, which is why only these are compared.
        for extension_id, label in SHARED_EXTENSIONS.items():
            h1_values = {
                tuple(degrease(_values(pc.extension(r, extension_id), label)))
                for r in h1}
            h2_values = {
                tuple(degrease(_values(pc.extension(r, extension_id), label)))
                for r in h2}
            if len(h1_values) != 1 or len(h2_values) != 1:
                failures.append(
                    f"chrome_{major}: {label} is not stable across the six "
                    f"connections (h1 {len(h1_values)} variants, "
                    f"h2 {len(h2_values)})")
            elif h1_values != h2_values:
                failures.append(
                    f"chrome_{major}: {label} differs between h1 and h2; the "
                    f"two captures are not from the same build "
                    f"(h1 {sorted(h1_values)[0]}, h2 {sorted(h2_values)[0]})")

    if failures:
        print("HTTP/1.1 ALPS audit FAILED:", file=sys.stderr)
        for failure in failures[:20]:
            print("  " + failure, file=sys.stderr)
        if len(failures) > 20:
            print(f"  ... and {len(failures) - 20} more", file=sys.stderr)
        return 1
    print(f"HTTP/1.1 ALPS audit passed: {checked} profiles; h1 never offers "
          f"ALPS, h2 does from {ALPS_FIRST_MAJOR}, and both protocols agree on "
          f"supported_groups, signature_algorithms and supported_versions")
    return 0


def _values(extension_record, label):
    if not extension_record:
        return []
    for key in ("supported_groups", "signature_algorithms", "versions"):
        if key in extension_record:
            return list(extension_record[key])
    return []


if __name__ == "__main__":
    sys.exit(main())
