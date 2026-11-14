"""Proof that the body ceiling stops Core reads, not just the Python consumer.

server, a tiny socket buffer, or a test that never had a chance to fail. The
server, a tiny socket buffer, or a test that never had a chance to fail. The
three arms below differ only in what the consumer does:

  park-1MiB   read 1 MiB then stop for 5 s   -> server must stall
  drain-fast  read everything immediately    -> server must finish
  park-late   read 40 MiB then stop for 5 s  -> server stalls late

offset from the start: after 40 MiB have already been consumed the same
offset from the start: after 40 MiB have already been consumed the same
~1 MiB of slack appears past the consumer.
"""

import os
import socket
import sys
import threading
import time

from chrome_client import Session

TOTAL = 100 * 1024 * 1024
#: How long each parked arm stays parked. Long enough that any socket-buffer
#: absorption has finished by the midpoint, which is what makes the tail column
#: meaningful rather than a snapshot of the kernel still draining.
HOLD_SECONDS = 5.0


def rss_kib():
    with open("/proc/%d/status" % os.getpid(), "r") as handle:
        for line in handle:
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    return 0


class Server(object):
    def __init__(self):
        self.socket = socket.socket()
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(4)
        self.port = self.socket.getsockname()[1]
        self.written = 0
        threading.Thread(target=self._serve, daemon=True).start()

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
                want = min(len(block), TOTAL - self.written)
                conn.sendall(block[:want])
                self.written += want
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def url(self):
        return "http://127.0.0.1:%d/big" % self.port

    def close(self):
        try:
            self.socket.close()
        except OSError:
            pass


def arm(name, park_after):
    server = Server()
    session = Session()
    response = session.get(server.url(), stream=True, timeout=120)
    chunks = response.iter_content(chunk_size=65536)
    consumed = 0
    while consumed < park_after:
        consumed += len(chunks.__next__())

    rss_before = rss_kib()
    written_before = server.written
    progress = []
    if park_after < TOTAL:
        deadline = time.time() + HOLD_SECONDS
        while time.time() < deadline:
            time.sleep(0.5)
            progress.append(server.written)
    written_after = server.written
    rss_after = rss_kib()

    rest = 0
    for chunk in chunks:
        rest += len(chunk)
    response.close()
    session.close()
    server.close()

    half = len(progress) // 2
    tail = (progress[-1] - progress[half]) if len(progress) > 1 else 0
    print("%-11s consumed %6.2f MiB | server during hold %7.3f MiB | tail %7.3f MiB | rss %+.2f MiB | drained %7.2f MiB" % (
        name, consumed / 1048576.0, (written_after - written_before) / 1048576.0,
        tail / 1048576.0, (rss_after - rss_before) / 1024.0, rest / 1048576.0))
    return (written_after - written_before), tail, rest


def main():
    print("100 MiB body, three consumer behaviours, %.0f s hold" % HOLD_SECONDS)
    print()
    a_prog, a_tail, a_rest = arm("park-1MiB", 1 * 1024 * 1024)
    b_prog, b_tail, b_rest = arm("drain-fast", TOTAL)
    c_prog, c_tail, c_rest = arm("park-late", 40 * 1024 * 1024)
    print()
    print("checks:")
    ok1 = a_prog < 8 * 1024 * 1024 and a_tail < 128 * 1024
    ok2 = b_rest < 1024
    ok3 = c_prog < 8 * 1024 * 1024 and c_tail < 128 * 1024
    print("  park-1MiB stalls (<8 MiB progress, <128 KiB tail): %s" % ok1)
    print("  drain-fast does not stall:                          %s" % ok2)
    print("  park-late stalls too:                               %s" % ok3)
    return 0 if (ok1 and ok2 and ok3) else 1


if __name__ == "__main__":
    sys.exit(main())