#!/usr/bin/env python3
"""Diagnostics for the synchronous (blocking) client: thread dump, lock waiters, pool curve.

The three questions a synchronous client raises are "who is the thread waiting on",
"how many threads are parked on a lock", and "is the connection pool the ceiling".
tools/prof-attr.py answers the first two at the process level; this one answers
them per request path and adds the pool curve, which needs the server view.

    tools/sync-diag.py dump        --threads 8
    tools/sync-diag.py locks       --threads 16 --requests 200
    tools/sync-diag.py pool-curve  --ladder 1,2,4,6,8,12,16

Every number here is measured against tools/perf_harness.FixtureServer, so the
"pool ceiling" is the server count of concurrent requests and not the client
own idea of how many it has open. A client cannot witness its own ceiling.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import perf_harness as H


def gauge(base, reset=False):
    url = base + "/inflight" + ("?reset=1" if reset else "")
    try:
        with urllib.request.urlopen(url, timeout=5) as handle:
            return json.loads(handle.read())
    except Exception:
        return {}


def drain(base, timeout=10.0):
    """Waits until the fixture has no request in flight, then resets its peak."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if gauge(base).get("now") == 0:
            break
        time.sleep(0.02)
    return gauge(base, reset=True)


def cmd_dump(args) -> int:
    """Dumps every thread Python stack while a workload runs.

    This is the jstack equivalent for this stack. Two things it shows that a
    profiler does not: a thread parked inside a blocking native call (its Python
    frame is still on the stack, so file:line names the call site), and a thread
    parked on a lock the facade owns (_cookie_lock, EngineCache._lock).
    """
    chrome_client = H.load_chrome_client()
    stop = threading.Event()
    dumps = []

    with H.FixtureServer() as server:
        url = server.url("/delay?ms=10&size=1024")
        session = chrome_client.Session()
        session.get(url)

        def worker(index):
            while not stop.is_set():
                try:
                    session.get("%s&n=%d" % (url, index), timeout=10)
                except Exception:
                    return

        pool = [threading.Thread(target=worker, args=(i,), daemon=True)
                for i in range(args.threads)]
        for thread in pool:
            thread.start()

        for _ in range(args.samples):
            time.sleep(args.interval)
            frames = sys._current_frames()
            dump = {}
            for thread in threading.enumerate():
                frame = frames.get(thread.ident)
                if frame is None:
                    continue
                chain = []
                walker = frame
                for _ in range(7):
                    if walker is None:
                        break
                    chain.append("%s:%d in %s"
                                 % (walker.f_code.co_filename.split("/")[-1],
                                    walker.f_lineno, walker.f_code.co_name))
                    walker = walker.f_back
                dump[thread.name] = chain
            dumps.append(dump)

        stop.set()
        for thread in pool:
            thread.join(timeout=10)
        session.close()

    tally = {}
    for dump in dumps:
        for chain in dump.values():
            if chain:
                tally[chain[0]] = tally.get(chain[0], 0) + 1
    print("%-6s %s" % ("count", "top frame (file:line in function)"))
    for frame, count in sorted(tally.items(), key=lambda kv: -kv[1])[:20]:
        print("%-6d %s" % (count, frame))
    print()
    print("reading this:")
    print("  a frame in sessions.py around await_response / start_stream is a")
    print("  thread blocked in the Core -- normal for a synchronous client.")
    print("  a frame in sessions.py or engine.py with _cookie_lock / _lock")
    print("  above it is a thread parked on a facade lock, which is the one")
    print("  kind of wait that more threads makes worse.")
    if args.out:
        Path(args.out).write_text(json.dumps(dumps, indent=2), encoding="utf-8")
    return 0


