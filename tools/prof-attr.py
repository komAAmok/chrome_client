#!/usr/bin/env python3
"""Attributes a measured slowdown or stall to a layer, with evidence per layer.

The load matrix says a cell is slow. It cannot say where the time goes, and the
candidates live in three different places: the Python facade (GIL, cookie jar,
response wrapping), the Rust binding (Condvar waits, callback queues), and
libminicronet (one IO thread, a global thread pool, the socket pool). A tool that
only produces a flame graph answers for whichever layer was compiled with frame
pointers, so each subcommand below answers for one layer and says what it cannot
see.

    tools/prof-attr.py threads  --concurrency 16 --requests 200
    tools/prof-attr.py syscalls --shape small-1k --concurrency 16
    tools/prof-attr.py stacks   --concurrency 16 --seconds 20
    tools/prof-attr.py pyhot    --shape small-1k --concurrency 8
    tools/prof-attr.py perf

On this host /proc/sys/kernel/perf_event_paranoid is 4 and bpftrace refuses to
run as non-root, so the perf and eBPF paths report unavailable-with-reason
instead of an empty graph. An empty graph reads as "no hotspot", which is the one
wrong answer a profiling tool must not give (docs/LESSONS.md 23 and 25).
"""

from __future__ import annotations

import argparse
import cProfile
import io
import json
import os
import pstats
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import perf_harness as H
from python_exe import interpreter

THREAD_STATE = {"R": "running", "S": "sleeping", "D": "disk-wait",
                "Z": "zombie", "T": "stopped", "t": "tracing-stop",
                "I": "idle", "X": "dead"}


def thread_table(pid: int) -> list:
    """Per-thread name, scheduler state and kernel wait channel.

    wchan is the kernel function a thread is parked in. For this library the
    interesting values are futex (a Mutex/Condvar, i.e. the Rust layer) and
    epoll_wait or poll_schedule_timeout (the Chromium IO thread). A run where the
    network thread sits in futex while requests queue is a lock problem, not a
    socket problem, and no amount of socket tuning changes it.
    """
    rows = []
    task_dir = "/proc/%d/task" % pid
    try:
        tids = sorted(int(t) for t in os.listdir(task_dir))
    except (FileNotFoundError, ProcessLookupError):
        return rows
    for tid in tids:
        row = {"tid": tid}
        try:
            with open("%s/%d/comm" % (task_dir, tid), "r", encoding="utf-8",
                      errors="replace") as handle:
                row["name"] = handle.read().strip()
        except OSError:
            row["name"] = "?"
        try:
            with open("%s/%d/stat" % (task_dir, tid), "r", encoding="ascii") as handle:
                raw = handle.read()
            close = raw.rfind(")")
            fields = raw[close + 2:].split()
            row["state"] = THREAD_STATE.get(fields[0], fields[0])
            row["utime"] = int(fields[11])
            row["stime"] = int(fields[12])
        except (OSError, IndexError, ValueError):
            row["state"] = "?"
            row["utime"] = 0
            row["stime"] = 0
        try:
            with open("%s/%d/wchan" % (task_dir, tid), "r", encoding="ascii") as handle:
                row["wchan"] = handle.read().strip() or "-"
        except OSError:
            row["wchan"] = "-"
        rows.append(row)
    return rows


def cmd_threads(args) -> int:
    """Samples the thread table while a workload runs, then reports the delta.

    The delta matters more than the snapshot: a thread whose state is futex for
    the whole run while its cpu ticks stay flat is parked, not busy, and a thread
    that appears only under load is a per-request worker.
    """
    chrome_client = H.load_chrome_client()
    import threading as _threading
    with H.FixtureServer() as server:
        url = server.url("/small?size=1024")
        with chrome_client.Session() as session:
            session.get(url)
            baseline = {row["tid"]: row for row in thread_table(os.getpid())}
            stop = _threading.Event()
            peak = {}

            def sample():
                while not stop.is_set():
                    for row in thread_table(os.getpid()):
                        peak.setdefault(row["tid"], row)
                    stop.wait(0.02)

            sampler = _threading.Thread(target=sample, daemon=True)
            sampler.start()
            urls = ["%s&n=%d" % (url, i) for i in range(args.requests)]
            result = H.drive_sync(session, urls, args.concurrency)
            stop.set()
            sampler.join(timeout=2.0)
            after = {row["tid"]: row for row in thread_table(os.getpid())}

    print("requests=%d concurrency=%d qps=%s p50=%s" %
          (result["requests"], result["concurrency"], result["qps"],
           result["latency"].get("p50_ms")))
    print("%-7s %-24s %-12s %-24s %s" %
          ("tid", "name", "state", "wchan", "cpu_ticks"))
    for tid in sorted(set(after) | set(peak)):
        row = after.get(tid) or peak.get(tid) or {}
        before = baseline.get(tid, {})
        delta = ((row.get("utime", 0) + row.get("stime", 0))
                 - (before.get("utime", 0) + before.get("stime", 0)))
        print("%-7d %-24s %-12s %-24s %d" %
              (tid, row.get("name", "?")[:24], row.get("state", "?"),
               row.get("wchan", "-")[:24], delta))
    print()
    print("interpretation:")
    print("  futex + flat cpu   -> parked on a Mutex/Condvar (Rust binding layer)")
    print("  epoll_wait         -> the single Chromium IO thread idling between events")
    print("  poll_schedule      -> a socket read waiting on the fixture")
    print("  running + high cpu -> the layer that is actually saturated")
    if args.out:
        Path(args.out).write_text(json.dumps({"result": result}, indent=2),
                                  encoding="utf-8")
    return 0


