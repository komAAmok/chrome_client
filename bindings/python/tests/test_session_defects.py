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
import contextlib
import os
import pathlib
import tempfile
import socket
import socketserver
import struct
import threading
import time
import unittest
from urllib.parse import quote, unquote

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
from chrome_client._python_impl.exceptions import map_native_error
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
        if self.path.startswith("/echo-header/"):
            return self._send(str(self.headers.get(self.path.rsplit("/", 1)[-1]) or "").encode())
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
        if self.path.startswith("/latin1"):
            # No charset parameter: RFC 2616 makes text/* ISO-8859-1 by default,
            # so the stream and .text paths must agree on decoding it.
            body = "caf\u00e9 na\u00efve".encode("iso-8859-1")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
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

    def test_a_numeric_cap_is_not_a_parameter_anymore(self):
        """Chromium owns the ceiling, so the option was removed, not faked."""
        with self.assertRaises(TypeError):
            chrome_client.Session(max_redirects=5)
        with self.assertRaises(TypeError):
            chrome_client.Session().get(self.base + "redirect/2", max_redirects=5)

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

    def test_chromiums_ceiling_is_the_only_cap(self):
        """Measured before the removal: a cap of 30 died at hop 21.

        The number comes from Chromium (net::URLRequest::kMaxRedirects), so the
        facade publishes it as a constant and accepts no per-call override.
        """
        self.assertEqual(sessions_module.CHROMIUM_MAX_REDIRECTS, 20)
        with chrome_client.Session() as session:
            with self.assertRaises(TypeError):
                session.get(self.base + "redirect/2", max_redirects=30)

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




class NoProxyWildcardTests(unittest.TestCase):
    """``NO_PROXY`` spellings that must exclude every host.

    Both used to fail OPEN: the request went through the proxy the caller had
    excluded, which is the one direction a bypass list must never get wrong.
    """

    def test_a_star_excludes_every_host(self):
        from chrome_client._python_impl.utils import should_bypass_proxies

        for url in ("http://example.com/x", "https://a.b.c:8443/y",
                    "http://10.0.0.5/"):
            self.assertTrue(should_bypass_proxies(url, no_proxy="*"), url)

    def test_a_star_in_the_environment_reaches_the_session(self):
        """The whole path: environment, proxy resolution, chosen transport."""
        import os

        saved = {name: os.environ.get(name) for name in
                 ("HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy")}
        os.environ["http_proxy"] = "http://proxy.invalid:3128"
        os.environ["HTTP_PROXY"] = "http://proxy.invalid:3128"
        os.environ["no_proxy"] = "*"
        os.environ["NO_PROXY"] = "*"
        try:
            with chrome_client.Session() as session:
                self.assertIsNone(
                    session._effective_proxy("http://example.com/x", None, None),
                    "NO_PROXY=* still routed the request through a proxy")
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_a_leading_dot_is_a_suffix(self):
        from chrome_client._python_impl.utils import should_bypass_proxies

        self.assertTrue(should_bypass_proxies("http://example.com/x",
                                              no_proxy=".example.com"))
        self.assertTrue(should_bypass_proxies("http://sub.example.com/x",
                                              no_proxy=".example.com"))
        self.assertFalse(should_bypass_proxies("http://other.com/x",
                                               no_proxy=".example.com"))

    def test_a_lone_dot_does_not_match_everything(self):
        """Stripping the dot from a bare "." would leave an empty entry."""
        from chrome_client._python_impl.utils import should_bypass_proxies

        self.assertFalse(should_bypass_proxies("http://example.com/x",
                                               no_proxy="."))

class HeaderDeletionRecordTests(unittest.TestCase):
    """A header set must not record a deletion it was never asked for.

    ``Headers`` records removals so they survive the merge with the session
    defaults, and two things read that record: ``_reject_core_owned_removals``
    refuses the request over it, and ``merge_setting`` lets it shadow a
    session-level value. Replacing a field used to record a removal as a side
    effect, so a caller who built headers the ordinary way silently lost the
    session's value for that name.
    """

    def test_replacing_a_field_is_not_a_deletion(self):
        from chrome_client._python_impl.structures import Headers

        headers = Headers({"User-Agent": "one"})
        headers.update(Headers({"User-Agent": "two"}))
        self.assertEqual(sorted(headers.deleted_names), [])
        self.assertEqual(headers["user-agent"], "two")

    def test_assignment_clears_an_earlier_deletion(self):
        from chrome_client._python_impl.structures import Headers

        headers = Headers({"X-Header": None})
        self.assertEqual(sorted(headers.deleted_names), ["x-header"])
        headers["X-Header"] = "back"
        self.assertEqual(sorted(headers.deleted_names), [])
        self.assertEqual(headers["x-header"], "back")

    def test_a_deletion_survives_a_copy(self):
        """The facade copies headers on the way to the wire."""
        from chrome_client._python_impl.structures import Headers

        original = Headers({"Sec-Fetch-User": None})
        for clone in (original.copy(), Headers(original)):
            self.assertEqual(sorted(clone.deleted_names), ["sec-fetch-user"],
                             "the copy forgot which field was removed")

    def test_a_deletion_reaches_the_store_merge(self):
        from chrome_client._python_impl.sessions import merge_setting
        from chrome_client._python_impl.structures import Headers

        session = Headers({"Accept": "session", "X-Keep": "k"})
        request = Headers({"Accept": "one"})
        request.update(Headers({"Accept": "two"}))
        merged = merge_setting(request, session, dict_class=Headers)
        self.assertEqual(merged["accept"], "two",
                         "the session value replaced the caller's override")
        self.assertEqual(merged["x-keep"], "k")

    def test_a_deletion_the_caller_asked_for_still_applies(self):
        from chrome_client._python_impl.sessions import merge_setting
        from chrome_client._python_impl.structures import Headers

        session = Headers({"X-Gone": "yes"})
        merged = merge_setting(Headers({"X-Gone": None}), session,
                               dict_class=Headers)
        self.assertNotIn("x-gone", merged)

    def test_duplicates_survive_an_update(self):
        from chrome_client._python_impl.structures import Headers

        target = Headers({"A": "1"})
        source = Headers()
        source.add("A", "2")
        source.add("A", "3")
        target.update(source)
        self.assertEqual(target.get_list("a"), ["2", "3"])



