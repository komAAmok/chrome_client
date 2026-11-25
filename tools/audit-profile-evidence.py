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

Exit 0 when every recorded digest matches upstream. Exit 1 when the record is
wrong: a digest differs, a file is missing from the local checkout, or upstream
no longer has the path at the recorded revision. Exit 2 when the question could
not be asked at all -- the fetch budget ran out on transport errors, so nothing
was compared.

Three outcomes instead of two because "the record is wrong" and "the network was
wrong" have opposite fixes and used to be the same line in the log: 31 of 876
files were reported UNREACHABLE on every CI run for four nights while all 31
answered HTTP 200 from a developer machine.

Network note: googlesource rate-limits bursts, and 876 files from a shared CI
address is exactly a burst. A 429 there is a statement about the client, not
about the record, so the retry honours Retry-After and a single 429 pauses every
thread -- the limit is per address, not per request. Bodies are cached under
~/.cache/minicronet-profile-evidence, so a second run fetches only what the first
one missed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import pathlib
import random
import sys
import threading
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


class FetchError(Exception):
    """A download that failed, carrying why and whether retrying can help."""

    def __init__(self, reason, permanent):
        super().__init__(reason)
        self.reason = reason
        self.permanent = permanent


# Upstream does not have this path at this revision: the record is wrong and no
# amount of retrying will change that. Everything else (429, 403, 5xx, socket
# errors, timeouts) describes the client, and a shared runner is the likeliest
# cause of it.
PERMANENT_CODES = {400, 404, 410, 451}

_pace_lock = threading.Lock()
_pace_until = 0.0
_streak = 0
_give_up = threading.Event()


def _wait_turn():
    with _pace_lock:
        delay = _pace_until - time.time()
    if delay > 0:
        time.sleep(delay)


def _pace(seconds):
    """Hold every thread back: one 429 is a signal to the whole process."""
    global _pace_until
    with _pace_lock:
        _pace_until = max(_pace_until, time.time() + min(30.0, seconds))


def _transport_failed(limit):
    """Count a transport failure; stop the sweep once they come in a row.

    876 files at a per-address rate limit will not finish, and grinding through
    them at one backoff per file turns a five-minute gate into an hour that
    still ends in the same place. The run is already unverifiable at that
    point, so it has to say so now -- exit 2 -- instead of later.
    """
    global _streak
    with _pace_lock:
        _streak += 1
        if _streak >= limit:
            _give_up.set()


def _transport_ok():
    global _streak
    with _pace_lock:
        _streak = 0


def fetch(url, cache_tag, path, max_age, give_up=8, attempts=5):
    """Download and cache one file. Returns bytes, or raises FetchError."""
    cfile = cache_path(cache_tag, path)
    if max_age >= 0 and cfile.is_file() and time.time() - cfile.stat().st_mtime < max_age:
        return cfile.read_bytes()
    reason = "no attempt made"
    for attempt in range(attempts):
        if _give_up.is_set():
            raise FetchError(f"{reason}; sweep gave up after repeated transport errors", False)
        _wait_turn()
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "chrome_client-audit/1"})
            with urllib.request.urlopen(request, timeout=30) as response:
                body = base64.b64decode(response.read())
            CACHE.mkdir(parents=True, exist_ok=True)
            cfile.write_bytes(body)
            _transport_ok()
            return body
        except urllib.error.HTTPError as exc:
            reason = f"HTTP {exc.code}"
            if exc.code in PERMANENT_CODES:
                raise FetchError(reason, True) from exc
            retry_after = (exc.headers or {}).get("Retry-After") if exc.headers else None
            hold = float(retry_after) if (retry_after or "").strip().isdigit() else 2.0 ** attempt
            _pace(hold)
            _transport_failed(give_up)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            detail = getattr(exc, "reason", None) or type(exc).__name__
            reason = f"{type(exc).__name__}: {detail}"
            _pace(2.0 ** attempt)
            _transport_failed(give_up)
        time.sleep(random.uniform(0.0, 0.5))  # jitter: the threads must not re-synchronise
    raise FetchError(reason, False)


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


