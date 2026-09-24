#!/usr/bin/env python3
"""Verifies the ERR_* table in exceptions.py against Chromium's net_error_list.h.

The table names the Chromium net error a caller sees, so a wrong or missing row
misreports the failure. It had drifted badly: eight codes carried the name of a
different error (e.g. -336 is ERR_NO_SUPPORTED_PROXIES, not
ERR_TUNNEL_CONNECTION_FAILED) and the codes a proxy failure actually produces
(-111 ERR_TUNNEL_CONNECTION_FAILED, -115 ERR_PROXY_AUTH_UNSUPPORTED) were absent,
so callers got a bare "Proxy (net error -111)".

Needs a Chromium checkout, so it runs where CHROMIUM_SRC is available:

    python3 tools/audit-net-error-names.py

Exit code 0 when every row agrees with the header, 1 otherwise.
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from core_paths import chromium_src  # noqa: E402  (path helper, see below)

HEADER = chromium_src() / "net" / "base" / "net_error_list.h"
TABLE = ROOT / "bindings/python/chrome_client/_python_impl/exceptions.py"


def main() -> int:
    if not HEADER.is_file():
        print("audit-net-error-names: no net_error_list.h at %s" % HEADER)
        return 1
    truth = {}
    for match in re.finditer(r"NET_ERROR\(([A-Z0-9_]+), (-?\d+)\)", HEADER.read_text()):
        truth[int(match.group(2))] = "ERR_" + match.group(1)

    text = TABLE.read_text()
    start = text.index("_NET_ERRORS = {")
    end = text.index("\n}\n", start)
    problems = []
    for line in text[start:end].splitlines():
        row = re.match(r'\s*(-?\d+): \("([A-Z0-9_]+)",', line)
        if not row:
            continue
        code, name = int(row.group(1)), row.group(2)
        expected = truth.get(code)
        if expected is None:
            problems.append("%d: %s is not a current Chromium net error" % (code, name))
        elif expected != name:
            problems.append("%d: table says %s, Chromium says %s" % (code, name, expected))

    # The codes a proxy failure actually produces must be present and named.
    for code in (-111, -115, -120, -121, -127, -130, -131, -136, -324, -336):
        if ('%d: ("%s"' % (code, truth.get(code, ""))) not in text:
            problems.append("%d (%s) is missing from the table" % (code, truth.get(code)))

    if problems:
        for problem in problems:
            print("audit-net-error-names: " + problem, file=sys.stderr)
        return 1
    print("net error names audit passed: every row matches %s" % HEADER.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