class DiscardDoesNotRewriteTheCallersRequestTests(unittest.TestCase):
    """``discard_cookies`` must not edit the request object it was given.

    ``send()`` takes a ``PreparedRequest`` the caller owns and may send again.
    The Cookie header used to be removed from that very object, so a caller who
    sent once with ``discard_cookies=True`` and once without got no Cookie
    header on the second call either: measured, the caller's header was gone
    before the second request was built. ``requests`` never mutates a request it
    sends.
    """

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d/" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_the_cookie_header_survives_a_discard_send(self):
        with chrome_client.Session() as session:
            request = chrome_client.Request(
                "GET", self.base + "echo-cookie",
                headers={"Cookie": "caller=1"}).prepare()
            session.send(request, discard_cookies=True)
            self.assertEqual(request.headers.get("Cookie"), "caller=1",
                             "the discard request rewrote the caller's header")
            echoed = session.send(request).text
            self.assertIn("caller=1", echoed,
                          "the second send carried no Cookie header")

    def test_a_discard_send_still_sends_no_cookie(self):
        with chrome_client.Session() as session:
            session.cookies.set("sid", "ABC", domain="127.0.0.1", path="/")
            request = chrome_client.Request(
                "GET", self.base + "echo-cookie",
                headers={"Cookie": "caller=1"}).prepare()
            echoed = session.send(request, discard_cookies=True).text
            self.assertNotIn("caller=1", echoed)
            self.assertNotIn("sid=ABC", echoed)

    def test_the_async_path_leaves_the_request_alone_too(self):
        async def run():
            async with chrome_client.AsyncSession() as session:
                request = chrome_client.Request(
                    "GET", self.base + "echo-cookie",
                    headers={"Cookie": "caller=1"}).prepare()
                await session.send(request, discard_cookies=True)
                self.assertEqual(request.headers.get("Cookie"), "caller=1")
                self.assertIn("caller=1", (await session.send(request)).text)

        asyncio.run(run())


class TextEncodingAgreementTests(unittest.TestCase):
    """``.text`` and ``iter_content(decode_unicode=True)`` must agree.

    A ``text/*`` response that names no charset is ISO-8859-1 by RFC 2616, and
    that is what requests answers. ``get_encoding_from_headers`` reported
    ``None``, so the streaming path fell through to the apparent-encoding guess
    and decoded the body as UTF-8 -- one response answered two ways, with U+FFFD
    where the accents were.
    """

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d/" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_a_latin1_text_body_decodes_the_same_both_ways(self):
        with chrome_client.Session() as session:
            streamed = b"".join(
                piece.encode("utf-8") for piece in session.get(
                    self.base + "latin1", stream=True).iter_content(
                        4, decode_unicode=True)).decode("utf-8")
            eager = session.get(self.base + "latin1").text
            self.assertEqual(streamed, eager,
                             "the stream and .text decoded the same body differently")
            self.assertNotIn("\ufffd", streamed)

    def test_the_charset_is_reported_for_a_text_type(self):
        from chrome_client.utils import get_encoding_from_headers

        self.assertEqual(
            get_encoding_from_headers({"content-type": "text/plain"}),
            "ISO-8859-1")
        self.assertEqual(
            get_encoding_from_headers({"content-type": "application/json"}),
            "utf-8")
        self.assertIsNone(
            get_encoding_from_headers({"content-type": "application/octet-stream"}))


class UrlHelperTests(unittest.TestCase):
    """Small helpers whose divergence from requests was observable."""

    def test_a_bare_host_with_a_port_keeps_being_a_host(self):
        from chrome_client.utils import prepend_scheme_if_needed

        self.assertEqual(prepend_scheme_if_needed("example.com:8080/x", "http"),
                         "http://example.com:8080/x")
        self.assertEqual(prepend_scheme_if_needed("example.com/x", "http"),
                         "http://example.com/x")
        self.assertEqual(prepend_scheme_if_needed("https://a.example/x", "http"),
                         "https://a.example/x")

    def test_an_is_ipv4_answer_follows_the_parser(self):
        from chrome_client.utils import is_ipv4_address

        self.assertTrue(is_ipv4_address("1.2.3"))
        self.assertTrue(is_ipv4_address("127.0.0.1"))
        self.assertFalse(is_ipv4_address("example.com"))

    def test_a_two_null_sample_that_is_not_utf16_identifies_nothing(self):
        from chrome_client.utils import guess_json_utf

        self.assertIsNone(guess_json_utf(b"\x00a\x00\x00"))
        self.assertEqual(guess_json_utf(b"\x00a\x00b"), "utf-16-be")
        self.assertEqual(guess_json_utf(b"a\x00b\x00"), "utf-16-le")



class PerRequestHttpVersionTests(unittest.TestCase):
    """A per-call ``http_version=`` has to be normalised like the session one.

    ``Session(http_version="h2")`` was always accepted, because the constructor
    normalises. A per-call value used to reach the Core verbatim, so
    ``get(url, http_version="h2")`` failed with "unknown http version" while the
    same value worked on the constructor. It also split the engine cache: "v2",
    "h2" and "HTTP/2" are one protocol but were three keys, so one session built
    three contexts for the same wire identity.
    """

    def _binding(self, value, **session_kwargs):
        with chrome_client.Session(**session_kwargs) as session:
            return session._engine_config(None, None, None, value)

    def test_every_spelling_resolves_to_the_same_pin(self):
        expected = self._binding("v2").http_version
        self.assertEqual(expected, "v2")
        for spelling in ("v2", "h2", "HTTP/2", "2", "2.0"):
            self.assertEqual(self._binding(spelling).http_version, expected,
                             "http_version=%r did not normalise" % (spelling,))

    def test_equivalent_spellings_share_one_engine_key(self):
        self.assertEqual(self._binding("v2").key, self._binding("h2").key,
                         "one protocol was two engine cache keys")
        self.assertEqual(self._binding("v1").key, self._binding("1.1").key)

    def test_an_unsupported_value_still_fails_closed(self):
        with self.assertRaises(chrome_client.UnsupportedFeature):
            self._binding("nonsense")

    def test_omitting_it_keeps_the_session_pin(self):
        pinned = self._binding(None, http_version="v1")
        self.assertEqual(pinned.http_version, "v1")
        overridden = self._binding("v2", http_version="v1")
        self.assertEqual(overridden.http_version, "v2")



