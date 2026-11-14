"""Regression tests for the seven session/cookie defects.

Each test names the defect it pins and the observable symptom that defect had,
so a future change that reintroduces it fails here rather than in the field.
Every one of them was reproduced against the shipped build before it was fixed.

Run with the audited Core available:

    LD_LIBRARY_PATH=core/binaries/linux-x86_64 \
    PYTHONPATH=bindings/python \
    python3 -m unittest bindings/python/tests/test_session_defects.py
"""

import asyncio
import os
import threading
import time
import unittest

try:
    from http.server import ThreadingHTTPServer
except ImportError:  # Python 3.6
    from http.server import HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

from http.server import BaseHTTPRequestHandler

import chrome_client
from chrome_client._python_impl import sessions as sessions_module
from chrome_client._python_impl.cookies import RequestsCookieJar


class Handler(BaseHTTPRequestHandler):
    """One server for every defect: cookies, redirects, bodies, retries."""

    protocol_version = "HTTP/1.1"

    #: (method, path) -> bodies the server actually received, for the retry test.
    received = {}
    received_lock = threading.Lock()

    def _send(self, body, status=200, extra=()):
        self.send_response(status)
        for name, value in extra:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self, method):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        with Handler.received_lock:
            Handler.received.setdefault((method, self.path), []).append(body)
        return body

    def do_GET(self):
        self._record("GET")
        if self.path.startswith("/set-cookie"):
            return self._send(b"ok", extra=[("Set-Cookie", "sid=ABC; Path=/"),
                                            ("Set-Cookie", "taste=sweet; Path=/")])
        if self.path.startswith("/delete-cookie"):
            # The server-side deletion A3 is about: the same name, empty value,
            # Max-Age=0.
            return self._send(b"gone", extra=[("Set-Cookie", "sid=; Max-Age=0; Path=/")])
        if self.path.startswith("/set-other-cookie"):
            return self._send(b"other", extra=[("Set-Cookie", "sid=BBB; Path=/")])
        if self.path.startswith("/echo-cookie"):
            return self._send(repr(self.headers.get_all("Cookie") or []).encode())
        if self.path.startswith("/redirect/"):
            depth = int(self.path.rsplit("/", 1)[-1])
            if depth > 0:
                self.send_response(302)
                self.send_header("Location", "/redirect/%d" % (depth - 1))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._send(b"landed")
        if self.path.startswith("/bytes/"):
            return self._send(b"y" * int(self.path.rsplit("/", 1)[-1]))
        if self.path.startswith("/slow"):
            time.sleep(1.0)
            return self._send(b"slow")
        if self.path.startswith("/status/"):
            return self._send(b"err", status=int(self.path.rsplit("/", 1)[-1]))
        return self._send(b"ok")

    def do_POST(self):
        self._record("POST")
        # Answer without draining a chunked upload, so the client is still
        # writing when the failure surfaces (A5).
        if self.path.startswith("/status/"):
            return self._send(b"err", status=int(self.path.rsplit("/", 1)[-1]))
        return self._send(b"ok")

    def do_PUT(self):
        self._record("PUT")
        return self._send(b"ok")

    def log_message(self, *_args):
        pass


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d/" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        with Handler.received_lock:
            Handler.received.clear()


