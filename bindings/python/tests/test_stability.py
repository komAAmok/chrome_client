"""Linux x86_64 Python regression checks for the public chrome_client API.

Run with the audited Core available, for example:
MINICRONET_CORE_DIR=... LD_LIBRARY_PATH=... PYTHONPATH=bindings/python:target/debug \
python -m unittest bindings/python/tests/test_stability.py
"""

import asyncio
import inspect
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

try:
    from http.server import ThreadingHTTPServer
except ImportError:  # Python 3.6
    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

import chrome_client
from chrome_client._python_impl import _proxy_from_proxies


class Handler(BaseHTTPRequestHandler):
    active = 0
    active_lock = threading.Lock()

    def do_GET(self):
        if self.path.startswith("/slow"):
            time.sleep(1.0)
        if self.path.startswith("/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            try:
                for _ in range(100):
                    self.wfile.write(b"x" * 32768)
                    self.wfile.flush()
                    time.sleep(0.001)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if self.path.startswith("/echo-cookie"):
            payload = self.headers.get("Cookie", "").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.startswith("/infinite"):
            with self.active_lock:
                type(self).active += 1
            self.send_response(200)
            self.end_headers()
            try:
                while True:
                    self.wfile.write(b"x" * 4096)
                    self.wfile.flush()
                    time.sleep(0.005)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with self.active_lock:
                    type(self).active -= 1
            return
        size = int(self.headers.get("X-Size", "32"))
        payload = (b"x" * size)
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except BrokenPipeError:
            pass

    def log_message(self, *_args):
        pass


class StabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d/" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    @staticmethod
    def run_async(coro):
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(coro)
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    def test_session_defaults_and_header_case(self):
        with chrome_client.Session(headers={"X-Size": "7"}) as session:
            response = session.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.content), 7)
        self.assertEqual(response.headers["Content-Length"], "7")

    def test_session_cookies_get_dict_and_request_merge(self):
        with chrome_client.Session(cookies={"session": "abc"}) as session:
            self.assertIsInstance(session.cookies, chrome_client.CookieJar)
            self.assertEqual(session.cookies.get_dict(), {"session": "abc"})

            session.cookies["extra"] = "1"
            self.assertEqual(session.cookies.get_dict(), {"session": "abc", "extra": "1"})

            body = session.get(self.url + "echo-cookie").text
            self.assertIn("session=abc", body)
            self.assertIn("extra=1", body)

            # Per-request cookies apply to that request only.
            body = session.get(self.url + "echo-cookie", cookies={"extra": "2"}).text
            self.assertIn("extra=2", body)
            self.assertEqual(session.cookies.get_dict(), {"session": "abc", "extra": "1"})

            # Domain and path filters now answer from real cookie metadata rather
            # than rejecting the call.
            self.assertEqual(session.cookies.get_dict(domain="example.com"), {})

    def test_streaming_and_response_limit(self):
        with chrome_client.Client() as client:
            response = client.get(self.url, headers={"X-Size": "4096"}, stream=True,
                                  max_response_bytes=8192)
            self.assertEqual(sum(len(chunk) for chunk in response.iter_content(257)), 4096)
            with self.assertRaises(chrome_client.ResponseTooLarge):
                client.get(self.url, headers={"X-Size": "4096"}, max_response_bytes=1024)

        async def run():
            async with chrome_client.AsyncClient() as client:
                response = await client.get(self.url, headers={"X-Size": "4096"}, stream=True,
                                             max_response_bytes=8192)
                total = 0
                async for chunk in response.aiter_bytes(257):
                    total += len(chunk)
                self.assertEqual(total, 4096)
                with self.assertRaises(chrome_client.ResponseTooLarge):
                    await client.get(self.url, headers={"X-Size": "4096"}, max_response_bytes=1024)
                response = await client.get(self.url, headers={"X-Size": "4096"}, stream=True,
                                             max_response_bytes=1024)
                with self.assertRaises(chrome_client.ResponseTooLarge):
                    async for _chunk in response.aiter_bytes():
                        pass

        self.run_async(run())

    def test_async_multichunk_stream_completes(self):
        async def run():
            async with chrome_client.AsyncClient() as client:
                response = await client.get(self.url + "stream", stream=True)
                total = 0
                async for chunk in response.aiter_bytes(8192):
                    total += len(chunk)
                self.assertEqual(total, 100 * 32768)

        self.run_async(run())

    def test_stalled_stream_does_not_block_other_requests(self):
        """A stalled consumer must pause only its own request.

        Reading one chunk and then stopping leaves the Rust body queue at its
        1 MiB ceiling. Before ABI v8 that blocked a Core callback thread, and
        because callbacks shared one process-wide sequenced runner every later
        request on the Engine stopped returning -- timeouts included, since the
        timeout's completion callback queued behind the blocked one. ABI v8
        answers on_body with MN_READ_PAUSE instead, so nothing blocks.
        """
        with chrome_client.Client() as stalled_client, \
                chrome_client.Client() as other_client:
            stalled = stalled_client.get(self.url + "infinite", stream=True)
            chunks = stalled.iter_content(65536)
            next(iter(chunks))
            time.sleep(0.5)  # let the queue reach the ceiling

            try:
                for client in (stalled_client, other_client):
                    for _ in range(3):
                        begin = time.time()
                        response = client.get(self.url, headers={"X-Size": "16"},
                                              timeout=5)
                        self.assertEqual(response.status_code, 200)
                        self.assertLess(time.time() - begin, 5.0)
            finally:
                stalled.close()

    def test_internationalized_host_is_canonicalized(self):
        """An internationalized hostname must reach the network like its punycode form.

        The Core embeds an IDNA-only ICU dataset and initializes ICU at Engine
        creation. Before that it linked ICU without initializing it, so Chromium's
        URL canonicalizer CHECK-failed and killed the interpreter with SIGTRAP on
        any non-ASCII host.
        """
        with chrome_client.Client() as client:
            # Both spellings of the same host must fail identically: a transport
            # or resolution error, never argument validation.
            failures = []
            for url in ("http://例え.テスト/", "http://xn--r8jz45g.xn--zckzah/"):
                with self.assertRaises(chrome_client.RequestException) as caught:
                    client.get(url, timeout=2)
                self.assertNotIsInstance(caught.exception, ValueError)
                failures.append(type(caught.exception))
            self.assertEqual(failures[0], failures[1])

            # A URL with no scheme is rejected before any I/O, with requests'
            # exception type rather than a native error string.
            with self.assertRaises(chrome_client.MissingSchema):
                client.get("not-a-url", timeout=2)

            response = client.get(self.url + "path/路径?q=值", timeout=5)
            self.assertEqual(response.status_code, 200)

    def test_response_close_cancels_stream(self):
        with chrome_client.Client() as client:
            response = client.get(self.url + "infinite", stream=True)
            response.close()
        deadline = time.time() + 2.0
        while time.time() < deadline:
            with Handler.active_lock:
                if Handler.active == 0:
                    break
            time.sleep(0.01)
        self.assertEqual(Handler.active, 0)

        async def run():
            async with chrome_client.AsyncClient() as client:
                response = await client.get(self.url + "infinite", stream=True)
                await response.aclose()

        self.run_async(run())
        deadline = time.time() + 2.0
        while time.time() < deadline:
            with Handler.active_lock:
                if Handler.active == 0:
                    break
            time.sleep(0.01)
        self.assertEqual(Handler.active, 0)

    def test_sync_timeout_maps_public_type(self):
        with chrome_client.Client() as client:
            with self.assertRaises(chrome_client.Timeout):
                client.get(self.url + "slow", timeout=0.01)

    def test_close_is_idempotent(self):
        client = chrome_client.Client()
        client.close()
        client.close()
        with self.assertRaises(chrome_client.RequestException):
            client.get(self.url)

        async def run():
            client = chrome_client.AsyncClient()
            await client.aclose()
            await client.aclose()
            with self.assertRaises(chrome_client.RequestException):
                await client.get(self.url)

        self.run_async(run())

    def test_public_api_surface(self):
        exported = set(chrome_client.__all__)
        # Names every release has published.
        self.assertLessEqual(
            {"Client", "Session", "AsyncClient", "AsyncSession", "Response",
             "AsyncResponse", "WebSocket", "AsyncWebSocket", "CaseInsensitiveDict",
             "CookieJar", "ResponseTooLarge", "RequestException", "Timeout", "requests",
             "get", "options", "head", "post", "put", "patch", "delete"},
            exported)
        # requests-shaped names callers port code against.
        self.assertLessEqual(
            {"HTTPError", "ConnectionError", "SSLError", "ProxyError", "TooManyRedirects",
             "JSONDecodeError", "ConnectTimeout", "ReadTimeout", "MissingSchema",
             "InvalidURL", "ChunkedEncodingError", "Request", "PreparedRequest",
             "HTTPAdapter", "HTTPBasicAuth", "codes", "cookiejar_from_dict"},
            exported)
        # curl_cffi-shaped names.
        self.assertLessEqual(
            {"CurlMime", "ExtraFingerprints", "CurlHttpVersion", "Headers", "Cookies",
             "RetryStrategy", "WsCloseCode"},
            exported)
        self.assertNotIn("minicronet", dir(chrome_client))
        self.assertNotIn("_all", dir(chrome_client))
        self.assertNotIn("_native_module", dir(chrome_client))
        self.assertNotIn("chrome_client_native36", dir(chrome_client))

        parameters = list(inspect.signature(chrome_client.Session.request).parameters)
        self.assertEqual(parameters[:5], ["self", "method", "url", "params", "data"])
        for name in ("headers", "cookies", "files", "auth", "timeout", "allow_redirects",
                     "proxies", "hooks", "stream", "verify", "cert", "json", "content",
                     "multipart", "impersonate", "proxy", "http_version",
                     "max_redirects", "max_response_bytes"):
            self.assertIn(name, parameters)
        with self.assertRaises(TypeError):
            chrome_client.Session().request("GET", self.url, legacy_field=True)

    def test_proxies_selects_scheme_and_is_mutable(self):
        proxies = {"http": "http://proxy-http", "https": "http://proxy-https", "all": "http://proxy-all"}
        client = chrome_client.Client(proxies=proxies)
        self.assertEqual(client.proxies, proxies)
        # `session.proxies` is an ordinary mutable mapping, as in requests.
        client.proxies["http"] = "http://replaced"
        self.assertEqual(_proxy_from_proxies("http://example.com", client.proxies),
                         "http://replaced")
        client.proxies.clear()
        self.assertEqual(client.proxies, {})
        self.assertEqual(chrome_client.Client().proxies, {})
        self.assertEqual(_proxy_from_proxies("https://example.com", proxies), "http://proxy-https")
        self.assertEqual(_proxy_from_proxies("ws://example.com", proxies), "http://proxy-http")

    def test_async_concurrency_matrix(self):
        async def run():
            async with chrome_client.AsyncSession() as session:
                for count in (32, 128, 1000):
                    responses = await asyncio.wait_for(
                        asyncio.gather(*[session.get(self.url) for _ in range(count)]), 30
                    )
                    self.assertEqual(len(responses), count)
                    self.assertTrue(all(response.status_code == 200 for response in responses))

        self.run_async(run())

    def test_async_cancel_timeout_and_large_body(self):
        async def run():
            async with chrome_client.AsyncClient() as session:
                cancelled = asyncio.ensure_future(session.get(self.url + "slow"))
                await asyncio.sleep(0.02)
                cancelled.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await cancelled
                with self.assertRaises(chrome_client.Timeout):
                    await session.get(self.url + "slow", timeout=0.02)
                response = await session.get(self.url, headers={"X-Size": "4194304"})
                self.assertEqual(len(response.content), 4194304)

        self.run_async(run())

    def test_sync_releases_gil(self):
        counter = [0]
        running = [True]

        def worker():
            while running[0]:
                counter[0] += 1

        thread = threading.Thread(target=worker)
        thread.start()
        try:
            with chrome_client.Client() as client:
                client.get(self.url + "slow")
        finally:
            running[0] = False
            thread.join()
        self.assertGreater(counter[0], 1000)

    def test_event_loop_can_close_after_cancel(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        task = asyncio.ensure_future(chrome_client.AsyncClient().get(self.url + "slow"), loop=loop)
        loop.run_until_complete(asyncio.sleep(0.01))
        task.cancel()
        loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
        loop.close()

    @unittest.skipUnless(
        os.environ.get("MINICRONET_WS_URL") or os.environ.get("MINICRONET_WSS_URL"),
        "set MINICRONET_WS_URL or MINICRONET_WSS_URL for a real WS/WSS endpoint; "
        "test_compat.WebSocketTests covers the handshake against a local server",
    )
    def test_websocket_sync_and_async(self):
        urls = [value for value in (os.environ.get("MINICRONET_WS_URL"), os.environ.get("MINICRONET_WSS_URL")) if value]
        for url in urls:
            with chrome_client.WebSocket(url) as socket:
                socket.send("ping")
                self.assertEqual(socket.recv(), "ping")

        async def run():
            for url in urls:
                async with chrome_client.AsyncClient() as client:
                    async with await client.websocket(url) as socket:
                        await socket.send("ping")
                        self.assertEqual(await socket.recv(), "ping")

        self.run_async(run())


if __name__ == "__main__":
    unittest.main()