class EngineBudgetValidationTests(unittest.TestCase):
    """`max_engines` follows the same validation rule as the other limits.

    ``int(max_engines)`` accepted three things it should not: ``None`` raised a
    bare ``TypeError`` from inside the cache, ``"8"`` was silently accepted, and
    ``0`` or a negative count became 1 without saying so. The sibling limits all
    reject a wrong type, so this one does too.
    """

    def _cache(self, value):
        from chrome_client.engine import EngineCache

        return EngineCache(value)

    def test_a_valid_ceiling_is_kept(self):
        self.assertEqual(self._cache(8).max_engines, 8)
        self.assertEqual(self._cache(1).max_engines, 1)

    def test_a_non_positive_ceiling_is_refused(self):
        for value in (0, -1):
            with self.assertRaises(ValueError):
                self._cache(value)

    def test_a_wrong_type_is_refused_as_a_value_error(self):
        """`None` used to escape as a bare TypeError from inside the cache."""
        for value in (None, "8", True, 2.5):
            with self.assertRaises(ValueError):
                self._cache(value)

    def test_the_session_default_is_unchanged(self):
        with chrome_client.Session() as session:
            self.assertEqual(session.stats["max_engines"], 8)



class ClosedSessionExceptionTests(unittest.TestCase):
    """Every closed-session path raises `SessionClosed`, not the base class.

    `BaseSession._ensure_open` raises `SessionClosed`, but the cookie jar and
    the engine cache reach the closed state through their own lookup and used to
    raise a bare `RequestException`. Measured: after `close()`,
    `session.cookies.set(...)` raised `RequestException`, so a caller writing
    `except SessionClosed` around it never matched.
    """

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d/" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _closed(self):
        session = chrome_client.Session()
        session.get(self.base)
        session.close()
        return session

    def test_a_cookie_assignment_after_close(self):
        session = self._closed()
        with self.assertRaises(chrome_client.SessionClosed):
            session.cookies.set("a", "1", domain="127.0.0.1", path="/")

    def test_a_cookie_clear_after_close(self):
        session = self._closed()
        with self.assertRaises(chrome_client.SessionClosed):
            session.cookies.clear()

    def test_an_engine_lookup_after_close(self):
        session = self._closed()
        with self.assertRaises(chrome_client.SessionClosed):
            session._engines.get(session._engine_config(None, None, None, None))

    def test_a_request_after_close_still_raises_the_same_class(self):
        session = self._closed()
        with self.assertRaises(chrome_client.SessionClosed):
            session.get(self.base)



class CaBundleErrorTests(unittest.TestCase):
    """A `verify=` CA bundle that cannot be used reports an SSL failure.

    Two paths were wrong, and both pointed the caller at the wrong thing:

    * an unreadable path escaped as a bare `FileNotFoundError` from `open()`,
      which is not a `RequestException`, so `except RequestException` around a
      request missed it, and the message named no option;
    * a readable but malformed PEM reached the Core, which answers a bare
      `InvalidArgument` -- the same token it uses for a malformed URL -- so the
      caller was told its URL was wrong.
    """

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d/" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _pem(self, text):
        handle = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False)
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_a_missing_ca_file_raises_ssl_error(self):
        """The path is read on the way to the engine, whichever branch names it.

        ``verify=`` reaches the loader twice: the per-call value, and the
        session value used when the call omits it. Both go through the same
        helper, and this covers both, because a mutation of either one alone
        leaves the other passing.
        """
        with chrome_client.Session(verify="/nonexistent/ca.pem") as session:
            with self.assertRaises(chrome_client.SSLError) as caught:
                session.get(self.base)
        self.assertIn("verify=", str(caught.exception))

    def test_a_missing_ca_file_raises_ssl_error_from_the_call_site(self):
        with chrome_client.Session() as session:
            with self.assertRaises(chrome_client.SSLError):
                session.get(self.base, verify="/nonexistent/ca.pem")

    def test_a_malformed_ca_file_raises_ssl_error(self):
        path = self._pem("not a pem\n")
        with chrome_client.Session(verify=path) as session:
            with self.assertRaises(chrome_client.SSLError):
                session.get(self.base)

    def test_neither_case_reports_the_url_as_the_problem(self):
        """The old classes were FileNotFoundError and InvalidURL."""
        path = self._pem("not a pem\n")
        for setting in ("/nonexistent/ca.pem", path):
            with chrome_client.Session(verify=setting) as session:
                try:
                    session.get(self.base)
                except chrome_client.InvalidURL:
                    self.fail("a CA problem was reported as a URL problem")
                except chrome_client.SSLError:
                    pass

    def test_a_real_bundle_is_still_accepted(self):
        """Rejecting every bundle would pass the tests above for the wrong reason."""
        import ssl

        try:
            paths = ssl.get_default_verify_paths()
        except AttributeError:
            self.skipTest("ssl.get_default_verify_paths is unavailable")
        bundle = paths.cafile or paths.openssl_cafile
        if not bundle or not os.path.exists(bundle):
            self.skipTest("no system CA bundle on this host")
        with chrome_client.Session(verify=bundle) as session:
            binding = session._binding(None, None, None, None)
            self.assertIsNotNone(binding.ca_pem)
            self.assertEqual(session.get(self.base).status_code, 200)



