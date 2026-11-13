#!/usr/bin/env python3
"""Finds which request shape grows, by how much per request, and where.

tools/leak-probe.py answers "flat or not" over two batches for five shapes, which
is what a release gate needs. It cannot answer the next three questions, and they
are the ones that lead to a fix:

  1. Is the growth linear in request count? Two batches can differ because of a
     cache filling, not because of a leak. Five batches with a least-squares slope
     per request separates the two: a cache converges, a leak does not.
  2. Which descriptor grew? A count of open files says "eight more"; readlink on
     each says "eight more sockets" or "eight more /dev/shm segments", and those
     have nothing to do with each other.
  3. Which shape is it? A leak is per-path. The shape that was measured in the
     test suite passing says nothing about the shape nobody exercised.

    tools/leak-hunt.py --out docs/leak-hunt.json
    tools/leak-hunt.py --only sync-abandon --batches 8 --requests 40
    tools/leak-hunt.py --repro sync-abandon

Every shape runs in its own subprocess so a crash, an abort in the Chromium
destructor, or a finalizer that never returns is recorded as that shape's result
instead of taking the harness with it.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import perf_harness as H
from python_exe import interpreter

SHAPES = (
    "sync-get",        # get(), body dropped
    "sync-stream",     # get(stream=True) + iter_content fully consumed
    "sync-abandon",    # get(stream=True) then close() without reading
    "sync-error",      # every request refused, error construction path
    "sync-timeout",    # every request stalls past its deadline
    "async-gather",    # AsyncSession + asyncio.gather
    "async-abandon",   # async stream opened, closed without reading
    "session-churn",   # one Session per request: engine build/teardown
    "churn-nogc",      # same, with the cyclic collector disabled: exposes cycles
    "client-churn",    # Client (the Session alias) create/get/close
    "asyncclient-churn",  # AsyncClient create/get/close, the async teardown path
    "config-rotation", # one engine config per request inside one Session
    "discard-cookies", # discard_cookies=True: one throwaway engine per request
)

CHILD = textwrap.dedent('''
    import asyncio, gc, json, os, sys
    import chrome_client

    shape, url, count, batches = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
    if shape == "churn-nogc" or shape == "client-churn" or shape == "asyncclient-churn":
        # The cyclic collector is what hides a reference cycle: with it off, a
        # cycle becomes linear growth instead of a periodic step, and the slope
        # says how many objects each request strands.
        gc.disable()

    def rss_kib():
        with open("/proc/self/status") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
        return 0

    def threads():
        with open("/proc/self/status") as handle:
            for line in handle:
                if line.startswith("Threads:"):
                    return int(line.split()[1])
        return 0

    def fd_kinds():
        kinds = {}
        base = "/proc/self/fd"
        for entry in os.listdir(base):
            try:
                target = os.readlink(os.path.join(base, entry))
            except OSError:
                continue
            if target.startswith("socket:"):
                key = "socket"
            elif target.startswith("/dev/shm"):
                key = "dev-shm"
            elif target.startswith("/memfd"):
                key = "memfd"
            elif "eventpoll" in target:
                key = "eventpoll"
            elif "eventfd" in target:
                key = "eventfd"
            elif "anon_inode" in target:
                key = "anon-inode"
            elif target.startswith("/dev/"):
                key = "dev"
            elif target.startswith("/proc"):
                key = "proc"
            else:
                key = "file"
            kinds[key] = kinds.get(key, 0) + 1
        return kinds

    def snapshot():
        return {"rss_kib": rss_kib(), "fds": sum(fd_kinds().values()),
                "fd_kinds": fd_kinds(), "threads": threads()}

    def mk(index):
        return "%s%si=%d" % (url, "&" if "?" in url else "?", index)

    def batch():
        if shape == "sync-get":
            with chrome_client.Session() as session:
                for index in range(count):
                    session.get(mk(index))
        elif shape == "sync-stream":
            with chrome_client.Session() as session:
                for index in range(count):
                    response = session.get(mk(index), stream=True)
                    for _ in response.iter_content(65536):
                        pass
                    response.close()
        elif shape == "sync-abandon":
            with chrome_client.Session() as session:
                for index in range(count):
                    session.get(mk(index), stream=True).close()
        elif shape == "sync-error":
            with chrome_client.Session() as session:
                for _ in range(count):
                    try:
                        session.get("http://127.0.0.1:65000/", timeout=1)
                    except Exception:
                        pass
        elif shape == "sync-timeout":
            with chrome_client.Session() as session:
                for _ in range(count):
                    try:
                        session.get(mk(0).replace("/small", "/stall") + "&ms=3000",
                                    timeout=0.05)
                    except Exception:
                        pass
        elif shape == "async-gather":
            async def main():
                async with chrome_client.AsyncSession() as session:
                    await asyncio.gather(*[
                        session.get(mk(i)) for i in range(count)])
            asyncio.run(main())
        elif shape == "async-abandon":
            async def main():
                async with chrome_client.AsyncSession() as session:
                    for i in range(count):
                        response = await session.get(mk(i),
                                                     stream=True)
                        await response.aclose()
            asyncio.run(main())
        elif shape == "session-churn":
            for index in range(count):
                with chrome_client.Session() as session:
                    session.get(mk(index))
        elif shape == "churn-nogc":
            for index in range(count):
                session = chrome_client.Session()
                session.get(mk(index))
                session.close()
                del session
        elif shape == "client-churn":
            for index in range(count):
                client = chrome_client.Client()
                client.get(mk(index))
                client.close()
                del client
        elif shape == "asyncclient-churn":
            for index in range(count):
                async def one(target):
                    client = chrome_client.AsyncClient()
                    try:
                        await client.get(target)
                    finally:
                        client.close()
                asyncio.run(one(mk(index)))
        elif shape == "config-rotation":
            with chrome_client.Session(max_engines=1) as session:
                for index in range(count):
                    session.get(mk(index), verify=(index % 2 == 0))
        elif shape == "discard-cookies":
            with chrome_client.Session() as session:
                for index in range(count):
                    session.get(mk(index), discard_cookies=True)

    batch()
    samples = [snapshot()]
    for _ in range(batches):
        batch()
        samples.append(snapshot())
    print(json.dumps({"samples": samples}))
''')


def measure(shape, url, count, batches, python, timeout):
    try:
        done = subprocess.run([python, "-c", CHILD, shape, url, str(count),
                               str(batches)],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"shape": shape, "status": "hang",
                "reason": "no result within %ss" % timeout}
    if done.returncode != 0:
        tail = (done.stderr or done.stdout).strip().splitlines()
        return {"shape": shape, "status": "crash",
                "reason": tail[-1] if tail else "exit %d" % done.returncode}
    lines = done.stdout.strip().splitlines()
    if not lines:
        return {"shape": shape, "status": "crash", "reason": "no output"}
    samples = json.loads(lines[-1])["samples"]
    xs = [i * count for i in range(len(samples))]
    # The floor of each pair of consecutive samples, not the raw series. A leak
    # raises the floor; a batch whose sockets have not finished closing raises one
    # point. Measured: async-gather produced 9,9,9,9,9,9,15 -- a single spike at
    # the last sample, which a raw least-squares fit reads as 0.09 fds per request
    # and calls a leak. Taking the pairwise minimum removes exactly that artifact
    # and leaves a real accumulation untouched, because a leak cannot lower the
    # floor.
    fds = _floor([s["fds"] for s in samples])
    rss = _floor([s["rss_kib"] for s in samples])
    threads = _floor([s["threads"] for s in samples])
    xs = xs[:len(fds)]
    fd_slope = H.slope_per_unit(fds, xs)
    rss_slope = H.slope_per_unit(rss, xs)
    thread_slope = H.slope_per_unit(threads, xs)
    # A leak keeps growing for the whole run; a cache fills once and then stops.
    # The whole-run slope cannot tell those apart when the warmup is included, so
    # the verdict uses the slope of the second half, where a converging series is
    # already flat.
    half = max(2, len(samples) // 2)
    late_xs = xs[half:]
    late_fds, late_rss, late_threads = fds[half:], rss[half:], threads[half:]
    late_fd = H.slope_per_unit(late_fds, late_xs)
    late_rss_slope = H.slope_per_unit(late_rss, late_xs)
    late_thread_slope = H.slope_per_unit(late_threads, late_xs)

    kinds = {}
    for sample in samples:
        for name, value in sample["fd_kinds"].items():
            kinds.setdefault(name, []).append(value)
    kind_slopes = {name: H.slope_per_unit(values, xs) for name, values in kinds.items()}

    return {
        "shape": shape,
        "status": "ok",
        "requests_per_batch": count,
        "batches": batches,
        "fd_series": fds,
        "rss_kib_series": rss,
        "threads_series": threads,
        "fd_per_request": round(fd_slope, 6) if fd_slope is not None else None,
        "rss_kib_per_request": round(rss_slope, 4) if rss_slope is not None else None,
        "threads_per_request": (round(thread_slope, 6)
                                if thread_slope is not None else None),
        "fd_kinds_per_request": {name: round(value, 6)
                                 for name, value in kind_slopes.items()
                                 if value is not None},
        "fd_kinds_last": samples[-1]["fd_kinds"],
        "late": {
            "fd_per_request": round(late_fd, 6) if late_fd is not None else None,
            "rss_kib_per_request": (round(late_rss_slope, 4)
                                    if late_rss_slope is not None else None),
            "threads_per_request": (round(late_thread_slope, 6)
                                    if late_thread_slope is not None else None),
        },
        "late_abs": {
            "fd": (late_fds[-1] - late_fds[0]) if late_fds else None,
            "rss_kib": (late_rss[-1] - late_rss[0]) if late_rss else None,
            "threads": (late_threads[-1] - late_threads[0]) if late_threads else None,
        },
        "verdict": verdict(late_fd, late_rss_slope, late_thread_slope,
                           late_fds[-1] - late_fds[0] if late_fds else 0,
                           late_rss[-1] - late_rss[0] if late_rss else 0,
                           late_threads[-1] - late_threads[0] if late_threads else 0),
    }


def _floor(series):
    """Pairwise minimum: keeps a rising floor, drops one-sample spikes."""
    if len(series) < 2:
        return list(series)
    return [min(series[index], series[index + 1])
            for index in range(len(series) - 1)]


def verdict(fd_slope, rss_slope, thread_slope,
            fd_abs=0, rss_abs=0, thread_abs=0) -> str:
    """A cache converges, a leak does not: the late slope is the discriminator.

    The thresholds are per request and deliberately tight, because the question
    is not "did RSS move" (it always does, by an allocator arena) but "does it
    move again for every request after the warmup". 1 KiB per request is 1 MiB
    per thousand requests; 1e-3 fds per request is one descriptor per thousand.

    Both a slope and a late slope are reported. A shape whose whole-run slope is
    positive but whose late slope is zero is a cache filling up; that is the case
    this function is written to stop calling a leak.

    A slope alone still misfires on a short series: three samples that wiggle by
    one descriptor fit a line with a positive slope. So a signal needs both a
    slope above the threshold and an absolute change above a floor that a wiggle
    cannot reach. The floors are one connection's worth of descriptors and one
    allocator arena's worth of RSS.
    """
    signals = []
    if (fd_slope is not None and fd_slope > 1e-3) and fd_abs >= 3:
        signals.append("fd")
    if (rss_slope is not None and rss_slope > 1.0) and rss_abs >= 2048:
        signals.append("rss")
    if (thread_slope is not None and thread_slope > 1e-3) and thread_abs >= 2:
        signals.append("threads")
    return "grows:" + ",".join(signals) if signals else "flat"


REPRO = '''#!/usr/bin/env python3
"""Minimal reproducer for the {shape} growth, generated by tools/leak-hunt.py.

