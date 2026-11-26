#!/usr/bin/env python3
"""Assert every remaining extra_fp field has an explicit verdict.

curl_cffi's ExtraFingerprints carries 15 knobs. Thirteen of them describe a TLS
or HTTP/2 degree of freedom that belongs to the pinned Chromium profile, so
accepting them would tell a caller they changed something that does not exist
here. Those thirteen were REMOVED rather than accepted-and-refused: an unknown
attribute raises AttributeError, an unknown mapping key raises
UnsupportedFeature from validate_extra_fp.

Two fields remain, and this gate pins their classification to
docs/COMPATIBILITY_BOUNDARY.md:

  HONOURED    The facade implements it without touching the fingerprint.

It checks both directions, the way the field/verdict pairs did before the
removal: a field without a verdict fails (adding a curl_cffi knob forces the
question "is this meaningful in a Chrome-only, Chromium-backed client?"), and a
verdict whose field is gone fails too, so the table cannot outlive the code.

    python3 tools/audit-extra-fp.py
    python3 tools/audit-extra-fp.py --verbose
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
IMPERSONATE = ROOT / 'bindings/python/chrome_client/_python_impl/impersonate.py'

# field -> (verdict, reason)
VERDICTS = {
    'header_order': ('HONOURED', 'the facade emits headers in the requested order'),
    'form_boundary': ('HONOURED', 'the multipart boundary is generated in Python'),
}


def slots(text):
    match = re.search(r'__slots__ = \((.*?)\)', text, re.DOTALL)
    if not match:
        return []
    return re.findall(r'"(\w+)"', match.group(1))


def honoured(text):
    match = re.search(r'_HONOURED_EXTRA_FP = \((.*?)\)', text, re.DOTALL)
    return set(re.findall(r'"(\w+)"', match.group(1))) if match else set()


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args(argv)

    if not IMPERSONATE.is_file():
        print(f'audit-extra-fp: missing {IMPERSONATE}', file=sys.stderr)
        return 1
    text = IMPERSONATE.read_text(encoding='utf-8')

    fields = slots(text)
    if not fields:
        print('audit-extra-fp: cannot read ExtraFingerprints.__slots__', file=sys.stderr)
        return 1

    failures = []

    # 1. Every field must have a verdict.
    for name in fields:
        if name not in VERDICTS:
            failures.append(
                f'{name}: no verdict. Decide whether this curl_cffi knob means '
                f'anything in a Chrome-only, Chromium-backed client, record it in '
                f'VERDICTS and in docs/COMPATIBILITY_BOUNDARY.md')

    # 2. A verdict must not outlive the field it describes.
    for name in VERDICTS:
        if name not in fields:
            failures.append(f'{name}: has a verdict but is no longer a field')

    # 3. The honoured list must agree with the verdicts, both ways.
    declared = honoured(text)
    for name in declared:
        verdict = VERDICTS.get(name, ('MISSING', ''))[0]
        if verdict != 'HONOURED':
            failures.append(
                f'{name}: listed in _HONOURED_EXTRA_FP but its verdict is {verdict}')
    for name, (verdict, _reason) in VERDICTS.items():
        if verdict == 'HONOURED' and name not in declared:
            failures.append(
                f'{name}: verdict is HONOURED but it is not in _HONOURED_EXTRA_FP')

    if failures:
        print('extra_fp verdict audit FAILED:', file=sys.stderr)
        for failure in failures:
            print('  ' + failure, file=sys.stderr)
        return 1

    if args.verbose:
        for name in fields:
            verdict, reason = VERDICTS[name]
            print(f'  {name:28} {verdict:14} {reason}')
        print()
    print('extra_fp verdict audit passed: %d field(s) classified (%s)'
          % (len(fields), ', '.join(sorted({v for v, _ in VERDICTS.values()}))))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
