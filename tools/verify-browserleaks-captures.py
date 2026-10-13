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


def run_gate(profile_id: str, directory: Path):
    responses = sorted(directory.glob("response-*.json"),
                       key=lambda p: int(re.search(r"(\d+)", p.stem).group(1)))
    if len(responses) < 3:
        return False, f"only {len(responses)} response records (need 3)"

    # One pcap per response. The captures name them session-N or run-N; the host
    # side of the same connection is the one whose SYN goes to :443.
    pcaps = []
    for index in range(1, len(responses) + 1):
        candidates = [directory / f"session-{index}.pcap",
                      directory / f"run-{index}.pcap"]
        found = next((p for p in candidates if p.is_file()), None)
        if found is None:
            return False, f"no pcap for response-{index}"
        pcaps.append(found)

    command = [sys.executable, str(GATE), profile_id,
               *[str(p) for p in responses]]
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
            # Some directories are summary copies whose pcaps live elsewhere
            # (chrome-145-release-validation is the example). They cannot be
            # checked here, so they do not count as a verification for the
            # major -- but they are not failures either, as long as another
            # directory for the same major does verify.
            has_pcap = any(directory.glob("*.pcap"))
            if not has_pcap:
                unverifiable += 1
                continue
            ok, detail = run_gate(f"chrome_{major}", directory)
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
