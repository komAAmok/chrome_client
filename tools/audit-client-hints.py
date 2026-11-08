#!/usr/bin/env python3
"""Audits the client-hint table against the captured evidence.

``bindings/python/chrome_client/_python_impl/client_hints.py`` is generated, and
a generated file that nothing checks is a file that drifts.  This script
re-derives the whole table from ``profiles/chrome_<major>/h1/captures.json`` and
fails when the two disagree, when a profile lost its evidence, or when the six
captures of one profile stopped agreeing with each other.

It also checks the two properties the runtime depends on but cannot verify at
import time:

* every captured ``User-Agent`` carries the same version string the Core derives
  (``core/source/minicronet.cc``), so a profile cannot announce one version over
  TLS and another in its headers;
* ``sec-ch-ua-platform`` is present exactly for the majors whose capture has it,
  so the runtime never invents a hint a release did not send.

Usage::

    python3 tools/audit-client-hints.py [--verbose]
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import importlib.util

REPO = pathlib.Path(__file__).resolve().parents[1]
PROFILES = REPO / "profiles"
GENERATED = (REPO / "bindings" / "python" / "chrome_client" / "_python_impl"
             / "client_hints.py")
RUNTIME = (REPO / "bindings" / "python" / "chrome_client" / "_python_impl"
           / "browser_headers.py")
CORE = REPO / "core" / "source" / "minicronet.cc"

errors: list[str] = []


def load_tool():
    """Imports the generator, so "expected" has one definition.

    Returns:
        The ``generate-client-hints`` module.
    """
    path = REPO / "tools" / "generate-client-hints.py"
    spec = importlib.util.spec_from_file_location("generate_client_hints", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def committed_table():
    """Returns the table as committed, read with ``ast`` so nothing is imported.

    Returns:
        A dict of major to list of (name, value) pairs.
    """
    tree = ast.parse(GENERATED.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "CAPTURED_HEADERS":
            return ast.literal_eval(node.value)
    errors.append("client_hints.py: CAPTURED_HEADERS is not defined")
    return {}


def core_full_versions():
    """Returns the pre-reduction version strings the Core carries.

    Returns:
        A list of version strings, indexed by ``major - 86``.
    """
    text = CORE.read_text(encoding="utf-8")
    block = text[text.index("kFullVersions"):]
    block = block[block.index("{"):block.index("};")]
    return re.findall(r'"([0-9.]+)"', block)


def check_table(verbose):
    """The committed table must equal what the captures say.

    Args:
        verbose: Print per-profile counts.
    """
    tool = load_tool()
    committed = committed_table()
    expected_majors = tool.majors()
    if not expected_majors:
        errors.append("no profile has h1 capture evidence; the audit would pass vacuously")
        return
    missing = [m for m in expected_majors if m not in committed]
    extra = [m for m in committed if m not in expected_majors]
    if missing:
        errors.append("client_hints.py is missing profile(s): %s" % missing)
    if extra:
        errors.append("client_hints.py has profile(s) with no evidence: %s" % extra)

    for major in expected_majors:
        if major not in committed:
            continue
        want = tool.frozen_headers(major)
        got = [tuple(pair) for pair in committed[major]]
        if got != want:
            first = next((i for i, (g, w) in enumerate(zip(got, want)) if g != w), None)
            detail = ("at index %d: %r vs %r" % (first, got[first], want[first])
                      if first is not None else
                      "%d pairs committed, %d captured" % (len(got), len(want)))
            errors.append("chrome_%d: committed headers differ from the capture (%s)"
                          % (major, detail))
    if verbose:
        print("  table: %d profiles re-derived from the captures" % len(expected_majors))


def check_user_agent_versions(verbose):
    """Every captured UA must carry the version the Core derives.

    Args:
        verbose: Print the derived count.
    """
    committed = committed_table()
    full = core_full_versions()
    if not full:
        errors.append("could not read kFullVersions from core/source/minicronet.cc")
        return
    checked = 0
    for major, pairs in sorted(committed.items()):
        ua = dict(pairs).get("User-Agent")
        if ua is None:
            errors.append("chrome_%d: the captured set has no User-Agent" % major)
            continue
        version = ua.rsplit("Chrome/", 1)[1].split(" ", 1)[0]
        if major < 105:
            index = major - 86
            if not 0 <= index < len(full):
                errors.append("chrome_%d: no kFullVersions entry for this major" % major)
                continue
            expected = full[index]
        else:
            expected = "%d.0.0.0" % major
        if version != expected:
            errors.append(
                "chrome_%d: captured User-Agent says %s, the Core derives %s"
                % (major, version, expected))
        checked += 1
    if verbose:
        print("  user-agent versions: %d checked against kFullVersions" % checked)


def check_platform_hint(verbose):
    """The platform hint must be present exactly where a capture had it.

    Args:
        verbose: Print how many profiles carry the hint.
    """
    committed = committed_table()
    with_hint = sorted(m for m, pairs in committed.items()
                       if any(name == "sec-ch-ua-platform" for name, _ in pairs))
    if not with_hint:
        errors.append("no profile carries sec-ch-ua-platform; the audit would pass vacuously")
        return
    gaps = [m for m in range(min(with_hint), max(with_hint) + 1) if m not in with_hint]
    if gaps:
        errors.append(
            "sec-ch-ua-platform is present for %d..%d except %s; a gap in the middle "
            "of the range is more likely a generation bug than a Chrome change"
            % (min(with_hint), max(with_hint), gaps))
    if verbose:
        print("  sec-ch-ua-platform: %d profiles, %d..%d"
              % (len(with_hint), min(with_hint), max(with_hint)))


def check_runtime_agrees(verbose):
    """The runtime must read the generated table, not a copy of it.

    Args:
        verbose: Print the checked symbol count.
    """
    text = RUNTIME.read_text(encoding="utf-8")
    if "from .client_hints import CAPTURED_HEADERS" not in text:
        errors.append("browser_headers.py does not import CAPTURED_HEADERS; a second "
                      "copy of the table would drift from the evidence")
    for symbol in ("def browser_headers(", "def header_order(", "def profile_major("):
        if symbol not in text:
            errors.append("browser_headers.py is missing %r" % symbol.strip())
    if verbose:
        print("  runtime: browser_headers reads the generated table")


def main():
    """Runs every check.

    Returns:
        Process exit status: 0 clean, 1 on any error.
    """
    parser = argparse.ArgumentParser(description="Audit the client hint table")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    print("Auditing the client hint table")
    check_table(args.verbose)
    check_user_agent_versions(args.verbose)
    check_platform_hint(args.verbose)
    check_runtime_agrees(args.verbose)
    for message in errors:
        print("error: %s" % message)
    print("\n%d error(s)" % len(errors))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
