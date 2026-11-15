#!/usr/bin/env python3
"""Enforces section 1.1: what the Core or Rust can do must not live in a binding.

The architecture diagram is an ownership table, not a suggestion. This gate makes
that table checkable by looking for the two shapes the rule forbids:

  1. A binding re-parsing bytes the Core already parsed. The Core hands out
     `HttpResponseHeaders::raw_headers()` and the facade splits it again, which
     means two implementations of the same grammar.

  2. A binding keeping a spec table (forbidden headers, hop-by-hop fields,
     status mappings) that the Core could enforce once for every language.

It cannot decide "should this move" by reading code, so it does not pretend to:
every violation that is knowingly still in place must be listed in KNOWN with a
reason and a review pointer, exactly as AGENTS section 1.1 requires. A new one
fails, and so does a stale entry that no longer matches anything -- otherwise the
list would silently describe a tree that no longer exists.

A pattern anchored at ``^NAME =`` DECLARES a table, and one file needs at most
one of those. The first version used ``re.search``, which reports one hit
however many times the name is bound, and the key is ``(path, pattern_index)``
-- so a second declaration of the same table in the same file collapsed onto
the first and the gate printed "passed". Declarations are counted now; call-site
patterns are not, because those legitimately repeat.

    python3 tools/audit-layer-ownership.py
    python3 tools/audit-layer-ownership.py --json
"""

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
BINDINGS = ROOT / "bindings"

# Patterns that indicate a binding is doing a layer's job. Each carries the rule
# it violates so the message says why rather than just what.
PATTERNS = (
    (r"def parse_raw_headers\(",
     "re-parses the Core's raw header block in a binding (section 1.1 rule 2)"),
    (r"_negotiated_http_version\(",
     "derives the negotiated protocol in a binding (section 1.1 rule 3)"),
    (r"http_version_from_status_line\(",
     "reads the protocol off the status line (section 1.1 rule 2)"),
    (r"reason_from_status_line\(",
     "re-derives the reason phrase from the status line (section 1.1 rule 2)"),
    (r"^_HOP_BY_HOP_HEADERS\s*=",
     "keeps a hop-by-hop header table in a binding (section 1.1 rule 4)"),
    (r"^_CORE_OWNED_HEADERS\s*=",
     "keeps a Core-owned header table in a binding (section 1.1 rule 4)"),
    (r"^_FORBIDDEN_WS_HEADERS\s*=",
     "keeps a WebSocket handshake header table in a binding (section 1.1 rule 4)"),
    (r"^_STATUS_[A-Z_]*\s*=\s*[{]",
     "keeps a status/reason mapping table in a binding (section 1.1 rule 4)"),
    (r"^_REASON_[A-Z_]*\s*=\s*[{]",
     "keeps a reason-phrase mapping table in a binding (section 1.1 rule 4)"),
)

