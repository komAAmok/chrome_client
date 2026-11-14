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

sys.path.insert(0, str(ROOT / "tools"))

from python_exe import interpreter  # noqa: E402  (needs ROOT above)
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
    # bindings/python36 has zero #[test] functions, so `cargo test` on it compiles
    # the crate and reports success unconditionally: the step looked like a gate
    # and could not fail. What actually needs guarding is the artifact itself --
    # that the legacy extension still loads, still speaks the same ABI, and still
    # drives the shared facade -- so this runs the real check instead.
    ("bindings", "python36-artifact",
     ["python3", "tools/audit-python36-artifact.py"],
     "needs a built bindings/python36 extension and the linux-x86_64 Core"),
    ("bindings", "python-tests",
     ["python3", "-m", "unittest", "discover", "-s", "bindings/python/tests",
      "-p", "test_*.py"],
     "needs the linux-x86_64 Core on the build host"),
    # The four audit-* gates above are static: they compare the profile table
    # against the captures and the source, so they catch a table edit nobody
    # re-captured. wire-smoke is the only fingerprint check that talks to a real
    # server, which is the level ABI v8 cannot fake.
    # The request header block had no byte-level baseline at all: profile gates
    # compare captures and sources, and wire-smoke checks a real connection's
    # negotiation, but nothing compared the fields a request actually carries
    # across the cache options. Measured: cache_mode="bypass" adds
    # "Pragma: no-cache" and "Cache-Control: no-cache", cache_mode="validate"
    # adds "Cache-Control: max-age=0", and every other cache arm is byte-identical
    # to the default.
    ("fingerprint", "frozen-headers", ["python3", "tools/audit-frozen-headers.py"],
     "needs the linux-x86_64 Core on the build host"),
    # discard_cookies runs on its own engine, so its ClientHello carries no
    # pre_shared_key and its HTTP cache is empty. That is a stated property of
    # this implementation rather than a Chromium default, so it needs a baseline
    # of its own: otherwise a change back to a shared engine would look like an
    # improvement in the throughput and leak numbers and pass every other gate.
    ("fingerprint", "discard-fingerprint",
     ["python3", "tools/audit-discard-fingerprint.py"],
     "needs the linux-x86_64 Core and openssl on the build host"),
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
    # leak-probe compares two batches, which is what a release gate needs, but the
    # cyclic collector runs between those batches: a reference cycle is collected
    # and the growth disappears. Measured before the fix in docs/PERF_TEST_AND_LEAK.md
    # section 5: 200 create/request/close cycles leaked 401 descriptors with the
    # collector disabled and looked flat with it enabled. leak-hunt runs the same
    # shapes with a per-request slope, a descriptor-kind breakdown and one shape
    # (churn-nogc) that turns the collector off on purpose.
    ("leaks", "leak-hunt", ["python3", "tools/leak-hunt.py",
                            # 8 batches, not 6: _floor() spends one sample, and the
                            # late window must keep LATE_WINDOW=3 points or the
                            # slope carries no information (docs/LESSONS.md 28).
                            # 6 batches left two, and the gate reported a real
                            # per-request leak as flat.
                            "--requests", "20", "--batches", "8",
                            "--repeat", "2",
                            "--out", "{release}/leak-hunt.json"],
     "needs the linux-x86_64 Core on the build host"),
]

#: Steps that need the Core on the dynamic loader path.
NEEDS_CORE = {
    "rust-check", "rust-clippy", "rust-tests", "python36-artifact", "python-tests",
    "wire-smoke", "bench", "leak-probe", "leak-hunt",
    # Both new fingerprint steps load the Core through the Python facade: without
    # it they raise ImportError from _native.load(), which the runner records as
    # "fail" rather than "unavailable". A missing prerequisite must never read as
    # a failed assertion.
    "frozen-headers", "discard-fingerprint",
}


def release_tag():
    """The version under review, taken from the same place the crates read it."""
    try:
        done = subprocess.run([interpreter(), str(ROOT / "tools/version.py")],
                              cwd=ROOT, capture_output=True, text=True, timeout=60)
    except subprocess.SubprocessError:
        return "unknown"
    return done.stdout.strip() or "unknown"


