#!/usr/bin/env python3
"""Shared fixture, metrics sampler and statistics for the performance harness.

Three CLIs import this module and none of them re-implements a server or a
counter:

    tools/load-matrix.py   concurrency ladder x request shape x fault injection
    tools/prof-attr.py     bottleneck attribution (thread state, syscalls, stacks)
    tools/leak-hunt.py     per-shape growth slopes and a minimal reproducer

Why a hand-written server instead of http.server: every fault this harness needs
to inject is a *wire-level* deviation -- a truncated Content-Length, a reset
instead of a FIN, a header that never arrives, a body that trickles. Those are
not expressible through BaseHTTPRequestHandler, and a fault injected at the
Python object layer would measure the client against a lie.

The server is the fixture, never the subject. Anything measured here describes
the client stack (Python facade -> Rust binding -> libminicronet) against
loopback, which is what docs/LESSONS.md section 19 requires: a local target and
an independent control group, because a public echo service own rate limiting
has been mistaken for a client defect before.
"""

from __future__ import annotations

import os
import socket
import socketserver
import statistics
import sys
import threading
import time
from typing import Iterable, Optional
from urllib.parse import parse_qs, urlsplit

CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


# --------------------------------------------------------------------------
# fixture server
# --------------------------------------------------------------------------

#: Live gauge of concurrent in-flight requests, updated by /concurrency. A
#: backpressure claim has to be checked on the server side: counting coroutines
#: that have started proves nothing, because asyncio starts all of them and the
#: library's gate is what serialises the actual requests.
INFLIGHT = {"now": 0, "peak": 0}