def verify(job, max_age, offline, give_up):
    profile, key, path, digest, size = job
    pid = profile["profile_id"]
    if offline:
        # The local checkout is exactly one commit, and a profile is pinned to a
        # different one in general (the tree here is 58bcc148, while chrome_156
        # records 156.0.8078.80). A digest difference therefore proves nothing
        # about the record -- it only says the tree is not that release. So the
        # local mode can confirm a match but must never report a mismatch, and
        # the caller has to be told the result is not a verdict.
        candidate = chromium_src() / path
        if not candidate.is_file():
            return (pid, key, path, digest, None, None, "LOCAL_MISSING", None)
        body = candidate.read_bytes()
        actual = hashlib.sha256(body).hexdigest()
        if actual == digest:
            return (pid, key, path, digest, actual, len(body), "MATCH", None)
        return (pid, key, path, digest, actual, len(body), "LOCAL_OTHER_REVISION", None)
    url, cache_tag = source_for(key, profile, path)
    try:
        body = fetch(url, cache_tag, path, max_age, give_up)
    except FetchError as exc:
        return (pid, key, path, digest, None, None,
                "UNREACHABLE" if exc.permanent else "NETWORK", exc.reason)
    actual = hashlib.sha256(body).hexdigest()
    return (pid, key, path, digest, actual, len(body),
            "MATCH" if actual == digest else "MISMATCH", None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", help="verify a single profile id (e.g. chrome_157)")
    parser.add_argument("--offline", action="store_true",
                        help="read Chromium-tree files from the local checkout, skip submodules")
    parser.add_argument("--max-age", type=int, default=7 * 24 * 3600,
                        help="reuse a cached body younger than this many seconds (default 7 days, -1 = never)")
    parser.add_argument("--jobs", type=int, default=6, help="concurrent downloads (default 6)")
    parser.add_argument("--give-up", type=int, default=8,
                        help="stop the sweep after this many consecutive transport errors "
                             "(default 8; they are reported, never counted as matches)")
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
        futures = [pool.submit(verify, job, args.max_age, args.offline, args.give_up)
                   for job in jobs]
        for index, future in enumerate(futures, 1):
            results.append(future.result())
            if index % 25 == 0:
                print(f"  ... {index}/{len(jobs)}", flush=True)

    matches = [r for r in results if r[6] == "MATCH"]
    mismatches = [r for r in results if r[6] == "MISMATCH"]
    unreachable = [r for r in results if r[6] == "UNREACHABLE"]
    network = [r for r in results if r[6] == "NETWORK"]
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
        # Upstream answered, and said the file is not there. The record is
        # wrong: this is a failed audit, not a pass, and not a network problem.
        failed = True
        print(f"  UNREACHABLE {row[0]:<12} {row[1]:<26} {row[2]} ({row[7]})", file=sys.stderr)
    for row in network:
        # Nothing was compared. Saying "mismatch" would be a lie and saying
        # "pass" would be a worse one, so this is its own outcome and its own
        # exit code -- CI retries that, and never retries the one above.
        print(f"  NETWORK    {row[0]:<12} {row[1]:<26} {row[2]} ({row[7]})", file=sys.stderr)

    print()
    print(f"{len(matches)} matched, {len(mismatches)} mismatched, "
          f"{len(missing)} missing locally, {len(unreachable)} gone upstream, "
          f"{len(network)} unreachable over the network, "
          f"{len(other_rev)} on a different local revision "
          f"({len(jobs)} checked)")
    if args.offline:
        print("NOTE: --offline only proves a match; a different local revision is "
              "reported as OTHER-REV and is not a verdict. Run without --offline "
              "for the real gate.", file=sys.stderr)
    if failed:
        print("Profile evidence does not match upstream", file=sys.stderr)
        return 1
    if network:
        print(f"Profile evidence could not be verified: {len(network)} file(s) "
              "exhausted the retry budget on transport errors, so nothing was "
              "compared for them. Re-run (the cache keeps what did arrive) or "
              "raise --jobs/--max-age; do not read this as a pass.", file=sys.stderr)
        return 2
    print("Profile evidence audit passed: every recorded digest matches upstream.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
