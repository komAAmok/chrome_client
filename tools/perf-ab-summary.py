#!/usr/bin/env python3
"""Summarises the raw A/B files into one JSON, medians and paired ratios.

Reads the files `tools/concurrency-ab.py` and `tools/mtls-concurrency.py` wrote
and produces the numbers a write-up may quote. It exists so a document cannot
disagree with the data it cites: the summary is regenerated, never typed.

Pairing is by ROUND INDEX, not by file order: the arms of one round ran back to
back on the same machine, so their difference is the thing being measured and
one round landing on a slow minute affects both sides.

    tools/perf-ab-summary.py docs/perf/ab-gate-0.2.11/raw
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import statistics
import sys


def median(values):
    """Median of a list, or None for an empty list."""
    return statistics.median(values) if values else None


def load(pattern, directory):
    """Returns {round: payload} for files matching *pattern*."""
    out = {}
    for path in sorted(directory.glob(pattern)):
        match = re.search(r"r(\d+)-", path.name)
        if not match:
            continue
        out[int(match.group(1))] = json.loads(path.read_text(encoding="utf-8"))
    return out


def concurrency_section(directory):
    """Builds the async/sync comparison from the concurrency-ab raw files."""
    patched = load("conc-r*-patched.json", directory)
    original = load("conc-r*-original.json", directory)
    rounds = sorted(set(patched) & set(original))

    def rows(payload):
        flat = []
        for row in payload["rows"]["async"]:
            flat.append(("async", row["requests"], row["distinct"], row))
        for row in payload["rows"]["sync"]:
            flat.append(("sync", row["threads"], None, row))
        return flat

    # A key names the arm; it is derived from the payload, never from position.
    arms = {}
    for kind, size, distinct, _row in rows(patched[rounds[0]]):
        key = "%s|%s|%s" % (kind, size, distinct)
        arms[key] = {"mode": kind, "size": size, "distinct": distinct,
                     "patched": [], "original": []}

    for round_index in rounds:
        for kind, size, distinct, row in rows(patched[round_index]):
            arms["%s|%s|%s" % (kind, size, distinct)]["patched"].append(row)
        for kind, size, distinct, row in rows(original[round_index]):
            arms["%s|%s|%s" % (kind, size, distinct)]["original"].append(row)

    summary = []
    for key in sorted(arms):
        arm = arms[key]
        paired = [p["qps"] / o["qps"]
                  for p, o in zip(arm["patched"], arm["original"])]
        ratio = median(paired)
        # A paired sign test: how many rounds agree with the median direction.
        agree = sum(1 for value in paired if (value >= 1) == (ratio >= 1))
        patched_qps = [row["qps"] for row in arm["patched"]]
        original_qps = [row["qps"] for row in arm["original"]]
        summary.append({
            "mode": arm["mode"],
            "size": arm["size"],
            "distinct": arm["distinct"],
            "rounds": len(paired),
            "original_qps": [round(value, 1) for value in original_qps],
            "patched_qps": [round(value, 1) for value in patched_qps],
            "original_median_qps": round(median(original_qps), 1),
            "patched_median_qps": round(median(patched_qps), 1),
            "paired_ratio_median": round(ratio, 4),
            "paired_delta_percent": round((ratio - 1) * 100, 2),
            "rounds_agreeing": agree,
            "intra_arm_spread_qps": round(max(original_qps) - min(original_qps), 1),
            # A sign test at n=5 needs 5/5 for anything to be said; at n=3, 3/3.
            #
            # Named for what it is: ONE sequence's sign test. It is not a
            # conclusion, because the arm order inside each round is fixed --
            # whichever arm runs second sees a machine the first one just loaded.
            # Measured: two arms reached 5/5 in the first sequence and reversed
            # sign in the swapped one. Only a second, swapped sequence can settle
            # it, so the field a reader should trust is
            # "reversed_and_agrees" from compare_sequences().
            "sign_test_conclusive": agree == len(paired) and len(paired) >= 3,
        })
    return summary


def mtls_section(directory):
    """Builds the mTLS comparison from the mtls-concurrency raw files."""
    patched = load("mtls-r*-patched.json", directory)
    original = load("mtls-r*-original.json", directory)
    rounds = sorted(set(patched) & set(original))
    if not rounds:
        return None
    out = {"rounds": len(rounds), "original": [], "patched": []}
    for round_index in rounds:
        out["original"].append(original[round_index])
        out["patched"].append(patched[round_index])
    for side in ("original", "patched"):
        out[side + "_qps"] = [round(row["requests_per_second"], 1) for row in out[side]]
        out[side + "_tcp"] = [row["tcp_connections"] for row in out[side]]
        out[side + "_median_qps"] = round(
            median([row["requests_per_second"] for row in out[side]]), 1)
        out[side + "_median_tcp"] = median([row["tcp_connections"] for row in out[side]])
    ratio = median([p["requests_per_second"] / o["requests_per_second"]
                    for p, o in zip(out["patched"], out["original"])])
    out["paired_ratio_median"] = round(ratio, 3)
    out["paired_delta_percent"] = round((ratio - 1) * 100, 1)
    return out


def compare_sequences(first, second):
    """Merges two sequences into one verdict per arm.

    A single sequence cannot separate a version difference from a fixed arm
    order: whichever arm runs second meets a machine the first one just loaded.
    On this repository's own data two arms reached 5/5 in the first sequence and
    reversed sign when the order was swapped, so the only statement worth making
    requires both sequences to agree on the SIGN, and it is reported under a name
    that cannot be mistaken for the single-sequence test.

    Args:
        first: The summary dict of the first sequence.
        second: The summary dict of the swapped sequence.

    Returns:
        One row per arm present in both, carrying ``reversed_and_agrees``.
    """

    def key(row):
        return (row["mode"], row["size"], row["distinct"])

    index = {key(row): row for row in (second.get("concurrency") or [])}
    merged = []
    for row in first.get("concurrency") or []:
        other = index.get(key(row))
        if other is None:
            continue
        same_sign = ((row["paired_delta_percent"] > 0)
                     == (other["paired_delta_percent"] > 0))
        merged.append({
            "mode": row["mode"],
            "size": row["size"],
            "distinct": row["distinct"],
            "first_delta_percent": row["paired_delta_percent"],
            "swapped_delta_percent": other["paired_delta_percent"],
            "first_sign_test_conclusive": row["sign_test_conclusive"],
            "swapped_sign_test_conclusive": other["sign_test_conclusive"],
            "reversed_and_agrees": bool(same_sign
                                        and row["sign_test_conclusive"]
                                        and other["sign_test_conclusive"]),
        })
    return merged


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("raw", help="directory holding the raw JSON files")
    parser.add_argument("--out")
    parser.add_argument("--swapped", metavar="RAW_DIR",
                        help="the same run with the arm order reversed; produce "
                             "a verdict a single sequence cannot support alone")
    args = parser.parse_args()
    directory = pathlib.Path(args.raw)
    if not directory.is_dir():
        print("error: %s is not a directory" % directory)
        return 1
    report = {
        "source": str(directory),
        "concurrency": concurrency_section(directory),
        "mtls": mtls_section(directory),
    }
    if args.swapped:
        swapped = pathlib.Path(args.swapped)
        if not swapped.is_dir():
            print("error: %s is not a directory" % swapped)
            return 1
        report["sequences"] = compare_sequences(
            report, {"concurrency": concurrency_section(swapped)})
    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    if args.out:
        pathlib.Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