class _Handler(socketserver.BaseRequestHandler):
    """One connection, keep-alive, with wire-level fault modes."""

    def handle(self) -> None:
        sock = self.request
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        buf = b""
        try:
            while True:
                parsed = self._read_request(sock, buf)
                if parsed is None:
                    return
                method, path, headers, body, buf = parsed
                # Every response is counted, not just the ones on a dedicated
                # gauge endpoint: "how many requests is the server handling at
                # once" is the only client-independent measure of the connection
                # pool ceiling, and it has to be available for whichever shape a
                # caller picked. /inflight itself is excluded so reading the
                # gauge cannot perturb it.
                counted = not path.startswith("/inflight")
                if counted:
                    INFLIGHT["now"] += 1
                    INFLIGHT["peak"] = max(INFLIGHT["peak"], INFLIGHT["now"])
                try:
                    if self._respond(sock, method, path, headers, body):
                        return
                finally:
                    if counted:
                        INFLIGHT["now"] -= 1
        except (ConnectionResetError, BrokenPipeError, OSError):
            return

    @staticmethod
    def _read_request(sock, buf):
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                return None
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        try:
            method, target, _version = lines[0].decode("latin-1").split(" ")
        except ValueError:
            return None
        headers = {}
        for line in lines[1:]:
            name, _, value = line.decode("latin-1").partition(":")
            headers[name.strip().lower()] = value.strip()
        length = int(headers.get("content-length", "0") or 0)
        while len(rest) < length:
            chunk = sock.recv(65536)
            if not chunk:
                return None
            rest += chunk
        body, buf = rest[:length], rest[length:]
        return method, target, headers, body, buf

    def _send(self, sock, status, headers, body, method):
        reason = {200: "OK", 302: "Found", 400: "Bad Request",
                  404: "Not Found", 500: "Internal Server Error",
                  503: "Service Unavailable"}.get(status, "OK")
        lines = ["HTTP/1.1 %d %s" % (status, reason)]
        for name, value in headers:
            lines.append("%s: %s" % (name, value))
        head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
        payload = b"" if method == "HEAD" else body
        sock.sendall(head + payload)

    def _respond(self, sock, method, target, headers, body) -> bool:
        """Returns True when the connection must be closed after this response."""
        parts = urlsplit(target)
        query = parse_qs(parts.query)
        path = parts.path

        def q(name, default):
            # A caller may append its cache-buster with "?" instead of "&", which
            # glues it onto the last value. Parsing the leading digits keeps the
            # fixture usable instead of turning a harness bug into a 500.
            raw = query.get(name, [str(default)])[0]
            digits = ""
            for character in raw:
                if character.isdigit():
                    digits += character
                else:
                    break
            return int(digits) if digits else default

        if path == "/small":
            size = q("size", 1024)
            self._send(sock, 200, [("Content-Length", size),
                                   ("Content-Type", "application/octet-stream")],
                       b"x" * size, method)
            return False

        if path == "/lines":
            # Newline-delimited body: the only shape that exercises iter_lines and
            # the chunk-boundary logic that splits a line across two reads.
            count = q("n", 5)
            body = b"".join(b"line-%d\n" % index for index in range(count))
            self._send(sock, 200, [("Content-Length", len(body)),
                                   ("Content-Type", "text/plain")], body, method)
            return False

        if path == "/json":
            import json as _json
            body = _json.dumps({"ok": True, "n": q("n", 1)}).encode()
            self._send(sock, 200, [("Content-Length", len(body)),
                                   ("Content-Type", "application/json")], body, method)
            return False

        if path == "/concurrency":
            # Holds the request open for delay_ms and reports the peak number of
            # requests the server saw at once. This is the only place a client's
            # concurrency limit is observable: the client's own counter would be
            # measuring itself.
            delay_ms, size = q("ms", 50), q("size", 64)
            time.sleep(delay_ms / 1000.0)
            body = b"x" * size
            self._send(sock, 200, [("Content-Length", len(body))], body, method)
            return False

        if path == "/inflight":
            # ?reset=1 zeroes the peak after reporting it, so a caller can measure
            # one cell at a time without the previous cell's peak leaking in.
            import json as _json
            body = _json.dumps(dict(INFLIGHT)).encode()
            if query.get("reset", ["0"])[0] == "1":
                INFLIGHT["peak"] = INFLIGHT["now"]
            self._send(sock, 200, [("Content-Length", len(body))], body, method)
            return False

        if path == "/echo":
            # Returns the request headers as JSON. A cookie assertion has to be
            # made on the wire, not on the facade's own record of what it meant
            # to send: response.request is not populated, so checking it proves
            # nothing.
            import json as _json
            body = _json.dumps({"method": method,
                                "path": target,
                                "headers": headers}, sort_keys=True).encode()
            self._send(sock, 200, [("Content-Length", len(body)),
                                   ("Content-Type", "application/json")],
                       body, method)
            return False

        if path == "/setcookie":
            # Cookies are what makes the facade seed an engine store, and a
            # rotating engine configuration is what makes that seed set grow.
            # Without this endpoint the growth is invisible: no cookie, no seed.
            name = query.get("name", ["sid"])[0]
            value = query.get("value", ["v"])[0]
            size = q("size", 64)
            self._send(sock, 200,
                       [("Content-Length", size),
                        ("Set-Cookie", "%s=%s; Path=/" % (name, value))],
                       b"x" * size, method)
            return False

        if path == "/status":
            code = q("code", 500)
            self._send(sock, code, [("Content-Length", 0)], b"", method)
            return False

        if path == "/chunked":
            chunks, size = q("chunks", 16), q("size", 65536)
            head = ("HTTP/1.1 200 OK\r\n"
                    "Content-Type: application/octet-stream\r\n"
                    "Transfer-Encoding: chunked\r\n\r\n").encode("latin-1")
            sock.sendall(head)
            block = b"x" * size
            for _ in range(chunks):
                sock.sendall(b"%x\r\n" % size + block + b"\r\n")
            sock.sendall(b"0\r\n\r\n")
            return False

        if path == "/slowbody":
            chunks, size, delay_ms = q("chunks", 8), q("size", 65536), q("ms", 20)
            total = chunks * size
            # Counted for the WHOLE response, headers and body: this is the only
            # way to see whether two streaming responses are open on the server at
            # the same time, which is the claim about max_clients and stream=True.
            # A gauge that only counts the pre-header phase reports 1 no matter what
            # the body does.
            self._send(sock, 200, [("Content-Length", total),
                                   ("Content-Type", "application/octet-stream")],
                       b"", method)
            block = b"x" * size
            for _ in range(chunks):
                sock.sendall(block)
                time.sleep(delay_ms / 1000.0)
            return False

        if path == "/delay":
            ms, size = q("ms", 100), q("size", 1024)
            time.sleep(ms / 1000.0)
            self._send(sock, 200, [("Content-Length", size)], b"x" * size, method)
            return False

        if path == "/redirect-cookie":
            # Sets a cookie AND redirects: the two things a facade that drove the
            # hops from Python used to get wrong (docs/COMPATIBILITY_BOUNDARY.md
            # "Redirects" lists both as measured drift).
            hops = q("n", 1)
            location = ("/redirect-cookie?n=%d" % (hops - 1)) if hops > 1 else "/small?size=16"
            self._send(sock, 302,
                       [("Content-Length", 0), ("Location", location),
                        ("Set-Cookie", "hop%d=y; Path=/" % hops)],
                       b"", method)
            return False

        if path == "/redirect":
            hops = q("n", 1)
            location = ("/redirect?n=%d" % (hops - 1)) if hops > 1 else "/small?size=16"
            self._send(sock, 302, [("Content-Length", 0), ("Location", location)],
                       b"", method)
            return False

        if path == "/stall":
            # Accept, read the request, answer nothing. The client only exit is
            # its own deadline; this is the case timeout=(0.2, 10) was fixed for.
            time.sleep(min(q("ms", 5000), 60000) / 1000.0)
            return True

        if path == "/reset":
            # SO_LINGER 0 makes close() emit RST rather than FIN, so the client
            # sees a connection error mid-transaction instead of a clean EOF.
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                b"\x01\x00\x00\x00\x00\x00\x00\x00")
            except OSError:
                pass
            return True

        if path == "/truncate":
            size = q("size", 1024)
            head = ("HTTP/1.1 200 OK\r\n"
                    "Content-Length: %d\r\n\r\n" % (size * 2)).encode("latin-1")
            sock.sendall(head + b"x" * size)
            return True

        self._send(sock, 404, [("Content-Length", 0)], b"", method)
        return False


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 512


