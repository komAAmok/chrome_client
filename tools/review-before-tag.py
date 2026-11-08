#!/usr/bin/env python3
"""Runs the full pre-tag review and writes one JSON report.

Cutting a tag is the only moment every claim in this repository is supposed to
be true at once, and the claims are checked by tools that nothing runs
together: the offline audits, a real request over h1/h2/h3, a concurrency
benchmark, a leak measurement, and the per-language bindings. Each of them
passed at some point; none of them was ever asked to pass on the same commit.

This script runs the whole set and writes one JSON document, so a release is a
record rather than a memory. It does not re-implement any check: every step
shells out to the tool that owns it, and a step that is unavailable on this
host is recorded as unavailable with the reason, never as a pass.

    tools/review-before-tag.py --out docs/release-review-<version>.json

Exit code is 0 only when every step that can run, passed. A step that cannot
run (no Chromium checkout, no network, no emulator) is reported as
"unavailable" with its reason and does not fail the run -- but a reader of the
JSON can count them, which is the point: silence is what let the ARMv7 row
claim a wheel it never had.

Groups, matching the four things a release must not regress:

  gates         the offline audits (ABI, README, stubs, core artifacts, profiles)
  fingerprint   h1/h2/h3 wire behaviour: padding, ALPS, H3 support, TLS range
  concurrency   sync/async throughput and the stalled-consumer isolation probe
  leaks         RSS and descriptor growth over repeated request batches
  bindings      the Rust, Python and python36 surfaces build and run
"""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core/binaries/linux-x86_64"

#: (group, name, command, why-unavailable) in the order a release needs them.
#: The fourth element is what the report says when the step cannot run here, so
#: "skipped" always comes with a reason instead of an empty field.
STEPS = [
    ("gates", "abi", ["tools/audit-abi.sh"], None),
    ("gates", "abi-layout", ["python3", "tools/audit-abi-layout.py"], None),
    ("gates", "readme", ["tools/audit-readme.sh"], None),
    ("gates", "pypi-metadata", ["tools/audit-pypi-metadata.sh"], None),
    ("gates", "python-stubs", ["python3", "tools/audit-python-stubs.py"], None),
    ("gates", "python-typing", ["python3", "tools/audit-python-typing.py"], None),
    ("gates", "extra-fp", ["python3", "tools/audit-extra-fp.py"], None),
    ("gates", "client-hints", ["python3", "tools/audit-client-hints.py"], None),
    ("gates", "core-binaries", ["tools/audit-core-binaries.sh"], None),
    ("gates", "core-inputs", ["tools/audit-core-inputs.sh"], None),
    ("gates", "core-size", ["python3", "tools/audit-core-size-ceilings.py"], None),
    ("gates", "core-payloads", ["python3", "tools/audit-core-payloads.py"], None),
    ("fingerprint", "padding-rule", ["python3", "tools/audit-padding-rule.py"], None),
    ("fingerprint", "tls-version-range", ["python3", "tools/audit-tls-version-range.py"], None),
    ("fingerprint", "h1-alps", ["python3", "tools/audit-h1-alps-absence.py"], None),
    ("fingerprint", "h3-support", ["python3", "tools/audit-h3-profile-support.py"], None),
    # Level 1 fetches each profile's own tag sources from googlesource: 72
    # profiles x 9 files is thousands of requests and takes far longer than the
    # rest of the review combined. AGENTS.md section 9 names this exact
    # invocation as the CI-scope gate, so the review runs the same one and the
    # newest profile -- the one a release is adding -- is what gets checked.
    # `--all` stays available for an occasional full sweep; that it did not run
    # is visible in the report instead of assumed.
    ("fingerprint", "profile-addition",
     ["python3", "tools/audit-profile-addition.py",
      "--levels", "1,2", "--profile", "chrome_157"],
     "needs the network (googlesource) and that profile's tag sources"),
    ("fingerprint", "profile-probes", ["tools/audit-profile-probes.sh"],
     "needs a Chromium checkout with gn/ninja"),
    ("fingerprint", "net-error-names", ["python3", "tools/audit-net-error-names.py"],
     "needs a Chromium checkout (CHROMIUM_SRC)"),
    ("bindings", "rust-format", ["cargo", "fmt", "--all", "--", "--check"], None),
    ("bindings", "rust-check", ["cargo", "check", "--workspace", "--all-targets"],
     "needs the linux-x86_64 Core on the build host"),
    ("bindings", "rust-clippy",
     ["cargo", "clippy", "--workspace", "--all-targets", "--", "-D", "warnings"],
     "needs the linux-x86_64 Core on the build host"),
    ("bindings", "rust-tests", ["cargo", "test", "--workspace", "--all-targets"],
     "needs the linux-x86_64 Core on the build host"),
    ("bindings", "python36-tests",
     ["cargo", "test", "--manifest-path", "bindings/python36/Cargo.toml",
      "--locked"],
     "needs the linux-x86_64 Core on the build host"),
    ("bindings", "python-tests",
     ["python3", "-m", "unittest", "discover", "-s", "bindings/python/tests",
      "-p", "test_*.py"],
     "needs the linux-x86_64 Core on the build host"),
    # The four audit-* gates above are static: they compare the profile table
    # against the captures and the source, so they catch a table edit nobody
    # re-captured. wire-smoke is the only fingerprint check that talks to a real
    # server, which is the level ABI v8 cannot fake.
    ("fingerprint", "wire-smoke", ["python3", "tools/run-http-smoke.py",
                                   "{smoke_binary}"],
     "needs the linux-x86_64 Core plus a built minicronet_smoke binary"),
    # Concurrency and leaks cannot be read off a source tree at all: they are
    # measured, and only on the host that runs the binding. Both write a JSON
    # record so a release has numbers to compare against, not just an exit code.
    ("concurrency", "bench", ["python3", "tools/bench-core-baseline.py",
                              "--out", "{release}/bench.json"],
     "needs the linux-x86_64 Core on the build host"),
    ("leaks", "leak-probe", ["python3", "tools/leak-probe.py",
                             "--out", "{release}/leak.json"],
     "needs the linux-x86_64 Core on the build host"),
]