def git(*args):
    """Runs one git command in the repository; empty string when git fails."""
    try:
        done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                              text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def release_directory(version):
    """The per-release evidence directory this run may write into.

    ``docs/review-<version>`` is the evidence for tag ``v<version>``: it is
    committed with the tag and every number in it describes that one commit.
    Naming the directory from the workspace version alone is not enough to
    keep that true, because the version does not change between commits --
    after v0.2.11 was tagged, the version stayed 0.2.11 while five commits
    landed, and a second review would have replaced the tagged evidence with
    numbers measured on a tree no tag points at. The JSON carries no field
    that would show the difference, so the directory has to.

    Three cases:

    * the directory does not exist -> this is a new release, use it (the normal
      flow: bump the version, review, commit the evidence, then tag);
    * it exists, HEAD is the commit that last wrote it, and the tree is clean
      -> a re-run on the same tree, reuse it;
    * anything else -> append the distance from the last tag and the dirty
      flag, so the run lands somewhere that names the tree it measured.

    Args:
        version: The workspace version, as ``release_tag`` read it.

    Returns:
        A repository-relative path.
    """
    base = "docs/review-%s" % version
    if not (ROOT / base).exists():
        return base
    owner = git("log", "-1", "--format=%H", "--", base)
    head = git("rev-parse", "HEAD")
    dirty = bool(git("status", "--porcelain"))
    if owner and owner == head and not dirty:
        return base
    # ``v0.2.11-5-g684c97f`` -> ``5.g684c97f``; a checkout with no tag at all
    # falls back to the short hash alone.
    described = git("describe", "--tags", "--long", "--always")
    suffix = "-".join(described.rsplit("-", 2)[1:]) if described.count("-") >= 2 \
        else described or "unknown"
    marker = base + "+" + suffix.replace("-g", ".")
    return marker + (".dirty" if dirty else "")


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


#: Used when the environment carries no PATH at all. Not a fallback for a
#: broken one: subprocess needs something, and these are the paths a step is
#: entitled to assume.
DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def prepend_path(existing, entry):
    """Puts *entry* first in a PATH-like variable, keeping what was there.

    Args:
        existing: The current value, which may be None (the environment can
            carry a key with a None value; os.environ.get() then returns None
            rather than the default).
        entry: The path to prepend.

    Returns:
        ``"<entry>:<existing>"``, or just ``"<entry>"`` when *existing* is
        empty or None. Never contains the literal "None".
    """
    if not existing:
        return str(entry)
    return "%s%s%s" % (entry, os.pathsep, existing)


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
    # setdefault() leaves an explicit None in place, and subprocess treats a
    # None PATH as "no PATH": every step then fails with FileNotFoundError on
    # "python3" even though the interpreter exists. Measured in this session,
    # where os.environ carries PATH=None. The same holding-true-for-None trap
    # applied to LD_LIBRARY_PATH and PYTHONPATH: prepending to them produced
    # "/core:None", which puts a directory named "None" on the loader path.
    env["PATH"] = env.get("PATH") or DEFAULT_PATH
    if CORE.is_dir():
        # Every runtime step loads the Core from here; a stale path would
        # measure a different library than the manifest describes.
        env["LD_LIBRARY_PATH"] = prepend_path(env.get("LD_LIBRARY_PATH"), CORE)
        env["PYTHONPATH"] = prepend_path(env.get("PYTHONPATH"),
                                         ROOT / "bindings/python")
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
        # Not simply the last line: Python flushes warnings (ResourceWarning,
        # "Task was destroyed but it is pending") to stderr at interpreter exit,
        # after the actual failure has been printed. Measured on a run that did
        # fail: the recorded reason was "ResourceWarning: unclosed <socket ...>"
        # and the real cause never reached the report, which makes a genuine
        # failure look like a warning and a warning look like the failure.
        #
        # Prefer a line that names a failure, then fall back to the tail.
        tail = (done.stderr or done.stdout).strip().splitlines()
        markers = ("FAILED", "ERROR:", "Traceback", "FAIL:", "error:", "fatal",
                   "panic", "not found", "No such file")
        named = [line for line in tail if any(m in line for m in markers)]
        if named:
            record["reason"] = named[0].strip()
            # Keep the tail too: the first marker says what broke, the last line
            # often says where.
            if tail and tail[-1].strip() != named[0].strip():
                record["reason_tail"] = tail[-1].strip()
        else:
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

    version = release_tag()
    context = {
        # One directory per release so two review runs cannot overwrite each
        # other's numbers, and so the numbers a tag was cut on stay attributable
        # to that tag. ``release_directory`` is what enforces the second half:
        # the version alone cannot, because it does not change between commits.
        "release": release_directory(version),
        # The path a step is actually given. It has to be absolute, because the
        # step runs from the repository root and the binary lives in a Chromium
        # tree that may be anywhere.
        "smoke_binary": str(smoke_binary()),
    }
    # The context that gets COMMITTED, with the one absolute path replaced by a
    # placeholder. AGENTS.md section 4 forbids a developer's home directory in
    # anything committed, and this file is committed: the 0.2.9 report carried
    # /home/<user>/chromium/... because no gate reads a JSON blob, which is
    # exactly how a rule survives in prose and dies in a data file. Two dicts
    # rather than one, because substituting the placeholder would break the
    # step that needs the real path.
    recorded_context = dict(context)
    recorded_context["smoke_binary"] = "$CHROMIUM_SRC/out/%s/%s" % (
        smoke_binary().parent.name, smoke_binary().name)
    report = {"environment": environment(), "groups": {}, "started": time.time(),
              "context": recorded_context}
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
    print("evidence: %s" % context["release"])
    print("report: %s (%d failures)" % (args.out, failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
