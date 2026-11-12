#!/usr/bin/env python3
"""Thread-ladder sweep for the synchronous (blocking) client model.

The question this answers is not "how fast is it" -- tools/load-matrix.py already
answers that -- but "where does adding a thread stop helping, and what changes at
that point". Those are two different measurements:

  throughput knee   the thread count past which requests/second stops rising
  latency knee      the thread count past which p99 rises faster than the mean

A single run at "high concurrency" cannot locate either, because a plateau can be
caused by the fixture, by the connection pool, by a lock, or by the scheduler, and
all four look identical from the client's own counter. So each step records the
server's view as well:

  server_peak        how many requests the fixture saw at once (the pool ceiling)
  ctxt_switches      voluntary + nonvoluntary, read from /proc/PID/status
  schedstat_wait_ns  time the process spent runnable-but-not-running
  cpu_cores          utime+stime over wall time

    tools/thread-ladder.py --out docs/perf/thread-ladder.json
    tools/thread-ladder.py --ladder 1,2,4,8,16,32 --requests 256 --shape delay

Every cell runs in the same process so the counters are comparable, but the
counters are read per cell and the deltas are what get reported: a cumulative
number cannot tell two adjacent cells apart.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import perf_harness as H

SHAPES = {
    # Fast responses: the client's own per-request cost dominates, so this is the
    # cell where a scheduler or lock problem shows up as a throughput collapse.
    "fast": "/small?size=1024",
    # Delay-dominated: the server holds each request, so the cell measures how
    # many requests the client can keep in flight rather than how fast it parses.
    "delay": "/delay?ms=20&size=1024",
    # Body-dominated: one socket read per request, large enough that the copy
    # cost is visible but small enough to stay in cache.
    "body": "/small?size=65536",
}


def _gauge(base: str, reset: bool = False) -> dict:
    """Reads (and optionally resets) the fixture's concurrent-request gauge.

    The peak is the only client-independent view of the connection-pool ceiling:
    a plateau at six requests at once with twelve threads means the pool is the
    limit, while a plateau at six *and* twelve sockets means the scheduler is.
    """
    import urllib.request
    url = base + "/inflight" + ("?reset=1" if reset else "")
    try:
        with urllib.request.urlopen(url, timeout=5) as handle:
            return json.loads(handle.read())
    except Exception:  # noqa: BLE001 - a gauge failure must not lose the cell
        return {}


def read_ctxt(pid: int) -> tuple:
    """Voluntary and nonvoluntary context switches for one pid."""
    voluntary = nonvoluntary = None
    try:
        with open("/proc/%d/status" % pid, "r", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("voluntary_ctxt_switches:"):
                    voluntary = int(line.split()[1])
                elif line.startswith("nonvoluntary_ctxt_switches:"):
                    nonvoluntary = int(line.split()[1])
    except (FileNotFoundError, ProcessLookupError, ValueError):
        return None, None
    return voluntary, nonvoluntary


def read_schedstat(pid: int):
    """(running_ns, waiting_ns, timeslices) from /proc/PID/schedstat.

    waiting_ns is the number that separates "the CPU is saturated" from "the
    thread pool is thrashing": a thread that is runnable but not scheduled is
    waiting, and no amount of client tuning removes that.
    """
    try:
        with open("/proc/%d/schedstat" % pid, "r", encoding="ascii") as handle:
            parts = handle.read().split()
        return int(parts[0]), int(parts[1]), int(parts[2])
    except (FileNotFoundError, ProcessLookupError, IndexError, ValueError):
        return None


def percentile(values, fraction):
    return H.percentile(values, fraction)


def run_cell(chrome_client, base, shape, threads, requests, timeout):
    path = SHAPES[shape]
    joiner = "&" if "?" in path else "?"
    # Distinct URLs per request: Chromium serializes same-cache-key transactions,
    # so a single URL would measure that serialization instead of the ladder.
    urls = ["%s%s%sn=%d" % (base, path, joiner, index)
            for index in range(requests)]

    # Reset the fixture's gauge before the cell so the peak belongs to this step
    # alone. Without it the previous step's peak leaks in and every step reports
    # the largest value seen so far.
    _gauge(base, reset=True)

    pid = os.getpid()
    ctxt_before = read_ctxt(pid)
    sched_before = read_schedstat(pid)
    cpu_before = H.cpu_seconds(pid)
    sampler = H.Sampler(interval=0.02)
    sampler.start()

    latencies = []
    errors = {}
    lock = threading.Lock()
    wall_start = time.perf_counter()

    with chrome_client.Session() as session:
        session.get(urls[0], timeout=timeout)  # warm the pool
        work = iter(urls)

        def worker():
            while True:
                with lock:
                    try:
                        url = next(work)
                    except StopIteration:
                        return
                begin = time.perf_counter()
                try:
                    response = session.get(url, timeout=timeout)
                    if response.status_code >= 400:
                        raise RuntimeError("HTTP %d" % response.status_code)
                except BaseException as exc:  # noqa: BLE001
                    with lock:
                        errors[type(exc).__name__] = errors.get(
                            type(exc).__name__, 0) + 1
                        latencies.append((time.perf_counter() - begin) * 1000.0)
                    continue
                with lock:
                    latencies.append((time.perf_counter() - begin) * 1000.0)

        pool = [threading.Thread(target=worker) for _ in range(threads)]
        for thread in pool:
            thread.start()
        for thread in pool:
            thread.join(timeout + 120)

    elapsed = time.perf_counter() - wall_start
    resources = sampler.stop()
    server = _gauge(base)
    ctxt_after = read_ctxt(pid)
    sched_after = read_schedstat(pid)
    cpu_after = H.cpu_seconds(pid)

    def delta(before, after, index):
        if before is None or after is None:
            return None
        return after[index] - before[index]

    report = H.latency_report(latencies)
    mean = report.get("mean_ms") or 0.0
    p99 = report.get("p99_ms") or 0.0
    return {
        "shape": shape,
        "threads": threads,
        "requests": requests,
        "seconds": round(elapsed, 3),
        "qps": round(requests / elapsed, 1) if elapsed else None,
        "latency": report,
        "p99_over_mean": round(p99 / mean, 2) if mean else None,
        "errors": errors,
        "ctxt_voluntary": delta(ctxt_before, ctxt_after, 0),
        "ctxt_involuntary": delta(ctxt_before, ctxt_after, 1),
        "sched_wait_ms": (round(delta(sched_before, sched_after, 1) / 1e6, 1)
                          if sched_before and sched_after else None),
        "cpu_cores": (round((cpu_after - cpu_before) / elapsed, 2)
                      if cpu_after is not None and cpu_before is not None
                      and elapsed else None),
        "resources": resources,
        "server_peak": server.get("peak"),
        "server_now_at_end": server.get("now"),
    }


def knee(points, field):
    """First doubling that stops buying throughput.

    "Stops buying" is less than 20% more requests/second for the same doubling:
    outside this fixture's run-to-run noise, inside what a working pool delivers.
    Returning the first such step (not the last pair) is the difference between
    reporting where the plateau begins and reporting where the run ended -- the
    earlier version returned the final pair, so a ladder that plateaued at 6 and
    was measured to 12 reported the 8-to-12 step instead of 6-to-8.
    """
    # Any step, not only a doubling: a ladder is often 1,2,4,6,8,12 precisely
    # because the ceiling is not a power of two. Requiring a doubling made the
    # 6-to-8 step -- the one where the plateau actually begins -- invisible.
    for previous, current in zip(points, points[1:]):
        before = previous.get(field) or 0
        after = current.get(field) or 0
        if before <= 0 or current["threads"] <= previous["threads"]:
            continue
        gain = after / before
        if gain < 1.2:
            return {"from_threads": previous["threads"],
                    "to_threads": current["threads"],
                    "gain": round(gain, 2)}
    return None


def latency_knee(points):
    """First step where p99/mean ratio jumps by more than half.

    The ratio is used instead of an absolute p99 so the number does not depend on
    which shape was chosen: a delay-dominated cell has a large p99 at every step.
    """
    baseline = None
    for point in points:
        ratio = point.get("p99_over_mean")
        if ratio is None:
            continue
        if baseline is None:
            baseline = ratio
            continue
        if ratio > baseline * 1.5:
            return {"threads": point["threads"], "ratio": ratio,
                    "baseline_ratio": baseline}
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--ladder", default="1,2,4,8,16,32")
    parser.add_argument("--requests", type=int, default=256)
    parser.add_argument("--shapes", nargs="*", default=["fast", "delay"])
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--control", action="store_true",
                        help="also run the http.client control group per step")
    args = parser.parse_args()

    chrome_client = H.load_chrome_client()
    ladder = [int(step) for step in args.ladder.split(",") if step.strip()]
    report = {
        "python": sys.version.split()[0],
        "ladder": ladder,
        "cpu_count": os.cpu_count(),
        "cells": {},
    }

    with H.FixtureServer() as server:
        report["fixture"] = server.base
        for shape in args.shapes:
            points = []
            for threads in ladder:
                cell = run_cell(chrome_client, server.base, shape, threads,
                                args.requests, args.timeout)
                points.append(cell)
                print("%-6s threads=%-3d qps=%-8s p50=%-7s p99=%-8s "
                      "p99/mean=%-5s server_peak=%-3s ctxt=%-6s "
                      "sched_wait=%-8s cpu=%-4s fd_peak=%-3s err=%s"
                      % (shape, threads, cell["qps"],
                         cell["latency"].get("p50_ms"),
                         cell["latency"].get("p99_ms"),
                         cell["p99_over_mean"], cell["server_peak"],
                         cell["ctxt_voluntary"], cell["sched_wait_ms"],
                         cell["cpu_cores"],
                         cell["resources"].get("fd_peak"),
                         cell["errors"] or "-"), flush=True)
            report["cells"][shape] = {
                "points": points,
                "throughput_knee": knee(points, "qps"),
                "latency_knee": latency_knee(points),
            }
            print("%-6s throughput knee: %s   latency knee: %s"
                  % (shape, report["cells"][shape]["throughput_knee"],
                     report["cells"][shape]["latency_knee"]), flush=True)

    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + chr(10), encoding="utf-8")
        print("wrote %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
