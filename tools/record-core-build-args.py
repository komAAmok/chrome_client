#!/usr/bin/env python3
"""Records what each Core artifact was built with, and what cannot be rebuilt.

AGENTS section 9 requires every artifact to reproduce its captures, and stage 0 of
docs/NEXT_STEPS.md records why that chain is broken today: the committed args.gn
cannot pass "gn gen", so only the build.ninja sitting in a developer out/
directory can produce an artifact. A manifest sha256 therefore proves that
somebody built this, not that the configuration in this repository built it.

This tool does not fix that. It makes the gap auditable:

  1. Recover the effective configuration from the build output, not from args.gn.
     Generated buildflag headers record every compile-time constant that actually
     reached the compiler, so they are evidence rather than intent.
  2. Record the per-target knobs that differ (target_cpu, target_os).
  3. Store it as core/build-args/<target>.snapshot.txt with a header saying it is
     a historical record, not a recipe.

--check re-derives the snapshot and fails if it drifted, so a rebuild that
changes the configuration cannot leave a stale snapshot behind.

Usage:
    python3 tools/record-core-build-args.py [--check]
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SNAPSHOT_DIR = ROOT / "core" / "build-args"
CHROMIUM = pathlib.Path(os.environ.get("CHROMIUM_SRC", "~/chromium/src")).expanduser()

#: Compile-time constants that decide what the artifact can do. Deliberately
#: short: these are the knobs a rebuild must preserve, not the thousands of
#: derived flags gn computes from them.
BUILDFLAG_HEADERS = (
    ("base/debug/debugging_buildflags.h", (
        "DCHECK_IS_CONFIGURABLE",
        "EXPENSIVE_DCHECKS_ARE_ON",
        "CAN_UNWIND_WITH_FRAME_POINTERS",
        "EXCLUDE_UNWIND_TABLES",
        "ENABLE_PROFILING",
    )),
    ("base/cfi_buildflags.h", (
        "CFI_CAST_CHECK",
        "CFI_ICALL_CHECK",
        "CFI_DIAG",
    )),
    # Two net knobs that are not debugging aids: they decide whether a caller can
    # use a quic:// proxy and a bracketed proxy chain at all, and the artifacts
    # this project ships deliberately override the upstream is_debug default
    # (tools/build-core-*.sh). Recording them makes a rebuild that quietly lost
    # the override fail here instead of at the first quic:// request.
    ("net/net_buildflags.h", (
        "ENABLE_QUIC_PROXY_SUPPORT",
        "ENABLE_BRACKETED_PROXY_URIS",
    )),
)

#: Per-target identity. The rest of args.gn is shared by all nine builds;
#: these two are the only ones that differ, so they are recorded explicitly
#: instead of being copied from one target to another.
def _build_dir_name(target):
    """Returns the gn output directory name for *target*.

    Kept beside TARGETS rather than derived, because the mapping is not a rule:
    Linux and macOS spell it out in full, Windows abbreviates the platform.
    tools/install-core-binaries.py holds the same table; if the two disagree the
    audit reads a directory the installer never wrote, and both stay quiet.

    Args:
        target: A key of TARGETS, e.g. "windows-x86_64".

    Returns:
        The directory name under $CHROMIUM_SRC/out, e.g. "MiniCronet-win-x86_64".
    """
    if target.startswith("windows-"):
        return "MiniCronet-win-" + target[len("windows-"):]
    return "MiniCronet-" + target

TARGETS = {
    "linux-x86": ("x86", "linux"),
    "linux-x86_64": ("x64", "linux"),
    "linux-armv7": ("arm", "linux"),
    "linux-arm64": ("arm64", "linux"),
    "windows-x86": ("x86", "win"),
    "windows-x86_64": ("x64", "win"),
    "windows-arm64": ("arm64", "win"),
    "macos-x86_64": ("x64", "mac"),
    "macos-arm64": ("arm64", "mac"),
}


def read_buildflags(out_dir: pathlib.Path):
    """Returns {name: value} for every recorded buildflag in *out_dir*.

    Args:
        out_dir: The gn output directory, e.g.
            "$CHROMIUM_SRC/out/MiniCronet-linux-x86_64".

    Returns:
        A mapping of flag name to the integer gn baked into the header. The
        headers are generated, so this reads what reached the compiler rather
        than what args.gn asked for.
    """
    gen = out_dir / "gen"
    found = {}
    for relative, names in BUILDFLAG_HEADERS:
        path = gen / relative
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for name in names:
            match = re.search(
                r"BUILDFLAG_INTERNAL_%s\(\)\s*\((\d+)\)" % re.escape(name),
                text)
            if match:
                found[name] = int(match.group(1))
    return found
def snapshot_text(target, flags):
    cpu, os_name = TARGETS[target]
    out = []
    out.append("# Core build configuration snapshot: " + target)
    out.append("#")
    out.append("# THIS IS A HISTORICAL RECORD, NOT A RECIPE.")
    out.append("#")
    out.append("# The committed args.gn cannot pass gn gen (docs/LESSONS.md #48,")
    out.append("# stage 0 in docs/NEXT_STEPS.md). This file records what the")
    out.append("# artifact was actually built with, recovered from the generated")
    out.append("# buildflag headers in the gn output directory.")
    out.append("#")
    out.append("# Reproduce with: python3 tools/record-core-build-args.py")
    out.append("")
    out.append("[target]")
    out.append("name = " + target)
    out.append("target_cpu = " + cpu)
    out.append("target_os = " + os_name)
    out.append("")
    out.append("[compile_flags]")
    for name in sorted(flags):
        out.append("%s = %d" % (name, flags[name]))
    return chr(10).join(out) + chr(10)


def main(argv):
    parser = argparse.ArgumentParser(description="Record Core build configurations.")
    parser.add_argument("--check", action="store_true",
                        help="re-derive and fail if a snapshot drifted")
    parser.add_argument("--chromium", default=str(CHROMIUM),
                        help="Chromium source root (default: $CHROMIUM_SRC)")
    args = parser.parse_args(argv)

    chromium = pathlib.Path(args.chromium).expanduser()
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    failures = []
    written = 0
    skipped = []

    for target in sorted(TARGETS):
        # The build directory is not "MiniCronet-<target>": Windows uses
        # "MiniCronet-win-<arch>", so the three Windows targets resolved to a
        # directory that never exists and were reported as "skipped" on every
        # run. A skip that is really a wrong path is indistinguishable from a
        # machine that never built them, which is how a gate keeps passing
        # while covering six of nine artifacts.
        out_dir = chromium / "out" / _build_dir_name(target)
        if not out_dir.is_dir():
            skipped.append(target)
            continue
        flags = read_buildflags(out_dir)
        if not flags:
            skipped.append(target)
            continue
        text = snapshot_text(target, flags)
        path = SNAPSHOT_DIR / (target + ".snapshot.txt")
        if args.check:
            if not path.is_file():
                failures.append("%s: no snapshot (run without --check)" % target)
                continue
            if path.read_text(encoding="utf-8") != text:
                failures.append("%s: snapshot drifted; the build output no longer "
                                "matches what is recorded" % target)
                continue
        else:
            path.write_text(text, encoding="utf-8")
            written += 1

    for failure in failures:
        print("record-core-build-args: " + failure, file=sys.stderr)
    if failures:
        return 1

    if args.check:
        print("Core build-args audit passed: %d snapshot(s) match the build output"
              % (len(TARGETS) - len(skipped)))
    else:
        print("Wrote %d snapshot(s) to core/build-args/" % written)
    if skipped:
        print("Skipped (no gn output directory): " + " ".join(skipped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))