def cmd_syscalls(args) -> int:
    """Counts syscalls of one workload with strace -c, following all threads.

    The ratio that identifies a wakeup problem is epoll_wait against read: a
    client that polls more often than it transfers has a notification problem,
    and body-copy tuning will not move it.
    """
    if not shutil.which("strace"):
        print("unavailable: strace not installed")
        return 2
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "load-matrix.py")
    # epoll_wait/poll/select are in the "desc" class, not "network"; naming them
    # directly is rejected by strace ("invalid system call 'epoll'"). The classes
    # below are the ones this stack actually uses.
    command = ["strace", "-f", "-c", "-e",
               "trace=network,desc,futex",
               interpreter(), script, "--mode", "sync",
               "--shapes", args.shape, "--ladder", str(args.concurrency),
               "--requests", str(args.requests), "--skip-faults",
               "--skip-control"]
    print("running: " + " ".join(command), file=sys.stderr)
    done = subprocess.run(command, capture_output=True, text=True)
    for line in (done.stderr or "").strip().splitlines()[-40:]:
        print(line)
    return 0 if done.returncode == 0 else 1


STACK_DRIVER = """import os, signal, sys, time
sys.path.insert(0, {tools_dir!r})
import perf_harness as H

concurrency, requests, seconds = int(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3])

chrome_client = H.load_chrome_client()
with H.FixtureServer() as server:
    url = server.url("/delay?ms=5&size=1024")
    with chrome_client.Session() as session:
        session.get(url)
        # The workload arms the timer only once it is mid-flight, so the stop
        # lands inside a request rather than during interpreter startup. SIGALRM
        # has no Python handler here, so gdb sees a genuine stop signal.
        signal.setitimer(signal.ITIMER_REAL, seconds)
        deadline = time.time() + seconds * 4
        while time.time() < deadline:
            H.drive_sync(session,
                         ["%s&n=%d" % (url, i) for i in range(requests)],
                         concurrency)
"""


def cmd_stacks(args) -> int:
    """Dumps every thread backtrace of a live workload with gdb.

    A child process is traceable by its own parent under yama ptrace_scope=1, so
    this works unprivileged when the same tool spawns the workload. The output is
    a poor man flame graph: a frame repeated across threads is the shared hot
    path, and a frame that appears only on the IO thread is that thread's own
    work.
    """
    if not shutil.which("gdb"):
        print("unavailable: gdb not installed")
        return 2
    # Written to the temp directory, not next to this file: it is generated on
    # every run and must never be committed (AGENTS.md 10 keeps generated
    # artifacts out of the tree).
    driver = os.path.join(tempfile.gettempdir(), "cc_stack_driver.py")
    Path(driver).write_text(
        STACK_DRIVER.replace("{tools_dir!r}",
                             repr(os.path.dirname(os.path.abspath(__file__)))),
        encoding="utf-8")
    # gdb has to be the PARENT for the attach to be permitted under
    # kernel.yama.ptrace_scope=1: an unprivileged process may trace its own
    # children, not a sibling it found by pgrep. gdb runs the workload itself and
    # interrupts it, which is the only form that works here without root.
    command = ["gdb", "-batch",
               "-ex", "set pagination off",
               "-ex", "handle SIGALRM stop print",
               "-ex", "handle SIGINT stop print",
               "-ex", "run",
               "-ex", "thread apply all bt 12",
               "--args", interpreter(), driver, str(args.concurrency),
               str(args.requests), str(args.seconds)]
    done = subprocess.run(command, capture_output=True, text=True, timeout=args.seconds + 120)
    text = done.stdout + done.stderr
    if "Could not attach" in text or "ptrace" in text:
        print("unavailable: the attach was refused (ptrace_scope or missing "
              "CAP_SYS_PTRACE). Run as root, or set "
              "/proc/sys/kernel/yama/ptrace_scope=0.")
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        return 2
    frames = re.findall(r"^#\d+\s+(?:0x[0-9a-f]+ in )?(\S+)",
                        text, re.MULTILINE)
    if not frames:
        print("unavailable: gdb produced no frames (the workload may have "
              "exited before the interrupt)")
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        return 2
    counts = {}
    for frame in frames:
        counts[frame] = counts.get(frame, 0) + 1
    print("%5s  %s" % ("count", "frame"))
    for frame, count in sorted(counts.items(), key=lambda kv: -kv[1])[:25]:
        print("%5d  %s" % (count, frame))
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0


