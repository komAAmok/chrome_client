#!/usr/bin/env python3
"""Asserts staged Core artifacts still match their manifests.

Both ends of the wheel workflow need the same check: before a build, so a
missing or hand-edited artifact stops the job instead of producing a wheel that
links against nothing, and after a macOS fusion step, so the fused file is
checked slice by slice. The Linux and macOS jobs each carried their own heredoc
and the Windows job carried a third; a fourth copy in the universal2 job would
be the one that silently drifts.

    python3 tools/verify-core-artifacts.py linux-x86_64
    python3 tools/verify-core-artifacts.py macos-x86_64 macos-arm64
"""

import hashlib
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def verify(target):
    directory = ROOT / "core/binaries" / target
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        raise SystemExit("%s: missing %s" % (target, manifest_path))
    manifest = json.loads(manifest_path.read_text())
    library = directory / manifest["library"]
    if not library.is_file():
        raise SystemExit("%s: missing %s" % (target, library))
    actual_sha = hashlib.sha256(library.read_bytes()).hexdigest()
    if actual_sha != manifest["sha256"]:
        raise SystemExit("%s: SHA-256 is %s, manifest says %s"
                         % (target, actual_sha, manifest["sha256"]))
    actual_size = library.stat().st_size
    if actual_size != manifest["size_bytes"]:
        raise SystemExit("%s: size is %d, manifest says %d"
                         % (target, actual_size, manifest["size_bytes"]))
    print("Core artifact verified: %s (%d bytes)" % (target, actual_size))


def main(argv):
    if len(argv) < 2:
        raise SystemExit("usage: verify-core-artifacts.py TARGET...")
    for target in argv[1:]:
        verify(target)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
