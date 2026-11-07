#!/usr/bin/env python3
"""Assert every Core size ceiling still satisfies the rule that derives it.

The rule is written in each `tools/audit-core-*.sh`:

    MAX_BYTES = (largest committed artifact for that platform) * 1.02,
                rounded up to the next 10,000

It is a *rule*, not a number, which is why it can go stale silently: adding
profiles to the table grows the artifacts, and the ceiling that was correct
last release is then below what its own rule demands. That happened -- Windows
sat at 9,600,000 against a required 9,610,000, i.e. 1.91% headroom where the
rule promises 2% -- and nothing noticed, because the only thing checking the
ceiling was the ceiling itself.

This gate reads both sides and compares them, so a rebuild that grows an
artifact forces the ceiling to be re-derived in the same change.

    python3 tools/audit-core-size-ceilings.py
"""

import json
import math
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

# platform -> (targets whose largest artifact sets the ceiling, the audit script)
GROUPS = {
    'linux': (['linux-x86', 'linux-x86_64', 'linux-arm64'],
              'tools/audit-core-linux.sh'),
    'windows': (['windows-x86', 'windows-x86_64', 'windows-arm64'],
                'tools/audit-core-windows.sh'),
    'macos': (['macos-x86_64', 'macos-arm64'], 'tools/audit-core-macos.sh'),
}


def largest_artifact(targets):
    biggest = 0
    for target in targets:
        directory = ROOT / 'core/binaries' / target
        manifest = json.loads((directory / 'manifest.json').read_text())
        biggest = max(biggest, (directory / manifest['library']).stat().st_size)
    return biggest


def main():
    failures = []
    for name, (targets, script) in GROUPS.items():
        text = (ROOT / script).read_text()
        match = re.search(r'MAX_BYTES=\$\{MAX_BYTES:-(\d+)\}', text)
        if not match:
            failures.append(f'{script}: no MAX_BYTES default found')
            continue
        ceiling = int(match.group(1))
        biggest = largest_artifact(targets)
        required = math.ceil(biggest * 1.02 / 10000) * 10000
        headroom = (ceiling - biggest) / ceiling * 100
        if ceiling < required:
            failures.append(
                f'{script}: ceiling {ceiling:,} is below the value its own rule '
                f'derives, {required:,} (biggest artifact {biggest:,} x 1.02, '
                f'rounded up to 10,000); headroom is {headroom:.2f}%. Re-derive '
                f'the ceiling and update the comment above it in the same change.')
            continue
        print(f'  {name:8} biggest={biggest:>9,} ceiling={ceiling:>9,} '
              f'required={required:>9,} headroom={headroom:5.2f}%')

    if failures:
        print('core size ceiling audit FAILED:', file=sys.stderr)
        for failure in failures:
            print('  ' + failure, file=sys.stderr)
        return 1
    print('core size ceiling audit passed: every MAX_BYTES satisfies its own '
          '2% rule')
    return 0


if __name__ == '__main__':
    sys.exit(main())