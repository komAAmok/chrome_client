#!/usr/bin/env python3
"""Async (event-loop) diagnostics: lag, queue depth, dangling futures, memory slope.

The five questions an asyncio client raises are not answerable from throughput:
a loop blocked by a synchronous callback and a loop starved by an unbounded
queue both show a plateau. What separates them is *when* the loop gets to run
and *what* is still outstanding, so every subcommand measures one of those:

    tools/async-diag.py lag        --concurrency 64 --requests 512
    tools/async-diag.py futures    --requests 512
    tools/async-diag.py slow-consumer --chunks 256 --consumer-ms 5
    tools/async-diag.py flood      --ladder 64,256,1024 --requests 4096
    tools/async-diag.py tasks      --requests 256

Lag is measured with a monotonic self-rescheduling callback, which is the only
way to see a blocked loop: wall-clock latency of a request cannot tell "the
server was slow" from "my callback held the loop for 200 ms".
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import perf_harness as H


class LagMonitor(object):
    """Samples event-loop lag: scheduled time minus actual time, per tick."""

    def __init__(self, interval=0.01):
        self.interval = interval
        self.samples = []
        self._task = None
        self._stop = False

    async def _run(self):
        while not self._stop:
            scheduled = time.perf_counter() + self.interval
            await asyncio.sleep(self.interval)
            self.samples.append(max(0.0, (time.perf_counter() - scheduled) * 1000.0))

    def start(self):
        self._task = asyncio.ensure_future(self._run())

    def stop(self):
        self._stop = True
        if self._task is not None:
            self._task.cancel()
        return H.latency_report(self.samples)


def count_tasks():
    try:
        tasks = asyncio.all_tasks()
    except RuntimeError:
        return 0
    return len([t for t in tasks if not t.done()])


def rss_kib():
    return H.read_status_field(os.getpid(), "VmRSS") or 0


async def run_lag(chrome_client, base, concurrency, requests, shape):
    path = "/small?size=1024" if shape == "fast" else "/delay?ms=20&size=1024"
    urls = ["%s%s%sn=%d" % (base, path, "&" if "?" in path else "?", i)
            for i in range(requests)]
    monitor = LagMonitor()
    monitor.start()
    peak_tasks = 0
    latencies = []
    errors = {}
    async with chrome_client.AsyncSession(max_clients=concurrency) as session:
        async def one(url):
            nonlocal peak_tasks
            begin = time.perf_counter()
            try:
                response = await session.get(url, timeout=30)
                if response.status_code >= 400:
                    raise RuntimeError("HTTP %d" % response.status_code)
            except BaseException as exc:
                errors[type(exc).__name__] = errors.get(type(exc).__name__, 0) + 1
            latencies.append((time.perf_counter() - begin) * 1000.0)
            peak_tasks = max(peak_tasks, count_tasks())

        begin = time.perf_counter()
        await asyncio.gather(*[one(url) for url in urls])
        elapsed = time.perf_counter() - begin
    lag = monitor.stop()
    return {"elapsed": round(elapsed, 3), "qps": round(requests / elapsed, 1),
            "latency": H.latency_report(latencies), "lag": lag,
            "peak_tasks": peak_tasks, "errors": errors}


def cmd_lag(args) -> int:
    """Event-loop lag under a request flood, at a fixed concurrency."""
    chrome_client = H.load_chrome_client()
    with H.FixtureServer() as server:
        result = asyncio.run(run_lag(chrome_client, server.base, args.concurrency,
                                     args.requests, args.shape))
    print("concurrency=%d requests=%d qps=%s peak_tasks=%d"
          % (args.concurrency, args.requests, result["qps"], result["peak_tasks"]))
    print("request latency: p50=%s p99=%s"
          % (result["latency"].get("p50_ms"), result["latency"].get("p99_ms")))
    lag = result["lag"]
    print("event-loop lag: p50=%.2f ms p99=%.2f ms max=%.2f ms"
          % (lag.get("p50_ms") or 0, lag.get("p99_ms") or 0, lag.get("max_ms") or 0))
    print()
    print("reading this: lag is time the loop was ready but could not run. Under");
    print("1 ms is healthy. If p99 lag tracks request latency, the loop is being");
    print("blocked by a synchronous callback; if it stays low while latency grows,");
    print("the wait is in the Core or the server, not the loop.")
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0


def _socket_states():
    """Per-state socket counts for this process, or {} when ss is unavailable."""
    import subprocess
    try:
        out = subprocess.run(["ss", "-tanp"], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    marker = "pid=%d," % os.getpid()
    states = {}
    for line in out.splitlines():
        if marker in line:
            state = line.split()[0]
            states[state] = states.get(state, 0) + 1
    return states


def cmd_futures(args) -> int:
    """Counts futures/tasks left pending after a batch completes."""
    chrome_client = H.load_chrome_client()

    async def run(base):
        urls = ["%s/small?size=1024&n=%d" % (base, i) for i in range(args.requests)]
        gc.collect()
        before = {"tasks": count_tasks(), "objects": len(gc.get_objects())}
        async with chrome_client.AsyncSession(max_clients=args.concurrency) as session:
            await asyncio.gather(*[session.get(url, timeout=30) for url in urls])
            await asyncio.sleep(0)
            after = {"tasks": count_tasks(), "objects": len(gc.get_objects())}
        await asyncio.sleep(0.05)
        settled = {"tasks": count_tasks(), "objects": len(gc.get_objects())}
        return before, after, settled

    with H.FixtureServer() as server:
        before, after, settled = asyncio.run(run(server.base))
    print("before : tasks=%d objects=%d" % (before["tasks"], before["objects"]))
    print("after  : tasks=%d objects=%d" % (after["tasks"], after["objects"]))
    print("settled: tasks=%d objects=%d" % (settled["tasks"], settled["objects"]))
    leaked = settled["tasks"] - before["tasks"]
    print()
    print("dangling tasks after the batch: %+d" % leaked)
    print("a positive number that scales with --requests is a future that is never");
    print("awaited or cancelled. The AsyncSession itself is closed above, so a task");
    print("that survives that is holding a native request open.")
    return 0 if leaked <= 0 else 1


def cmd_slow_consumer(args) -> int:
    """A consumer that reads slower than the server sends: backpressure test."""
    chrome_client = H.load_chrome_client()

    async def run(base):
        url = "%s/chunked?chunks=%d&size=65536" % (base, args.chunks)
        rss_start = rss_kib()
        samples = []
        async with chrome_client.AsyncSession() as session:
            response = await session.get(url, stream=True, timeout=120)
            total = 0
            chunks = 0
            async for chunk in response.aiter_content(65536):
                total += len(chunk)
                chunks += 1
                if args.consumer_ms:
                    await asyncio.sleep(args.consumer_ms / 1000.0)
                if chunks % 16 == 0:
                    samples.append(rss_kib() - rss_start)
            await response.aclose()
        return total, chunks, samples, rss_kib() - rss_start

    with H.FixtureServer() as server:
        total, chunks, samples, growth = asyncio.run(run(server.base))
    print("consumed %d bytes in %d chunks, consumer delay %s ms/chunk"
          % (total, chunks, args.consumer_ms))
    print("RSS growth over the transfer: %+d KiB" % growth)
    print("RSS samples every 16 chunks (KiB over start): %s"
          % (samples[:12],))
    print()
    print("reading this: the Rust body queue is capped at STREAM_BUFFER_LIMIT");
    print("(1 MiB), so a slow consumer must flatten the curve, not grow it. A");
    print("slope that keeps climbing means the cap is not being applied and the");
    print("whole body is being buffered in memory.")
    return 0


def cmd_flood(args) -> int:
    """Concurrency flood: does memory or lag grow with outstanding requests?"""
    chrome_client = H.load_chrome_client()
    ladder = [int(step) for step in args.ladder.split(",") if step.strip()]
    with H.FixtureServer() as server:
        print("%-8s %-9s %-8s %-9s %-9s %s"
              % ("limit", "qps", "p99", "lag_p99", "rss_peak", "tasks_peak"))
        for concurrency in ladder:
            sampler = H.Sampler(interval=0.05)
            sampler.start()
            result = asyncio.run(run_lag(chrome_client, server.base, concurrency,
                                         args.requests, "fast"))
            resources = sampler.stop()
            print("%-8d %-9s %-8s %-9.2f %-9s %d"
                  % (concurrency, result["qps"],
                     result["latency"].get("p99_ms"),
                     result["lag"].get("p99_ms") or 0,
                     resources.get("rss_kib_peak"), result["peak_tasks"]))
    print()
    print("reading this: rss_peak should track the concurrency limit, not the");
    print("number of requests submitted. If it tracks the submitted count, the");
    print("gate is not gating and every request is buffering at once.")
    return 0


def cmd_tasks(args) -> int:
    """Cancellation: does cancelling a task free its native request?

    The failure this looks for is a future that is cancelled in Python while the
    Core keeps its request running: the task disappears, the socket does not.
    """
    chrome_client = H.load_chrome_client()

    async def main():
        with H.FixtureServer() as server:
            async with chrome_client.AsyncSession() as session:
                return await run_session(session, server.base, args.requests)

    baseline_tasks, end_tasks, baseline_fds, end_fds = asyncio.run(main())
    print("cancelled %d stalled requests" % args.requests)
    print("tasks: %d -> %d   fds: %s -> %s"
          % (baseline_tasks, end_tasks, baseline_fds, end_fds))
    print()
    print("reading this: the TASK count must not grow -- a climbing task count means");
    print("cancel() never reached the native request. The FD count is expected to grow");
    print("by one per cancelled request while the peer is still holding its end open:");
    print("measured with /stall (a peer that never answers) the client socket sits in");
    print("FIN-WAIT-2/CLOSE-WAIT until the peer responds or TCP gives up. With a peer");
    print("that answers at 400 ms and a 50 ms deadline, the count returned to baseline");
    print("after the peer replied. So the fd growth here is TCP half-close, not a leak;");
    print("it becomes a real limit only if cancels outpace the peer. See");
    print("docs/ASYNC_CONCURRENCY.md section 4.")
    return 0 if end_tasks <= baseline_tasks else 1


async def run_session(session, base, requests):
    stall = "%s/stall?ms=30000" % base
    gc.collect()
    baseline_tasks = count_tasks()
    baseline_fds = H.fd_count(os.getpid())
    for _ in range(requests):
        task = asyncio.ensure_future(session.get(stall, timeout=60))
        await asyncio.sleep(0.001)
        task.cancel()
        try:
            await task
        except BaseException:
            pass
    await asyncio.sleep(0.2)
    return (baseline_tasks, count_tasks(), baseline_fds,
            H.fd_count(os.getpid()))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    lag = sub.add_parser("lag", help="event-loop lag under a flood")
    lag.add_argument("--concurrency", type=int, default=64)
    lag.add_argument("--requests", type=int, default=512)
    lag.add_argument("--shape", choices=("fast", "delay"), default="fast")
    lag.add_argument("--out")
    lag.set_defaults(func=cmd_lag)

    futures = sub.add_parser("futures", help="dangling task/future count")
    futures.add_argument("--requests", type=int, default=512)
    futures.add_argument("--concurrency", type=int, default=64)
    futures.set_defaults(func=cmd_futures)

    slow = sub.add_parser("slow-consumer", help="backpressure with a slow consumer")
    slow.add_argument("--chunks", type=int, default=256)
    slow.add_argument("--consumer-ms", type=float, default=5.0)
    slow.set_defaults(func=cmd_slow_consumer)

    flood = sub.add_parser("flood", help="concurrency flood ladder")
    flood.add_argument("--ladder", default="64,256,1024")
    flood.add_argument("--requests", type=int, default=4096)
    flood.set_defaults(func=cmd_flood)

    tasks = sub.add_parser("tasks", help="cancellation frees native requests")
    tasks.add_argument("--requests", type=int, default=128)
    tasks.set_defaults(func=cmd_tasks)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