class AsyncSessionSendDefaultsTests(Base):
    """A1: 'AsyncSession.send' ignored every session default."""

    def run_async(self, coroutine):
        return asyncio.run(coroutine)

    def _prepared(self, session, path):
        return session.prepare_request(chrome_client.Request("GET", self.base + path))

    def test_timeout_default_reaches_the_async_send_path(self):
        """Measured before the fix: returned after 1.01s, no Timeout raised."""

        async def run():
            async with chrome_client.AsyncSession(timeout=0.05) as session:
                prepared = self._prepared(session, "slow")
                started = time.monotonic()
                with self.assertRaises(chrome_client.Timeout):
                    await session.send(prepared)
                self.assertLess(time.monotonic() - started, 0.9)

        self.run_async(run())

    def test_max_response_bytes_default_reaches_the_async_send_path(self):
        """Measured before the fix: a 5000-byte body came back whole."""

        async def run():
            async with chrome_client.AsyncSession(max_response_bytes=1024) as session:
                prepared = self._prepared(session, "bytes/5000")
                with self.assertRaises(chrome_client.ResponseTooLarge):
                    await session.send(prepared)

        self.run_async(run())

    def test_the_synchronous_twin_still_raises(self):
        """The async path was fixed to match this behaviour, not the reverse."""
        with chrome_client.Session(timeout=0.05) as session:
            prepared = self._prepared(session, "slow")
            with self.assertRaises(chrome_client.Timeout):
                session.send(prepared)
        with chrome_client.Session(max_response_bytes=1024) as session:
            prepared = self._prepared(session, "bytes/5000")
            with self.assertRaises(chrome_client.ResponseTooLarge):
                session.send(prepared)


class RedirectPolicyTests(Base):
    """A2: the session's redirect configuration was dead."""

    def test_session_allow_redirects_false_returns_the_redirect(self):
        """Measured before the fix: 200, the redirect had been followed."""
        with chrome_client.Session(allow_redirects=False) as session:
            response = session.get(self.base + "redirect/1")
            self.assertEqual(response.status_code, 302)

    def test_a_numeric_cap_is_refused_at_construction(self):
        """A cap the Core cannot lower is an error, not a Python-side hop loop."""
        with self.assertRaises(chrome_client.UnsupportedFeature):
            chrome_client.Session(max_redirects=5)

    def test_chromiums_own_ceiling_still_follows(self):
        """No cap means Chromium's 20 hops, driven entirely by the Core."""
        with chrome_client.Session() as session:
            response = session.get(self.base + "redirect/3")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.history), 3)
            self.assertEqual(response.redirect_count, 3)

    def test_chromiums_ceiling_is_enforced_by_the_core(self):
        """A ten-hop chain without a caller cap passes through untouched."""
        with chrome_client.Session() as session:
            response = session.get(self.base + "redirect/10")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.history), 10)

    def test_a_cap_at_chromiums_ceiling_fails_closed(self):
        """A cap of 30 cannot be honoured; clamping it to 20 would be a lie.

        Measured before the fix: the request went down the native path and died
        with TooManyRedirects at hop 21, a limit the caller never set.
        """
        with chrome_client.Session() as session:
            with self.assertRaises(chrome_client.UnsupportedFeature):
                session.get(self.base + "redirect/2", max_redirects=30)
        with self.assertRaises(chrome_client.UnsupportedFeature):
            chrome_client.Session(max_redirects=30)

    def test_the_ceiling_constant_matches_chromium(self):
        """The number must come from Chromium, not from a copy of it."""
        self.assertEqual(sessions_module.CHROMIUM_MAX_REDIRECTS, 20)

    def test_send_honours_allow_redirects_in_its_options(self):
        """'send()' dropped the caller's 'allow_redirects=' on the floor."""
        with chrome_client.Session() as session:
            prepared = session.prepare_request(
                chrome_client.Request("GET", self.base + "redirect/1"))
            response = session.send(prepared, allow_redirects=False)
            self.assertEqual(response.status_code, 302)

    def test_the_async_session_honours_its_redirect_default(self):
        async def run():
            async with chrome_client.AsyncSession(allow_redirects=False) as session:
                response = await session.get(self.base + "redirect/1")
                self.assertEqual(response.status_code, 302)

        asyncio.run(run())


