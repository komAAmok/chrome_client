"""Thread-pool concurrency: the library must not deadlock under a pool.

The question "does a thread pool deadlock this library" has one real answer in
this repository's history: it did, once, in two different ways, and both are
fixed. So the tests here are the shapes that would have caught them, plus the
shapes a caller is most likely to hit.

Every test runs its body on a watchdog thread and fails with a traceback dump
instead of hanging the suite. A deadlock that shows up as a hung CI job is a
deadlock nobody investigates; one that fails with a dump names the frames.

The two historical deadlocks, for the record:

1. `fork()` after an engine exists. The child inherits the dead
   `ThreadPoolInstance` and `network_thread_`, and since
   `Get()` still returns non-null nothing rebuilds them, so any network call
   blocked forever. Fixed by a `pthread_atfork` guard in the Core, which
   turns the hang into `InitializationFailed`.
2. GIL vs Rust `Mutex` lock ordering: `schedule_event` held a
   `MutexGuard` across the GIL acquisition. Fixed by dropping the guard before
   `Python::attach`.
"""

import faulthandler
import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor


def _run_with_watchdog(testcase, label, body, seconds=60.0):
    """Runs *body* on its own thread; a hang fails the test with a dump.

    `faulthandler` is what makes the failure useful: the dump names the frames
    each thread is parked in, which is the difference between "it hung" and "it
    hung in the Core's Condvar wait".
    """
    state = {"done": False, "error": None}

    def runner():
        try:
            body()
            state["done"] = True
        except BaseException as exc:  # noqa: BLE001 - the point is to report it
            state["error"] = exc

    thread = threading.Thread(target=runner, name="watchdog-target", daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        faulthandler.dump_traceback()
        testcase.fail("%s did not return within %.0fs (deadlock?)" % (label, seconds))
    if state["error"] is not None:
        raise state["error"]


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import threading as _threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            disable_nagle_algorithm = True

            def do_GET(self):
                if self.path.startswith("/chunked"):
                    chunks, size = 32, 65536
                    self.send_response(200)
                    self.send_header("Content-Length", str(chunks * size))
                    self.end_headers()
                    block = b"x" * size
                    try:
                        for _ in range(chunks):
                            self.wfile.write(block)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                if self.path.startswith("/slow"):
                    time.sleep(0.4)
                body = b"x" * 64
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

            def handle_one_request(self):
                # A cancelled or closed client resets the connection; that is the
                # behaviour under test, not a fixture failure.
                try:
                    BaseHTTPRequestHandler.handle_one_request(self)
                except (ConnectionResetError, BrokenPipeError):
                    self.close_connection = True

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        cls.thread = _threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)