class _TimedRLock(object):
    """A RLock wrapper that records how long each acquisition waited."""

    def __init__(self, sink):
        self._inner = threading.RLock()
        self._sink = sink

    def acquire(self, *a, **k):
        begin = time.perf_counter()
        got = self._inner.acquire(*a, **k)
        self._sink.append((time.perf_counter() - begin) * 1000.0)
        return got

    def release(self):
        return self._inner.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def cmd_locks(args) -> int:
    """Measures how long threads wait on the facade own locks.

    The locks are wrapped rather than sampled: a sampling profiler can miss a
    short critical section entirely, and the question here is cumulative wait,
    not instantaneous state. Only the facade locks are instrumented -- the Core
    internal ones are not reachable from Python, and pretending otherwise would
    report zero for the layer that actually matters.
    """
    chrome_client = H.load_chrome_client()
    import chrome_client._python_impl.engine as engine_mod
    import chrome_client._python_impl.sessions as sessions_mod

    stats = {"cookie_lock": [], "engine_lock": []}
    original_session_init = sessions_mod.BaseSession.__init__
    original_cache_init = engine_mod.EngineCache.__init__

    def patched_init(self, *a, **k):
        original_session_init(self, *a, **k)
        self._cookie_lock = _TimedRLock(stats["cookie_lock"])

    def patched_cache_init(self, *a, **k):
        original_cache_init(self, *a, **k)
        self._lock = _TimedRLock(stats["engine_lock"])

    sessions_mod.BaseSession.__init__ = patched_init
    engine_mod.EngineCache.__init__ = patched_cache_init
    elapsed = 0.0
    try:
        with H.FixtureServer() as server:
            url = server.url("/small?size=1024")
            with chrome_client.Session() as session:
                session.get(url)
                work = iter(["%s&n=%d" % (url, i)
                             for i in range(args.requests)])
                lock = threading.Lock()

                def worker():
                    while True:
                        with lock:
                            try:
                                target = next(work)
                            except StopIteration:
                                return
                        try:
                            session.get(target, timeout=15)
                        except Exception:
                            pass

                pool = [threading.Thread(target=worker)
                        for _ in range(args.threads)]
                begin = time.perf_counter()
                for thread in pool:
                    thread.start()
                for thread in pool:
                    thread.join(120)
                elapsed = time.perf_counter() - begin
    finally:
        sessions_mod.BaseSession.__init__ = original_session_init
        engine_mod.EngineCache.__init__ = original_cache_init

    print("threads=%d requests=%d wall=%.2fs"
          % (args.threads, args.requests, elapsed))
    budget = elapsed * args.threads * 1000.0
    for name, waits in stats.items():
        report = H.latency_report(waits)
        total = sum(waits)
        ratio = (total / budget) if budget else 0.0
        print("  %-12s calls=%-7d total_wait=%-9.1f ms p50=%-7.3f p99=%-7.3f"
              " share_of_thread_time=%.3f"
              % (name, report.get("count") or 0, total,
                 report.get("p50_ms") or 0.0, report.get("p99_ms") or 0.0,
                 ratio))
    print()
    print("reading this: share_of_thread_time is total wait divided by wall times")
    print("threads. Near 1 means every request queues on the lock for its whole")
    print("duration; near 0 means the lock is effectively free.")
    return 0


def cmd_pool_curve(args) -> int:
    """Server-observed concurrency per thread step: the pool ceiling curve."""
    chrome_client = H.load_chrome_client()
    ladder = [int(step) for step in args.ladder.split(",") if step.strip()]
    rows = []
    with H.FixtureServer() as server:
        url = server.url("/delay?ms=40&size=256")
        for threads in ladder:
            drain(server.base)
            targets = ["%s&n=%d" % (url, i) for i in range(threads * 2)]
            work = iter(targets)
            lock = threading.Lock()

            def worker():
                while True:
                    with lock:
                        try:
                            target = next(work)
                        except StopIteration:
                            return
                    try:
                        session.get(target, timeout=20)
                    except Exception:
                        pass

            with chrome_client.Session() as session:
                session.get(url)
                pool = [threading.Thread(target=worker) for _ in range(threads)]
                begin = time.perf_counter()
                for thread in pool:
                    thread.start()
                for thread in pool:
                    thread.join(120)
                elapsed = time.perf_counter() - begin
            state = gauge(server.base)
            rows.append({"threads": threads, "server_peak": state.get("peak"),
                         "qps": round(len(targets) / elapsed, 1)})
            print("  threads=%-3d server_peak=%-3d qps=%-7.1f"
                  % (threads, state.get("peak"), len(targets) / elapsed))

    ceiling = max((row["server_peak"] or 0) for row in rows)
    print()
    print("ceiling observed: %s concurrent requests" % ceiling)
    print("if it stops rising while threads keep doubling, the pool is the limit,")
    print("not the scheduler: the thread that cannot get a socket is parked in the")
    print("Core, not spinning.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    dump = sub.add_parser("dump", help="per-thread Python stack sampling")
    dump.add_argument("--threads", type=int, default=8)
    dump.add_argument("--samples", type=int, default=6)
    dump.add_argument("--interval", type=float, default=0.4)
    dump.add_argument("--out")
    dump.set_defaults(func=cmd_dump)

    locks = sub.add_parser("locks", help="facade lock wait measurement")
    locks.add_argument("--threads", type=int, default=8)
    locks.add_argument("--requests", type=int, default=200)
    locks.set_defaults(func=cmd_locks)

    pool = sub.add_parser("pool-curve", help="server-observed pool ceiling")
    pool.add_argument("--ladder", default="1,2,4,6,8,12,16")
    pool.set_defaults(func=cmd_pool_curve)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
