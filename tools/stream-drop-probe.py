"""Proof that dropping a streaming response cancels it instead of draining it.

The property this measures is not visible from the return value of anything: a
dropped body stream used to keep the Core reading, because abandon_body resumes
the read so a paused request is not stranded.  The request did finish, so every
assertion about completion passed -- the cost was the rest of the body arriving
over the socket with nobody to receive it.

Four arms differ only in what the caller does after the first chunk:

  close          read one chunk then response.close()   -> Core cancels
  abandon        read one chunk then drop every ref     -> must behave the same
  abandon-async  the same on the awaitable path         -> must behave the same
  consume        read everything                        -> server finishes

The two abandon arms are what regressed: measured on a 256 MiB body, dropping
the reference pulled the remaining 252.56 MiB in 1.5 s before the socket closed,
against 3.44 MiB for an explicit close.  The check here is that the server stops
within DROP_SETTLE seconds of the drop, with less than SLACK_BYTES more written.

Contract-level unit tests cannot see this: the terminal state of the request is
the same either way.  Only a real server can tell "cancelled" from "drained".

Run with the Core available:

    LD_LIBRARY_PATH=core/binaries/linux-x86_64 \
      PYTHONPATH=bindings/python python3 tools/stream-drop-probe.py
"""

import asyncio
import gc
import socket
import sys
import threading
import time

from chrome_client import AsyncSession, Session

MIB = 1024 * 1024
TOTAL = 64 * MIB
FIRST_CHUNK = 64 * 1024
#: How long the server keeps counting after the caller drops or closes.  Long
#: enough that a resumed read would have finished (measured: 252.56 MiB in
#: 1.5 s), short enough that the whole probe stays under two seconds per arm.
DROP_SETTLE = 1.5
#: Progress allowed after the drop.  The socket already holds about 1 MiB plus
#: the queue ceiling, so anything under 8 MiB is slack rather than a read that
#: continued; a resumed read of this body is 64 MiB.
SLACK_BYTES = 8 * MIB


class Server(object):
    """One connection, counted, so whether the Core kept reading is observable."""

    def __init__(self):
        self.socket = socket.socket()
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(4)
        self.port = self.socket.getsockname()[1]
        self.written = 0
        self.closed = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def url(self, name):
        return "http://127.0.0.1:%d/%s" % (self.port, name)

    def _serve(self):
        try:
            conn, _ = self.socket.accept()
        except OSError:
            return
        try:
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n"
                         b"Connection: close\r\n\r\n" % TOTAL)
            block = b"x" * 65536
            while self.written < TOTAL:
                conn.sendall(block)
                self.written += len(block)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
            self.closed = 1

    def close(self):
        try:
            self.socket.close()
        except OSError:
            pass


def _settle(server, written_at_drop):
    """Waits DROP_SETTLE and reports the progress made after the drop."""
    deadline = time.time() + DROP_SETTLE
    while time.time() < deadline:
        time.sleep(0.05)
    return server.written - written_at_drop


def arm_close():
    server = Server()
    session = Session()
    try:
        response = session.get(server.url("close"), stream=True, timeout=30)
        next(iter(response.iter_content(chunk_size=FIRST_CHUNK)))
        at_drop = server.written
        response.close()
        after = _settle(server, at_drop)
        closed = server.closed
    finally:
        session.close()
        server.close()
    return at_drop, after, closed


def arm_abandon():
    """Drops every reference without close(): the ResourceWarning path."""
    server = Server()
    session = Session()
    try:
        response = session.get(server.url("abandon"), stream=True, timeout=30)
        chunks = iter(response.iter_content(chunk_size=FIRST_CHUNK))
        next(chunks)
        at_drop = server.written
        del chunks, response
        gc.collect()
        after = _settle(server, at_drop)
        closed = server.closed
    finally:
        session.close()
        server.close()
    return at_drop, after, closed


def arm_abandon_async():
    """The same property on the awaitable path, dropped inside a live loop."""
    server = Server()

    async def run():
        session = AsyncSession()
        try:
            response = await session.get(server.url("abandon-async"),
                                         stream=True, timeout=30)
            chunks = response.aiter_content(chunk_size=FIRST_CHUNK)
            await chunks.__anext__()
            at_drop = server.written
            del chunks, response
            gc.collect()
            # Let the release run: an abandoned AsyncResponse schedules its own
            # cancellation on this loop.
            deadline = time.time() + DROP_SETTLE
            while time.time() < deadline:
                await asyncio.sleep(0.05)
            return at_drop, server.written - at_drop, server.closed
        finally:
            session.close()

    try:
        return asyncio.run(run())
    finally:
        server.close()


def arm_consume():
    server = Server()
    session = Session()
    try:
        response = session.get(server.url("consume"), stream=True, timeout=30)
        read = sum(len(chunk) for chunk in response.iter_content(chunk_size=FIRST_CHUNK))
        response.close()
        closed = server.closed
    finally:
        session.close()
        server.close()
    return read, server.written, closed


def main():
    print("%d MiB body, %.1f s settle after the drop, %d MiB slack" % (
        TOTAL // MIB, DROP_SETTLE, SLACK_BYTES // MIB))
    print()
    results = []
    for name, arm in (("close", arm_close),
                      ("abandon", arm_abandon),
                      ("abandon-async", arm_abandon_async),
                      ("consume", arm_consume)):
        at_drop, after, closed = arm()
        results.append((name, at_drop, after))
        print("%-14s at drop %6.2f MiB | after drop %8.2f MiB | server closed=%d" % (
            name, at_drop / float(MIB), after / float(MIB), closed))
    print()
    checks = []
    for name, at_drop, after in results:
        if name == "consume":
            checks.append((name, at_drop >= TOTAL - MIB))
        else:
            checks.append((name, after < SLACK_BYTES))
    print("checks:")
    for name, ok in checks:
        what = ("read the whole body" if name == "consume"
                else "stopped within %d MiB of the drop" % (SLACK_BYTES // MIB))
        print("  %-14s %s: %s" % (name, what, ok))
    return 0 if all(ok for _name, ok in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