class SharedSessionTests(_Base):
    """One Session shared by every pool worker is the documented usage."""

    def test_a_pool_shares_one_session(self):
        import chrome_client

        def body():
            with chrome_client.Session() as session:
                session.get(self.base + "/small", timeout=20)
                with ThreadPoolExecutor(max_workers=32) as pool:
                    codes = list(pool.map(
                        lambda index: session.get(
                            "%s/small?n=%d" % (self.base, index),
                            timeout=20).status_code,
                        range(128)))
            self.assertEqual(set(codes), {200})

        _run_with_watchdog(self, "32 workers sharing one Session", body)

    def test_a_session_per_thread_also_works(self):
        import chrome_client

        def body():
            def one(index):
                with chrome_client.Session() as session:
                    return session.get("%s/small?n=%d" % (self.base, index),
                                       timeout=20).status_code

            with ThreadPoolExecutor(max_workers=16) as pool:
                codes = list(pool.map(one, range(32)))
            self.assertEqual(set(codes), {200})

        _run_with_watchdog(self, "Session per thread", body)

    def test_a_stalled_stream_does_not_wedge_other_workers(self):
        """The v7 shape: one consumer parks past the queue ceiling.

        The process-wide callback runner that made this fatal is gone; a
        per-request runner means the stalled request parks its own thread only.
        """
        import chrome_client

        def body():
            with chrome_client.Session() as session:
                session.get(self.base + "/small", timeout=20)
                stalled = session.get(self.base + "/chunked", stream=True,
                                      timeout=20)
                iterator = stalled.iter_content(65536)
                next(iterator)          # take one chunk, then stop draining
                time.sleep(0.5)         # let the queue hit its ceiling
                with ThreadPoolExecutor(max_workers=8) as pool:
                    codes = list(pool.map(
                        lambda index: session.get(
                            "%s/small?n=%d" % (self.base, index),
                            timeout=20).status_code,
                        range(16)))
                stalled.close()
            self.assertEqual(set(codes), {200})

        _run_with_watchdog(self, "stalled stream + 8 workers", body)

    def test_close_under_concurrent_workers_returns(self):
        """close() must not leave a worker parked forever."""
        import chrome_client

        def body():
            session = chrome_client.Session()
            session.get(self.base + "/small", timeout=20)
            stop = threading.Event()

            def worker():
                while not stop.is_set():
                    try:
                        session.get(self.base + "/small", timeout=10)
                    except Exception:  # noqa: BLE001 - close() is the expected one
                        return

            workers = [threading.Thread(target=worker, daemon=True)
                       for _ in range(16)]
            for thread in workers:
                thread.start()
            time.sleep(0.3)
            session.close()
            stop.set()
            for thread in workers:
                thread.join(10)
            self.assertFalse(any(thread.is_alive() for thread in workers),
                             "a worker never returned after close()")

        _run_with_watchdog(self, "close() under 16 workers", body)


class ForkGuardTests(_Base):
    """fork() must fail fast, not hang. This is the historical deadlock."""

    def test_fork_after_an_engine_reports_instead_of_hanging(self):
        if not hasattr(os, "fork"):
            self.skipTest("fork is unavailable on this platform")
        import chrome_client

        session = chrome_client.Session()
        session.get(self.base + "/small", timeout=20)   # engine now exists

        try:
            self._fork_child_must_fail(session)
        finally:
            session.close()

    def _fork_child_must_fail(self, session):
        import warnings

        read_fd, write_fd = os.pipe()
        # Python 3.12 warns that forking a multi-threaded process may deadlock.
        # That is exactly the premise of this test: the child must report
        # InitializationFailed instead of hanging, so the warning is expected.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            pid = os.fork()
        if pid == 0:                                    # child
            os.close(read_fd)
            try:
                chrome_client.Session().get(self.base + "/small", timeout=5)
                message = b"ok"
            except BaseException as exc:                # noqa: BLE001
                message = type(exc).__name__.encode()
            try:
                os.write(write_fd, message)
            finally:
                os._exit(0)
        os.close(write_fd)
        os.set_blocking(read_fd, False)
        deadline = time.time() + 20
        collected = b""
        while time.time() < deadline and not collected:
            try:
                chunk = os.read(read_fd, 256)
                if chunk:
                    collected += chunk
                    break
            except BlockingIOError:
                pass
            done, _ = os.waitpid(pid, os.WNOHANG)
            if done == pid:
                break
            time.sleep(0.02)
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        os.close(read_fd)

        self.assertTrue(collected,
                        "the forked child produced no result within 20s: the "
                        "atfork guard is not turning the deadlock into an error")
        self.assertNotEqual(collected, b"ok",
                            "a forked child reused the parent engine; it must "
                            "fail with InitializationFailed instead")


class EventLoopBoundaryTests(_Base):
    def test_the_sync_api_warns_inside_a_running_loop(self):
        """It cannot be prevented, so it must at least be visible."""
        import asyncio
        import warnings

        import chrome_client

        async def body():
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                with chrome_client.Session() as session:
                    session.get(self.base + "/small", timeout=20)
            return [str(item.message) for item in caught
                    if issubclass(item.category, RuntimeWarning)]

        messages = asyncio.run(body())
        self.assertTrue(any("event loop" in message for message in messages),
                        "a blocking call inside a loop produced no warning: %r"
                        % (messages,))


if __name__ == "__main__":
    unittest.main()
