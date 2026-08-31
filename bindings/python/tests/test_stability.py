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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import chrome_client
from chrome_client._python_impl import _proxy_from_proxies


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/slow"):
            time.sleep(1.0)
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

    def test_session_defaults_and_header_case(self):
        with chrome_client.Session(headers={"X-Size": "7"}) as session:
            response = session.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.content), 7)
        self.assertEqual(response.headers["Content-Length"], "7")

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

        asyncio.run(run())

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

        asyncio.run(run())

    def test_public_api_has_no_legacy_exports(self):
        expected = {
            "Client", "Session", "AsyncClient", "AsyncSession", "Response",
            "AsyncResponse", "WebSocket", "AsyncWebSocket", "CaseInsensitiveDict", "ResponseTooLarge",
            "RequestException", "Timeout", "requests", "get", "options", "head",
            "post", "put", "patch", "delete",
        }
        self.assertEqual(set(chrome_client.__all__), expected)
        self.assertNotIn("minicronet", dir(chrome_client))
        self.assertNotIn("_all", dir(chrome_client))
        self.assertNotIn("_native_module", dir(chrome_client))
        self.assertNotIn("_sys", dir(chrome_client))
        self.assertNotIn("chrome_client_native36", dir(chrome_client))
        self.assertEqual(
            list(inspect.signature(chrome_client.Client.request).parameters),
            ["self", "method", "url", "params", "data", "json", "headers", "cookies",
             "timeout", "allow_redirects", "stream", "impersonate", "proxy", "proxies", "verify",
             "max_response_bytes"],
        )
        with self.assertRaises(TypeError):
            chrome_client.Client().request("GET", self.url, legacy_field=True)

    def test_proxies_selects_scheme_and_explicit_proxy_wins(self):
        proxies = {"http": "http://proxy-http", "https": "http://proxy-https", "all": "http://proxy-all"}
        client = chrome_client.Client(proxies=proxies)
        self.assertEqual(client.proxies, proxies)
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

        asyncio.run(run())

    def test_async_cancel_timeout_and_large_body(self):
        async def run():
            async with chrome_client.AsyncClient() as session:
                cancelled = asyncio.create_task(session.get(self.url + "slow"))
                await asyncio.sleep(0.02)
                cancelled.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await cancelled
                with self.assertRaises(chrome_client.Timeout):
                    await session.get(self.url + "slow", timeout=0.02)
                response = await session.get(self.url, headers={"X-Size": "4194304"})
                self.assertEqual(len(response.content), 4194304)

        asyncio.run(run())

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
        task = loop.create_task(chrome_client.AsyncClient().get(self.url + "slow"))
        loop.run_until_complete(asyncio.sleep(0.01))
        task.cancel()
        loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
        loop.close()

    @unittest.skipUnless(
        os.environ.get("MINICRONET_WS_URL") or os.environ.get("MINICRONET_WSS_URL"),
        "set MINICRONET_WS_URL or MINICRONET_WSS_URL for WS/WSS endpoint",
    )
    def test_websocket_sync_and_async(self):
        urls = [value for value in (os.environ.get("MINICRONET_WS_URL"), os.environ.get("MINICRONET_WSS_URL")) if value]
        for url in urls:
            with chrome_client.WebSocket(url) as socket:
                socket.send("ping")
                self.assertEqual(socket.recv(), "ping")

        async def run():
            for url in urls:
                async with await chrome_client.AsyncClient().websocket(url) as socket:
                    await socket.send("ping")
                    self.assertEqual(await socket.recv(), "ping")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