class NoneMeansDefaultTests(unittest.TestCase):
    """`None` on a documented flag means "keep the default", never "turn it off".

    Every one of these options is documented as a flag whose default is `True`,
    and none of them documents `None`. `bool(None)` is `False`, so passing one
    silently selected the off state: measured, `verify=None` disabled
    certificate verification, `cache=None` disabled the HTTP cache, and
    `default_headers=None` dropped every captured browser header.
    """

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d/" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_verify_none_does_not_disable_verification(self):
        with chrome_client.Session(verify=None) as session:
            self.assertTrue(session._binding(None, None, None, None).verify)

    def test_cache_none_does_not_disable_the_cache(self):
        with chrome_client.Session(cache=None) as session:
            self.assertTrue(session._binding(None, None, None, None).cache)

    def test_trust_env_none_does_not_ignore_the_environment(self):
        import os

        saved = os.environ.get("HTTP_PROXY")
        os.environ["HTTP_PROXY"] = "http://env.invalid:3128"
        try:
            with chrome_client.Session(trust_env=None) as session:
                self.assertEqual(
                    session._effective_proxy(self.base, None, None),
                    "http://env.invalid:3128")
        finally:
            if saved is None:
                os.environ.pop("HTTP_PROXY", None)
            else:
                os.environ["HTTP_PROXY"] = saved

    def test_default_headers_none_still_injects_the_profile_set(self):
        """`default_headers=None` used to drop all twelve captured headers."""
        with chrome_client.Session(impersonate="chrome_155",
                                   default_headers=None) as session:
            self.assertTrue(session.default_headers)
            prepared = session.prepare_request(
                chrome_client.Request("GET", self.base))
            names = {name.lower() for name, _ in prepared.wire_headers()}
        self.assertIn("sec-ch-ua", names)
        self.assertIn("sec-fetch-mode", names)

    def test_an_explicit_false_is_still_off(self):
        """The fix must not turn False into True."""
        with chrome_client.Session(verify=False) as session:
            self.assertFalse(session._binding(None, None, None, None).verify)
        with chrome_client.Session(cache=False) as session:
            self.assertFalse(session._binding(None, None, None, None).cache)
        with chrome_client.Session(impersonate="chrome_155",
                                   default_headers=False) as session:
            self.assertFalse(session.default_headers)
            prepared = session.prepare_request(
                chrome_client.Request("GET", self.base))
            names = {name.lower() for name, _ in prepared.wire_headers()}
        self.assertNotIn("sec-ch-ua", names)



class ProxyChainValidationTests(unittest.TestCase):
    """`proxy_chain=` is validated where it is decided, not by the Core.

    Two problems shared one root cause: the shape of `proxy_chain=` was never
    checked here, so a bad value reached the Core, which gives up and leaves the
    context unbuilt. The caller then saw `InitializationFailed` naming nothing.

    * `proxy=` together with `proxy_chain=` is rejected by the ABI ("they
      describe the connection path twice"), but that rejection arrived with the
      FIRST REQUEST as `InvalidURL` / `InvalidArgument` -- a URL error for a
      configuration conflict.
    * a malformed entry (`"not a proxy"`, `"http://a"`) produced the same
      `InitializationFailed` as any other engine failure.
    """

    def test_proxy_and_proxy_chain_together_are_refused(self):
        with self.assertRaises(chrome_client.InvalidProxyURL) as caught:
            chrome_client.Session(proxy="http://x:1", proxy_chain="http://y:1")
        self.assertIn("proxy_chain", str(caught.exception))

    def test_the_refusal_does_not_wait_for_a_request(self):
        """The old failure surfaced from get(), as InvalidURL."""
        try:
            chrome_client.Session(proxy="http://x:1", proxy_chain="http://y:1")
        except chrome_client.InvalidURL as error:
            if not isinstance(error, chrome_client.InvalidProxyURL):
                self.fail("the conflict was reported as a URL problem")
        except chrome_client.InvalidProxyURL:
            pass

    def test_a_malformed_entry_names_the_entry(self):
        for entry in ("not a proxy", "http://a", "http://a:x", "a:1"):
            with self.assertRaises(chrome_client.UnsupportedFeature) as caught:
                chrome_client.Session(proxy_chain=entry)
            self.assertIn("proxy_chain", str(caught.exception), entry)

    def test_an_unsupported_scheme_names_the_scheme(self):
        with self.assertRaises(chrome_client.UnsupportedFeature) as caught:
            chrome_client.Session(proxy_chain="ftp://a:1")
        self.assertIn("ftp", str(caught.exception))

    def test_every_accepted_scheme_still_works(self):
        for chain in ("http://a:1", "https://a:1", "socks4://a:1",
                      "socks5://a:1", "http://a:1,http://b:2",
                      "http://[::1]:8080"):
            with chrome_client.Session(proxy_chain=chain) as session:
                self.assertEqual(session.proxy_chain, chain)

    def test_either_option_alone_is_still_accepted(self):
        with chrome_client.Session(proxy="http://x:1") as session:
            self.assertEqual(session.proxy, "http://x:1")
            self.assertIsNone(session.proxy_chain)
        with chrome_client.Session(proxy_chain="http://y:1") as session:
            self.assertIsNone(session.proxy)
            self.assertEqual(session.proxy_chain, "http://y:1")



