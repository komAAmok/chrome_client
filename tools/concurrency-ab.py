#!/usr/bin/env python3
"""One concurrency pass over the shapes that answer "did throughput move?".

Written for an A/B between two source trees: it prints JSON and nothing else, so
two runs are compared rather than paraphrased. Every arm is a fixed shape, and
the fields are named so a diff cannot be read against the wrong row -- the first
version of this comparison indexed the list positionally and reported the "one
URL @1024" arm as "distinct @1024".

Shapes:

    distinct  distinct URLs per request, so HttpCache never serialises -> the
              ceiling is the client stack
    same      one URL, so the HttpCache queue is the ceiling
    ladder    the synchronous analogue: threads x distinct URLs

The fixture disables Nagle and writes each response in one call. Without that a
keep-alive response split across small writes costs ~40 ms per request from the
delayed-ACK timer, which swamps every client-side number (see
docs/PERF_TEST_AND_LEAK.md, "一个真实的测量陷阱").

    tools/concurrency-ab.py --requests 256,1024 --threads 8,32
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import resource
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CLK_TCK = os.sysconf("SC_CLK_TCK")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    #: See the module docstring: without this the delayed-ACK timer dominates.
    disable_nagle_algorithm = True

    def do_GET(self):
        body = b"x" * 1024
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *_args):
        pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8192
    allow_reuse_address = True


def network_thread_cpu():
    """Seconds of CPU spent by the Core's single IO thread.

    It is named "MiniCronetNet" by `core/source/engine.cc`; summing every
    `ThreadPoolForeg` instead would count the fixture when it runs in-process.

    Returns:
        Accumulated user+system seconds, or 0.0 when the thread is absent.
    """
    total = 0.0
    for tid in os.listdir("/proc/self/task"):
        try:
            name = open("/proc/self/task/%s/comm" % tid).read().strip()
            if name != "MiniCronetNet":
                continue
            parts = open("/proc/self/task/%s/stat" % tid).read().rsplit(")", 1)[1].split()
            total += (int(parts[11]) + int(parts[12])) / CLK_TCK
        except OSError:
            continue
    return total


def rss_kib():
    """Resident set size of this process, in KiB."""
    with open("/proc/self/status", encoding="ascii") as handle:
        return int(handle.read().split("VmRSS:")[1].split()[0])


async def _async_arm(base, requests, distinct, profile):
    """Runs one asynchronous arm and returns its best of three."""
    from chrome_client import AsyncSession

    session = AsyncSession(impersonate=profile, max_clients=requests)
    urls = ([base + "/d%d" % index for index in range(requests)] if distinct
            else [base + "/same"] * requests)
    try:
        await session.get(urls[0])
        await asyncio.sleep(0.1)
        best = None
        for _ in range(3):
            cpu_before, rss_before = network_thread_cpu(), rss_kib()
            started = time.perf_counter()
            responses = await asyncio.gather(*[session.get(url) for url in urls])
            wall = time.perf_counter() - started
            row = {
                "qps": requests / wall,
                "wall_seconds": wall,
                "ok": sum(1 for response in responses if response.status_code == 200),
                "network_thread_ms_per_request":
                    1000 * (network_thread_cpu() - cpu_before) / requests,
                "rss_kib_delta": rss_kib() - rss_before,
            }
            if best is None or row["qps"] > best["qps"]:
                best = row
    finally:
        await session.aclose()
    best.update({"mode": "async", "requests": requests, "distinct": distinct,
                 "profile": profile})
    return best


def _sync_arm(base, threads, per_thread, profile):
    """Runs one synchronous ladder cell and returns its row."""
    from chrome_client import Session

    session = Session(impersonate=profile)
    session.get(base + "/warm")
    latencies, lock = [], threading.Lock()

    def worker(index):
        for round_index in range(per_thread):
            started = time.perf_counter()
            session.get(base + "/s%d_%d" % (index, round_index))
            with lock:
                latencies.append((time.perf_counter() - started) * 1000)

    cpu_before = network_thread_cpu()
    started = time.perf_counter()
    workers = [threading.Thread(target=worker, args=(index,))
               for index in range(threads)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join()
    wall = time.perf_counter() - started
    cpu_after = network_thread_cpu()
    session.close()

    latencies.sort()
    total = threads * per_thread
    return {
        "mode": "sync",
        "threads": threads,
        "requests": total,
        "qps": total / wall,
        "wall_seconds": wall,
        "latency_ms_p50": latencies[len(latencies) // 2],
        "latency_ms_p99": latencies[int(len(latencies) * 0.99)],
        "network_thread_ms_per_request": 1000 * (cpu_after - cpu_before) / total,
        "profile": profile,
    }


async def _main(args):
    server = _Server(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    rows = {"async": [], "sync": []}
    try:
        for requests in args.requests:
            rows["async"].append(await _async_arm(base, requests, True, args.profile))
            rows["async"].append(await _async_arm(base, requests, False, args.profile))
        for threads in args.threads:
            rows["sync"].append(_sync_arm(base, threads, args.per_thread, args.profile))
    finally:
        server.shutdown()
        server.server_close()
    return {"python": sys.version.split()[0], "cpu_count": os.cpu_count(),
            "profile": args.profile, "fixture": base, "rows": rows}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", default="256,1024",
                        help="comma-separated in-flight counts for the async arms")
    parser.add_argument("--threads", default="8,32",
                        help="comma-separated thread counts for the sync ladder")
    parser.add_argument("--per-thread", type=int, default=40)
    parser.add_argument("--profile", default="chrome_120")
    parser.add_argument("--out")
    args = parser.parse_args()
    args.requests = [int(value) for value in args.requests.split(",") if value]
    args.threads = [int(value) for value in args.threads.split(",") if value]

    resource.setrlimit(resource.RLIMIT_NOFILE, (60000, 60000))
    report = asyncio.run(_main(args))
    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
