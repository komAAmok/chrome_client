#!/usr/bin/env python3
"""Fails when a cache option changes the request header block.

Six cache configurations produce six different request header blocks, and only
some of the differences are visible from the Python API: the option is called
`cache`, and every one of them is supposed to be a local decision about what the
client stores. Two of them are not -- Chromium adds headers for them:

    net/http/http_network_transaction.cc:1482-1487
    if (load_flags & LOAD_BYPASS_CACHE) {
      SetHeader(Pragma, "no-cache");
      SetHeader(Cache-Control, "no-cache");
    } else if (load_flags & LOAD_VALIDATE_CACHE) {
      SetHeader(Cache-Control, "max-age=0");
    }

That matters because "same URL concurrency is 5x faster with the cache off" is
a real measurement (`docs/ASYNC_CONCURRENCY.md`, 229 against 1238 req/s) and the
fastest way to collect it is to pass `cache_mode="bypass"` everywhere -- which
silently adds `Pragma: no-cache` and `Cache-Control: no-cache` to every request
and moves the fingerprint. This gate makes that cost visible instead of leaving
it to whoever reads the diff.

The check captures the real bytes: a socket server reads one request head from
the Core, and each arm is compared against the default arm.

Two things a naive version of this comparison cannot see, and both are the
point of a header gate rather than a detail:

* **Field order.** Comparing with "line in baseline" is a set comparison, so
  swapping two fields -- an HTTP/1.1 order change, which is a fingerprint change
  (AGENTS red line 1) -- produced an empty diff and passed. The field NAME
  sequence is compared first.
* **The default block itself.** A baseline captured in the same run moves with
  every arm, so deleting a default header from all six configurations was
  invisible. The default block is now pinned against
  profiles/<profile>/h1/request_head.txt, a checked-in file, and capture() is
  the only thing that may write it (--record).

    LD_LIBRARY_PATH=core/binaries/linux-x86_64 PYTHONPATH=bindings/python \
        python3 tools/audit-frozen-headers.py --profile chrome_120
"""

from __future__ import annotations

import argparse
import pathlib
import socket
import sys
import threading

#: arm name -> (Session kwargs, per-request kwargs, header lines that arm adds)
ARMS = (
    ("default", {}, {}, ()),
    ("cache=False", {"cache": False}, {}, ()),
    ("cache_mode=no_store", {}, {"cache_mode": "no_store"}, ()),
    ("cache_mode=force", {}, {"cache_mode": "force"}, ()),
    ("cache_mode=bypass", {}, {"cache_mode": "bypass"},
     ("Cache-Control: no-cache", "Pragma: no-cache")),
    ("cache_mode=validate", {}, {"cache_mode": "validate"},
     ("Cache-Control: max-age=0",)),
)


#: Checked-in default header block per profile. Written only by --record, and
#: compared on every ordinary run, so the one thing a same-run baseline cannot
#: detect -- a change to the baseline itself -- is what this file is for.
BASELINE_DIR = pathlib.Path(__file__).resolve().parents[1] / "profiles"


def baseline_path(profile):
    """Returns the path pinning *profile*'s default request head.

    Args:
        profile: The ``impersonate`` value, or None for no profile.

    Returns:
        A :class:`pathlib.Path`. The file need not exist.
    """
    name = profile or "none"
    return BASELINE_DIR / name / "h1" / "request_head.txt"


def names_of(lines):
    """Returns the lower-cased field names of already-split header *lines*.

    Use this rather than :func:`field_names` whenever the request line is not
    part of the input: ``field_names`` drops the first line because it assumes
    it is the request line, so feeding it a re-joined field list silently
    discarded the first field.

    Args:
        lines: Header field lines, without the request line.

    Returns:
        A list of lower-cased names, in the order the bytes carry them.
    """
    return [line.split(":", 1)[0].strip().lower() for line in lines if line]


def field_names(head):
    """Returns the header field names of *head*, in emission order.

    Args:
        head: The captured head, request line included.

    Returns:
        A list of lower-cased names, in the order the bytes carry them.
    """
    return names_of(head.split("\r\n")[1:])


class _HeadCollector(object):
    """One-request socket server that returns the raw head it received."""

    def __init__(self):
        self.socket = socket.socket()
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(8)
        self.port = self.socket.getsockname()[1]
        self.heads = []
        self._closed = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while not self._closed:
            try:
                raw, _ = self.socket.accept()
            except OSError:
                return
            buf = b""
            try:
                while b"\r\n\r\n" not in buf:
                    piece = raw.recv(65536)
                    if not piece:
                        break
                    buf += piece
                head, _, _rest = buf.partition(b"\r\n\r\n")
                self.heads.append(head.decode("latin-1"))
                raw.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            except OSError:
                pass
            finally:
                try:
                    raw.close()
                except OSError:
                    pass

    def url(self, path="/frozen"):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def close(self):
        self._closed = True
        try:
            self.socket.close()
        except OSError:
            pass


