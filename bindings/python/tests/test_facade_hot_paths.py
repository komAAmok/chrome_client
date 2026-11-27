"""Regression tests for the facade hot paths this audit changed.

Each test pins a contract rather than a speed, because a speed assertion cannot
be a gate: the numbers live in docs/PERF_AUDIT_0.2.20.md and they move with the
host. What must not move is the behaviour these four changes sit underneath.

The four paths, and what a regression looks like:

* ``Headers.add`` appends into the stored joined value instead of rescanning
  the whole field list. The joined string, the first-written casing and the
  field order must stay exactly what ``", ".join(...)`` produced.
* ``environ_proxy_map`` scans the environment as bytes instead of decoding
  every key. It must still see a proxy assigned through ``os.environ`` -- a
  bytes *view* built at import (``os.environb``) does not, measured -- and it
  must agree with the honest str loop on every shape.
* ``browser_headers`` and ``full_header_order`` are memoised. The memo must
  hand out a fresh list, or one request mutating a header would change what
  the next request sends.
* ``EngineCache.get`` builds its key once per call instead of five times.
"""

import os
import threading
import unittest

from chrome_client._python_impl import browser_headers as bh
from chrome_client._python_impl.structures import Headers
from chrome_client._python_impl.utils import environ_proxy_map


def _honest_proxy_scan():
    """The pre-change implementation, kept as the reference answer."""
    found = None
    for name in os.environ:
        lowered = name.lower()
        if not lowered.endswith("_proxy"):
            continue
        value = os.environ.get(name)
        if not value:
            continue
        if found is None:
            found = {}
        found[lowered[:-6]] = value
    return found


class HeaderAppendTests(unittest.TestCase):
    def test_duplicates_join_in_insertion_order(self):
        headers = Headers()
        headers.add("Set-Cookie", "a=1")
        headers.add("set-cookie", "b=2")
        headers.add("SET-COOKIE", "c=3")
        self.assertEqual(headers["set-cookie"], "a=1, b=2, c=3")
        self.assertEqual(headers.get_list("set-cookie"), ["a=1", "b=2", "c=3"])
        self.assertEqual(headers.multi_items(),
                         [("Set-Cookie", "a=1"), ("set-cookie", "b=2"),
                          ("SET-COOKIE", "c=3")])

    def test_the_reported_casing_is_the_first_one_written(self):
        headers = Headers()
        headers.add("X-First", "1")
        headers.add("x-first", "2")
        self.assertEqual(list(headers.items()), [("X-First", "1, 2")])
        self.assertEqual(list(headers.lower_items()), [("x-first", "1, 2")])

    def test_replacing_a_field_resets_the_join(self):
        headers = Headers()
        headers.add("X-Y", "1")
        headers.add("x-y", "2")
        headers["X-Y"] = "3"
        self.assertEqual(headers.get_list("x-y"), ["3"])
        self.assertEqual(list(headers.items()), [("X-Y", "3")])

    def test_removing_then_adding_leaves_no_deletion_record(self):
        headers = Headers()
        headers["X-Gone"] = "1"
        del headers["X-Gone"]
        self.assertEqual(sorted(headers.deleted_names), ["x-gone"])
        headers.add("X-Gone", "2")
        self.assertEqual(headers.get_list("X-Gone"), ["2"])
        self.assertEqual(sorted(headers.deleted_names), ["x-gone"])


class EnvironmentScanTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def _clear_proxy_vars(self):
        for name in [key for key in list(os.environ)
                     if key.lower().endswith("_proxy")]:
            del os.environ[name]

    def test_a_proxy_assigned_through_os_environ_is_seen(self):
        self._clear_proxy_vars()
        self.assertIsNone(environ_proxy_map())
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:8080"
        self.assertEqual(environ_proxy_map(), {"http": "http://127.0.0.1:8080"})

    def test_the_scan_agrees_with_the_str_loop_on_every_shape(self):
        shapes = [{},
                  {"HTTP_PROXY": "http://a:1"},
                  {"https_proxy": "http://b:2", "ALL_PROXY": "socks5://c:3"},
                  {"HTTP_PROXY": ""},
                  {"no_proxy": "x", "http_proxy": "h:1"},
                  {"SOCKS_PROXY": "socks5://d:4", "ftp_proxy": "http://e:5"}]
        for shape in shapes:
            self._clear_proxy_vars()
            for name, value in shape.items():
                os.environ[name] = value
            self.assertEqual(environ_proxy_map(), _honest_proxy_scan(), shape)

    def test_an_empty_value_does_not_count_as_configured(self):
        self._clear_proxy_vars()
        os.environ["HTTP_PROXY"] = ""
        self.assertIsNone(environ_proxy_map())


