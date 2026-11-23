#!/usr/bin/env python3
"""Verify a built wheel really embeds the Core of the version it claims.

Building successfully is not the same as being installable, and neither is the
same as being the wheel the tag describes. `AGENTS.md` section 13 says so, and
this project has already paid for it once: a wheel shipped unable to send a
single request because the extension imported but the Core version it linked
against disagreed with the manifest.

So a release wheel is checked on three independent facts:

  1. it exists, and its filename carries the release version;
  2. it contains the Core runtime for its platform (a wheel that only ships the
     Python files would import and then fail at the first request);
  3. the Core inside it announces the version the tag names -- read out of the
     wheel with `strings`, not out of the repository it was built from.

    tools/verify-release-wheel.py --dist dist --tag v0.2.15 \
        --core-target linux-x86_64
"""


import argparse
import pathlib
import re
import subprocess
import sys
import zipfile

#: Platform target -> the Core runtime file the wheel must carry.
RUNTIME_BY_TARGET = {
    "linux-x86_64": "libminicronet.so",
    "linux-x86": "libminicronet.so",
    "linux-arm64": "libminicronet.so",
    "linux-armv7": "libminicronet.so",
    "macos-arm64": "libminicronet.dylib",
    "macos-x86_64": "libminicronet.dylib",
    "windows-x86_64": "minicronet.dll",
    "windows-x86": "minicronet.dll",
    "windows-arm64": "minicronet.dll",
}

VERSION_RE = re.compile(rb"\b\d+\.\d+\.\d+\b")


def die(message):
    print(f"::error::{message}", file=sys.stderr)
    raise SystemExit(1)


def wheels_for(dist):
    found = sorted(dist.glob("*.whl"))
    if not found:
        die(f"no wheel under {dist}")
    return found


def version_from_tag(tag):
    if not tag.startswith("v"):
        die(f"tag {tag!r} does not start with v")
    return tag[1:]


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=pathlib.Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--core-target", required=True)
    parser.add_argument("--version", default=None,
                        help="expected release version; defaults to the tag without its v")
    args = parser.parse_args(argv)

    expected = args.version.lstrip("v") if args.version else version_from_tag(args.tag)
    runtime = RUNTIME_BY_TARGET.get(args.core_target)
    if runtime is None:
        die(f"unknown core target {args.core_target!r}")

    wheels = wheels_for(args.dist)
    for wheel in wheels:
        name = wheel.name
        # 1. the filename must name this release
        if f"-{expected}-" not in name:
            die(f"{name}: filename does not carry version {expected}")

        with zipfile.ZipFile(wheel) as archive:
            members = archive.namelist()
            # 2. the platform runtime must be inside.
            #
            # auditwheel renames it and moves it into <package>.libs/: the
            # shipped name is libminicronet-<8 hex>.so, not libminicronet.so.
            # Verified against the real chrome_client-0.2.6 wheel on PyPI,
            # which carries chrome_client.libs/libminicronet-b302fcc5.so.
            # Matching on the bare filename would pass a wheel that ships no
            # Core at all, so the stem is what gets matched.
            # 两个平台的打包器布局不同，实测：
            #   linux  auditwheel  -> chrome_client.libs/libminicronet-<8 hex>.so
            #   macos  delocate    -> chrome_client/.dylibs/libminicronet.dylib
            # 所以匹配的是「文件名以 stem 开头、以正确后缀结尾」，两种都覆盖。
            stem = runtime.rsplit(".", 1)[0]          # libminicronet / minicronet
            suffix = "." + runtime.rsplit(".", 1)[1]  # .so / .dylib / .dll
            runtime_members = [
                m for m in members
                if m.endswith(suffix)
                and "/" in m
                and m.rsplit("/", 1)[1].startswith(stem)
            ]
            if not runtime_members:
                die(f"{name}: no {stem}*{suffix} inside; the wheel would import "
                    f"and then fail on the first request. Members: "
                    f"{[m for m in members if m.endswith(suffix)][:5]}")

            # 3. the runtime must announce the expected version
            with archive.open(runtime_members[0]) as handle:
                blob = handle.read()

        # Core is stripped, but the version string is data, not a symbol.
        # Read it out of the shipped bytes rather than trusting the repository.
        try:
            text = subprocess.run(["strings", "-a", "-"], input=blob,
                                  capture_output=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError):
            text = blob
        found = {m.decode() for m in VERSION_RE.findall(text)}

        if expected not in found:
            die(f"{name}: {runtime} does not contain version {expected}; "
                f"found {sorted(found)[:6]}")
        print(f"ok {name}: {runtime} reports {expected}")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