def capture(server, profile, session_kwargs, request_kwargs):
    """Returns the header block one request sends.

    All arms share one collector, so the "Host" field -- which carries the port
    -- is identical across arms and only the cache-driven fields can differ.
    A fresh "Session" per arm gives each its own engine, so one arm's cache
    cannot answer another arm's request.

    Args:
        server: The shared "_HeadCollector".
        profile: The "impersonate" value, or "None".
        session_kwargs: Extra "Session" keywords.
        request_kwargs: Extra "get" keywords.

    Returns:
        The head as a str, with the request line first.
    """
    import chrome_client

    del server.heads[:]
    with chrome_client.Session(impersonate=profile, **session_kwargs) as session:
        session.get(server.url(), **request_kwargs)
    if not server.heads:
        raise SystemExit("no request reached the collector")
    return server.heads[-1]


def header_lines(head):
    """Splits a captured head into its field lines, request line excluded."""
    return [line for line in head.split("\r\n")[1:] if line]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="chrome_120")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--record", action="store_true",
                        help="write this build's default head as the pinned baseline")
    args = parser.parse_args()

    errors = []
    server = _HeadCollector()
    try:
        baseline_head = capture(server, args.profile, {}, {})
        observations = [
            (name, capture(server, args.profile, session_kwargs, request_kwargs),
             expected_extra)
            for name, session_kwargs, request_kwargs, expected_extra in ARMS]
    finally:
        server.close()

    baseline = header_lines(baseline_head)
    pinned = baseline_path(args.profile)
    if args.record:
        pinned.parent.mkdir(parents=True, exist_ok=True)
        pinned.write_text(baseline_head + "\n", encoding="utf-8")
        print("recorded %d fields to %s" % (len(baseline), pinned))
        return 0

    # The pinned block is the arm a same-run baseline cannot check: when every
    # configuration loses a header, every arm still agrees with every other arm.
    if pinned.is_file():
        # newline="" keeps the CRLF the capture wrote; the default reader
        # translates it to LF and header_lines() would then see one long line.
        with pinned.open(encoding="utf-8", newline="") as handle:
            expected_head = handle.read().rstrip("\r\n")
        expected_fields = header_lines(expected_head)
        # Host carries the fixture ephemaral port, so its VALUE cannot be pinned;
        # its presence and position can, and are.
        expected_compare = [l for l in expected_fields
                            if not l.lower().startswith("host:")]
        baseline_compare = [l for l in baseline
                            if not l.lower().startswith("host:")]
        observed_names = [n for n in field_names(baseline_head) if n != "host"]
        if observed_names != names_of(expected_compare):
            errors.append(
                "the default block changed FIELD ORDER: pinned %r, now %r"
                % (names_of(expected_compare), observed_names))
        added = [l for l in baseline_compare if l not in expected_compare]
        removed = [l for l in expected_compare if l not in baseline_compare]
        if added:
            errors.append("the default block gained %r" % (added,))
        if removed:
            errors.append("the default block lost %r" % (removed,))
    else:
        print("note: no pinned baseline at %s; run --record once to create it"
              % pinned)

    for name, head, expected_extra in observations:
        observed = header_lines(head)
        added = [line for line in observed if line not in baseline]
        removed = [line for line in baseline if line not in observed]
        expected = list(expected_extra)
        if args.verbose:
            print("%-24s +%s -%s" % (name, added, removed))
        if removed:
            errors.append("%s removed %r" % (name, removed))
        if sorted(added) != sorted(expected):
            errors.append(
                "%s added %r, expected %r" % (name, sorted(added), sorted(expected)))
        # Field ORDER is a fingerprint (AGENTS red line 1), and a set comparison
        # cannot see a swap: the same lines are present, in a different order.
        # Only the baseline block's own relative order is asserted, by taking
        # the observed names down to the baseline names as a subsequence. That
        # is the property a regression would break -- a default field moving
        # relative to another -- and it does not require knowing where an arm's
        # own extra fields land, which is Chromium's business
        # (net/http/http_network_transaction.cc:1482-1487 sets them on the same
        # block rather than appending a second one).
        baseline_names = field_names(baseline_head)
        observed_names = field_names(head)
        common = [n for n in observed_names if n in baseline_names]
        if common != baseline_names:
            errors.append(
                "%s changed the default block's FIELD ORDER: %r, expected %r"
                % (name, common, baseline_names))

    for error in errors:
        print("error: " + error)
    if errors:
        return 1
    print("frozen headers: %d arms, %d baseline fields, only documented arms "
          "move them, order pinned" % (len(ARMS), len(baseline)))
    return 0

if __name__ == "__main__":
    sys.exit(main())
