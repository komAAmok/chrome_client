#!/usr/bin/env python3
"""Assert every platform artifact carries the same profile payload bytes.

The per-platform Core builds are compiled from one source tree and one profile
table, but nothing checked that the *data* actually came out identical. Two
things can go wrong that the existing audits cannot see:

  * an artifact built from a stale table (its size and SHA-256 are consistent
    with its own manifest, so audit-core-binaries.sh cannot tell);
  * a payload that differs by platform because of endianness, word size or a
    conditional compile.

The trust_anchors (0xca34) payload is the sharpest witness available: it is a
204-byte uint8_t array on chrome_152 and a 184-byte one from chrome_153 on, it
is emitted verbatim by SelectAllTrustAnchorIDs(), and it is the one profile
field where a wrong value is a real fingerprint deviation rather than a
performance difference -- chrome_152 shipped 28 IDs while real Chrome 152 sends
32, and that went unnoticed for a whole release cycle.

So: take the arrays out of the generated header (the definition), then require
every artifact to embed each of them byte for byte. A platform whose build
disagrees with the header, or with the other platforms, fails here.

This is a static check and deliberately so. Only linux-x86_64 can be executed
on the build host; the other seven cannot be loaded at all without qemu or a
target machine. Asserting what is *in* the artifacts is the strongest claim
that can be made about them without running them, and it is exactly the claim
a stale or divergent build would break.

    python3 tools/audit-core-payloads.py
    python3 tools/audit-core-payloads.py --verbose
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
HEADER = ROOT / 'core/source/minicronet/profile_table_generated.h'
NORMALIZED = ROOT / 'profiles/normalized_profiles.json'

TARGETS = (
    'linux-x86', 'linux-x86_64', 'linux-arm64',
    'windows-x86', 'windows-x86_64', 'windows-arm64',
    'macos-x86_64', 'macos-arm64',
)

# Header array prefixes whose byte payload must be identical across platforms.
PAYLOAD_PREFIXES = (('trust_anchor_ids_', 'trust_anchors (0xca34) ID list'),)

ARRAY_RE = re.compile(r'inline constexpr uint8_t (\w+)\[\] = \{(.*?)\n\};',
                      re.DOTALL)


def header_arrays(text):
    arrays = {}
    for match in ARRAY_RE.finditer(text):
        name = match.group(1)
        values = [v.strip() for v in match.group(2).replace(chr(10), ' ').split(',')
                  if v.strip()]
        arrays[name] = bytes(int(v, 16) for v in values)
    return arrays


def normalized_payloads():
    document = json.loads(NORMALIZED.read_text(encoding='utf-8'))
    out = {}
    for entry in document['profiles']:
        encoded = (entry.get('tls') or {}).get('trust_anchor_ids_encoded')
        if encoded:
            out[entry['profile_id']] = bytes.fromhex(encoded)
    return out


def artifact_path(target):
    directory = ROOT / 'core/binaries' / target
    manifest = json.loads((directory / 'manifest.json').read_text())
    return directory / pathlib.Path(manifest['library']).name


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args(argv)

    if not HEADER.is_file():
        print(f'audit-core-payloads: missing {HEADER}', file=sys.stderr)
        return 1

    arrays = header_arrays(HEADER.read_text(encoding='utf-8'))
    if not arrays:
        print('audit-core-payloads: no uint8_t payload arrays in the header',
              file=sys.stderr)
        return 1

    wanted = {}
    for prefix, description in PAYLOAD_PREFIXES:
        matched = {n: d for n, d in arrays.items() if n.startswith(prefix) and d}
        if not matched:
            print(f'audit-core-payloads: the header defines no {prefix}* array; '
                  f'either the field was removed or the generator changed shape',
                  file=sys.stderr)
            return 1
        wanted[description] = matched

    # The header is generated from the profile input. If the two disagree, one
    # is stale, and every artifact would be certified against whichever copy
    # this script happened to read.
    known = set(normalized_payloads().values())
    for matched in wanted.values():
        for name, data in matched.items():
            if data not in known:
                print(f'audit-core-payloads: {name} ({len(data)} bytes) is in the '
                      f'header but not in profiles/normalized_profiles.json; the '
                      f'generated table is stale', file=sys.stderr)
                return 1

    failures = []
    checked = 0
    for target in TARGETS:
        path = artifact_path(target)
        if not path.is_file():
            failures.append(f'{target}: missing {path.name}')
            continue
        data = path.read_bytes()
        for description, matched in wanted.items():
            for name, payload in sorted(matched.items()):
                checked += 1
                if payload not in data:
                    failures.append(
                        f'{target}: does not embed {name} ({len(payload)} bytes, '
                        f'{description}); this artifact was built from a '
                        f'different table than {HEADER.name}, so its payload '
                        f'differs from the other platforms')
                elif args.verbose:
                    print(f'  {target:16} {name:26} {len(payload):>4} bytes ok')

    if failures:
        print('core payload audit FAILED:', file=sys.stderr)
        for failure in failures[:20]:
            print('  ' + failure, file=sys.stderr)
        if len(failures) > 20:
            print(f'  ... and {len(failures) - 20} more', file=sys.stderr)
        return 1

    total = sum(len(p) for m in wanted.values() for p in m.values())
    print(f'core payload audit passed: {checked} artifact/payload pairs across '
          f'{len(TARGETS)} targets; every artifact embeds the header'
          f"'s {total} payload bytes byte for byte")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))