#!/usr/bin/env python3
"""Generates the per-profile request header table from the captured evidence.

The header set Chrome sends is *evidence*, not a guess.  ``profiles/chrome_<major>/
h1/captures.json`` holds six independent captures of that release and all six
agree byte for byte; this script freezes the first one into
``bindings/python/chrome_client/_python_impl/client_hints.py``.

Two fields cannot be frozen verbatim, because a client must not claim a platform
the socket is not on (``core/source/minicronet.cc`` makes the same argument for
the User-Agent it derives):

* ``User-Agent`` carries the platform token inside its parenthesised group.
* ``sec-ch-ua-platform`` names the platform outright.

Both are emitted with the captured (Windows) value and rewritten at runtime.
``tools/audit-client-hints.py`` re-derives the whole table from the captures and
fails on any drift, so the committed file can never quietly disagree with the
evidence.

Usage::

    python3 tools/generate-client-hints.py           # rewrite if stale
    python3 tools/generate-client-hints.py --check   # exit 1 if stale
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
PROFILES = REPO / "profiles"
TARGET = (REPO / "bindings" / "python" / "chrome_client" / "_python_impl"
          / "client_hints.py")

#: Headers Chromium emits on its own.  ``Host`` and ``Connection`` are transport
#: level and never belong in a request extra-header list; ``User-Agent`` and
#: ``Accept-Encoding`` stay, because their *position* is part of the capture and
#: the runtime re-emits them at that position.
TRANSPORT_HEADERS = ("Host", "Connection")

#: The generated module docstring, one entry per line.  Written out rather than
#: embedded as one literal so an apostrophe in the prose cannot break the file.
GENERATED_DOCSTRING = (
    "Per-profile request headers, generated from the captured evidence.",
    "",
    "Do not edit by hand: run tools/generate-client-hints.py.  Each entry is the",
    "exact ordered header list that release sent, minus Host and Connection",
    "(transport level, never extra headers).",
    "",
    "User-Agent and sec-ch-ua-platform are stored as captured -- a Windows",
    "desktop -- and rewritten at runtime to the host platform.  A client may not",
    "claim a platform the socket is not on, which is the same rule",
    "core/source/minicronet.cc applies to the User-Agent it derives.",
)


def captures(major):
    """Returns every captured header block for one profile, in file order.

    Args:
        major: The Chrome major version.

    Returns:
        A list of header-line lists, one per capture.
    """
    path = PROFILES / ("chrome_%d" % major) / "h1" / "captures.json"
    blocks = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        first = json.loads(line)
        block = first.get("http1")
        headers = block.get("headers") if isinstance(block, dict) else block
        blocks.append(list(headers or []))
    return blocks


def frozen_headers(major):
    """Returns the ordered (name, value) pairs this profile must send.

    Args:
        major: The Chrome major version.

    Returns:
        A list of (name, value) tuples in capture order.

    Raises:
        SystemExit: The six captures disagree, or fewer than six exist.
    """
    blocks = captures(major)
    if len(blocks) != 6:
        raise SystemExit("chrome_%d: %d captures, 6 required" % (major, len(blocks)))
    distinct = {tuple(block) for block in blocks}
    if len(distinct) != 1:
        raise SystemExit(
            "chrome_%d: the six captures disagree (%d distinct header blocks); "
            "the evidence is not stable enough to freeze" % (major, len(distinct)))
    pairs = []
    for line in blocks[0]:
        name, _, value = line.partition(":")
        name, value = name.strip(), value.strip()
        if name in TRANSPORT_HEADERS:
            continue
        pairs.append((name, value))
    return pairs


def majors():
    """Returns every pinned major that has h1 capture evidence.

    Returns:
        A sorted list of major numbers.
    """
    found = []
    for directory in PROFILES.glob("chrome_*"):
        suffix = directory.name.split("_", 1)[1]
        if suffix.isdigit() and (directory / "h1" / "captures.json").exists():
            found.append(int(suffix))
    return sorted(found)


def render():
    """Returns the full text of the generated module.

    Returns:
        The module source as a string.
    """
    out = ["\"\"\""]
    out.extend(GENERATED_DOCSTRING)
    out.append("\"\"\"")
    out.append("")
    out.append("#: Ordered header list per pinned profile, as captured.")
    out.append("CAPTURED_HEADERS = {")
    for major in majors():
        out.append("    %d: (" % major)
        for name, value in frozen_headers(major):
            out.append("        (%r, %r)," % (name, value))
        out.append("    ),")
    out.append("}")
    out.append("")
    return "\n".join(out)


def main():
    """Writes the generated module, or checks it when --check is given.

    Returns:
        Process exit status: 0 when current, 1 when stale and checking.
    """
    parser = argparse.ArgumentParser(description="Generate the client hint table")
    parser.add_argument("--check", action="store_true",
                        help="exit 1 if the generated file is stale")
    args = parser.parse_args()

    expected = render()
    current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
    if current == expected:
        print("client hints are up to date (%d profiles)" % len(majors()))
        return 0
    if args.check:
        print("stale: %s" % TARGET.relative_to(REPO), file=sys.stderr)
        print("run: python3 tools/generate-client-hints.py", file=sys.stderr)
        return 1
    TARGET.write_text(expected, encoding="utf-8")
    print("rewrote %s (%d profiles)" % (TARGET.relative_to(REPO), len(majors())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
