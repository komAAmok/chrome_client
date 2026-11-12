"""Locks the ``max_clients`` claim in docs/COMPATIBILITY_BOUNDARY.md.

The claim is not "streams are unbounded" -- it is narrower and was stated
imprecisely enough to be untestable. What the code does:

* the gate is acquired in ``AsyncSession._send_once`` and released in its
  ``finally``, so it covers the request from admission until
  ``_perform``` returns;
* ``_perform`` returns after ``_await_headers`` for ``stream=True`` and after the
  whole body for a buffered response.

So a buffered transfer is bounded end to end, and a streaming transfer is bounded
only up to its headers. Both halves are measured here against the server, because
the client's own counter would be measuring itself.

Each test counts concurrent requests on the server for the WHOLE response
(headers and body). A gauge that only counts the pre-header phase reports one for
every mode and proves nothing -- that mistake is why the claim was unverified for
so long.
"""

import asyncio
import json
import threading
import time
import unittest
import urllib.request

import chrome_client


class _Fixture(object):
    """Counting server: headers at once, then a body trickled over 0.4 s."""

    CHUNKS = 10
    CHUNK = 65536
    DELAY = 0.04

    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.now = 0
        self.peak = 0
        self.lock = threading.Lock()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            disable_nagle_algorithm = True

            def do_GET(self):
                if self.path.startswith("/inflight"):
                    body = json.dumps({"now": fixture.now,
                                       "peak": fixture.peak}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    try:
                        self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                if self.path.startswith("/reset"):
                    with fixture.lock:
                        fixture.peak = fixture.now
                    self.send_response(200)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                with fixture.lock:
                    fixture.now += 1
                    fixture.peak = max(fixture.peak, fixture.now)
                try:
                    total = fixture.CHUNKS * fixture.CHUNK
                    self.send_response(200)
                    self.send_header("Content-Length", str(total))
                    self.end_headers()
                    block = b"x" * fixture.CHUNK
                    for _ in range(fixture.CHUNKS):
                        self.wfile.write(block)
                        self.wfile.flush()
                        time.sleep(fixture.DELAY)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    with fixture.lock:
                        fixture.now -= 1

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def drain(self, timeout=10.0):
        """Waits for the fixture to be idle, then resets the peak.

        Without the wait an abandoned streaming response is still sleeping inside
        its handler, and its counter leaks into the next measurement -- measured,
        that turned a correct peak of 1 into a reported 3.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self._state()
            if state["now"] == 0:
                break
            time.sleep(0.05)
        self._get("/reset")

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as handle:
            return handle.read()

    def _state(self):
        return json.loads(self._get("/inflight"))

    def peak_now(self):
        return self._state()["peak"]

    def stop(self):
        self.server.shutdown()
        self.thread.join(timeout=5)


class MaxClientsGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = _Fixture()

    @classmethod
    def tearDownClass(cls):
        cls.fixture.stop()

    @staticmethod
    def run_async(coro):
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(coro)
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    def test_session_does_not_accept_max_clients(self):
        """The knob is AsyncSession-only; the shared stub used to claim otherwise."""
        with self.assertRaises(TypeError):
            chrome_client.Session(max_clients=2)

    def test_buffered_requests_are_bounded_end_to_end(self):
        self.fixture.drain()

        async def run():
            async with chrome_client.AsyncSession(max_clients=1) as session:
                await asyncio.gather(
                    session.get(self.fixture.base + "/a"),
                    session.get(self.fixture.base + "/b"))

        self.run_async(run())
        self.assertEqual(self.fixture.peak_now(), 1,
                         "max_clients=1 must never let two buffered responses "
                         "be in flight at once")

    def test_streaming_requests_are_bounded_only_up_to_the_headers(self):
        self.fixture.drain()

        async def run():
            async with chrome_client.AsyncSession(max_clients=1) as session:
                async def one(path):
                    response = await session.get(self.fixture.base + path,
                                                 stream=True)
                    total = 0
                    async for chunk in response.aiter_content(65536):
                        total += len(chunk)
                    await response.aclose()
                    return total

                # Bounded on purpose: a regression that makes the gate cover the
                # body phase would deadlock this gather (the second request can
                # never be admitted while the first holds the gate through its
                # whole body). A deadlocked test reads as a hung CI job rather
                # than a failure, so the wait is capped and the timeout is
                # reported as the assertion it is.
                return await asyncio.wait_for(
                    asyncio.gather(one("/s1"), one("/s2")), timeout=8.0)

        try:
            sizes = self.run_async(run())
        except asyncio.TimeoutError:
            self.fail("two streaming requests deadlocked under max_clients=1: the "
                      "gate is being held through the body phase")
        self.assertEqual(sizes, [self.fixture.CHUNKS * self.fixture.CHUNK] * 2)
        self.assertEqual(self.fixture.peak_now(), 2,
                         "the body phase of a streaming response runs outside "
                         "the gate, so the server sees both at once")

    def test_streaming_is_not_slower_than_serialised(self):
        """The timing consequence of the previous test, as a second witness.

        Two 0.4 s bodies taking 0.78 s would mean the gate covered them; taking
        about 0.4 s means it did not. The bound is loose on purpose -- this is a
        consequence check, not the primary assertion.
        """
        self.fixture.drain()

        async def run():
            async with chrome_client.AsyncSession(max_clients=1) as session:
                async def one(path):
                    response = await session.get(self.fixture.base + path,
                                                 stream=True)
                    async for _ in response.aiter_content(65536):
                        pass
                    await response.aclose()

                started = time.perf_counter()
                await asyncio.wait_for(
                    asyncio.gather(one("/t1"), one("/t2")), timeout=8.0)
                return time.perf_counter() - started

        try:
            elapsed = self.run_async(run())
        except asyncio.TimeoutError:
            self.fail("streaming bodies deadlocked: the gate covers the body")
        serialised = self.fixture.CHUNKS * self.fixture.DELAY * 2
        self.assertLess(elapsed, serialised * 0.75,
                        "two streaming bodies took %.2fs; serialised would be "
                        "%.2fs, so the gate covered the body phase"
                        % (elapsed, serialised))


if __name__ == "__main__":
    unittest.main()
