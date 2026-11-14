#!/usr/bin/env python3
"""Pins what a discard request does to the wire, so the cost stays visible.

``discard_cookies=True`` runs the request on its own engine
(:meth:`BaseSession._throwaway_slot`), because Chromium has no per-request
cookie switch: ``LOAD_DO_NOT_SEND_COOKIES`` was deleted in M87
(docs/TBD_VERIFICATION.md, T-1) and ``set_disallow_credentials`` also changes
privacy mode and client-certificate behaviour
(net/url_request/url_request.cc:594-597, url_request_http_job.cc:527-537).

An engine owns its TLS session cache, H2/H3 pools and Alt-Svc properties
(core/source/engine.cc:540-543), so a throwaway engine cannot resume. That is
a real, measurable difference in the ClientHello between two requests of the
same session -- one flag apart -- and it is not a defect in the browser sense:
a fresh Chrome context opens a full handshake too. It is a property of this
implementation, and this gate exists so it stays a stated property instead of
drifting into a surprise.

What is asserted, against a local TLS listener that peeks at the ClientHello:

  1. a plain request that reuses a pooled connection resumes, and its
     ClientHello carries extension 41 (pre_shared_key);
  2. every discard request opens a new connection, does not resume, and its
     ClientHello carries no extension 41;
  3. the two blocks otherwise carry the same extension SET -- the flag changes
     resumption, not the TLS configuration;
  4. cache state is not shared: a cacheable response is served from cache for a
     plain request and re-fetched by a discard request.

(4) is the part that would catch a future change back to the shared-engine
implementation, which is where ``only_if_cached`` used to work and now cannot.

    LD_LIBRARY_PATH=core/binaries/linux-x86_64 PYTHONPATH=bindings/python \
        python3 tools/audit-discard-fingerprint.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "bindings", "python"))

#: TLS extension the resumption path adds.  41 is pre_shared_key; its presence
#: in a ClientHello is what makes JA4 report a resumption.  See
#: profiles/*/h1/captures.json for the two shapes a real Chrome produces.
EXTENSION_PRE_SHARED_KEY = 41

#: Extension 21, RFC 7685 padding.  Chromium decides per connection whether to
#: emit it (tools/audit-padding-rule.py documents the 512-byte window rule and
#: why every profile table leaves the width at 0), so a set comparison that
#: included it would report noise as a difference.
EXTENSION_PADDING = 21


def is_grease(value):
    """True when *value* is a GREASE codepoint, per RFC 8701.

    Args:
        value: An extension type id (or any 16-bit codepoint).

    Returns:
        True for 0x0a0a, 0x1a1a ... 0xfafa, the values BoringSSL randomises per
        connection.  Those differ on every ClientHello by construction, so they
        cannot take part in a set comparison.
    """
    return (value & 0x0f0f) == 0x0a0a and (value >> 8) == (value & 0xff)


def stable_extensions(values):
    """Returns *values* without GREASE and without the padding extension.

    Args:
        values: Extension type ids from one ClientHello, or None.

    Returns:
        A set of the ids that say something about the TLS configuration rather
        than about this one connection.
    """
    return {value for value in (values or ())
            if not is_grease(value) and value != EXTENSION_PADDING}


def parse_client_hello_extensions(record):
    """Returns the extension ids of a ClientHello, or None if not one.

    Args:
        record: The first bytes of a TLS stream, starting with the record header.

    Returns:
        A list of extension type ids, or None when the bytes are not a
        ClientHello (a resumed handshake starts with the same record type, so
        this only decides whether the block can be read at all).
    """
    body = record[5:]
    if not body or body[0] != 1:
        return None
    position = 4 + 2 + 32                      # handshake head, version, random
    if position >= len(body):
        return None
    session_id_length = body[position]
    position += 1 + session_id_length
    if position + 2 > len(body):
        return None
    cipher_length = struct.unpack("!H", body[position:position + 2])[0]
    position += 2 + cipher_length
    if position >= len(body):
        return None
    compression_length = body[position]
    position += 1 + compression_length
    if position + 2 > len(body):
        return []
    extensions_length = struct.unpack("!H", body[position:position + 2])[0]
    position += 2
    end = min(position + extensions_length, len(body))
    extensions = []
    while position + 4 <= end:
        kind, length = struct.unpack("!HH", body[position:position + 4])
        position += 4
        extensions.append(kind)
        position += length
    return extensions


class _Listener(object):
    """A TLS listener that records, per connection, whether it resumed."""

    def __init__(self, certificate, key):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key)
        self.context = context
        self.socket = socket.socket()
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(64)
        self.port = self.socket.getsockname()[1]
        self.rows = []
        self.lock = threading.Lock()
        self.closed = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _record(self, row):
        """Appends one observation; called from the listener thread."""
        with self.lock:
            self.rows.append(row)

    def take(self):
        """Returns the rows seen so far and clears the list."""
        with self.lock:
            rows, self.rows = self.rows, []
        return rows

    def _serve(self):
        while not self.closed:
            try:
                raw, _ = self.socket.accept()
            except OSError:
                return
            try:
                raw.settimeout(5)
                # Peek, do not consume: the handshake needs the bytes intact.
                head = raw.recv(16384, socket.MSG_PEEK)
                extensions = parse_client_hello_extensions(head)
                secure = self.context.wrap_socket(raw, server_side=True)
                self._record({
                    "resumed": bool(secure.session_reused),
                    "extensions": extensions,
                    "has_psk": bool(extensions) and EXTENSION_PRE_SHARED_KEY in extensions,
                    "count": len(extensions) if extensions is not None else None,
                })
                secure.recv(65536)
                secure.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                               b"Connection: close\r\n\r\nok")
                time.sleep(0.02)
                secure.close()
            except (OSError, ssl.SSLError) as error:
                self._record({"error": "%s: %s" % (type(error).__name__, error)})

    def url(self, path="/frozen"):
        """Returns a URL on this listener."""
        return "https://127.0.0.1:%d%s" % (self.port, path)

    def close(self):
        """Stops accepting."""
        self.closed = True
        try:
            self.socket.close()
        except OSError:
            pass


def mint_certificate(directory):
    """Writes a self-signed certificate for 127.0.0.1 and returns both paths.

    The session trusts it through ``verify=<path>``, so the handshake is a real
    verified one rather than the insecure verifier, which would leave the TLS
    configuration a different shape from a shipped session.

    Args:
        directory: Where to write ``cert.pem`` and ``key.pem``.

    Returns:
        ``(certificate_path, key_path)``.

    Raises:
        SystemExit: ``openssl`` is missing, so the check cannot run.
    """
    if shutil.which("openssl") is None:
        raise SystemExit("openssl is required to mint the fixture certificate")
    certificate = os.path.join(directory, "cert.pem")
    key = os.path.join(directory, "key.pem")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", key, "-out", certificate, "-days", "2",
         "-subj", "/CN=127.0.0.1",
         "-addext", "subjectAltName=IP:127.0.0.1"],
        check=True, capture_output=True)
    return certificate, key


def observe(listener, session, url, discard, rounds):
    """Returns one row per connection the session opened for *rounds* requests."""
    listener.take()
    for _ in range(rounds):
        session.get(url, discard_cookies=discard)
        time.sleep(0.05)
    return listener.take()


def check(profile, rounds, verbose):
    """Runs the assertions and returns a list of error strings."""
    import chrome_client

    errors = []
    directory = tempfile.mkdtemp(prefix="discard-fp-")
    certificate, key = mint_certificate(directory)
    listener = _Listener(certificate, key)
    try:
        with chrome_client.Session(impersonate=profile, verify=certificate) as session:
            url = listener.url()
            plain = observe(listener, session, url, False, rounds)
            discard = observe(listener, session, url, True, rounds)
    finally:
        listener.close()

    if verbose:
        print("plain  :", json.dumps(plain))
        print("discard:", json.dumps(discard))

    good = [row for row in plain if "error" not in row]
    if len(good) < 2:
        errors.append("plain arm produced %d usable connections, need at least 2" % len(good))
    else:
        resumed = [row for row in good if row["resumed"]]
        if not resumed:
            errors.append("plain arm never resumed, so the pool is not being reused "
                          "and the discard comparison means nothing: %r" % (good,))
        elif not resumed[-1]["has_psk"]:
            errors.append("plain arm's resumed ClientHello carries no pre_shared_key "
                          "(extension 41), so resumption is not visible on the wire: %r"
                          % (resumed[-1],))

    bad = [row for row in discard if "error" not in row]
    if len(bad) != rounds:
        errors.append("discard arm produced %d connections, expected %d (one per request)"
                      % (len(bad), rounds))
    for index, row in enumerate(bad):
        if row["resumed"]:
            errors.append("discard connection %d resumed; the throwaway engine "
                          "reused a session from somewhere" % index)
        if row["has_psk"]:
            errors.append("discard connection %d carried pre_shared_key; a fresh "
                          "engine has no session to resume" % index)

    if good and bad:
        # GREASE and padding are per-connection by design, so they are removed
        # before the sets are compared; what is left describes the TLS
        # configuration, and the only licence this flag has to change it is
        # pre_shared_key.
        plain_extensions = stable_extensions(good[-1]["extensions"])
        discard_extensions = stable_extensions(bad[0]["extensions"])
        only_in_plain = sorted(plain_extensions - discard_extensions)
        only_in_discard = sorted(discard_extensions - plain_extensions)
        if only_in_plain != [EXTENSION_PRE_SHARED_KEY] or only_in_discard:
            errors.append(
                "the flag changed more than resumption: only-in-resumed %r, "
                "only-in-fresh %r. The documented difference is exactly "
                "[%d]." % (only_in_plain, only_in_discard, EXTENSION_PRE_SHARED_KEY))
    return errors


def check_cache_is_not_shared(profile):
    """A cacheable response must be re-fetched by a discard request.

    Args:
        profile: The profile name to pin.

    Returns:
        A list of error strings.
    """
    import chrome_client
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        """Counts how often the body had to be produced."""

        protocol_version = "HTTP/1.1"
        served = 0

        def do_GET(self):
            Handler.served += 1
            body = b"cacheable"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=600")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/cache" % server.server_port
    errors = []
    try:
        with chrome_client.Session(impersonate=profile) as session:
            session.get(url)
            Handler.served = 0
            session.get(url)
            session.get(url)
            if Handler.served != 0:
                errors.append("the plain arm did not read its own cache (%d refetches)"
                              % Handler.served)
            Handler.served = 0
            session.get(url, discard_cookies=True)
            session.get(url, discard_cookies=True)
            if Handler.served != 2:
                errors.append("discard requests reached the origin %d times, expected 2; "
                              "either the throwaway engine is now sharing the session cache "
                              "(good for caching, wrong for isolation) or the fixture is "
                              "not cacheable" % Handler.served)
    finally:
        server.shutdown()
        server.server_close()
    return errors


def main():
    """Runs the checks; returns a process exit code."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="chrome_120")
    parser.add_argument("--rounds", type=int, default=3,
                        help="connections per arm; 3 is enough to see a resume")
    parser.add_argument("--verbose", action="store_true")
    arguments = parser.parse_args()

    errors = check(arguments.profile, arguments.rounds, arguments.verbose)
    errors += check_cache_is_not_shared(arguments.profile)
    for error in errors:
        print("error: " + error)
    if errors:
        return 1
    print("discard fingerprint: %d connections per arm, resumption is the only "
          "documented difference, cache is not shared" % arguments.rounds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
