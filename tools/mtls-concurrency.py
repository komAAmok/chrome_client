#!/usr/bin/env python3
"""mTLS concurrency: how many connections a session opens for one server.

This is the arm that separated `EngineSlot._applied_certs` from everything else.
The Core's `SetClientCertificate` synchronously refreshes that origin's
connection group (`net/socket/ssl_client_socket.cc:274-287` calls
`NotifySSLConfigForServersChanged`), so a facade that re-registers the same
certificate on every request closes the connection it was about to reuse.
Throughput barely moves at one request at a time; under concurrency the
connection count is the whole story -- measured 369 TCP connections for 64
concurrent requests before the fix, 5 after.

The server is a real TLS listener with `CERT_REQUIRED`, so a wrong certificate
is a handshake failure and not a silent reuse of a pooled connection. Certificates
come from `bindings/python/tests/test_client_certificates.py`'s minting scheme;
pass a directory built the same way with --certs.

    tools/mtls-concurrency.py --requests 64 --certs /tmp/mtls
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import resource
import ssl
import socket
import sys
import threading
import time


class _Server(threading.Thread):
    """A TLS listener that demands a client certificate and counts handshakes."""

    def __init__(self, cert_dir):
        super().__init__(daemon=True)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(os.path.join(cert_dir, "srv.pem"),
                                os.path.join(cert_dir, "srv.key"))
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(os.path.join(cert_dir, "ca.pem"))
        self.context = context
        self.counts = {"tcp": 0, "handshakes": 0, "requests": 0}
        self.lock = threading.Lock()
        self.socket = socket.socket()
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(512)
        self.port = self.socket.getsockname()[1]

    def reset(self):
        """Zeroes the counters, so one arm does not count the previous one."""
        with self.lock:
            for key in self.counts:
                self.counts[key] = 0

    def run(self):
        while True:
            try:
                raw, _ = self.socket.accept()
            except OSError:
                return
            with self.lock:
                self.counts["tcp"] += 1
            threading.Thread(target=self._serve, args=(raw,), daemon=True).start()

    def _serve(self, raw):
        try:
            connection = self.context.wrap_socket(raw, server_side=True)
            with self.lock:
                self.counts["handshakes"] += 1
            buf = b""
            while True:
                while b"\r\n\r\n" not in buf:
                    piece = connection.recv(65536)
                    if not piece:
                        return
                    buf += piece
                _, _, buf = buf.partition(b"\r\n\r\n")
                with self.lock:
                    self.counts["requests"] += 1
                connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        except Exception:  # noqa: BLE001 - a failed handshake is a datum
            return


async def _run(server, requests, cert_dir, profile):
    """Sends *requests* concurrent requests and returns the measured row."""
    from chrome_client import AsyncSession

    url = "https://localhost:%d/x" % server.port
    session = AsyncSession(
        impersonate=profile,
        verify=os.path.join(cert_dir, "ca.pem"),
        cert=(os.path.join(cert_dir, "cli.pem"), os.path.join(cert_dir, "cli.key")),
        max_clients=requests)
    try:
        await session.get(url)          # warm: one connection is expected
        server.reset()
        started = time.perf_counter()
        responses = await asyncio.gather(
            *[session.get(url + "?i=%d" % index) for index in range(requests)])
        wall = time.perf_counter() - started
    finally:
        await session.aclose()
    return {
        "requests": requests,
        "ok": sum(1 for response in responses if response.status_code == 200),
        "wall_seconds": round(wall, 6),
        "requests_per_second": round(requests / wall, 1),
        "tcp_connections": server.counts["tcp"],
        "tls_handshakes": server.counts["handshakes"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=64)
    parser.add_argument("--certs", required=True,
                        help="directory with ca.pem, srv.pem, srv.key, cli.pem, cli.key")
    parser.add_argument("--profile", default="chrome_120")
    parser.add_argument("--out")
    args = parser.parse_args()

    resource.setrlimit(resource.RLIMIT_NOFILE, (60000, 60000))
    server = _Server(args.certs)
    server.start()
    try:
        row = asyncio.run(_run(server, args.requests, args.certs, args.profile))
    finally:
        try:
            server.socket.close()
        except OSError:
            pass
    row["profile"] = args.profile
    text = json.dumps(row, sort_keys=True)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