It does nothing but the one shape, {requests} times, and prints RSS, descriptor
count and descriptor kinds every {requests} requests. Run it, then:

  - watch the fd kind that grows (socket vs dev-shm vs anon-inode names the owner)
  - attach: gdb -p PID -batch -ex "thread apply all bt 12"
  - if RSS grows and fds do not: python3 -X tracemalloc=25 this_file.py

  LD_LIBRARY_PATH=core/binaries/linux-x86_64 PYTHONPATH=bindings/python \
      python3 this_file.py
"""
import os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import chrome_client

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        body = b"x" * 1024
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", "sid=v; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

_server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
threading.Thread(target=_server.serve_forever, daemon=True).start()
URL = "http://127.0.0.1:%d/small?size=1024" % _server.server_address[1]
COUNT = {requests}
BATCHES = {batches}

def rss_kib():
    with open("/proc/self/status") as handle:
        for line in handle:
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    return 0

def fd_kinds():
    kinds = {{}}
    base = "/proc/self/fd"
    for entry in os.listdir(base):
        try:
            target = os.readlink(os.path.join(base, entry))
        except OSError:
            continue
        key = ("socket" if target.startswith("socket:")
               else "dev-shm" if target.startswith("/dev/shm")
               else "memfd" if target.startswith("/memfd")
               else "anon-inode" if "anon_inode" in target
               else "file")
        kinds[key] = kinds.get(key, 0) + 1
    return kinds

def batch():
{body}

for round_index in range(BATCHES + 1):
    if round_index:
        batch()
    kinds = fd_kinds()
    print("batch=%d rss=%d KiB fds=%d threads=%d %s"
          % (round_index, rss_kib(), sum(kinds.values()),
             len(os.listdir("/proc/self/task")), kinds), flush=True)
'''

BODIES = {
    "sync-get": "    with chrome_client.Session() as session:\n"
                "        for index in range(COUNT):\n"
                "            session.get('%s?i=%d' % (URL, index))",
    "sync-stream": "    with chrome_client.Session() as session:\n"
                   "        for index in range(COUNT):\n"
                   "            response = session.get('%s?i=%d' % (URL, index), stream=True)\n"
                   "            for _ in response.iter_content(65536):\n"
                   "                pass\n"
                   "            response.close()",
    "sync-abandon": "    with chrome_client.Session() as session:\n"
                    "        for index in range(COUNT):\n"
                    "            session.get('%s?i=%d' % (URL, index), stream=True).close()",
    "async-gather": "    import asyncio\n"
                    "    async def main():\n"
                    "        async with chrome_client.AsyncSession() as session:\n"
                    "            await asyncio.gather(*[\n"
                    "                session.get('%s?i=%d' % (URL, i)) for i in range(COUNT)])\n"
                    "    asyncio.run(main())",
    "churn-nogc": "    import gc\n"
                  "    gc.disable()\n"
                  "    for index in range(COUNT):\n"
                  "        session = chrome_client.Session()\n"
                  "        session.get('%s?i=%d' % (URL, index))\n"
                  "        session.close()\n"
                  "        del session",
    "client-churn": "    import gc\n"
                     "    gc.disable()\n"
                     "    for index in range(COUNT):\n"
                     "        client = chrome_client.Client()\n"
                     "        client.get('%s?i=%d' % (URL, index))\n"
                     "        client.close()\n"
                     "        del client",
    "asyncclient-churn": "    import asyncio, gc\n"
                         "    gc.disable()\n"
                         "    for index in range(COUNT):\n"
                         "        async def one(target):\n"
                         "            client = chrome_client.AsyncClient()\n"
                         "            try:\n"
                         "                await client.get(target)\n"
                         "            finally:\n"
                         "                client.close()\n"
                         "        asyncio.run(one('%s?i=%d' % (URL, index)))",
    "config-rotation": "    with chrome_client.Session(max_engines=1) as session:\n"
                       "        for index in range(COUNT):\n"
                       "            session.get('%s?i=%d' % (URL, index), verify=(index % 2 == 0))",
    "session-churn": "    for index in range(COUNT):\n"
                     "        with chrome_client.Session() as session:\n"
                     "            session.get('%s?i=%d' % (URL, index))",
    "discard-cookies": "    with chrome_client.Session() as session:\n"
                       "        for index in range(COUNT):\n"
                       "            session.get('%s?i=%d' % (URL, index), discard_cookies=True)",
}


def write_repro(shape, url, count, batches, out_dir) -> Path:
    body = BODIES.get(shape, BODIES["sync-get"])
    text = REPRO.format(shape=shape, requests=count, batches=batches,
                        url=url, body=body)
    path = Path(out_dir) / ("repro_%s.py" % shape.replace("-", "_"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def merge_runs(shape, runs) -> dict:
    """Collapses repeated runs into one verdict that must reproduce.

    A descriptor count sampled right after a batch is not deterministic: sockets
    linger in the client's pool and in TIME_WAIT, so a single series can show a
    positive slope with no leak behind it. Measured: async-gather reported
    "grows:fd" in one run and a flat 9,9,9,9,9,9,9,9,9 in the next. Reporting the
    growth only when every run agrees keeps the tool from sending someone after a
    phantom, which is the failure mode docs/LESSONS.md 18 describes.
    """
    usable = [run for run in runs if run.get("status") == "ok"]
    if not usable:
        return runs[0] if runs else {"shape": shape, "status": "crash",
                                     "reason": "no run produced a result"}
    merged = dict(usable[0])
    # Every run is recorded, including the ones that failed: a shape whose second
    # run crashed is not "flat", and dropping the failed record would leave the
    # report claiming one clean run where there were two attempts.
    merged["runs"] = [{"verdict": run.get("verdict"),
                       "status": run.get("status"),
                       "reason": run.get("reason"),
                       "fd_per_request": run.get("fd_per_request"),
                       "rss_kib_per_request": run.get("rss_kib_per_request"),
                       "fd_series": run.get("fd_series"),
                       "late_abs": run.get("late_abs")} for run in runs]
    merged["run_count"] = len(runs)
    merged["ok_count"] = len(usable)
    verdicts = [run["verdict"] for run in usable]
    merged["verdict"] = verdicts[0] if len(set(verdicts)) == 1 else (
        "unstable:" + "/".join(verdicts))
    merged["reproduced"] = len(set(verdicts)) == 1 and len(usable) == len(runs)
    if len(usable) != len(runs):
        merged["unstable_reason"] = "; ".join(
            "%s: %s" % (run.get("status"), run.get("reason"))
            for run in runs if run.get("status") != "ok")
    return merged


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--only", nargs="*", default=list(SHAPES))
    parser.add_argument("--requests", type=int, default=40,
                        help="requests per batch")
    parser.add_argument("--batches", type=int, default=5,
                        help="batches after the warmup; 5 gives a usable slope")
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--repeat", type=int, default=2,
                        help="runs per shape; growth must reproduce in all of "
                             "them, because a one-off wiggle in a 3-sample series "
                             "fits a positive slope just as well as a leak")
    parser.add_argument("--repro", metavar="SHAPE",
                        help="write a standalone reproducer for one shape")
    parser.add_argument("--repro-dir", default="docs/perf")
    args = parser.parse_args()

    with H.FixtureServer() as server:
        url = server.url("/small?size=1024")
        if args.repro:
            path = write_repro(args.repro, url, args.requests, args.batches,
                               args.repro_dir)
            print("wrote %s" % path)
            return 0

        results = []
        failures = 0
        for shape in args.only:
            runs = [measure(shape, url, args.requests, args.batches,
                            interpreter(), args.timeout)
                    for _ in range(max(1, args.repeat))]
            record = merge_runs(shape, runs)
            results.append(record)
            if (record["status"] != "ok"
                    or not record["verdict"].startswith("flat")
                    or not record.get("reproduced", True)):
                failures += 1
            if record["status"] != "ok":
                print("%-16s %-8s %s" % (shape, record["status"],
                                         record.get("reason")), flush=True)
                continue
            late = record["late"]
            print("%-16s fd/req=%-9s rss/req=%-8s | late fd=%-9s rss=%-8s %s"
                  % (shape, record["fd_per_request"], record["rss_kib_per_request"],
                     late["fd_per_request"], late["rss_kib_per_request"],
                     record["verdict"]), flush=True)

    report = {"shapes": results, "requests_per_batch": args.requests,
              "batches": args.batches, "failures": failures}
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + chr(10),
                            encoding="utf-8")
    print("leak hunt: %d/%d shapes flat" % (len(results) - failures, len(results)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
