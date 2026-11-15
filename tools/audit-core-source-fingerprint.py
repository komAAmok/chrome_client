#!/usr/bin/env python3
"""Refuses a repository whose Core artifacts predate their own sources.

Nothing in the tree compared `core/source/` with `core/binaries/`. The manifests
record the artifact hash, its size and the Chromium revision, but nothing about
the sources it was built from -- so editing `core/source/*.cc` and skipping the
rebuild leaves nine artifacts that look perfectly valid: same version string,
same size ceiling, same payloads, same revision. Measured while fixing the DNS
resolver: `core/source/engine.cc` carried a +11 diff for a full round while every
gate stayed green and the shipped `.so` still behaved as the old source did.

The fingerprint is a SHA-256 over every build input in filename order, with
`minicronet_version_generated.h` skipped because `tools/sync-core.sh` rewrites it
from `workspace.package.version` at build time.

    python3 tools/audit-core-source-fingerprint.py
    python3 tools/audit-core-source-fingerprint.py --record   # after a rebuild
"""

import argparse
import hashlib
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "core" / "source"
BINARIES = ROOT / "core" / "binaries"

GENERATED = "minicronet_version_generated.h"


def inputs():
    files = list(SOURCE.glob("*.cc")) + list(SOURCE.glob("*.c"))
    files += list((SOURCE / "minicronet").glob("*.h"))
    files.append(SOURCE / "BUILD.gn")
    return sorted(path for path in files if path.name != GENERATED)


def fingerprint():
    digest = hashlib.sha256()
    files = inputs()
    for path in files:
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest(), len(files)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", action="store_true",
                        help="write the current fingerprint into every manifest")
    args = parser.parse_args()

    current, count = fingerprint()
    manifests = sorted(BINARIES.glob("*/manifest.json"))
    if not manifests:
        print("no manifests under core/binaries", file=sys.stderr)
        return 1

    if args.record:
        for path in manifests:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["core_sources_sha256"] = current
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print("recorded %s over %d files into %d manifest(s)"
              % (current, count, len(manifests)))
        return 0

    stale = []
    missing = []
    for path in manifests:
        data = json.loads(path.read_text(encoding="utf-8"))
        recorded = data.get("core_sources_sha256")
        if recorded is None:
            missing.append(path.parent.name)
        elif recorded != current:
            stale.append(path.parent.name)

    if missing or stale:
        print("core source fingerprint audit FAILED:", file=sys.stderr)
        for name in missing:
            print("  %s: manifest has no core_sources_sha256" % name,
                  file=sys.stderr)
        for name in stale:
            print("  %s: built from different sources than core/source/ holds"
                  % name, file=sys.stderr)
        print("  current: %s over %d files" % (current, count), file=sys.stderr)
        return 1

    print("core source fingerprint audit passed: %d artifact(s) were built "
          "from the %d files in core/source/ (%s)"
          % (len(manifests), count, current))
    return 0


if __name__ == "__main__":
    sys.exit(main())