class FixtureServer:
    """Loopback target with the fault modes the matrix needs."""

    def __init__(self) -> None:
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    @property
    def base(self) -> str:
        return "http://127.0.0.1:%d" % self._server.server_address[1]

    def url(self, path: str) -> str:
        return self.base + path

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------

def read_status_field(pid: int, field: str) -> Optional[int]:
    try:
        with open("/proc/%d/status" % pid, "r", encoding="ascii") as handle:
            for line in handle:
                if line.startswith(field + ":"):
                    return int(line.split()[1])
    except (FileNotFoundError, ProcessLookupError, ValueError):
        return None
    return None


def cpu_seconds(pid: int) -> Optional[float]:
    """utime+stime in seconds, read without parsing comm embedded spaces."""
    try:
        with open("/proc/%d/stat" % pid, "r", encoding="ascii") as handle:
            raw = handle.read()
    except (FileNotFoundError, ProcessLookupError):
        return None
    close = raw.rfind(")")
    if close < 0:
        return None
    fields = raw[close + 2:].split()
    try:
        return (int(fields[11]) + int(fields[12])) / CLK_TCK
    except (IndexError, ValueError):
        return None


def fd_count(pid: int) -> Optional[int]:
    try:
        return len(os.listdir("/proc/%d/fd" % pid))
    except (FileNotFoundError, ProcessLookupError):
        return None


class Sampler(threading.Thread):
    """Samples RSS / fd / thread count / CPU of one pid at a fixed interval.

    Sampling in a background thread rather than reading the counters before and
    after is what makes a *peak* visible: a descriptor leak that is closed again
    by the time the run ends, or an fd ceiling hit only at concurrency 128,
    leaves no trace in an endpoint measurement.
    """

    def __init__(self, pid: Optional[int] = None, interval: float = 0.05) -> None:
        super().__init__(daemon=True)
        self.pid = pid if pid is not None else os.getpid()
        self.interval = interval
        self.samples = []
        self._halt = threading.Event()

    def run(self) -> None:
        start = time.perf_counter()
        while not self._halt.is_set():
            rss = read_status_field(self.pid, "VmRSS")
            threads = read_status_field(self.pid, "Threads")
            cpu = cpu_seconds(self.pid)
            fds = fd_count(self.pid)
            if rss is None and threads is None and cpu is None and fds is None:
                return
            self.samples.append({
                "t": round(time.perf_counter() - start, 3),
                "rss_kib": rss, "threads": threads, "fds": fds, "cpu_s": cpu,
            })
            self._halt.wait(self.interval)

    def stop(self) -> dict:
        self._halt.set()
        self.join(timeout=2.0)
        return self.summary()

    def summary(self) -> dict:
        if not self.samples:
            return {"samples": 0}

        def series(key):
            return [s[key] for s in self.samples if s.get(key) is not None]

        rss, fds, threads = series("rss_kib"), series("fds"), series("threads")
        cpu = series("cpu_s")
        out = {
            "samples": len(self.samples),
            "duration_s": round(self.samples[-1]["t"], 3),
            "rss_kib_first": rss[0] if rss else None,
            "rss_kib_peak": max(rss) if rss else None,
            "rss_kib_last": rss[-1] if rss else None,
            "fd_first": fds[0] if fds else None,
            "fd_peak": max(fds) if fds else None,
            "fd_last": fds[-1] if fds else None,
            "threads_peak": max(threads) if threads else None,
            "threads_last": threads[-1] if threads else None,
        }
        if len(cpu) >= 2:
            out["cpu_s"] = round(cpu[-1] - cpu[0], 3)
            wall = self.samples[-1]["t"] - self.samples[0]["t"]
            out["cpu_cores_used"] = (round((cpu[-1] - cpu[0]) / wall, 2)
                                     if wall else None)
        return out


