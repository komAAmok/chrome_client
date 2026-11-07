#!/usr/bin/env python3
"""Assert every curl_cffi extra_fp field has an explicit verdict.

curl_cffi simulates Chrome, Firefox and Safari, so some of its `extra_fp` knobs
exist for browsers this project does not implement, or for a TLS stack it does
not use. Accepting such a knob silently would tell a caller they changed a
degree of freedom that does not exist here.

This gate pins the classification in docs/COMPATIBILITY_BOUNDARY.md to the code:

  MEANINGLESS     Chrome does the thing itself, or the mechanism is absent.
                  Only a neutral value may be accepted.
  PROFILE_OWNED   The value is real but frozen per release by impersonate=.
                  Any attempt to set it must raise.
  HONOURED        The facade implements it without touching the fingerprint.

A field that is in the class but not in this table fails here, so adding a
curl_cffi knob forces the question "is this meaningful in a Chrome-only,
Chromium-backed client?" to be answered in the same change.

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
#
# MEANINGLESS fields must accept only a neutral value; PROFILE_OWNED fields must
# reject any value; HONOURED fields are implemented by the facade.
VERDICTS = {
    # Chrome never sends Delegated Credentials; the mechanism is absent here.
    'tls_delegated_credential': ('MEANINGLESS', 'Delegated Credentials is a Firefox/Safari mechanism; Chrome does not send it'),
    # H3 shares the TLS-layer signature_algorithms; there is no separate H3 set.
    'http3_sig_hash_algs': ('MEANINGLESS', 'H3 uses the same signature_algorithms as TLS; no separate H3 list exists'),
    # Extension order is the profile's policy, not a per-request choice.
    'http3_tls_extension_order': ('MEANINGLESS', 'extension order comes from the profile policy, not a per-H3 setting'),
    # Chrome does not send record_size_limit.
    'tls_record_size_limit': ('MEANINGLESS', 'Chrome does not send the record_size_limit extension'),
    # Cookie splitting is a curl concept; Chromium's CookieStore decides.
    'split_cookies': ('MEANINGLESS', 'curl cookie-engine concept; Chromium CookieStore owns the semantics'),
    # Chrome always sends a HEADERS priority (256/0/exclusive) on every release.
    'http2_no_priority': ('MEANINGLESS', 'every release sends HEADERS priority (256,0,exclusive); "none" is not a Chrome state'),

    'http2_stream_weight': ('PROFILE_OWNED', 'constant 256 on all 72 releases; part of the HEADERS frame'),
    'http2_stream_exclusive': ('PROFILE_OWNED', 'constant 1 on all 72 releases; part of the HEADERS frame'),
    'tls_grease': ('PROFILE_OWNED', 'BoringSSL draws a fresh GREASE value per connection'),
    'tls_permute_extensions': ('PROFILE_OWNED', 'Chrome permutes the extension block per connection'),
    'tls_min_version': ('PROFILE_OWNED', 'profile tls_version_min (0x0301 for 86..97, 0x0303 from 98)'),
    'tls_cert_compression': ('PROFILE_OWNED', 'compress_certificate is [brotli] on every release'),
    'tls_signature_algorithms': ('PROFILE_OWNED', 'profile signature_algorithms span, audited per release'),

    'header_order': ('HONOURED', 'the facade emits headers in the requested order'),
    'form_boundary': ('HONOURED', 'the multipart boundary is generated in Python'),
}

# Neutral values the MEANINGLESS fields may be passed. Anything else must raise.
#
# A field whose default is already `None` ("not asked for") needs no extra
# entry: validate_extra_fp accepts `None` and `False` for every field before it
# consults this table. Only fields whose DEFAULT IS A NON-NONE VALUE need an
# entry, because their documented default would otherwise be rejected --
# tls_delegated_credential defaults to "", tls_record_size_limit to 0,
# http2_no_priority to False. http3_sig_hash_algs, http3_tls_extension_order and
# split_cookies all default to None, so None already passes and no entry is owed.
# (This gate caught that distinction: it initially demanded an entry for all six
# MEANINGLESS fields, which would have been three entries that no caller needs.)
NEUTRAL = {
    'tls_delegated_credential': ('', None),
    'tls_record_size_limit': (0, None),
    'http2_no_priority': (False, None),
}

# MEANINGLESS fields whose signature default is None: no neutral entry needed.
NONE_DEFAULTED = {
    'http3_sig_hash_algs',
    'http3_tls_extension_order',
    'split_cookies',
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

    # 3. The honoured list must agree with the verdicts.
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

    # 4. A MEANINGLESS field must have a neutral value it can be passed with.
    for name, (verdict, _reason) in VERDICTS.items():
        if verdict != 'MEANINGLESS':
            continue
        if name in NEUTRAL or name in NONE_DEFAULTED:
            continue
        failures.append(
            f'{name}: MEANINGLESS but declares no neutral value, so a caller '
            f'passing the documented default would be rejected; add it to '
            f'NEUTRAL (non-None default) or NONE_DEFAULTED (defaults to None)')

    for name in NONE_DEFAULTED:
        verdict = VERDICTS.get(name, ('MISSING', ''))[0]
        if verdict != 'MEANINGLESS':
            failures.append(
                f'{name}: listed in NONE_DEFAULTED but its verdict is {verdict}')

    if failures:
        print('extra_fp verdict audit FAILED:', file=sys.stderr)
        for failure in failures:
            print('  ' + failure, file=sys.stderr)
        return 1

    counts = {}
    for verdict, _reason in VERDICTS.values():
        counts[verdict] = counts.get(verdict, 0) + 1
    if args.verbose:
        for name in fields:
            verdict, reason = VERDICTS[name]
            print(f'  {name:28} {verdict:14} {reason}')
        print()
    summary = ', '.join(f'{v} {counts[v]}' for v in ('MEANINGLESS', 'PROFILE_OWNED', 'HONOURED'))
    print(f'extra_fp verdict audit passed: {len(fields)} curl_cffi fields classified ({summary})')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