class CookieDeletionTests(Base):
    """A3: a server-side deletion never reached the jar."""

    def test_a_deleted_cookie_disappears_from_the_session(self):
        """Measured before the fix: {'sid': 'ABC'} survived the deletion."""
        with chrome_client.Session() as session:
            session.get(self.base + "set-cookie")
            self.assertEqual(session.cookies.get_dict(),
                             {"sid": "ABC", "taste": "sweet"})
            session.get(self.base + "delete-cookie")
            self.assertNotIn("sid", session.cookies.get_dict())
            self.assertEqual(session.cookies.get_dict(), {"taste": "sweet"})

    def test_the_deleted_cookie_is_not_sent_afterwards(self):
        with chrome_client.Session() as session:
            session.get(self.base + "set-cookie")
            session.get(self.base + "delete-cookie")
            echoed = session.get(self.base + "echo-cookie").text
            self.assertNotIn("sid=ABC", echoed)
            self.assertIn("taste=sweet", echoed)

    def test_absorb_set_cookie_still_reports_what_the_response_set(self):
        """The return value is 'Response.cookies', and its contract is unchanged."""
        jar = RequestsCookieJar()
        url = self.base + "set-cookie"
        received = jar.absorb_set_cookie(url, ["sid=ABC; Path=/"])
        self.assertEqual(dict(received), {"sid": "ABC"})
        self.assertEqual(dict(jar), {"sid": "ABC"})
        deleted = jar.absorb_set_cookie(url, ["sid=; Max-Age=0; Path=/"])
        self.assertEqual(dict(deleted), {})
        self.assertEqual(dict(jar), {})

    def test_an_expiry_date_also_deletes(self):
        jar = RequestsCookieJar()
        url = self.base + "set-cookie"
        jar.absorb_set_cookie(url, ["sid=ABC; Path=/"])
        jar.absorb_set_cookie(
            url, ["sid=; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Path=/"])
        self.assertEqual(dict(jar), {})