#: Steps that need the Core on the dynamic loader path.
NEEDS_CORE = {
    "rust-check", "rust-clippy", "rust-tests", "python36-tests", "python-tests",
    "wire-smoke", "bench", "leak-probe",
}


def release_tag():
    """The version under review, taken from the same place the crates read it."""
    try:
        done = subprocess.run([sys.executable, str(ROOT / "tools/version.py")],
                              cwd=ROOT, capture_output=True, text=True, timeout=60)
    except subprocess.SubprocessError:
        return "unknown"
    return done.stdout.strip() or "unknown"


def smoke_binary():
    """Where a built minicronet_smoke would be, whether or not it exists.

    The binary is a product of the Chromium tree, not of this repository, so the
    path is derived from CHROMIUM_SRC with the same default the build scripts
    use. Returning the path even when it is absent lets the report say "no
    binary at <path>" rather than omitting the step.
    """
    root = os.environ.get("CHROMIUM_SRC") or str(ROOT.parent / "chromium" / "src")
    return Path(root) / "out/MiniCronet-linux-x86_64/minicronet_smoke"


def environment():
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpus": os.cpu_count(),
        "core": str(CORE.relative_to(ROOT)) if CORE.is_dir() else None,
    }


def substitute(argv, context):
    """Expands the {release} and {smoke_binary} placeholders in one step."""
    out = []
    for item in argv:
        try:
            out.append(item.format(**context))
        except (KeyError, IndexError):
            out.append(item)
    return out


def missing_prerequisite(argv, name, context):
    """Returns a reason when a step cannot run, or None when it can.

    The Core and the smoke binary are the two things a step here can need that a
    checkout does not contain: the Core is committed, the binary is a build
    product of a Chromium tree. Naming which one is missing is what keeps a
    skipped step distinguishable from a passing one.
    """
    if name in NEEDS_CORE and not CORE.is_dir():
        return "no Core at %s" % CORE
    smoke = context.get("smoke_binary")
    if smoke and smoke in argv and not Path(smoke).is_file():
        return ("no minicronet_smoke binary; build it with "
                "tools/build-core-linux-x86_64.sh")
    return None


def run(argv, name, unavailable_reason, context):
    """Runs one step; returns the record that goes into the report."""
    argv = substitute(argv, context)
    blocker = missing_prerequisite(argv, name, context)
    if blocker:
        return {"status": "unavailable", "reason": blocker}
    if shutil.which(argv[0]) is None:
        return {"status": "unavailable",
                "reason": unavailable_reason or "%s is not on PATH" % argv[0]}
    # The report writes into a per-release directory; a step that takes an --out
    # path cannot create it, so the directory has to exist before the step runs.
    release_dir = ROOT / context["release"]
    release_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    env = dict(os.environ)
    env.setdefault("PATH", os.environ.get("PATH", ""))
    if CORE.is_dir():
        # Every runtime step loads the Core from here; a stale path would
        # measure a different library than the manifest describes.
        env["LD_LIBRARY_PATH"] = "%s%s%s" % (
            CORE, os.pathsep, env.get("LD_LIBRARY_PATH", ""))
        env["PYTHONPATH"] = "%s%s%s" % (
            ROOT / "bindings/python", os.pathsep, env.get("PYTHONPATH", ""))
    try:
        done = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True,
                              text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        return {"status": "fail", "seconds": round(time.time() - started, 1),
                "reason": "timed out after 1800s"}
    record = {
        "status": "pass" if done.returncode == 0 else "fail",
        "seconds": round(time.time() - started, 1),
        "exit_code": done.returncode,
    }
    if done.returncode != 0:
        tail = (done.stderr or done.stdout).strip().splitlines()
        record["reason"] = tail[-1] if tail else "(no output)"
    else:
        tail = (done.stdout or "").strip().splitlines()
        record["summary"] = tail[-1] if tail else ""
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, type=Path,
                        help="where to write the JSON report")
    parser.add_argument("--only", nargs="*", default=None,
                        help="restrict to these step names")
    parser.add_argument("--bench-out", type=Path, default=None,
                        help="also write the concurrency baseline here")
    args = parser.parse_args()

    context = {
        # One directory per release so two review runs cannot overwrite each
        # other's numbers, and so the numbers a tag was cut on stay attributable
        # to that tag.
        "release": "docs/review-%s" % release_tag(),
        "smoke_binary": str(smoke_binary()),
    }
    report = {"environment": environment(), "groups": {}, "started": time.time(),
              "context": context}
    failures = 0
    for group, name, argv, reason in STEPS:
        if args.only and name not in args.only:
            continue
        record = run(argv, name, reason, context)
        report["groups"].setdefault(group, {})[name] = record
        tag = record["status"].upper()
        print("%-8s %-20s %s" % (tag, name, record.get("reason")
                                  or record.get("summary", "")), flush=True)
        if record["status"] == "fail":
            failures += 1

    report["seconds"] = round(time.time() - report.pop("started"), 1)
    report["failures"] = failures
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + chr(10),
                         encoding="utf-8")
    print("report: %s (%d failures)" % (args.out, failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
