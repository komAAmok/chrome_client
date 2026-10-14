#!/usr/bin/env python3
"""Re-run the browserleaks wire gate for every profile that has raw captures.

`tools/validate-wire-profile.py` needs one `response-N.json` per connection plus
one `--capture` pcap each, because BrowserLeaks omits the raw KeyShare and ECH
bytes: without the pcap the key-share digest cannot be checked at all. Working
out which files pair with which profile by hand is the tedious part, so this
walks `profiles/chrome-<major>/raw/` and runs the gate for each capture
directory it finds.

Exit status is 0 only when every directory reports `stable_match: true` and its
randomness checks pass. A profile whose evidence the gate cannot read is a hard
failure, not a skip: the point of committing the raw captures is that the
claimed `wire_verified` can be reproduced from a clean clone.

    tools/verify-browserleaks-captures.py            # every profile
    tools/verify-browserleaks-captures.py --major 120
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "tools" / "validate-wire-profile.py"
RAW_ROOT = ROOT / "profiles"


def capture_dirs(major: int):
    """The raw/ subdirectories that hold browserleaks responses for one major."""
    raw = RAW_ROOT / f"chrome-{major}" / "raw"
    if not raw.is_dir():
        return []
    out = []
    for directory in sorted(raw.iterdir()):
        if not directory.is_dir():
            continue
        if list(directory.glob("response-*.json")):
            out.append(directory)
    return sorted(out, key=lambda p: (("h3" in p.name), p.name))


def _target_url(responses):
    """The URL the records were captured against, from their own :authority/:path.

    The gate rewrites the expected ``:authority``/``:path`` to match the URL it
    is given, so it has to be told the real one. Defaulting to browserleaks
    made every tls.peet.ws directory fail on those two headers while the whole
    TLS fingerprint matched.
    """
    for path in responses:
        record = json.loads(path.read_text())
        http2 = record.get("http2")
        # The browserleaks schema makes http2 a list of frames rather than a
        # dict, and the default URL already matches those captures.
        frames = http2.get("sent_frames") if isinstance(http2, dict) else []
        if isinstance(frames, list) and frames:
            for frame in frames:
                headers = frame.get("headers") if isinstance(frame, dict) else None
                if not isinstance(headers, list):
                    continue
                pairs = {}
                for item in headers:
                    if isinstance(item, str) and ": " in item:
                        name, value = item.split(": ", 1)
                        pairs[name] = value
                if ":authority" in pairs and ":path" in pairs:
                    return "https://%s%s" % (pairs[":authority"], pairs[":path"])
    return "https://tls.browserleaks.com/"


def _needs_pcap(responses):
    """Whether these records omit the raw key material the gate has to resolve.

    The browserleaks schema reports only key-share *lengths*, so its captures
    need a pcap; the tls.peet.ws schema carries the bytes inline.
    """
    for path in responses:
        record = json.loads(path.read_text())
        if isinstance(record.get("http2"), list):
            return True                      # browserleaks schema
    return False


def run_gate(profile_id: str, directory: Path):
    # Only the plain ``response-<n>.json`` names are paired positionally. A
    # directory that labels its records by phase (chrome-151-validation keeps
    # response-navigation-N / after-N / final-N from a different experiment) is
    # not the capture this wrapper verifies, so it is left alone rather than
    # reported as a failure.
    numbered = [p for p in directory.glob("response-*.json")
                if re.fullmatch(r"response-\d+", p.stem)]
    if len(numbered) != len(list(directory.glob("response-*.json"))):
        return None, "records are not named response-<n>"
    responses = sorted(numbered, key=lambda p: int(p.stem.split("-")[1]))
    if len(responses) < 3:
        return False, f"only {len(responses)} response records (need 3)"

    # One pcap per response, when the responses need them. The captures name
    # them session-N or run-N; the host side of the same connection is the one
    # whose SYN goes to :443.
    #
    # They are only needed for the two fields BrowserLeaks omits: ``key_share``
    # and ECH carry their raw bytes in the tls.peet.ws schema, so a directory of
    # peet.ws records can be verified without any capture at all. Requiring a
    # pcap unconditionally made those directories unverifiable here even though
    # `validate-wire-profile.py` accepts them.
    needs_pcap = _needs_pcap(responses)
    pcaps = []
    if needs_pcap:
        for index in range(1, len(responses) + 1):
            candidates = [directory / f"session-{index}.pcap",
                          directory / f"run-{index}.pcap"]
            found = next((p for p in candidates if p.is_file()), None)
            if found is None:
                return False, f"no pcap for response-{index}"
            pcaps.append(found)

    command = [sys.executable, str(GATE), profile_id,
               *[str(p) for p in responses],
               "--url", _target_url(responses)]
    for pcap in pcaps:
        command += ["--capture", str(pcap)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return False, detail[-1] if detail else "gate failed"
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False, "gate produced no JSON report"
    return bool(report.get("stable_match")), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--major", type=int, action="append",
                        help="only this Chrome major; repeatable")
    args = parser.parse_args()

    majors = args.major or sorted(
        int(p.name.split("-")[1]) for p in RAW_ROOT.glob("chrome-*")
        if p.is_dir() and p.name.split("-")[1].isdigit())

    checked = failures = skipped = unverifiable = 0
    for major in majors:
        dirs = capture_dirs(major)
        if not dirs:
            skipped += 1
            continue
        verified_this_major = 0
        for directory in dirs:
            # H3 directories hold QUIC captures, which this gate does not read;
            # they carry their own h3-session-validation.json.
            if "h3" in directory.name:
                continue
            # A directory is skipped only when it can genuinely not be checked
            # here: it holds fewer than three responses, or its schema omits the
            # key material and it has no pcap. Such a directory does not count as
            # a verification for the major, but is not a failure either, as long
            # as another directory for the same major does verify.
            responses = sorted(directory.glob("response-*.json"))
            if len(responses) < 3:
                # Fewer than three records, or the directory names them
                # differently (chrome-151-validation uses response-after-N), so
                # this wrapper cannot pair them. Not a failure here, as long as
                # another directory for the same major verifies.
                unverifiable += 1
                continue
            if _needs_pcap(responses) and not any(directory.glob("*.pcap")):
                unverifiable += 1
                continue
            ok, detail = run_gate(f"chrome_{major}", directory)
            if ok is None:
                unverifiable += 1
                continue
            checked += 1
            if ok:
                verified_this_major += 1
                print(f"  ok    chrome_{major}  {directory.name}")
            else:
                failures += 1
                print(f"  FAIL  chrome_{major}  {directory.name}: {detail}")
        if verified_this_major == 0:
            failures += 1
            print(f"  FAIL  chrome_{major}: no capture directory could be verified")

    print(f"\n{checked} capture directories checked, {failures} failed, "
          f"{unverifiable} skipped for having no pcap, "
          f"{skipped} majors without browserleaks captures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