class AddressInvalidIsANetworkErrorTests(unittest.TestCase):
    """ERR_ADDRESS_INVALID must stay inside the network hierarchy.

    `core/source/minicronet/error_mapping.h` routes -108 to `MN_ERROR_NETWORK`,
    but the binding table refined it to `InvalidURL`, which is not a
    `ConnectionError` subclass -- so `except ConnectionError` missed it. requests
    raises `ConnectionError` for the same address, so the refinement was also a
    compatibility difference, not only a hierarchy one.
    """

    def test_address_invalid_maps_into_the_network_hierarchy(self):
        error = map_native_error(RuntimeError("Network net error -108"))
        self.assertIsInstance(error, chrome_client.ConnectionError)
        self.assertNotIsInstance(error, chrome_client.InvalidURL)

    def test_the_name_survives_the_class_change(self):
        error = map_native_error(RuntimeError("Network net error -108"))
        self.assertIn("ERR_ADDRESS_INVALID", str(error))

    def test_a_disallowed_port_is_still_a_url_problem(self):
        """-312 is a URL configuration error, unlike -108."""
        error = map_native_error(RuntimeError("Network net error -312"))
        self.assertIsInstance(error, chrome_client.InvalidURL)
        self.assertNotIsInstance(error, chrome_client.ConnectionError)



class _HeaderEchoServer(object):
    """Records the headers of every request it receives."""

    def __init__(self):
        self._seen = {}
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(8)
        self.port = self._socket.getsockname()[1]
        self._stop = False
        worker = threading.Thread(target=self._serve)
        worker.daemon = True
        worker.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def headers_for(self, path):
        return self._seen.get(path, {})

    def _serve(self):
        while not self._stop:
            try:
                client, _ = self._socket.accept()
            except OSError:
                return
            try:
                raw = b""
                while b"\r\n\r\n" not in raw:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    raw += chunk
                head = raw.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1")
                lines = head.split("\r\n")
                target = lines[0].split(" ")[1]
                headers = {}
                for line in lines[1:]:
                    if ":" in line:
                        name, value = line.split(":", 1)
                        headers[name.strip()] = value.strip()
                self._seen[target] = headers
                if target.startswith("/redirect"):
                    query = target.split("?", 1)[1] if "?" in target else ""
                    to = unquote(query.split("to=", 1)[1]) if "to=" in query else "/"
                    # Duplicate Set-Cookie and a mixed-case name: a
                    # re-parser can lose either, so the hop has to carry
                    # what the Core parsed.
                    client.sendall(("HTTP/1.1 302 Found\r\nLocation: %s\r\n"
                                    "Set-Cookie: a=1\r\n"
                                    "Set-Cookie: b=2\r\n"
                                    "X-MiXeD-Case: v\r\n"
                                    "Content-Length: 0\r\n\r\n" % to).encode())
                else:
                    client.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            except OSError:
                pass
            finally:
                try:
                    client.close()
                except OSError:
                    pass

    def close(self):
        self._stop = True
        try:
            self._socket.close()
        except OSError:
            pass


class CrossOriginRedirectKeepsAuthorizationTests(unittest.TestCase):
    """Pins a known Core gap, not a desired behaviour.

    Chromium removes sensitive headers one layer above `//net`, in
    `services/network/url_loader.cc:1025`, by passing `removed_headers` to
    `FollowDeferredRedirect()`. The Core calls that with `std::nullopt`
    (`core/source/request.cc:350`), so `Authorization` survives a cross-origin
    hop and reaches the new host. Measured against requests, which drops it.

    Asserting the CURRENT behaviour keeps the gap visible: when the Core starts
    stripping, this test fails and must be inverted rather than deleted.
    `docs/COMPATIBILITY_BOUNDARY.md` records the limitation and the workaround.
    """

    def setUp(self):
        self.first = _HeaderEchoServer()
        self.second = _HeaderEchoServer()

    def tearDown(self):
        self.first.close()
        self.second.close()

    def _url(self):
        return self.first.url + "/redirect?to=" + quote(self.second.url + "/dest", safe="")

    def test_a_cross_origin_hop_still_carries_authorization(self):
        with chrome_client.Session() as session:
            session.get(self._url(), headers={"Authorization": "Bearer probe"},
                        timeout=5)
        self.assertEqual(
            self.second.headers_for("/dest").get("Authorization"), "Bearer probe",
            "the Core now strips it; invert this test instead of deleting it")

    def test_a_same_origin_hop_keeps_it_too(self):
        """Same origin keeps it, which is correct and must stay that way."""
        url = self.first.url + "/redirect?to=" + quote("/dest", safe="")
        with chrome_client.Session() as session:
            session.get(url, headers={"Authorization": "Bearer probe"}, timeout=5)
        self.assertEqual(
            self.first.headers_for("/dest").get("Authorization"), "Bearer probe")

    def test_not_following_the_hop_keeps_the_header_at_home(self):
        """The documented workaround: stop at the 3xx and replay deliberately."""
        with chrome_client.Session() as session:
            response = session.get(self._url(), headers={"Authorization": "Bearer probe"},
                                   allow_redirects=False, timeout=5)
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.second.headers_for("/dest").get("Authorization"))



class Abi12RedirectTests(unittest.TestCase):
    """ABI 12: structured redirect fields, and opt-in credential stripping.

    Two gaps closed together because they share one Core hook:

    * `mn_request_redirect_fn` now carries `mn_header_t` fields, so a hop no
      longer needs its raw block re-split in Python -- the same duplication
      `on_response` stopped doing earlier.
    * A cross-origin hop used to send the caller's `Authorization` to the new
      host, which requests does not do. The removal is an explicit opt-in
      (`strip_authorization=True`) because ABI 11 callers never got it and a
      silent behaviour change is worse than the gap.
    """

    def setUp(self):
        self.first = _HeaderEchoServer()
        self.second = _HeaderEchoServer()

    def tearDown(self):
        self.first.close()
        self.second.close()

    def _cross_origin(self):
        return (self.first.url + "/redirect?to="
                + quote(self.second.url + "/dest", safe=""))

    def test_a_hop_carries_parsed_fields_including_duplicates(self):
        url = self.first.url + "/redirect?to=" + quote("/dest", safe="")
        with chrome_client.Session() as session:
            response = session.get(url, timeout=5)
        hop = response.history[0]
        self.assertEqual(hop.status_code, 302)
        # Two Set-Cookie lines must both survive, in arrival order.
        self.assertEqual(hop.headers.get_list("set-cookie"), ["a=1", "b=2"])
        self.assertEqual(hop.headers.get("X-MiXeD-Case"), "v")

    def test_stripping_is_opt_in_and_defaults_off(self):
        with chrome_client.Session() as session:
            session.get(self._cross_origin(),
                        headers={"Authorization": "Bearer probe"}, timeout=5)
        self.assertEqual(
            self.second.headers_for("/dest").get("Authorization"), "Bearer probe",
            "the default must keep the ABI 11 behaviour")

    def test_strip_authorization_drops_it_on_a_cross_origin_hop(self):
        with chrome_client.Session(strip_authorization=True) as session:
            session.get(self._cross_origin(),
                        headers={"Authorization": "Bearer probe"}, timeout=5)
        self.assertIsNone(
            self.second.headers_for("/dest").get("Authorization"))

    def test_a_same_origin_hop_keeps_the_header_even_when_stripping(self):
        url = self.first.url + "/redirect?to=" + quote("/dest", safe="")
        with chrome_client.Session(strip_authorization=True) as session:
            session.get(url, headers={"Authorization": "Bearer probe"}, timeout=5)
        self.assertEqual(
            self.first.headers_for("/dest").get("Authorization"), "Bearer probe")