# Every violation still in the tree. Each entry is (file, pattern index, reason).
# The reason must name what the Core cannot do yet and where to re-check it;
# "not migrated yet" is not a reason, it is the absence of one.
KNOWN = {
    ("bindings/python/chrome_client/_python_impl/models.pyi", 0): (
        "the stub declares the same helper the implementation still needs;",
        "both go when the response carries a structured header list."),
    ("bindings/python/chrome_client/_python_impl/models.pyi", 2): (
        "the stub declares the status-line fallback used on redirect hops."),
    ("bindings/python/chrome_client/_python_impl/models.pyi", 3): (
        "the stub declares the reason-phrase fallback; see the .py entry."),
    ("bindings/python/chrome_client/_python_impl/sessions.py", 1): (
        "the call site of the fallback described in models.py above: it passes",
        "the Core value when there is one, and _negotiated_http_version only",
        "for a hop the Core never saw."),
    ("bindings/python/chrome_client/_python_impl/sessions.py", 3): (
        "the call site that fills Response.reason; same ABI gap as models.py."),    ("bindings/python/chrome_client/_python_impl/models.py", 0): (
        "the response callback now carries mn_header_t fields, so this is only",
        "reached for a redirect hop: on_redirect still receives the raw block",
        "and nothing else. Re-check when mn_request_redirect_fn gains the field",
        "array (core/abi/minicronet.h, mn_request_redirect_fn)."),
    ("bindings/python/chrome_client/_python_impl/models.py", 1): (
        "kept only for the redirect hops and cache hits where no Core response",
        "exists to ask; the value itself now comes from",
        "mn_request_negotiated_protocol (sessions.py _finalize)."),
    ("bindings/python/chrome_client/_python_impl/models.py", 2): (
        "same fallback as above: only used when the Core answers UNKNOWN."),
    ("bindings/python/chrome_client/_python_impl/models.py", 3): (
        "on_response now passes the reason phrase, so this is only used for a",
        "redirect hop, which has the raw block but no parsed reason."),
    ("bindings/python/chrome_client/_python_impl/sessions.py", 4): (
        "the Core accepts every header name, so the refusal has to happen",
        "somewhere. Moving it needs mn_request_config to reject the seven names",
        "and return MN_ERROR_INVALID_ARGUMENT, before the loop that calls",
        "headers.SetHeader in Request::StartOnNetworkThread",
        "(core/source/request.cc:187-246).",
        "Scheduled as ABI 13: docs/NEXT_STEPS.md, the ABI 13 plan and the",
        "three-item header-validation proposal -- head-name rejection is",
        "item 1."),
    ("bindings/python/chrome_client/_python_impl/sessions.py", 5): (
        "SetExtraRequestHeaders only adds, so a caller-owned removal is a no-op",
        "the Core cannot report. Needs a Core-side per-name removal entry point",
        "alongside the MergeFrom that builds the merged set",
        "(core/source/request.cc:276-279). Scheduled as ABI 13, item 2."),
    ("bindings/python/chrome_client/_python_impl/sessions.py", 6): (
        "the WebSocket handshake builds its own header block in Chromium",
        "(websocket_basic_handshake_stream.cc), so the ABI never sees which",
        "names are reserved. Needs mn_websocket_config to reject them.",
        "Scheduled as ABI 13, item 3."),
}


def sources():
    """Yields (relative path, text) for every binding source file."""
    suffixes = {".py", ".pyi", ".go", ".ts", ".js", ".rs"}
    for path in sorted(BINDINGS.rglob("*")):
        if not path.is_file() or path.suffix not in suffixes:
            continue
        if "target" in path.parts or "node_modules" in path.parts:
            continue
        # A test is allowed to name a rule it is checking; the rule under audit is
        # the shipped binding, not its assertions.
        if path.name.startswith("test_") or "tests" in path.parts:
            continue
        try:
            yield str(path.relative_to(ROOT)), path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    found = {}
    duplicates = []
    for relative, text in sources():
        for index, (pattern, why) in enumerate(PATTERNS):
            hits = len(re.findall(pattern, text, re.MULTILINE))
            if not hits:
                continue
            found[(relative, index)] = why
            # A pattern anchored at ``^NAME =`` declares a table, and one file
            # needs exactly one. re.search reported a single hit however many
            # times the name was bound, and the key is (path, pattern_index),
            # so a second definition in the same file was invisible: measured
            # with two ``^_CORE_OWNED_HEADERS =`` blocks in one module, the
            # gate printed "passed". The call-site patterns are excluded --
            # those legitimately appear many times in one file.
            if pattern.startswith("^") and hits > 1:
                duplicates.append((relative, index, hits))

    failures = []
    for key, why in sorted(found.items()):
        if key not in KNOWN:
            failures.append(
                f"{key[0]}: {why}\n"
                "    not in KNOWN: either move it into the Core, or record here "
                "what the Core cannot do yet and where to re-check (AGENTS 1.1)")
    stale = [key for key in KNOWN if key not in found]
    for key in sorted(stale):
        failures.append(
            f"{key[0]}: KNOWN entry for pattern {key[1]} matches nothing now; "
            "the migration happened or the pattern rotted -- delete the entry")

    for relative, index, count in duplicates:
        failures.append(
            "%s: pattern %d declares its table %d times; one is the reviewed "

            "        site, the rest are unreviewed" % (relative, index, count))

    report = {"violations": len(found), "known": len(KNOWN), "stale": len(stale),
              "duplicate_definitions": len(duplicates)}
    if args.json:
        print(json.dumps(report, indent=1))
    if failures:
        print("layer ownership audit FAILED:", file=sys.stderr)
        for failure in failures:
            print("  " + failure, file=sys.stderr)
        return 1

    print(f"layer ownership audit passed: {len(found)} layer-crossing sites, all "
          f"recorded in KNOWN with the Core gap that blocks each one")
    return 0


if __name__ == "__main__":
    sys.exit(main())
