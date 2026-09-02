#!/usr/bin/env python3
"""Records a throughput/concurrency baseline for the Core plus Python binding.

Everything runs against a local HTTP server, so results describe the client
stack rather than a network path. Point the loader at the Core under test:

    LD_LIBRARY_PATH=core/binaries/linux-x86_64 \
    PYTHONPATH=bindings/python \
        tools/bench-core-baseline.py --out docs/baseline-linux-x86_64.json

The `stalled_consumer_isolation` case is the one that matters for the
backpressure work: it stalls one streaming consumer past the 1 MiB queue
ceiling and then measures whether unrelated requests still complete.
"""

import argparse
import asyncio
import json
import statistics
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import chrome_client


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # A keep-alive response split across several small writes costs ~40 ms per
    # request from the delayed-ACK timer, which would swamp every client-side
    # number measured here. Disable Nagle and emit each small response as one
    # write so the benchmark measures the client stack.
    disable_nagle_algorithm = True

    def do_GET(self):
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        if parts.path == "/stream":
            chunk = b"x" * int(query.get("size", ["65536"])[0])
            count = int(query.get("chunks", ["1024"])[0])
            self.send_response(200)
            self.send_header("Content-Length", str(len(chunk) * count))
            self.end_headers()
            try:
                for _ in range(count):
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        payload = b"x" * int(query.get("size", ["1024"])[0])
        head = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Length: %d\r\n"
            "Content-Type: application/octet-stream\r\n"
            "\r\n" % len(payload)
        ).encode("ascii")
        try:
            self.wfile.write(head + payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *_args):
        pass


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


def sync_sequential(url, count):
    latencies = []
    with chrome_client.Client() as client:
        client.get(url)  # warm the connection pool and TLS/HTTP state
        start = time.perf_counter()
        for _ in range(count):
            begin = time.perf_counter()
            response = client.get(url)
            assert response.status_code == 200, response.status_code
            latencies.append((time.perf_counter() - begin) * 1000.0)
        elapsed = time.perf_counter() - start
    return {
        "requests": count,
        "seconds": round(elapsed, 3),
        "requests_per_second": round(count / elapsed, 1),
        "latency_ms_p50": round(statistics.median(latencies), 2),
        "latency_ms_p95": round(percentile(latencies, 0.95), 2),
    }


def async_concurrent(url, count, concurrency):
    async def run():
        async with chrome_client.AsyncClient() as client:
            await client.get(url)
            semaphore = asyncio.Semaphore(concurrency)

            async def one():
                async with semaphore:
                    response = await client.get(url)
                    assert response.status_code == 200, response.status_code

            start = time.perf_counter()
            await asyncio.gather(*[one() for _ in range(count)])
            return time.perf_counter() - start

    elapsed = asyncio.run(run())
    return {
        "requests": count,
        "concurrency": concurrency,
        "seconds": round(elapsed, 3),
        "requests_per_second": round(count / elapsed, 1),
    }


def stream_throughput(url, chunk_size):
    with chrome_client.Client() as client:
        start = time.perf_counter()
        response = client.get(url, stream=True)
        total = 0
        try:
            for chunk in response.iter_content(chunk_size):
                total += len(chunk)
        finally:
            response.close()
        elapsed = time.perf_counter() - start
    return {
        "bytes": total,
        "seconds": round(elapsed, 3),
        "mib_per_second": round(total / elapsed / (1024 * 1024), 1),
        "read_chunk_bytes": chunk_size,
    }


def stalled_consumer_isolation(base_url, probes, timeout):
    """Stalls one streaming consumer and times unrelated requests.

    Reading a single chunk and then stopping leaves the Rust body queue above its
    1 MiB ceiling, so the Core callback thread blocks inside `on_body`. Requests
    on the same Engine and on a second Engine are then timed separately: a
    process-wide callback runner stalls both, a per-request runner stalls neither.
    """
    small = base_url + "/small?size=1024"
    stream = base_url + "/stream?chunks=1024&size=65536"
    result = {"probes": probes, "timeout_seconds": timeout}

    with chrome_client.Client() as stalled_client, \
            chrome_client.Client() as other_client:
        stalled = stalled_client.get(stream, stream=True)
        chunks = stalled.iter_content(65536)
        next(iter(chunks))  # take one chunk, then stop draining
        time.sleep(1.0)  # let the queue reach its ceiling

        for label, client in (("same_engine", stalled_client),
                              ("other_engine", other_client)):
            completed, failures, latencies = 0, 0, []
            for _ in range(probes):
                begin = time.perf_counter()
                try:
                    response = client.get(small, timeout=timeout)
                    assert response.status_code == 200, response.status_code
                    completed += 1
                except Exception:  # noqa: BLE001 - any failure is a stall signal
                    failures += 1
                latencies.append((time.perf_counter() - begin) * 1000.0)
            result[label] = {
                "completed": completed,
                "failed": failures,
                "latency_ms_p50": round(statistics.median(latencies), 2),
                "latency_ms_max": round(max(latencies), 2),
            }
        try:
            stalled.close()
        except Exception:  # noqa: BLE001 - close races with a blocked callback
            pass
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", help="write the JSON report to this path")
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--stream-chunks", type=int, default=1024)
    parser.add_argument("--probes", type=int, default=20)
    parser.add_argument("--probe-timeout", type=float, default=5.0)
    parser.add_argument("--skip-isolation", action="store_true")
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = "http://127.0.0.1:%d" % server.server_port

    report = {
        "native_module": chrome_client._python_impl._native.__name__,
        "python": sys.version.split()[0],
        "server": base_url,
    }
    try:
        report["sync_sequential_1kib"] = sync_sequential(
            base_url + "/small?size=1024", args.requests)
        report["async_concurrency_32"] = async_concurrent(
            base_url + "/small?size=1024", args.requests, 32)
        report["async_concurrency_128"] = async_concurrent(
            base_url + "/small?size=1024", args.requests, 128)
        report["stream_throughput_64mib"] = stream_throughput(
            base_url + "/stream?chunks=%d&size=65536" % args.stream_chunks,
            64 * 1024)
        if not args.skip_isolation:
            report["stalled_consumer_isolation"] = stalled_consumer_isolation(
                base_url, args.probes, args.probe_timeout)
    finally:
        server.shutdown()

    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