# --------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------

def percentile(values: Iterable, fraction: float) -> Optional[float]:
    """Nearest-rank percentile: no interpolation, so p99 of 100 samples is real."""
    ordered = sorted(values)
    if not ordered:
        return None
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def latency_report(latencies_ms) -> dict:
    if not latencies_ms:
        return {"count": 0}
    return {
        "count": len(latencies_ms),
        "min_ms": round(min(latencies_ms), 3),
        "p50_ms": round(percentile(latencies_ms, 0.50), 3),
        "p90_ms": round(percentile(latencies_ms, 0.90), 3),
        "p99_ms": round(percentile(latencies_ms, 0.99), 3),
        "p999_ms": round(percentile(latencies_ms, 0.999), 3),
        "max_ms": round(max(latencies_ms), 3),
        "mean_ms": round(statistics.fmean(latencies_ms), 3),
        "stdev_ms": (round(statistics.pstdev(latencies_ms), 3)
                     if len(latencies_ms) > 1 else 0.0),
    }


def slope_per_unit(ys, xs) -> Optional[float]:
    """Least-squares slope: growth per request, which is what a leak looks like."""
    n = len(ys)
    if n < 2:
        return None
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator


def classify_error(exc: BaseException) -> str:
    return type(exc).__name__


# --------------------------------------------------------------------------
# workload drivers
# --------------------------------------------------------------------------

def _record(errors, statuses, status, exc=None):
    if exc is not None:
        name = classify_error(exc)
        errors[name] = errors.get(name, 0) + 1
    elif status is not None:
        statuses[status] = statuses.get(status, 0) + 1


def drive_sync(session, urls, concurrency: int, **kwargs) -> dict:
    """Drives session.get from a thread pool, splitting queue wait from service.

    Two numbers matter and they are not the same one. service_ms is the time
    inside session.get: the client stack plus the fixture. queue_ms is the time a
    request spent waiting for a free worker. A benchmark that reports only their
    sum cannot tell "the client is slow" from "the ladder oversubscribed the
    workers", and the async driver would then be incomparable with this one
    because asyncio queues every coroutine before the first one starts.

    Every worker shares one Session on purpose: a per-thread Session would
    measure engine construction instead of request handling, and the shared path
    is the one the facade documents as thread-safe (EngineCache is RLock-guarded
    and the blocking calls release the GIL through py.detach).
    """
    from concurrent.futures import ThreadPoolExecutor

    service = []
    queue = []
    errors = {}
    statuses = {}
    lock = threading.Lock()

    def one(url: str) -> None:
        begin = time.perf_counter()
        try:
            response = session.get(url, **kwargs)
            status = response.status_code
        except BaseException as exc:  # noqa: BLE001 - every failure is a datum
            with lock:
                service.append((time.perf_counter() - begin) * 1000.0)
                _record(errors, statuses, None, exc)
            return
        with lock:
            service.append((time.perf_counter() - begin) * 1000.0)
            _record(errors, statuses, status)

    begin = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        submitted = []
        for url in urls:
            submitted.append((pool.submit(one, url), time.perf_counter()))
        for future, at in submitted:
            future.result()
            # Waiting for a worker is queue time only when the ladder is wider
            # than the pool; below that the number is submit-loop overhead and
            # reporting it would invent a stall that never happened.
            queue.append(0.0 if len(urls) <= concurrency
                         else max(0.0, (time.perf_counter() - at) * 1000.0))
    elapsed = time.perf_counter() - begin
    return {"mode": "sync", "concurrency": concurrency, "requests": len(urls),
            "seconds": round(elapsed, 3),
            "qps": round(len(urls) / elapsed, 1) if elapsed else None,
            "errors": errors, "statuses": statuses,
            "latency": latency_report(service),
            "queue_latency": latency_report(queue)}


