"""What framing each upload shape puts on the wire, and why files= cannot stream.

The question this settles: can ``files=`` be rendered as an iterator instead of
one bytes object?  Rendering it incrementally is easy in Python.  Keeping the
wire identical is not, and that is what decides it.

ABI fact (checked, not assumed): ``mn_upload_mode_t`` offers MN_UPLOAD_FIXED,
which takes the whole body at once (crates/minicronet/src/request.rs:626 hands
the Core ``body.as_slice()``), and MN_UPLOAD_CHUNKED, whose write entry point is
documented as valid only for chunked requests (bindings/python/src/lib.rs:551).
There is no "known length, fed incrementally" mode, so an incrementally rendered
body is always chunked on the wire.

Measured here, over a raw socket so the framing is read rather than inferred:

  files=<file object>   Content-Length + multipart/form-data   (what Chrome sends)
  data=<file object>    Transfer-Encoding: chunked             (documented stream)

So streaming ``files=`` would trade a Chrome-identical request for a smaller
Python buffer.  This probe pins the framing so that trade cannot be made by
accident: a change that flips ``files=`` to chunked fails here and has to be a
decision rather than a side effect.

Run with the Core available:

    LD_LIBRARY_PATH=core/binaries/linux-x86_64 \
      PYTHONPATH=bindings/python python3 tools/upload-framing-probe.py
"""

import io
import os
import socket
import sys
import threading

from chrome_client import Session

PAYLOAD = b"payload-marker-" + b"p" * (1024 * 1024)


class Server(object):
    """Accepts one request and records its head plus how the body was framed."""

    def __init__(self):
        self.socket = socket.socket()
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(4)
        self.port = self.socket.getsockname()[1]
        self.head = b""
        self.body = b""
        self.framing = "none"
        threading.Thread(target=self._serve, daemon=True).start()

    def url(self):
        return "http://127.0.0.1:%d/upload" % self.port

    def _read_head(self, conn):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return data
            data += chunk
        return data

    def _serve(self):
        try:
            conn, _ = self.socket.accept()
        except OSError:
            return
        try:
            head = self._read_head(conn)
            header_blob, _, rest = head.partition(b"\r\n\r\n")
            self.head = header_blob
            lowered = header_blob.lower()
            if b"transfer-encoding: chunked" in lowered:
                self.framing = "chunked"
                self.body = self._read_chunked(conn, rest)
            elif b"content-length:" in lowered:
                self.framing = "content-length"
                length = 0
                for line in header_blob.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1].strip())
                self.body = self._read_exact(conn, rest, length)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                         b"Connection: close\r\n\r\nok")
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _read_exact(conn, already, length):
        data = already
        while len(data) < length:
            chunk = conn.recv(min(65536, length - len(data)))
            if not chunk:
                break
            data += chunk
        return data

    def _read_chunked(self, conn, already):
        buffer = already
        body = b""
        while True:
            while b"\r\n" not in buffer:
                chunk = conn.recv(65536)
                if not chunk:
                    return body
                buffer += chunk
            line, _, buffer = buffer.partition(b"\r\n")
            size = int(line.split(b";")[0], 16)
            if size == 0:
                return body
            while len(buffer) < size + 2:
                chunk = conn.recv(65536)
                if not chunk:
                    return body
                buffer += chunk
            body += buffer[:size]
            buffer = buffer[size + 2:]

    def close(self):
        try:
            self.socket.close()
        except OSError:
            pass


def capture(kind):
    server = Server()
    session = Session()
    try:
        if kind == "files":
            handle = io.BytesIO(PAYLOAD)
            session.post(server.url(), files={"f": ("payload.bin", handle)},
                         timeout=30)
        else:
            handle = io.BytesIO(PAYLOAD)
            session.post(server.url(), data=handle, timeout=30)
    finally:
        session.close()
        server.close()
    return server.head, server.body, server.framing


def check(kind, framing, head, body):
    """The framing each shape must keep.  Also used crosswise as a self-check."""
    lowered = head.lower()
    if kind == "files":
        return (framing == "content-length"
                and b"transfer-encoding: chunked" not in lowered
                and b"content-type: multipart/form-data; boundary=" in lowered
                and PAYLOAD in body)
    return (framing == "chunked"
            and b"content-length:" not in lowered
            and PAYLOAD in body)


def main():
    print("%d KiB payload, raw socket" % (len(PAYLOAD) // 1024))
    print()
    captured = {}
    for kind in ("files", "data"):
        head, body, framing = capture(kind)
        captured[kind] = (head, body, framing)
        content_type = "?"
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-type:"):
                content_type = line.split(b":", 1)[1].strip().decode("latin-1")
        print("%-5s framing=%-14s body=%6.2f KiB  content-type=%s" % (
            kind, framing, len(body) / 1024.0, content_type))
    print()
    checks = []
    for kind, (head, body, framing) in captured.items():
        checks.append((kind, check(kind, framing, head, body)))
    files_head, files_body, _files_framing = captured["files"]
    data_head, data_body, _data_framing = captured["data"]
    # Self-check: the checker must reject the other shape, otherwise it would
    # pass on any request and prove nothing.
    cross = [("files-checker on data", not check("files", "chunked", data_head, data_body)),
             ("data-checker on files", not check("data", "content-length", files_head, files_body))]
    print("checks:")
    for kind, ok in checks:
        print("  %-5s keeps its framing: %s" % (kind, ok))
    print("self-check:")
    for name, ok in cross:
        print("  %-22s rejects: %s" % (name, ok))
    return 0 if all(ok for _n, ok in checks + cross) else 1


if __name__ == "__main__":
    sys.exit(main())
