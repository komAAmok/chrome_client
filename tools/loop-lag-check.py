#!/usr/bin/env python3
"""Measures event-loop lag under an HTTP load, in the two shapes that differ.

docs/TBD_VERIFICATION.md (T-6) recorded that the table in
docs/ASYNC_CONCURRENCY.md was measured on a shape the table header did not name,
and that the probe used to re-measure it was never committed. This is that
probe: the two shapes are one URL (HttpCache serialises on a single key) and N
distinct URLs (it does not), and the lag numbers mean opposite things.

    LD_LIBRARY_PATH=core/binaries/linux-x86_64 PYTHONPATH=bindings/python \
        python3 tools/loop-lag-check.py --requests 512 --shape both

Output is JSON so a document can quote it without retyping:
  wall_ms, qps, lag_p50_ms, lag_p99_ms, lag_max_ms, network_thread_ms_per_request
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    """One 1 KiB response, so the body is not what is being measured."""

    protocol_version = "HTTP/1.1"

    def do_GET(self):
        body = b"x" * 1024
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


async def measure(base, count, distinct, cache_mode):
    """Returns the lag statistics of one shape.

    Args:
        base: Origin of the fixture.
        count: Requests in flight at once, all of them, one wave.
        distinct: When true every request gets its own URL, so HttpCache has no
            shared key to serialise on.
        cache_mode: ``cache_mode=`` passed to each request.

    Returns:
        A dict of measurements.
    """
    import chrome_client

    loop = asyncio.get_running_loop()
    lags = []
    stop = False

    async def ticker():
        last = loop.time()
        while not stop:
            await asyncio.sleep(0.001)
            now = loop.time()
            lags.append((now - last - 0.001) * 1000.0)
            last = now

    URLs = ["%s/s?i=%d" % (base, index) if distinct else "%s/s" % base
            for index in range(count)]
    task = asyncio.ensure_future(ticker())
    await asyncio.sleep(0.01)
    started = time.perf_counter()
    async with chrome_client.AsyncSession() as session:
        await asyncio.gather(*[session.get(url, cache_mode=cache_mode)
                               for url in URLs])
    elapsed = time.perf_counter() - started
    stop = True
    await task
    ordered = sorted(lags)
    return {
        "requests": count,
        "distinct_urls": distinct,
        "cache_mode": cache_mode,
        "wall_ms": round(elapsed * 1000, 1),
        "qps": round(count / elapsed, 1),
        "lag_p50_ms": round(ordered[len(ordered) // 2], 2),
        "lag_p99_ms": round(ordered[int(len(ordered) * 0.99)], 2),
        "lag_max_ms": round(ordered[-1], 2),
        "samples": len(ordered),
    }


def main():
    """Runs the requested shapes; returns a process exit code."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=512)
    parser.add_argument("--shape", choices=("one-url", "distinct", "both"),
                        default="both")
    parser.add_argument("--cache-mode", default=None,
                        help="e.g. no_store, to separate HttpCache queueing from "
                             "loop cost")
    arguments = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % server.server_port
    shapes = (("one-url", False), ("distinct", True))
    if arguments.shape != "both":
        shapes = tuple(row for row in shapes if row[0] == arguments.shape)
    try:
        for name, distinct in shapes:
            result = asyncio.run(measure(base, arguments.requests, distinct,
                                         arguments.cache_mode))
            result["shape"] = name
            print(json.dumps(result, sort_keys=True), flush=True)
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
