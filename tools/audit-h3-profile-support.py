#!/usr/bin/env python3
"""Assert that a forced-H3 engine is refused for profiles that cannot do HTTP/3.

A profile table entry carries two independent facts: whether the release
*attempts* QUIC by default, and whether the release can do HTTP/3 at all. Only
the second may decide whether a forced H3 engine is accepted, and getting it
wrong is invisible from the table alone -- before this gate existed a chrome_86
engine forced to H3 sent a QUIC ClientHello and received HTTP 200, reporting an
HTTP/3 fingerprint that release never had.

This gate reads the generated table directly, because that is what the Core
compiles in: a stale JSON input cannot make it pass, and a hand-edited header
cannot either.

    python3 tools/audit-h3-profile-support.py            # default table path
    python3 tools/audit-h3-profile-support.py --header P # an alternative table
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HEADER = ROOT / 'core/source/minicronet/profile_table_generated.h'

# The releases whose HTTP/3 behaviour the reference evidence cannot describe.
# Recorded here as the expected value so a table change must argue with this
# line rather than silently widening or narrowing the set.
UNSUPPORTED = set(range(86, 93))
ALL_PROFILES = set(range(86, 158))


def entries(header_text):
    """major -> the body text of one table entry."""
    out = {}
    pattern = re.compile(r'"chrome_(\d+)",\n(.*?)\n  \},', re.DOTALL)
    for match in pattern.finditer(header_text):
        out[int(match.group(1))] = match.group(2)
    return out

def bool_run(body):
    """The bare true/false tokens of one entry, in order."""
    tokens = []
    for line in body.splitlines():
        stripped = line.strip().rstrip(',')
        if stripped in ('true', 'false'):
            tokens.append(stripped == 'true')
    return tokens


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--header', default=str(DEFAULT_HEADER))
    args = parser.parse_args()

    text = Path(args.header).read_text(encoding='utf-8')
    table = entries(text)
    missing = ALL_PROFILES - set(table)
    if missing:
        raise SystemExit(
            f'{args.header}: table is missing {len(missing)} profiles: '
            f'{sorted(missing)[:8]}')

    # The emitted order inside RuntimeProfileData is fixed; locate the H3 flag
    # by its position rather than by value, so a wrong value cannot hide behind
    # a coincidentally matching index.
    struct = re.search(r'struct RuntimeProfileData \{([\s\S]*?)\n\};', text)
    if not struct:
        raise SystemExit('cannot find RuntimeProfileData in the header')
    fields = [line.strip() for line in struct.group(1).splitlines()
              if line.strip().endswith(';') and 'span' not in line]
    names = []
    for field in fields:
        match = re.search(r'(\w+)\s*;', field)
        if match:
            names.append(match.group(1))
    if 'h3_supported' not in names or 'advertise_h3_datagram' not in names:
        raise SystemExit('the table no longer carries the HTTP/3 fields')

    # bool_run() is every bare true/false token in emitted order. Anchor the
    # read on the struct's own field order instead of a hard-coded offset: the
    # two HTTP/3 booleans sit directly before enable_quic, and counting bools
    # from the front silently shifts the day a bool is added before them.
    # Non-bool fields must be listed here or the position of the H3 flags shifts.
    NON_BOOL = {'id', 'key_share_count', 'h2_params_index',
                'tls_extension_order_policy', 'client_hello_padding_length',
                'network_feature_flags', 'tls_version_min', 'tls_version_max'}
    bool_fields = [name for name in names if name not in NON_BOOL]
    h3_index = bool_fields.index('h3_supported')
    datagram_index = bool_fields.index('advertise_h3_datagram')

    failures = []
    for major, body in sorted(table.items()):
        run = bool_run(body)
        if len(run) != len(bool_fields):
            failures.append(
                f'chrome_{major}: {len(run)} boolean values for '
                f'{len(bool_fields)} boolean fields')
            continue
        h3_supported = run[h3_index]
        expected = major not in UNSUPPORTED
        if h3_supported != expected:
            failures.append(
                f'chrome_{major}: h3_supported={h3_supported}, expected {expected}')
        # SETTINGS_H3_DATAGRAM(51) boundary, from the reference captures:
        # absent 93..119, present from 120.
        expected_datagram = major >= 120
        if run[datagram_index] != expected_datagram:
            failures.append(
                f'chrome_{major}: advertise_h3_datagram={run[datagram_index]}, '
                f'expected {expected_datagram}')
    if failures:
        print('H3 profile support audit FAILED:', file=sys.stderr)
        for failure in failures:
            print('  ' + failure, file=sys.stderr)
        return 1
    print(f'H3 profile support audit passed: {len(table)} profiles, '
          f'{len(UNSUPPORTED)} marked unsupported '
          f'({min(UNSUPPORTED)}..{max(UNSUPPORTED)})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
