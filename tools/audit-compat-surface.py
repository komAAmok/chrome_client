#!/usr/bin/env python3
"""Pins the accepted keyword surface of Session / AsyncSession.

0.3.0 removed the curl_cffi keywords this Core cannot honour -- ja3, akamai,
perk, interface, doh_url, curl_options, max_recv_speed, max_redirects,
dns_over_tls, dns_padding, quote, thread, debug -- plus referer, which the
Referer header already covers. They were accepted-and-refused or
accepted-and-ignored, and this project is a Chromium-backed fingerprint client,
not a libcurl drop-in.

Removing them is only half the job: without this gate the next "let us accept it
for compatibility" change puts them back, and nothing notices. So every keyword
the four entry points accept must appear in SURFACE with a verdict, and every
verdict must describe a keyword that still exists:

  ENGINE   reaches the Core through the engine configuration
  REQUEST  forwarded on each request (options dict, per-call flags)
  FACADE   handled entirely by the facade (containers, hooks, auth, env)

A keyword with no verdict fails, a verdict whose keyword is gone fails, and a
name on REMOVED that reappears in a signature fails.

    python3 tools/audit-compat-surface.py
    python3 tools/audit-compat-surface.py --verbose
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SESSIONS = ROOT / "bindings" / "python" / "chrome_client" / "_python_impl" / "sessions.py"

# keyword -> verdict
SURFACE = {
    # ---- engine-level: the value lands in EngineConfig and builds the engine
    "impersonate": "ENGINE",
    "proxy": "ENGINE",
    "proxies": "ENGINE",
    "proxy_auth": "ENGINE",
    "verify": "ENGINE",
    "cert": "ENGINE",
    "user_agent": "ENGINE",
    "accept_language": "ENGINE",
    "http_version": "ENGINE",
    "cache": "ENGINE",
    "host_mapping": "ENGINE",
    "dns_nameservers": "ENGINE",
    "doh_template": "ENGINE",
    "dns_secure_mode": "ENGINE",
    "dns_rotate": "ENGINE",
    "hsts_bypass": "ENGINE",
    "proxy_chain": "ENGINE",
    "platform": "ENGINE",
    "max_sockets_per_group": "ENGINE",
    # ---- per request: resolved into the options dict or a per-call flag
    "method": "REQUEST",
    "url": "REQUEST",
    "data": "REQUEST",
    "json": "REQUEST",
    "content": "REQUEST",
    "files": "REQUEST",
    "multipart": "REQUEST",
    "accept_encoding": "REQUEST",
    "cache_mode": "REQUEST",
    "priority": "REQUEST",
    "content_callback": "REQUEST",
    "timeout": "REQUEST",
    "allow_redirects": "REQUEST",
    "max_response_bytes": "REQUEST",
    "stream": "REQUEST",
    "retry": "REQUEST",
    "discard_cookies": "REQUEST",
    "header_policy": "REQUEST",
    "extra_fp": "REQUEST",
    "strip_authorization": "REQUEST",
    "take_body_buffers": "REQUEST",
    "max_clients": "REQUEST",
    # ---- facade only: containers, hooks, auth, environment
    "headers": "FACADE",
    "cookies": "FACADE",
    "params": "FACADE",
    "auth": "FACADE",
    "hooks": "FACADE",
    "trust_env": "FACADE",
    "base_url": "FACADE",
    "default_headers": "FACADE",
    "default_encoding": "FACADE",
    "raise_for_status": "FACADE",
    "response_class": "FACADE",
    "max_engines": "FACADE",
}

#: Removed in 0.3.0. A signature that accepts one of these again is a
#: regression, not a feature: the Core cannot honour them, and this project
#: does not promise curl_cffi's keyword set.
REMOVED = (
    "ja3", "akamai", "perk", "interface", "doh_url", "curl_options",
    "max_recv_speed", "max_redirects", "dns_over_tls", "dns_padding",
    "quote", "thread", "debug", "referer",
)

ENTRY_POINTS = (
    ("BaseSession", "__init__"),
    ("AsyncSession", "__init__"),
    ("Session", "request"),
    ("AsyncSession", "request"),
)


def parameters():
    """{entry point: [parameter names]} for the four entry points."""
    tree = ast.parse(SESSIONS.read_text(encoding="utf-8"))
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for klass, method in ENTRY_POINTS:
                if node.name == klass and item.name == method:
                    found["%s.%s" % (klass, method)] = [
                        argument.arg for argument in item.args.args][1:]
    return found


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    if not SESSIONS.is_file():
        print("audit-compat-surface: missing %s" % SESSIONS, file=sys.stderr)
        return 1
    entries = parameters()
    missing = [name for klass, method in ENTRY_POINTS
               if "%s.%s" % (klass, method) not in entries
               for name in ["%s.%s" % (klass, method)]]
    if missing:
        print("audit-compat-surface: cannot read %s" % ", ".join(missing),
              file=sys.stderr)
        return 1

    accepted = set()
    for names in entries.values():
        accepted.update(names)

    failures = []
    # 1. Every accepted keyword needs a verdict.
    for name in sorted(accepted - set(SURFACE)):
        failures.append(
            "%s: accepted but has no verdict. Decide whether it reaches the "
            "Core, the request, or the facade only, record it in SURFACE, and "
            "update README.md's compatibility table" % name)
    # 2. A verdict must not outlive the keyword.
    for name in sorted(set(SURFACE) - accepted):
        failures.append("%s: has a verdict but no entry point accepts it" % name)
    # 3. The removed keywords stay removed.
    for name in REMOVED:
        if name in accepted:
            failures.append(
                "%s: was removed in 0.3.0 because the Core cannot honour it, "
                "and it is accepted again" % name)

    if failures:
        print("compat surface audit FAILED:", file=sys.stderr)
        for failure in failures:
            print("  " + failure, file=sys.stderr)
        return 1

    if args.verbose:
        for name in sorted(accepted):
            print("  %-24s %s" % (name, SURFACE[name]))
        print()
    counts = {}
    for verdict in SURFACE.values():
        counts[verdict] = counts.get(verdict, 0) + 1
    print("compat surface audit passed: %d keyword(s) across %d entry points "
          "(ENGINE %d, REQUEST %d, FACADE %d), %d removed keyword(s) absent"
          % (len(accepted), len(entries), counts.get("ENGINE", 0),
             counts.get("REQUEST", 0), counts.get("FACADE", 0), len(REMOVED)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
