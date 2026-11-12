#!/usr/bin/env python3
"""Runs the concurrency matrix and writes one JSON report.

The matrix is a ladder, not a list: every cell is (shape x concurrency) and
the interesting quantity is how a cell moves as concurrency doubles. A single
run at "high concurrency" cannot distinguish a client that saturates from a
fixture that saturates, which is why the control group runs first and why the
fixture serves every shape itself rather than borrowing http.server.

    LD_LIBRARY_PATH=core/binaries/linux-x86_64 \
    PYTHONPATH=bindings/python \
        tools/load-matrix.py --out docs/perf-matrix.json

Shapes and what each one is for:

  small-1k      per-request overhead: engine lookup, header parse, response wrap
  body-64k      one socket read per request; isolates body copy cost
  chunked-4m    chunk reassembly across the Rust body queue
  stream-4m     iter_content with a 64 KiB consumer; queue backpressure
  delay-10ms    latency-dominated cell: at low concurrency it measures the
                fixture timer, at high concurrency it measures the client
                ability to keep N transactions in flight
  status-500    error construction on a 4xx/5xx path (no body)

Fault cells are separate because they answer a different question: not "how
fast" but "does the failure terminate and free its resources". Each one is
run with a deadline so a hang shows up as a timeout count instead of a stuck
harness.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import perf_harness as H


SHAPES = {
    "small-1k": "/small?size=1024",
    "body-64k": "/small?size=65536",
    "chunked-4m": "/chunked?chunks=64&size=65536",
    "delay-10ms": "/delay?ms=10&size=1024",
    "status-500": "/status?code=500",
}


def build_urls(base, path, count, distinct):
    """distinct=True appends a cache-busting query so HttpCache cannot serialize.

    Chromium serializes transactions that share one cache key, so a single-URL
    ladder measures that serialization instead of concurrency (LESSONS.md 19
    and the docstring of tools/bench-core-baseline.py). The distinct form is the
    default; --single-url keeps the serialized form available for comparison.
    """
    if not distinct:
        return [base + path] * count
    joiner = "&" if "?" in path else "?"
    return ["%s%s%sn=%d" % (base, path, joiner, i) for i in range(count)]


def run_cell(chrome_client, base, shape, concurrency, requests, mode,
             distinct, timeout, sampler_interval):
    path = SHAPES[shape]
    urls = build_urls(base, path, requests, distinct)
    kwargs = {}
    if timeout:
        kwargs["timeout"] = timeout

    sampler = H.Sampler(interval=sampler_interval)
    sampler.start()
    begin = time.perf_counter()
    try:
        if mode == "client":
            # Client is Session and AsyncClient is AsyncSession (sessions.py:3127),
            # so this cell exists to prove the alias behaves identically under
            # load rather than only in a smoke test.
            with chrome_client.Client() as session:
                session.get(urls[0], **kwargs)
                result = H.drive_sync(session, urls, concurrency, **kwargs)
        elif mode == "sync":
            with chrome_client.Session() as session:
                session.get(urls[0], **kwargs)  # warm engine + socket
                result = H.drive_sync(session, urls, concurrency, **kwargs)
        else:
            result = H.drive_async(
                urls, concurrency,
                chrome_client.AsyncClient if mode == "asyncclient"
                else chrome_client.AsyncSession, **kwargs)
    except BaseException as exc:  # noqa: BLE001 - a crashed cell is a result
        result = {"mode": mode, "concurrency": concurrency, "requests": requests,
                  "error": "%s: %s" % (type(exc).__name__, exc)}
    wall = time.perf_counter() - begin
    result["wall_seconds"] = round(wall, 3)
    result["shape"] = shape
    result["path"] = path
    result["distinct_urls"] = distinct
    result["resources"] = sampler.stop()
    return result


def run_fault(chrome_client, base, kind, timeout, repeats):
    """Each fault must terminate inside its deadline and leave no descriptor.

    A hang is recorded as a timeout rather than crashing the harness: the whole
    point of the fault matrix is that the client is allowed to be slow, and only
    forbidden to never return.
    """
    cases = {
        "stall-timeout": (base + "/stall?ms=30000", timeout or 1.0),
        "reset": (base + "/reset", timeout or 5.0),
        "truncated-body": (base + "/truncate?size=1024", timeout or 5.0),
        "refused-port": ("http://127.0.0.1:1/", timeout or 1.0),
        "status-503": (base + "/status?code=503", timeout or 5.0),
        "redirect-loop": (base + "/redirect?n=50", timeout or 5.0),
    }
    url, deadline = cases[kind]
    before = H.fd_count(os.getpid())
    outcomes = {}
    latencies = []
    with chrome_client.Session() as session:
        for _ in range(repeats):
            begin = time.perf_counter()
            try:
                response = session.get(url, timeout=deadline)
                key = "HTTP %d" % response.status_code
            except BaseException as exc:  # noqa: BLE001
                key = H.classify_error(exc)
            latencies.append((time.perf_counter() - begin) * 1000.0)
            outcomes[key] = outcomes.get(key, 0) + 1
    after = H.fd_count(os.getpid())
    return {
        "kind": kind,
        "url": url,
        "deadline_s": deadline,
        "repeats": repeats,
        "outcomes": outcomes,
        "latency": H.latency_report(latencies),
        "fd_before": before,
        "fd_after": after,
        "fd_growth": (after - before) if (before is not None and after is not None) else None,
        "hangs": outcomes.get("Timeout", 0),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--ladder", default="1,4,16,32,64,128",
                        help="concurrency steps, comma separated")
    parser.add_argument("--requests", type=int, default=0,
                        help="requests per cell; default max(64, 8*concurrency)")
    parser.add_argument("--mode",
                        choices=("sync", "async", "both", "client", "asyncclient",
                                 "aliases"),
                        default="both",
                        help="client/asyncclient drive the alias names; aliases "
                             "runs Session and Client on the same cells")
    parser.add_argument("--shapes", nargs="*", default=list(SHAPES),
                        help="subset of the request shapes")
    parser.add_argument("--single-url", action="store_true",
                        help="share one cache key: measures HttpCache serialization")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--sample-interval", type=float, default=0.05)
    parser.add_argument("--skip-control", action="store_true")
    parser.add_argument("--skip-faults", action="store_true")
    parser.add_argument("--fault-repeats", type=int, default=5)
    args = parser.parse_args()

    chrome_client = H.load_chrome_client()
    ladder = [int(step) for step in args.ladder.split(",") if step.strip()]
    report = {
        "python": sys.version.split()[0],
        "native_module": getattr(chrome_client, "__name__", "chrome_client"),
        "ladder": ladder,
        "single_url": args.single_url,
        "cpu_count": os.cpu_count(),
        "cells": [],
    }

    with H.FixtureServer() as server:
        report["fixture"] = server.base
        if not args.skip_control:
            report["control_group"] = [
                H.control_group(server.base, max(200, 40 * c), c)
                for c in ladder]
        for shape in args.shapes:
            for concurrency in ladder:
                requests = args.requests or max(64, 8 * concurrency)
                if args.mode == "both":
                    modes = ("sync", "async")
                elif args.mode == "aliases":
                    modes = ("sync", "client")
                else:
                    modes = (args.mode,)
                for mode in modes:
                    cell = run_cell(chrome_client, server.base, shape,
                                    concurrency, requests, mode,
                                    not args.single_url, args.timeout,
                                    args.sample_interval)
                    report["cells"].append(cell)
                    lat = cell.get("latency", {})
                    que = cell.get("queue_latency", {})
                    print("%-11s %-5s c=%-3d qps=%-7s p50=%-6s p99=%-7s "
                          "queue_p50=%-7s fd=%-4s thr=%-3s cpu=%-4s err=%s"
                          % (shape, mode, concurrency, cell.get("qps"),
                             lat.get("p50_ms"), lat.get("p99_ms"),
                             que.get("p50_ms"),
                             cell["resources"].get("fd_peak"),
                             cell["resources"].get("threads_peak"),
                             cell["resources"].get("cpu_cores_used"),
                             cell.get("errors") or cell.get("error") or "-"),
                          flush=True)
        if not args.skip_faults:
            report["faults"] = []
            for kind in ("stall-timeout", "reset", "truncated-body",
                         "refused-port", "status-503", "redirect-loop"):
                record = run_fault(chrome_client, server.base, kind,
                                   args.timeout, args.fault_repeats)
                report["faults"].append(record)
                print("fault %-15s %s fd%+d" % (kind, record["outcomes"],
                                                record["fd_growth"] or 0),
                      flush=True)

    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + chr(10), encoding="utf-8")
        print("wrote %s" % args.out)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