@contextlib.contextmanager
def _patched(original, replacement):
    """Replaces one attribute for the duration of the block, then restores it."""
    chrome_client._python_impl.sessions.BaseSession._native_request = replacement
    try:
        yield
    finally:
        chrome_client._python_impl.sessions.BaseSession._native_request = original


class EngineOptionForwardingTests(unittest.TestCase):
    """A constructor option must reach the Core, not just the session object.

    ``take_body_buffers`` was stored on the session and never passed to
    ``engine.request()``, so the documented zero-copy option did nothing. No
    gate could see it: both behaviours return identical bytes, and the option
    was accepted without complaint. So the assertion is on the ARGUMENT the
    engine receives -- reading ``session.take_body_buffers`` would pass even
    with the forwarding deleted, which is exactly the mistake that produced the
    defect.
    """

    def _forwarded(self, **session_options):
        """Returns the kwargs ``engine.request`` received for one call."""
        recorded = {}

        class _Engine:
            def request(self, *args, **kwargs):
                recorded.update(kwargs)
                recorded["positional"] = args
                raise chrome_client.RequestException("stub")

        class _Slot:
            engine = _Engine()

        with chrome_client.Session(**session_options) as session:
            prepared = session.prepare_request(
                chrome_client.Request("GET", "http://127.0.0.1:1/"))
            with self.assertRaises(chrome_client.RequestException):
                session._native_request(_Slot(), prepared, None, True, None, None)
        return recorded

    def test_take_body_buffers_reaches_the_engine(self):
        recorded = self._forwarded(take_body_buffers=True)
        self.assertIs(recorded.get("take_body_buffers"), True,
                      "take_body_buffers must reach engine.request()")

    def test_the_off_default_is_forwarded_too(self):
        recorded = self._forwarded()
        self.assertIs(recorded.get("take_body_buffers"), False,
                      "the off value must be passed explicitly, not omitted")

    def test_strip_authorization_reaches_the_engine(self):
        recorded = self._forwarded(strip_authorization=True)
        self.assertIn(True, recorded.get("positional", ()),
                      "strip_authorization is the eleventh positional argument")


class Py36BindingShapeTests(unittest.TestCase):
    """The two extensions must hand the shared facade the same shapes.

    They are separate crates on separate pyo3 versions and nothing compared
    them: the 3.6 extension declared a different engine constructor, a different
    request signature and different event arities, so every request through it
    raised TypeError while tools/audit-python36-artifact.py still passed. These
    compare the DECLARED PARAMETER LISTS, not substrings: a substring test
    passes on a tree where the parameter was deleted but the name still appears
    in an assignment two lines below, which is what the first version of this
    class did.

    Source-level on purpose: the 3.6 extension cannot be imported by the
    interpreter running this suite, and the artifact gate covers the runtime
    half.
    """

    def _source(self, name):
        root = pathlib.Path(chrome_client.__file__).resolve().parents[2]
        return (root / name / "src" / "lib.rs").read_text()

    def test_the_request_signature_accepts_what_the_facade_passes(self):
        legacy = _parameters(self._source("python36"), "fn request(")
        self.assertEqual(
            legacy[:11],
            ["method", "url", "headers", "body", "timeout",
             "allow_redirects", "redirect", "cache", "priority", "chunked",
             "strip_authorization"],
            "the facade passes eleven positional arguments, so their order and "
            "count are part of the contract")
        self.assertIn("take_body_buffers", legacy,
                      "the facade passes take_body_buffers= by keyword")

    def test_the_engine_constructor_declares_every_facade_keyword(self):
        legacy = _parameters(self._source("python36"), "fn new(")
        modern = _parameters(self._source("python"), "fn new(")
        missing = [name for name in modern if name not in legacy]
        self.assertEqual(
            missing, [],
            "the shared facade passes every keyword the modern constructor "
            "declares, so the 3.6 constructor must declare them too")

    def test_the_event_and_hop_tuples_have_the_same_arity(self):
        modern = self._source("python")
        legacy = self._source("python36")
        for name in ("PyEvent", "PyRedirect"):
            self.assertEqual(
                _tuple_arity(modern, name), _tuple_arity(legacy, name),
                "%s has a different arity in the two extensions; the facade "
                "unpacks it positionally" % name)