def drive_async(urls, concurrency: int, session_factory, **kwargs) -> dict:
    """Drives AsyncSession.get through asyncio.gather with max_clients=N.

    The facade own gate (max_clients -> asyncio.Semaphore) is the backpressure
    knob under test, so the ladder drives it rather than replacing it.
    asyncio.gather starts every coroutine before any of them finishes, so a
    latency measured from coroutine start is mostly semaphore queue time: at
    gate=1 with 48 requests that reported a 98 ms p50 for a 4 ms request. The
    service clock therefore starts when the request is admitted, and the wait
    before admission is reported separately as queue_latency.
    """
    import asyncio

    service = []
    queue = []
    errors = {}
    statuses = {}

    async def run() -> float:
        gate = asyncio.Semaphore(concurrency)
        async with session_factory(max_clients=concurrency) as session:
            async def one(url: str) -> None:
                start = time.perf_counter()
                async with gate:
                    admitted = time.perf_counter()
                    queue.append((admitted - start) * 1000.0)
                    try:
                        response = await session.get(url, **kwargs)
                        status = response.status_code
                    except BaseException as exc:  # noqa: BLE001
                        service.append((time.perf_counter() - admitted) * 1000.0)
                        _record(errors, statuses, None, exc)
                        return
                    service.append((time.perf_counter() - admitted) * 1000.0)
                    _record(errors, statuses, status)

            begin = time.perf_counter()
            await asyncio.gather(*[one(url) for url in urls])
            return time.perf_counter() - begin

    elapsed = asyncio.run(run())
    return {"mode": "async", "concurrency": concurrency, "requests": len(urls),
            "seconds": round(elapsed, 3),
            "qps": round(len(urls) / elapsed, 1) if elapsed else None,
            "errors": errors, "statuses": statuses,
            "latency": latency_report(service),
            "queue_latency": latency_report(queue)}

def control_group(base: str, count: int, concurrency: int,
                  path: str = "/small?size=1024") -> dict:
    """Server ceiling measured without the library under test.

    http.client over a keep-alive connection is the smallest possible client for
    this target, so its throughput is the number the library is compared
    against. Without it a plateau at 1,600 req/s cannot be attributed: the
    harness would be reporting the fixture own limit (LESSONS.md 19).
    """
    import http.client
    from concurrent.futures import ThreadPoolExecutor

    host, port = base.split("//", 1)[1].split(":")

    def worker(n: int) -> None:
        conn = http.client.HTTPConnection(host, int(port), timeout=30)
        try:
            for _ in range(n):
                conn.request("GET", path)
                response = conn.getresponse()
                response.read()
                if response.status != 200:
                    raise RuntimeError("HTTP %d" % response.status)
        finally:
            conn.close()

    per_worker = max(1, count // concurrency)
    begin = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(worker, [per_worker] * concurrency))
    elapsed = time.perf_counter() - begin
    total = per_worker * concurrency
    return {"mode": "control/http.client", "concurrency": concurrency,
            "requests": total, "seconds": round(elapsed, 3),
            "qps": round(total / elapsed, 1) if elapsed else None}


def load_chrome_client():
    """Imports the facade or explains which piece is missing, without a traceback."""
    try:
        import chrome_client
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            "chrome_client is not importable: %s\n"
            "Set LD_LIBRARY_PATH to the Core directory and PYTHONPATH to "
            "bindings/python, e.g.\n"
            "  LD_LIBRARY_PATH=core/binaries/linux-x86_64 "
            "PYTHONPATH=bindings/python python3 %s" % (exc, sys.argv[0]))
    return chrome_client