def cmd_pyhot(args) -> int:
    """cProfile over one workload: the facade share of the wall clock.

    cProfile charges time to Python frames only, so a request whose 4 ms is spent
    below the binding appears as one 4 ms call to a single C function. That is
    the answer this subcommand exists to give: how much of the per-request cost
    is facade, and how much is underneath it.
    """
    chrome_client = H.load_chrome_client()
    profile = cProfile.Profile()
    with H.FixtureServer() as server:
        url = server.url("/small?size=1024")
        with chrome_client.Session() as session:
            session.get(url)
            urls = ["%s&n=%d" % (url, i) for i in range(args.requests)]
            profile.enable()
            result = H.drive_sync(session, urls, args.concurrency)
            profile.disable()
    buffer = io.StringIO()
    pstats.Stats(profile, stream=buffer).sort_stats("tottime").print_stats(25)
    text = buffer.getvalue()
    print(text[:6000])
    print("qps=%s p50=%s p99=%s" %
          (result["qps"], result["latency"].get("p50_ms"),
           result["latency"].get("p99_ms")))
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0


def cmd_perf(args) -> int:
    """Reports whether perf/eBPF can run here, and the exact command if they can.

    A profiling gate that silently produces an empty report is worse than no gate
    (docs/LESSONS.md 25): it turns "could not measure" into "nothing found". So
    this prints the paranoid level, decides, and exits 2 when measurement is
    impossible on this host.
    """
    paranoid = None
    try:
        paranoid = int(Path("/proc/sys/kernel/perf_event_paranoid").read_text().strip())
    except (OSError, ValueError):
        pass
    report = {
        "perf": shutil.which("perf"),
        "bpftrace": shutil.which("bpftrace"),
        "perf_event_paranoid": paranoid,
        "uid": os.getuid(),
        "perf_allowed": paranoid is not None and paranoid <= 1,
        "ebpf_allowed": os.getuid() == 0 and bool(shutil.which("bpftrace")),
    }
    print(json.dumps(report, indent=2))
    if not report["perf_allowed"]:
        print("unavailable here: perf_event_paranoid=%s forbids unprivileged"
              % paranoid)
        print("counters. On a host that allows it:")
        print("  perf record -F 999 -g --call-graph dwarf -p PID -- sleep 20")
        print("  perf report --stdio --sort comm,dso,symbol | head -40")
        print("  perf script | flamegraph.pl > flame.svg")
    if not report["ebpf_allowed"]:
        print("unavailable here: bpftrace needs root. On a host that has it:")
        print("  bpftrace -e 'tracepoint:syscalls:sys_enter_connect "
              "{ @[comm] = count(); }'")
        print("  bpftrace -e 'kprobe:tcp_retransmit_skb { @[comm] = count(); }'")
        print("  bpftrace -e 'profile:hz:99 /pid == PID/ { @[ustack] = count(); }'")
    return 0 if (report["perf_allowed"] or report["ebpf_allowed"]) else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--shape", default="small-1k")
        p.add_argument("--concurrency", type=int, default=16)
        p.add_argument("--requests", type=int, default=200)
        p.add_argument("--out")

    threads = sub.add_parser("threads", help="per-thread state and wchan")
    common(threads)
    threads.set_defaults(func=cmd_threads)

    syscalls = sub.add_parser("syscalls", help="strace -c of one workload")
    common(syscalls)
    syscalls.set_defaults(func=cmd_syscalls)

    stacks = sub.add_parser("stacks", help="gdb all-thread backtraces")
    common(stacks)
    stacks.add_argument("--seconds", type=float, default=20.0)
    stacks.set_defaults(func=cmd_stacks)

    pyhot = sub.add_parser("pyhot", help="cProfile of one workload")
    common(pyhot)
    pyhot.set_defaults(func=cmd_pyhot)

    perf = sub.add_parser("perf", help="perf/eBPF availability and commands")
    common(perf)
    perf.set_defaults(func=cmd_perf)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