def _parameters(source, opener):
    """Returns the parameter names of one Rust fn declaration.

    Splits on top-level commas only: a type like ``Option<Vec<(String, String)>>``
    contains commas that are not parameter separators, and splitting naively put
    a fragment of the type into the name list -- which made the comparison fail
    on a correct tree.
    """
    start = source.index(opener)
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "(":
            depth += 1
        elif source[index] == ")":
            depth -= 1
            if depth == 0:
                body = source[start + len(opener):index]
                break
    else:
        raise AssertionError("%r is not a parenthesised parameter list" % opener)

    parts = []
    current = []
    angle = paren = 0
    for char in body:
        if char == "<":
            angle += 1
        elif char == ">":
            angle -= 1
        elif char == "(":
            paren += 1
        elif char == ")":
            paren -= 1
        if char == "," and angle == 0 and paren == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))

    names = []
    for part in parts:
        part = part.strip()
        if not part or part.startswith("//"):
            continue
        token = part.split(":")[0].strip()
        if token in ("&self", "self", "py"):
            continue
        names.append(token)
    return names


def _tuple_arity(source, name):
    """Counts the top-level elements of one ``type NAME = (...)`` tuple."""
    block = _type_block(source, name)
    inner = block[block.index("(") + 1:block.rindex(")")]
    depth = 0
    count = 0
    seen = False
    for char in inner:
        if char in "(<[":
            depth += 1
        elif char in ")>]":
            depth -= 1
        elif char == "," and depth == 0:
            count += 1
        elif not char.isspace() and depth == 0:
            seen = True
    return count + (1 if seen and count == 0 else 0)


def _type_block(source, name):
    """Returns the text of one ``type NAME = (...)`` declaration."""
    start = source.index("type %s = " % name)
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "(":
            depth += 1
        elif source[index] == ")":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError("%s is not a parenthesised tuple" % name)

class SameSiteCookieTests(unittest.TestCase):
    "    A SameSite=None cookie must not fail the whole response."

    "    cookie_same_site matched lowercase only, so the spelling a server"
    "    actually sends -- SameSite=None, Lax, Strict -- reached the binding"
    "    as an unknown value and raised ValueError from Session().get(). One"
    "    such Set-Cookie made the entire response unusable."
    "    "

    def _serve(self, set_cookie):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Set-Cookie", set_cookie)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:%d/" % server.server_address[1]

    def test_none_capitalized_is_accepted(self):
        # SameSite=None is only storable together with Secure, and over plain
        # HTTP Chromium correctly refuses it. What this asserts is narrower and
        # is the defect: the VALUE must parse. Before the fix the request
        # raised ValueError and no cookie was seen at all.
        url = self._serve("a=1; Path=/; SameSite=None; Secure")
        with chrome_client.Session() as session:
            response = session.get(url, timeout=5)
        self.assertEqual(response.status_code, 200)

    def test_none_without_secure_is_rejected_by_the_store(self):
        # Chromium refuses to store SameSite=None without Secure, and the
        # facade mirrors the jar into the store as the cookie arrives. So the
        # request raises -- but with the STORE's refusal, not with a ValueError
        # naming an unknown SameSite value, which is what it used to do.
        url = self._serve("a=1; Path=/; SameSite=None")
        with chrome_client.Session() as session:
            with self.assertRaises(chrome_client.RequestException) as caught:
                session.get(url, timeout=5)
        self.assertNotIn("SameSite", str(caught.exception),
                         "the value parsed; the refusal is about Secure")

    def test_every_capitalized_spelling_is_accepted(self):
        for spelling in ("Lax", "Strict", "lax", "strict"):
            with self.subTest(spelling=spelling):
                url = self._serve("b=2; Path=/; SameSite=%s" % spelling)
                with chrome_client.Session() as session:
                    response = session.get(url, timeout=5)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(session.cookies.get_dict(), {"b": "2"})

    def test_an_unknown_value_is_still_refused(self):
        # Fail closed: a SameSite value no browser accepts must not silently
        # become "unspecified" and change what goes on the wire.
        # ``chrome_client._python_impl._native`` is the extension itself (the
        # package re-exports it under that name); the loader module is the one
        # that carries ``native``. Reach the extension the same way the facade
        # does.
        from chrome_client._python_impl import _native as extension
        engine = extension.PyEngine()
        with self.assertRaises(ValueError) as caught:
            engine.set_cookie("c", "3", "example.test", "/",
                              "https://example.test/", False, False,
                              "sideways", False, 0)
        self.assertIn("sideways", str(caught.exception))


class EchStatusReportingTests(unittest.TestCase):
    "    Response.ech_status must report what the Core reported."

    "    The sync path read head.ech_status, and PyResponse never declared"
    "    that field: getattr returned its default, so every synchronous"
    "    response reported None while the async path -- which asks the"
    "    request object -- reported the real value. Both paths are asserted"
    "    together because disagreeing is exactly the defect."
    "    "

    def _serve(self):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:%d/" % server.server_address[1]

    def test_the_sync_path_reports_a_value(self):
        url = self._serve()
        with chrome_client.Session() as session:
            response = session.get(url, timeout=5)
        self.assertEqual(response.ech_status, "not_attempted")

    def test_both_paths_agree(self):
        url = self._serve()
        with chrome_client.Session() as session:
            sync_value = session.get(url, timeout=5).ech_status

        async def one():
            async with chrome_client.AsyncSession() as session:
                return (await session.get(url, timeout=5)).ech_status

        async_value = asyncio.run(one())
        self.assertEqual(sync_value, async_value)

