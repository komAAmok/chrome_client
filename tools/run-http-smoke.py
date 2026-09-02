#!/usr/bin/env python3
import http.server
import os
import socket
import subprocess
import sys
import threading
import time


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    cache_requests = 0
    method_requests = []

    def read_body(self):
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks = []
            while True:
                line = self.rfile.readline().strip()
                size = int(line.split(b";", 1)[0], 16)
                if size == 0:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length)

    def echo(self):
        if self.path == "/redirect307":
            self.read_body()
            self.send_response(307)
            self.send_header("Location", "/echo")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = self.read_body()
        type(self).method_requests.append((self.command, body))
        payload = self.command.encode() + b":" + body
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/disconnect":
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/stream")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        elif self.path == "/redirect307":
            self.send_response(307)
            self.send_header("Location", "/echo")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        elif self.path == "/cookie-set":
            body = b"set"
            self.send_response(200)
            self.send_header("Set-Cookie", "mn_cookie=1; Path=/")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        elif self.path == "/cookie-read":
            body = self.headers.get("Cookie", "").encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        elif self.path == "/request-headers":
            body = (
                f"Accept-Language: {self.headers.get('Accept-Language', '')}\n"
                f"Accept-Encoding: {self.headers.get('Accept-Encoding', '')}\n"
            ).encode()
        elif self.path == "/echo":
            self.echo()
            return
        elif self.path == "/slow":
            time.sleep(0.25)
            body = b"late"
        elif self.path == "/cache":
            type(self).cache_requests += 1
            body = b"cached"
        else:
            body = b"x" * 100_000
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        if self.path == "/cache":
            self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_POST = echo
    do_PUT = echo
    do_PATCH = echo
    do_DELETE = echo
    do_OPTIONS = echo

    def log_message(self, format, *args):
        pass


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    base = f"http://127.0.0.1:{server.server_port}"
    for mode, url in (
        ("success", f"{base}/stream"),
        ("success", f"{base}/redirect"),
        ("timeout", f"{base}/slow"),
        ("cancel", f"{base}/slow"),
        ("network-error", f"{base}/disconnect"),
    ):
        subprocess.run([sys.argv[1], mode, url], check=True, env=os.environ)
    subprocess.run(
        [sys.argv[1], "cache-modes", f"{base}/cache"],
        check=True,
        env=os.environ,
    )
    subprocess.run(
        [sys.argv[1], "cache-miss", f"{base}/only-cache"],
        check=True,
        env=os.environ,
    )
    subprocess.run(
        [sys.argv[1], "cache-disabled", f"{base}/cache"],
        check=True,
        env=os.environ,
    )
    headers = subprocess.run(
        [sys.argv[1], "dump", f"{base}/request-headers"],
        check=True,
        env=os.environ,
        capture_output=True,
    ).stdout.decode()
    assert "Accept-Language: en-US,en;q=0.9" in headers
    assert "br" in headers
    assert "zstd" in headers
    subprocess.run(
        [sys.argv[1], "method", f"{base}/echo", "POST", "payload"],
        check=True,
        env=os.environ,
    )
    for method in ("PATCH", "DELETE", "OPTIONS"):
        subprocess.run(
            [sys.argv[1], "method", f"{base}/echo", method, "payload"],
            check=True,
            env=os.environ,
        )
    subprocess.run(
        [sys.argv[1], "chunked", f"{base}/echo", "PUT", "chunked-payload"],
        check=True,
        env=os.environ,
    )
    subprocess.run(
        [sys.argv[1], "redirect-manual", f"{base}/redirect"],
        check=True,
        env=os.environ,
    )
    subprocess.run(
        [sys.argv[1], "redirect-error", f"{base}/redirect"],
        check=True,
        env=os.environ,
    )
    subprocess.run(
        [sys.argv[1], "redirect-upload", f"{base}/redirect307"],
        check=True,
        env=os.environ,
    )
    subprocess.run(
        [sys.argv[1], "cookie", base],
        check=True,
        env=os.environ,
    )
    subprocess.run(
        [sys.argv[1], "concurrent", f"{base}/slow", "32"],
        check=True,
        env=os.environ,
    )
    assert Handler.cache_requests == 5, (
        f"HTTP cache policy mismatch: origin received {Handler.cache_requests} requests"
    )
finally:
    server.shutdown()
    thread.join()
