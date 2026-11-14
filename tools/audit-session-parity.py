#!/usr/bin/env python3
"""Fails when the two session constructors stop accepting the same options.

AsyncSession spells out its own parameter list instead of forwarding **kwargs,
because a keyword a caller mistypes has to be a TypeError and not a silently
ignored option. That is the right shape, and it is also how eleven engine-level
options -- host_mapping, dns_nameservers, doh_template, dns_secure_mode,
dns_over_tls, dns_rotate, dns_padding, hsts_bypass, proxy_chain, platform and
max_sockets_per_group -- reached Session and not AsyncSession.

Nothing caught it: the stub was generated from the same list, so the audit that
compares stubs against the implementation agreed with both sides being wrong.

platform= is the one that changes the wire (it decides the User-Agent group and
sec-ch-ua-platform), so the gap made a fingerprint-relevant option unreachable
from the async API.

Usage::

    python3 tools/audit-session-parity.py
    python3 tools/audit-session-parity.py --verbose
"""

from __future__ import annotations

import argparse
import inspect
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bindings" / "python"))

#: Options that are legitimately session-specific. Everything else has to be
#: accepted by both constructors.
ASYNC_ONLY = frozenset({"max_clients"})
SYNC_ONLY = frozenset()


def parameters(cls):
    """Returns the signature of cls.__init__ as ordered (name, default, kind).

    Args:
        cls: The session class to inspect.

    Returns:
        A list of ``(name, default, kind)``, ``self`` excluded, in declaration
        order. The default is compared because a differing default is a
        behavioural difference even when both names exist; a sentinel marks
        "no default" so it cannot be confused with the string "None".
    """
    signature = inspect.signature(cls.__init__)
    rows = []
    for name, parameter in signature.parameters.items():
        if name == "self":
            continue
        if parameter.default is inspect.Parameter.empty:
            default = "<required>"
        else:
            default = repr(parameter.default)
        rows.append((name, default, parameter.kind.name))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    import chrome_client

    sync = parameters(chrome_client.Session)
    asynchronous = parameters(chrome_client.AsyncSession)
    errors = []

    sync_names = [row[0] for row in sync]
    async_names = [row[0] for row in asynchronous]
    by_name = {row[0]: row for row in asynchronous}

    missing = [name for name in sync_names if name not in by_name]
    if missing:
        errors.append(
            "AsyncSession does not accept %d option(s) Session does: %s"
            % (len(missing), ", ".join(missing)))
    extra = [name for name in async_names
             if name not in sync_names and name not in ASYNC_ONLY]
    if extra:
        errors.append(
            "AsyncSession accepts %d option(s) with no counterpart: %s"
            % (len(extra), ", ".join(extra)))

    # A shared name is not a shared option: a different default is a different
    # behaviour, and for platform= it is a different User-Agent group and a
    # different sec-ch-ua-platform, i.e. a fingerprint.
    for name, default, kind in sync:
        counterpart = by_name.get(name)
        if counterpart is None:
            continue
        if counterpart[1] != default:
            errors.append(
                "%s defaults to %s on Session but %s on AsyncSession"
                % (name, default, counterpart[1]))
        if counterpart[2] != kind:
            errors.append("%s is %s on Session but %s on AsyncSession"
                          % (name, kind, counterpart[2]))

    # Order of the options BOTH classes already had. Neither constructor is
    # keyword-only, so a parameter inserted before an existing one silently
    # changes what an existing positional call means.
    shared_sync = [name for name in sync_names if name in by_name]
    shared_async = [name for name in async_names if name in sync_names]
    if shared_sync != shared_async:
        for index, (left, right) in enumerate(zip(shared_sync, shared_async)):
            if left != right:
                errors.append(
                    "the shared options changed order at index %d: Session has "
                    "%r, AsyncSession has %r, so a positional caller of one now "
                    "passes a different option" % (index, left, right))
                break

    if args.verbose:
        print("Session      : %d options" % len(sync))
        print("AsyncSession : %d options" % len(asynchronous))
        print("shared       : %d" % len(shared_sync))
    for error in errors:
        print("error: " + error)
    if errors:
        return 1
    print("session constructors agree (%d shared options; defaults and order "
          "included)" % len(shared_sync))
    return 0


if __name__ == "__main__":
    sys.exit(main())