class DefaultResolverQueriesHttpsRecordTests(unittest.TestCase):
    """Which resolver the engine builds, and what that resolver asks for.

    ECH needs an ECHConfigList, and an ECHConfigList only ever comes from a
    DNS HTTPS record (net/dns/dns_response_result_extractor.cc:581). Reaching
    one requires the built-in client to run at all: with
    ManagerOptions::insecure_dns_mode left at its kDisabled default,
    HostResolverManager pushes no DNS task
    (net/dns/host_resolver_manager.cc:1354-1355), only TaskType::SYSTEM runs,
    and HostResolverSystemTask is getaddrinfo() with no HTTPS-record path.

    What these tests can see is worth stating, because claiming more than a
    test observes is how a defect gets frozen into an assertion. The default
    path resolves through the system configuration; this suite runs
    unprivileged and cannot own the nameserver that path talks to. What is
    observable without privileges is the engine resolver once a nameserver
    is supplied -- the same code path the default configuration takes, only
    with the list coming from the caller instead of from the system.
    """

    def _listen(self):
        """Returns (port, seen); seen collects (qname, qtype) as they arrive."""
        seen = []

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                data, reply = self.request
                try:
                    index = 12
                    labels = []
                    while data[index] != 0:
                        length = data[index]
                        labels.append(data[index + 1:index + 1 + length].decode())
                        index += length + 1
                    qtype = struct.unpack("!H", data[index + 1:index + 3])[0]
                    seen.append((".".join(labels), qtype))
                except Exception:
                    return
                header = data[0:2] + b"\x81\x80" + struct.pack("!HHHH", 1, 0, 0, 0)
                reply.sendto(header + data[12:index + 5], self.client_address)

        server = socketserver.UDPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2.0)
        self.addCleanup(server.shutdown)
        return server.server_address[1], seen

    def _drain(self, seen, wanted, seconds=3.0):
        """Waits until |wanted| shows up in |seen| or the deadline passes."""
        deadline = time.time() + seconds
        while time.time() < deadline and wanted not in [q for _, q in seen]:
            time.sleep(0.05)
        return [q for _, q in seen]

    def test_the_https_record_question_is_sent(self):
        """A caller-supplied nameserver receives a type-65 question.

        This pins the query set, not the default nameserver list: both go
        through the same resolver branch, and a caller-supplied list is the
        only form this unprivileged suite can observe. It catches the HTTPS
        question disappearing again; the default path needs port 53 and is
        recorded in docs/TBD_VERIFICATION.md instead of being asserted here.
        """
        port, seen = self._listen()
        with chrome_client.Session(dns_nameservers="127.0.0.1:%d" % port) as s:
            try:
                s.get("https://example.invalid/", timeout=3)
            except Exception:
                pass
        qtypes = self._drain(seen, 65)
        self.assertIn(65, qtypes,
                      "no type-65 (HTTPS) question was sent: %r" % (seen,))

    def test_the_address_question_still_follows(self):
        """Asking for the HTTPS record must not replace the address lookup."""
        port, seen = self._listen()
        with chrome_client.Session(dns_nameservers="127.0.0.1:%d" % port) as s:
            try:
                s.get("https://example.invalid/", timeout=3)
            except Exception:
                pass
        qtypes = self._drain(seen, 1)
        self.assertIn(1, qtypes,
                      "no type-1 (A) question was sent: %r" % (seen,))

    def test_an_http_url_needs_no_dns_option(self):
        """An engine created with no dns_* option still builds and resolves."""
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = "http://127.0.0.1:%d/" % server.server_address[1]
        with chrome_client.Session() as session:
            response = session.get(url, timeout=5)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.ech_status, "not_attempted")

class DnsOverTlsRefusalTests(unittest.TestCase):
    """dns_over_tls / dns_padding are gone; the ABI field stays at zero.

    Chromium desktop has no DoT client. Setting DnsConfig::dns_over_tls_active
    only makes DnsClient::CanUseInsecureDnsTransactions() return false
    (net/dns/dns_client.cc:168), so the built-in resolver declines every job and
    TaskType::SYSTEM answers. Measured before the refusal: with dns_nameservers
    on a local UDP port, dns_over_tls=False delivered 4 queries and =True
    delivered 0 on UDP and 0 on TCP, while the request still returned 200
    through the system resolver. EDNS0 padding has no Chromium control at all.

    Both keywords were therefore removed rather than accepted-and-refused: an
    unknown keyword is a TypeError, so no caller can believe the option did
    something. What remains is the engine's own field, always zero, because the
    ABI requires it.
    """

    def test_the_keywords_are_gone(self):
        for name in ("dns_over_tls", "dns_padding"):
            with self.assertRaises(TypeError):
                chrome_client.Session(**{name: True})
            with self.assertRaises(TypeError):
                chrome_client.AsyncSession(**{name: True})

    def test_the_engine_still_carries_the_zero(self):
        """EngineConfig still passes the ABI field; only the keyword is gone."""
        with chrome_client.Session() as session:
            self.assertFalse(session.dns_over_tls)
            self.assertFalse(session.dns_padding)

    def test_dns_rotate_is_still_honoured(self):
        """The control case: rotate IS applied (DnsConfigOverrides::rotate)."""
        with chrome_client.Session(dns_rotate=True) as session:
            self.assertTrue(session.dns_rotate)


class SessionHeadersAssignmentTests(Base):
    """A plain dict handed to Session.headers must not survive as a dict.

    requests lets a session be given a dict there, and it used to be stored
    as-is: the first method to touch it, Headers.multi_items(), raised
    AttributeError from inside prepare_request -- five frames away from
    the assignment that caused it. Both spellings are pinned here, and both
    go through impersonate=, so the captured-header path (the only caller
    of PyEngine.platform_identity) actually runs.
    """

    HEADERS = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cache-Control": "no-cache",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://www.ttbz.org.cn",
        "Pragma": "no-cache",
        "Referer": "https://www.ttbz.org.cn/Home/Standard/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }

    def test_assigning_a_plain_dict_is_coerced(self):
        with chrome_client.Session(impersonate="chrome_136") as session:
            session.headers = dict(self.HEADERS)
            self.assertIsInstance(session.headers, sessions_module.Headers)
            response = session.get(self.base + "echo-header/origin")
            self.assertEqual(response.content, b"https://www.ttbz.org.cn")

    def test_update_on_the_assigned_mapping_still_merges(self):
        with chrome_client.Session(impersonate="chrome_120") as session:
            session.headers.update(dict(self.HEADERS))
            self.assertIsInstance(session.headers, sessions_module.Headers)
            response = session.get(self.base + "echo-header/referer")
            self.assertEqual(
                response.content, b"https://www.ttbz.org.cn/Home/Standard/")


if __name__ == "__main__":
    unittest.main()