class BrowserHeaderMemoTests(unittest.TestCase):
    def setUp(self):
        bh._BROWSER_HEADERS_CACHE.clear()
        bh._FULL_HEADER_ORDER_CACHE.clear()

    def test_the_memo_returns_a_fresh_list_every_call(self):
        first = bh.browser_headers("chrome_120")
        second = bh.browser_headers("chrome_120")
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        first.append(("X-Poison", "1"))
        self.assertNotIn(("X-Poison", "1"), bh.browser_headers("chrome_120"))

    def test_the_order_memo_returns_a_fresh_list_every_call(self):
        first = bh.full_header_order("chrome_120")
        second = bh.full_header_order("chrome_120")
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        first.append("x-poison")
        self.assertNotIn("x-poison", bh.full_header_order("chrome_120"))

    def test_the_memo_is_keyed_by_every_input_that_changes_the_answer(self):
        base = bh.browser_headers("chrome_120")
        other_platform = bh.browser_headers("chrome_120", None, "windows")
        # The captured Accept-Language for this profile is already zh-CN, so the
        # override has to name a different one to be visible at all.
        self.assertIn(("Accept-Language", "zh-CN,zh;q=0.9"), base)
        other_language = bh.browser_headers("chrome_120", "fr-FR,fr;q=0.9")
        self.assertNotEqual(base, other_platform)
        self.assertNotEqual(base, other_language)
        self.assertEqual(base, bh.browser_headers("chrome_120"))

    def test_an_unknown_profile_is_still_empty(self):
        self.assertEqual(bh.browser_headers(None), [])
        self.assertEqual(bh.browser_headers("chrome_0"), [])
        self.assertEqual(bh.full_header_order(None), [])


class _Fixture(object):
    """One keep-alive endpoint, four headers including a duplicate."""

    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            disable_nagle_algorithm = True

            def do_GET(self):
                body = b"x" * 1024
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Dup", "one")
                self.send_header("X-Dup", "two")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._server.shutdown()
        self._server.server_close()

    @property
    def url(self):
        return "http://127.0.0.1:%d/p" % self._server.server_address[1]


class AsyncResponseFieldTests(unittest.TestCase):
    """The asynchronous response must use the fields the Core parsed.

    Before this contract existed, the response event carried an empty field
    list, so _AsyncState read header_fields()/reason_phrase() off the request,
    both answered None (the head is consumed by the same call), and _finalize
    fell back to parse_raw_headers -- the facade re-parsing the grammar the Core
    had already parsed, which AGENTS.md 1.1 forbids. Measured: one parse per
    asynchronous response against zero on the synchronous path.

    The first test asserts the property directly by making the Python parser
    raise, so a regression cannot hide behind an equivalent-looking result.
    """

    def test_the_async_path_does_not_need_the_python_parser(self):
        import asyncio
        from chrome_client import AsyncSession
        from chrome_client._python_impl import sessions as facade

        def refuse(_raw):
            raise AssertionError(
                "the asynchronous response re-parsed its header block in Python")

        original = facade.parse_raw_headers
        facade.parse_raw_headers = refuse
        try:
            async def run(url):
                session = AsyncSession()
                try:
                    return await session.get(url)
                finally:
                    await session.aclose()

            with _Fixture() as fixture:
                response = asyncio.run(run(fixture.url))
        finally:
            facade.parse_raw_headers = original
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.reason, "OK")
        self.assertEqual(response.headers.get_list("x-dup"), ["one", "two"])

    def test_async_and_sync_agree_on_the_whole_head(self):
        import asyncio
        from chrome_client import AsyncSession, Session

        with _Fixture() as fixture:
            async def run():
                session = AsyncSession()
                try:
                    return await session.get(fixture.url)
                finally:
                    await session.aclose()

            asynchronous = asyncio.run(run())
            session = Session()
            try:
                synchronous = session.get(fixture.url)
            finally:
                session.close()
        self.assertEqual(asynchronous.status_code, synchronous.status_code)
        self.assertEqual(asynchronous.reason, synchronous.reason)
        self.assertEqual(asynchronous.status_line, synchronous.status_line)
        self.assertEqual(asynchronous.http_version, synchronous.http_version)
        self.assertEqual(list(asynchronous.headers.items()),
                         list(synchronous.headers.items()))


if __name__ == "__main__":
    unittest.main()