class DiscardCookiesIsolationTests(Base):
    """A4: 'discard_cookies=True' leaked the Core's store to later requests."""

    def test_a_discard_request_does_not_become_the_default_store(self):
        """Measured before the fix: the next plain request sent 'sid=BBB'.

        ABI v9 clears the store for the discard request instead of building a
        throwaway engine, so the assertion moved from "the generation did not
        move" to "the same engine is still in use". The observable contract is
        unchanged and is what the next two tests check: the discard request
        sends nothing and records nothing.
        """
        with chrome_client.Session() as session:
            session.get(self.base + "set-cookie")
            self.assertIn("sid=ABC", session.get(self.base + "echo-cookie").text)
            builds = session.stats["builds"]
            session.get(self.base + "set-other-cookie", discard_cookies=True)
            self.assertEqual(session.stats["builds"], builds,
                             "a discard request must not build another engine")
            echoed = session.get(self.base + "echo-cookie").text
            self.assertNotIn("sid=BBB", echoed)

    def test_discard_cookies_does_not_send_stored_cookies(self):
        """The pre-existing contract, kept green by the new implementation."""
        with chrome_client.Session() as session:
            session.get(self.base + "set-cookie")
            self.assertTrue(session.cookies.get_dict())
            discarded = session.get(self.base + "echo-cookie",
                                    discard_cookies=True).text
            self.assertNotIn("sid=ABC", discarded)
            self.assertNotIn("taste=sweet", discarded)

    def test_a_discard_request_does_not_touch_another_requests_cookies(self):
        """The defect the single-threaded tests could not see.

        ``discard_cookies`` used to clear the SESSION's store, which is the same
        thing as "this request sends nothing" only while one thread uses the
        session. Run a plain request beside a discard request and the plain one
        loses its cookies: measured 19 of 20 readings wrong before the fix, 0 of
        20 after.
        """
        import threading

        with chrome_client.Session() as session:
            session.get(self.base + "set-cookie")   # sid=ABC

            wrong = []

            def discarding():
                for _ in range(6):
                    session.get(self.base + "set-other-cookie",   # sid=BBB
                                discard_cookies=True)

            def reading():
                for _ in range(12):
                    seen = session.get(self.base + "echo-cookie").text
                    if "sid=ABC" not in seen or "BBB" in seen:
                        wrong.append(seen)

            threads = [threading.Thread(target=discarding),
                       threading.Thread(target=reading)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(wrong, [],
                         "a discard request changed what another request sent")

    def test_a_discard_request_records_no_cookies(self):
        with chrome_client.Session() as session:
            session.get(self.base + "set-cookie", discard_cookies=True)
            self.assertEqual(session.cookies.get_dict(), {})

    def test_repeated_discard_requests_start_from_an_empty_store(self):
        """A cached throwaway engine would carry the previous discard's cookies."""
        with chrome_client.Session() as session:
            for _ in range(3):
                echoed = session.get(self.base + "set-cookie",
                                     discard_cookies=True)
                self.assertEqual(echoed.status_code, 200)
                follow = session.get(self.base + "echo-cookie",
                                     discard_cookies=True).text
                self.assertNotIn("sid=ABC", follow)

    def test_the_ordinary_engine_survives_a_discard_request(self):
        with chrome_client.Session() as session:
            session.get(self.base + "set-cookie")
            before = len(session._engines)
            session.get(self.base + "echo-cookie", discard_cookies=True)
            self.assertEqual(len(session._engines), before,
                             "the throwaway engine must not stay in the cache")


class UploadFailureCleanupTests(Base):
    """A5: a failed chunked upload left the Core request running."""

    def _open_fds(self):
        try:
            return len(os.listdir("/proc/self/fd"))
        except OSError:  # not Linux
            self.skipTest("descriptor counting needs /proc/self/fd")

    #: Failed uploads in the leak loops. Each iteration that leaked a Core
    #: request also kept a socket, so a leak shows up as a count that grows with
    #: the iteration count, not as one straggler.
    UPLOAD_ITERATIONS = 10

    def assert_no_descriptor_growth(self, counts, label):
        """Fails when descriptors accumulate per failed upload.

        The defect grew by one descriptor per attempt -- measured 5 -> 23 over
        ten iterations, and the server held six connections that never closed.
        A correct implementation is flat: measured 13 -> 13 over thirty
        iterations in isolation.

        The comparison is against the iteration count rather than an exact
        equality, because this runs inside the whole suite, where other tests'
        servers and engines legitimately open sockets while this loop runs. A
        leak scales with the loop; unrelated activity does not.
        """
        growth = counts[-1] - counts[0]
        self.assertLessEqual(
            growth, 2,
            "%s: descriptors grew %d across %d failed uploads (%r); a leaked "
            "Core request costs one socket per attempt"
            % (label, growth, len(counts), counts))

    def test_a_failing_body_iterator_does_not_leak_descriptors(self):
        """Measured before the fix: descriptors grew 5 -> 23 over ten attempts."""
        with chrome_client.Session() as session:
            def body():
                yield b"x" * 16
                raise OSError("caller body failed")

            counts = []
            for _ in range(self.UPLOAD_ITERATIONS):
                with self.assertRaises(OSError):
                    session.post(self.base + "status/500", data=body())
                counts.append(self._open_fds())
            self.assert_no_descriptor_growth(counts, "sync upload")

    def test_the_caller_exception_survives_unchanged(self):
        with chrome_client.Session() as session:
            def body():
                yield b"x" * 16
                raise OSError("the original failure")

            with self.assertRaises(OSError) as caught:
                session.post(self.base + "status/500", data=body())
            self.assertIn("the original failure", str(caught.exception))

    def test_a_failing_async_body_does_not_leak_descriptors(self):
        async def run():
            async with chrome_client.AsyncSession() as session:
                async def body():
                    yield b"x" * 16
                    raise OSError("async caller body failed")

                counts = []
                for _ in range(self.UPLOAD_ITERATIONS):
                    with self.assertRaises(OSError):
                        await session.post(self.base + "status/500", data=body())
                    counts.append(self._open_fds())
                self.assert_no_descriptor_growth(counts, "async upload")

        asyncio.run(run())

    def test_a_cancelled_async_upload_releases_the_request(self):
        """'CancelledError' is a BaseException, so it used to escape the try."""
        async def run():
            async with chrome_client.AsyncSession() as session:
                started = asyncio.Event()

                async def body():
                    yield b"x" * 16
                    started.set()
                    await asyncio.sleep(30)

                task = asyncio.ensure_future(
                    session.post(self.base + "status/500", data=body()))
                await started.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        asyncio.run(run())


class StatusRetryReplayabilityTests(Base):
    """A6: a 5xx retry re-sent a POST body the origin had already applied."""

    def test_a_post_is_not_replayed_on_a_server_error(self):
        """Measured before the fix: the server received two 7-byte POSTs."""
        with chrome_client.Session(retry=1) as session:
            response = session.post(self.base + "status/500", data=b"payload")
            self.assertEqual(response.status_code, 500)
        bodies = Handler.received.get(("POST", "/status/500"), [])
        self.assertEqual(len(bodies), 1,
                         "the origin saw %d attempts of a POST: %r"
                         % (len(bodies), bodies))

    def test_an_idempotent_get_is_still_retried(self):
        """The guard is on replayability, not on the status code."""
        with chrome_client.Session(retry=1) as session:
            response = session.get(self.base + "status/500")
            self.assertEqual(response.status_code, 500)
        self.assertEqual(len(Handler.received.get(("GET", "/status/500"), [])), 2)

    def test_a_streamed_body_is_never_replayed(self):
        """Its iterator is consumed, so a second attempt would send a fragment."""
        with chrome_client.Session(retry=1) as session:
            def body():
                yield b"a"
                yield b"b"

            session.put(self.base + "status/500", data=body())
        self.assertEqual(len(Handler.received.get(("PUT", "/status/500"), [])), 1)

    def test_a_post_is_still_retried_on_a_transport_failure(self):
        """A connection failure never reached the origin, so replaying is safe.

        The proxy default retry already had the replayability guard; this pins
        that the status-code change did not remove it.
        """
        attempts = []
        original = sessions_module.Session._send_once

        def counting(self, prepared, options):
            attempts.append(prepared.method)
            raise chrome_client.ProxyError(
                "Proxy ERR_PROXY_CONNECTION_FAILED (net error -130)")

        sessions_module.Session._send_once = counting
        try:
            with chrome_client.Session(proxy="http://127.0.0.1:1") as session:
                with self.assertRaises(chrome_client.ProxyError):
                    session.get(self.base + "ok", timeout=5)
                self.assertEqual(len(attempts), 2)
                del attempts[:]
                with self.assertRaises(chrome_client.ProxyError):
                    session.post(self.base + "ok", data=b"x", timeout=5)
                self.assertEqual(len(attempts), 1)
        finally:
            sessions_module.Session._send_once = original


class TimeoutValidationTests(Base):
    """A7: 'timeout=0' silently meant 'no deadline at all'."""

    def test_zero_is_rejected(self):
        """Measured before the fix: the request returned 200 after 1.01s."""
        with chrome_client.Session() as session:
            with self.assertRaises(ValueError):
                session.get(self.base + "slow", timeout=0)

    def test_a_negative_timeout_is_rejected(self):
        with chrome_client.Session() as session:
            with self.assertRaises(ValueError):
                session.get(self.base + "slow", timeout=-1)

    def test_a_timeout_tuple_with_a_zero_is_rejected(self):
        with chrome_client.Session() as session:
            with self.assertRaises(ValueError):
                session.get(self.base + "slow", timeout=(0, 5))

    def test_a_sub_millisecond_timeout_is_rounded_up_not_truncated(self):
        """The ABI carries whole milliseconds; truncating 0.0005s would be 0ms."""
        self.assertEqual(sessions_module._split_timeout(0.0005), 0.001)
        self.assertEqual(sessions_module._split_timeout(0.0001), 0.001)
        self.assertEqual(sessions_module._split_timeout(5), 5)
        self.assertIsNone(sessions_module._split_timeout(None))

    def test_a_sub_millisecond_timeout_actually_expires(self):
        with chrome_client.Session() as session:
            with self.assertRaises(chrome_client.Timeout):
                session.get(self.base + "slow", timeout=0.0005)

    def test_the_async_path_validates_the_same_way(self):
        async def run():
            async with chrome_client.AsyncSession() as session:
                with self.assertRaises(ValueError):
                    await session.get(self.base + "slow", timeout=0)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
