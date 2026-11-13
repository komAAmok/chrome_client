#!/usr/bin/env python3
"""Measures whether the Python binding leaks across repeated request batches.

`test_async_requests_do_not_leak` already compares two equal batches inside the
test suite. That check is about the async callback path and it runs one shape of
request; a release review needs the other shapes the binding offers, because a
leak is per-path and the batch that happens to be measured is the only one that
passes.

So five shapes run in sequence, each in its own subprocess so a crash or a hung
finalizer cannot be mistaken for a clean run:

  sync-get        get(), body discarded
  sync-stream     get(stream=True) + iter_content(), fully consumed
  sync-abandon    get(stream=True), close() without reading
  async-gather    AsyncSession, asyncio.gather over N requests
  error-path      every request to a refused port, so the error path allocates

Each shape reports RSS and open descriptors at three points: after a warmup
batch, after batch 1, after batch 2. A leak is linear in request count, so the
second batch must cost no more than the first plus a fixed tolerance -- the same
argument the in-suite test uses, applied to the paths it does not cover.

    LD_LIBRARY_PATH=core/binaries/linux-x86_64 PYTHONPATH=bindings/python \
        tools/leak-probe.py --out docs/leak-<version>.json

Exit code 0 when every shape is flat, 1 when any shape grows.
"""

import argparse
import json
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from python_exe import interpreter

#: Descriptor and RSS growth allowed between batch 1 and batch 2, per shape.
#: Both are absolute rather than proportional because the signal being looked
#: for is "grows with the loop", not "grew a little": Chromium keeps caches and
#: a pool, and the second batch legitimately costs a few pages of arena.
MAX_FD_GROWTH = 8
MAX_RSS_GROWTH_KIB = 24 * 1024

CHILD = textwrap.dedent("""
    import asyncio, json, os, sys
    import chrome_client

    shape, url, count = sys.argv[1], sys.argv[2], int(sys.argv[3])

    def rss_kib():
        with open("/proc/self/status") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
        return 0

    def fds():
        return len(os.listdir("/proc/self/fd"))

    def batch():
        if shape == "sync-get":
            with chrome_client.Session() as session:
                for index in range(count):
                    session.get("%s?i=%d" % (url, index))
        elif shape == "sync-stream":
            with chrome_client.Session() as session:
                for index in range(count):
                    response = session.get("%s?i=%d" % (url, index), stream=True)
                    for _ in response.iter_content(65536):
                        pass
                    response.close()
        elif shape == "sync-abandon":
            with chrome_client.Session() as session:
                for index in range(count):
                    session.get("%s?i=%d" % (url, index), stream=True).close()
        elif shape == "async-gather":
            async def main():
                async with chrome_client.AsyncSession() as session:
                    await asyncio.gather(*[
                        session.get("%s?i=%d" % (url, i)) for i in range(count)])
            asyncio.run(main())
        elif shape == "error-path":
            with chrome_client.Session() as session:
                for _ in range(count):
                    try:
                        session.get("http://127.0.0.1:1/", timeout=1)
                    except Exception:
                        pass

    batch()
    warm_fd, warm_rss = fds(), rss_kib()
    batch()
    first_fd, first_rss = fds(), rss_kib()
    batch()
    second_fd, second_rss = fds(), rss_kib()
    print(json.dumps({"fd": [warm_fd, first_fd, second_fd],
                      "rss_kib": [warm_rss, first_rss, second_rss]}), flush=True)
    # Flushed explicitly because this process has been observed to die during
    # interpreter teardown. With a block-buffered stdout the samples were lost and
    # the parent saw a signal with no output at all, which reads as "this shape
    # measured nothing" instead of "it measured fine and then crashed on exit".
    sys.stdout.flush()
    sys.stderr.flush()
""")

SHAPES = ("sync-get", "sync-stream", "sync-abandon", "async-gather", "error-path")


def measure(shape, url, count, python, timeout):
    done = subprocess.run([python, "-c", CHILD, shape, url, str(count)],
                          capture_output=True, text=True, timeout=timeout)
    if done.returncode != 0:
        tail = (done.stderr or done.stdout).strip().splitlines()
        # A child killed by a signal is not the same failure as a child that
        # raised: the samples may even be in stdout already, and the process
        # died during interpreter teardown after doing all its work. Naming the
        # signal keeps that visible instead of collapsing it into one stderr
        # line, which is what an abort looked like before: status "error" with a
        # blank reason, indistinguishable from a missing interpreter or a typo.
        if done.returncode < 0:
            signal_number = -done.returncode
            try:
                signal_name = signal.Signals(signal_number).name
            except ValueError:
                signal_name = "signal %d" % signal_number
            return {"shape": shape, "status": "error",
                    "signal": signal_name,
                    "reason": "child died with %s%s" % (
                        signal_name,
                        " after printing its samples" if done.stdout.strip() else ""),
                    "stdout_tail": done.stdout.strip().splitlines()[-1:] or None}
        return {"shape": shape, "status": "error",
                "reason": tail[-1] if tail else "child exited %d" % done.returncode}
    samples = json.loads(done.stdout.strip().splitlines()[-1])
    fd_growth = samples["fd"][2] - samples["fd"][1]
    rss_growth = samples["rss_kib"][2] - samples["rss_kib"][1]
    ok = fd_growth <= MAX_FD_GROWTH and rss_growth <= MAX_RSS_GROWTH_KIB
    return {"shape": shape, "status": "pass" if ok else "fail",
            "fd": samples["fd"], "rss_kib": samples["rss_kib"],
            "fd_growth": fd_growth, "rss_growth_kib": rss_growth,
            "limits": {"fd": MAX_FD_GROWTH, "rss_kib": MAX_RSS_GROWTH_KIB}}


def start_server():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        disable_nagle_algorithm = True

        def do_GET(self):
            body = b"x" * 1024
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d/" % server.server_address[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--requests", type=int, default=60)
    parser.add_argument("--only", nargs="*", default=list(SHAPES))
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()

    server, url = start_server()
    results = []
    failures = 0
    try:
        for shape in args.only:
            try:
                record = measure(shape, url, args.requests, interpreter(),
                                 args.timeout)
            except subprocess.TimeoutExpired:
                record = {"shape": shape, "status": "error",
                          "reason": "timed out after %ss" % args.timeout}
            results.append(record)
            if record["status"] != "pass":
                failures += 1
            detail = record.get("reason") or "fd %+d, rss %+d KiB" % (
                record["fd_growth"], record["rss_growth_kib"])
            print("%-6s %-14s %s" % (record["status"].upper(), shape, detail),
                  flush=True)
    finally:
        server.shutdown()

    report = {"shapes": results, "requests_per_batch": args.requests,
              "failures": failures}
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + chr(10),
                             encoding="utf-8")
    print("leak probe: %d/%d shapes flat" % (len(results) - failures, len(results)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
