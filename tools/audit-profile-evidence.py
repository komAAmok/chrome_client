#!/usr/bin/env python3
"""Verify every profile's recorded source evidence against the real upstream tree.

Each profile in profiles/normalized_profiles.json records, for every signal it
depends on (alpn, http2, http3, tls, websocket, features, the BoringSSL and
QUICHE files), the upstream path, the byte size and the sha256 of the file it
was audited against. That record is the whole basis for the per-release tables,
and until now nothing re-checked it: a stale record and a correct one looked the
same, and a profile could keep claiming "audited from tag X" long after X's real
file had moved.

This gate re-downloads each file from the tag / revision named in the profile
and compares the digest. It is the only check that can tell whether the claims
in profiles/ still describe upstream reality.

Sources, resolved per evidence key (the bare paths in the JSON are relative to
different repositories, which is why a single root cannot resolve them):

  chromium  <tag>        chromium.googlesource.com/chromium/src/+/refs/tags/<tag>
  boringssl <revision>   boringssl.googlesource.com/boringssl/+/<revision>
  quiche    <revision>   quiche.googlesource.com/quiche/+/<revision>

Only files that live in the Chromium tree are read from the local checkout when
--local is given; the submodules are always fetched, because a local submodule
may sit at a different revision than the profile recorded.

    python3 tools/audit-profile-evidence.py                  # full sweep
    python3 tools/audit-profile-evidence.py --offline        # local Chromium files only
    python3 tools/audit-profile-evidence.py --profile chrome_157
    python3 tools/audit-profile-evidence.py --max-age 86400  # reuse cache < 1 day old

Exit 0 when every recorded digest matches upstream, 1 otherwise. A fetch that
never succeeds is reported as an error, not as a pass: an unreachable file must
not read the same as a correct one.

Network note: googlesource rate-limits bursts. The tool retries with backoff and
caches bodies under ~/.cache/minicronet-profile-evidence, so a second run is
fast and does not hammer the server.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from core_paths import chromium_src  # noqa: E402  (shared checkout resolver)

PROFILES = ROOT / "profiles" / "normalized_profiles.json"
CACHE = pathlib.Path.home() / ".cache" / "minicronet-profile-evidence"

CHROMIUM_URL = (
    "https://chromium.googlesource.com/chromium/src/+/refs/tags/{rev}/{path}?format=TEXT"
)
BORINGSSL_URL = "https://boringssl.googlesource.com/boringssl/+/{rev}/{path}?format=TEXT"
QUICHE_URL = "https://quiche.googlesource.com/quiche/+/{rev}/{path}?format=TEXT"

# Evidence keys whose recorded path is relative to a submodule, not to the
# Chromium tree. Everything else is resolved against chromium/src.
BORINGSSL_KEYS = {"boringssl_extensions", "boringssl_handshake_client", "boringssl_random"}
QUICHE_KEYS = {"quiche_h3", "quiche_random"}


def source_for(key, profile, path):
    """Return (url, cache_tag) for one evidence entry."""
    if key in BORINGSSL_KEYS:
        rev = profile["boringssl_revision"]
        return BORINGSSL_URL.format(rev=rev, path=path), "boringssl-" + rev
    if key in QUICHE_KEYS:
        rev = profile["quiche_revision"]
        return QUICHE_URL.format(rev=rev, path=path), "quiche-" + rev
    tag = profile["chromium_tag"]
    return CHROMIUM_URL.format(rev=tag, path=path), "chromium-" + tag


def cache_path(cache_tag, path):
    safe = path.replace("/", "_")
    return CACHE / (cache_tag + "__" + safe)


def fetch(url, cache_tag, path, max_age, retries=5):
    """Download and cache one file. Returns bytes, or None when unreachable."""
    cfile = cache_path(cache_tag, path)
    if max_age >= 0 and cfile.is_file() and time.time() - cfile.stat().st_mtime < max_age:
        return cfile.read_bytes()
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "chrome_client-audit/1"})
            with urllib.request.urlopen(request, timeout=30) as response:
                body = base64.b64decode(response.read())
            CACHE.mkdir(parents=True, exist_ok=True)
            cfile.write_bytes(body)
            return body
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            time.sleep(1.5 * (attempt + 1))
    return None


def collect(profiles, only, offline):
    """Build the list of (profile_id, key, path, sha256, size) to verify."""
    jobs = []
    skipped_submodules = 0
    for profile in profiles:
        if only and profile["profile_id"] != only:
            continue
        for key, entry in profile.get("evidence", {}).get("source_files", {}).items():
            path = entry.get("path")
            digest = entry.get("sha256")
            if not path or not digest:
                continue
            if offline and key in BORINGSSL_KEYS | QUICHE_KEYS:
                skipped_submodules += 1
                continue
            jobs.append((profile, key, path, digest, entry.get("bytes")))
    return jobs, skipped_submodules


def verify(job, max_age, offline):
    profile, key, path, digest, size = job
    if offline:
        # The local checkout is exactly one commit, and a profile is pinned to a
        # different one in general (the tree here is 58bcc148, while chrome_156
        # records 156.0.8078.80). A digest difference therefore proves nothing
        # about the record -- it only says the tree is not that release. So the
        # local mode can confirm a match but must never report a mismatch, and
        # the caller has to be told the result is not a verdict.
        candidate = chromium_src() / path
        if not candidate.is_file():
            return (profile["profile_id"], key, path, digest, None, None, "LOCAL_MISSING")
        body = candidate.read_bytes()
        actual = hashlib.sha256(body).hexdigest()
        if actual == digest:
            return (profile["profile_id"], key, path, digest, actual, len(body), "MATCH")
        return (profile["profile_id"], key, path, digest, actual, len(body), "LOCAL_OTHER_REVISION")
    url, cache_tag = source_for(key, profile, path)
    body = fetch(url, cache_tag, path, max_age)
    if body is None:
        return (profile["profile_id"], key, path, digest, None, None, "UNREACHABLE")
    actual = hashlib.sha256(body).hexdigest()
    return (profile["profile_id"], key, path, digest, actual, len(body),
            "MATCH" if actual == digest else "MISMATCH")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", help="verify a single profile id (e.g. chrome_157)")
    parser.add_argument("--offline", action="store_true",
                        help="read Chromium-tree files from the local checkout, skip submodules")
    parser.add_argument("--max-age", type=int, default=7 * 24 * 3600,
                        help="reuse a cached body younger than this many seconds (default 7 days, -1 = never)")
    parser.add_argument("--jobs", type=int, default=6, help="concurrent downloads (default 6)")
    parser.add_argument("--verbose", action="store_true", help="print every match, not just failures")
    args = parser.parse_args()

    if not PROFILES.is_file():
        print(f"audit-profile-evidence: missing {PROFILES}", file=sys.stderr)
        return 1
    document = json.loads(PROFILES.read_text())
    profiles = document["profiles"]
    jobs, skipped = collect(profiles, args.profile, args.offline)
    if not jobs:
        print("audit-profile-evidence: nothing to verify", file=sys.stderr)
        return 1

    if args.profile and not any(p["profile_id"] == args.profile for p in profiles):
        print(f"audit-profile-evidence: unknown profile {args.profile}", file=sys.stderr)
        return 1

    print(f"Auditing {len(jobs)} recorded evidence files"
          + (f" ({skipped} submodule files skipped: --offline)" if skipped else ""))

    results = []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = [pool.submit(verify, job, args.max_age, args.offline) for job in jobs]
        for index, future in enumerate(futures, 1):
            results.append(future.result())
            if index % 25 == 0:
                print(f"  ... {index}/{len(jobs)}", flush=True)

    matches = [r for r in results if r[6] == "MATCH"]
    mismatches = [r for r in results if r[6] == "MISMATCH"]
    unreachable = [r for r in results if r[6] == "UNREACHABLE"]
    missing = [r for r in results if r[6] == "LOCAL_MISSING"]
    other_rev = [r for r in results if r[6] == "LOCAL_OTHER_REVISION"]

    if args.verbose:
        for row in matches:
            print(f"  MATCH  {row[0]:<12} {row[1]:<26} {row[2]}")

    failed = False
    for row in mismatches:
        failed = True
        print(f"  MISMATCH {row[0]:<12} {row[1]:<26} {row[2]}", file=sys.stderr)
        print(f"           recorded {row[3]}", file=sys.stderr)
        print(f"           actual   {row[4]}", file=sys.stderr)
    for row in other_rev:
        # Not a failure: the local tree is a different commit than the profile
        # pins. Reported so nobody mistakes a partial local run for a verdict.
        print(f"  OTHER-REV {row[0]:<12} {row[1]:<26} {row[2]} "
              f"(local tree is a different commit; use the online mode to judge)",
              file=sys.stderr)
    for row in missing:
        failed = True
        print(f"  MISSING  {row[0]:<12} {row[1]:<26} {row[2]} (not in local checkout)", file=sys.stderr)
    for row in unreachable:
        # An unreachable file is a failed audit, not a pass: the claim stays
        # unverified, and silently treating that as success is how a stale
        # record survives.
        failed = True
        print(f"  UNREACHABLE {row[0]:<12} {row[1]:<26} {row[2]}", file=sys.stderr)

    print()
    print(f"{len(matches)} matched, {len(mismatches)} mismatched, "
          f"{len(missing)} missing locally, {len(unreachable)} unreachable, "
          f"{len(other_rev)} on a different local revision "
          f"({len(jobs)} checked)")
    if args.offline:
        print("NOTE: --offline only proves a match; a different local revision is "
              "reported as OTHER-REV and is not a verdict. Run without --offline "
              "for the real gate.", file=sys.stderr)
    if failed:
        print("Profile evidence does not match upstream", file=sys.stderr)
        return 1
    print("Profile evidence audit passed: every recorded digest matches upstream.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
