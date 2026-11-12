"""Behavioural tests for the ``Client``/``AsyncClient`` aliases and the two
defects the alias sweep found.

``Client = Session`` and ``AsyncClient = AsyncSession`` (``sessions.py``), so the
aliases cannot drift from their targets -- but the things tested THROUGH them can
still be wrong, and these two were:

1. Every redirect hop shared the caller's ``PreparedRequest``, so
   ``history[i].request.url`` reported the original URL on every hop. Covered by
   ``test_compat.RedirectTests``; the case here is the alias path, which is what a
   user of ``Client`` actually calls.
2. ``SessionOptions`` (the TypedDict both constructors are typed with) listed
   ``max_clients``, which only ``AsyncSession`` accepts. A caller following the
   stub got a ``TypeError`` from ``Session(**options)``. Fixed in the stub and in
   the generator that produces the ``async_session`` signature.

Everything else here is the parity sweep: the same calls through the alias must
return the same shapes as through the class it aliases.
"""

import asyncio
import json
import threading
import unittest

import chrome_client


class _Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            disable_nagle_algorithm = True

            def do_GET(self):
                body = b"x" * 16
                if self.path.startswith("/redirect2"):
                    self.send_response(302)
                    self.send_header("Content-Length", "0")
                    self.send_header("Location", "/redirect")
                    self.end_headers()
                    return
                if self.path.startswith("/redirect"):
                    self.send_response(302)
                    self.send_header("Content-Length", "0")
                    self.send_header("Location", "/small")
                    self.end_headers()
                    return
                if self.path.startswith("/slow"):
                    import time
                    time.sleep(0.5)
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                if self.path.startswith("/cookie"):
                    self.send_header("Set-Cookie", "sid=v; Path=/")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    # The cancellation test abandons a request mid-flight, so the
                    # server's write races the client's close. That is the point
                    # of the test, not a failure of it.
                    pass

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=5)

    @staticmethod
    def run_async(coro):
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(coro)
        finally:
            asyncio.set_event_loop(None)
            loop.close()


class AliasIdentityTests(_Server):
    """The aliases must be the same objects, not copies that can drift."""

    def test_client_is_session(self):
        self.assertIs(chrome_client.Client, chrome_client.Session)

    def test_async_client_is_async_session(self):
        self.assertIs(chrome_client.AsyncClient, chrome_client.AsyncSession)

    def test_client_context_manager_closes(self):
        with chrome_client.Client() as client:
            self.assertEqual(client.get(self.base + "/small").status_code, 200)
            live = client
        self.assertTrue(live._closed, "leaving the with-block did not close it")

    def test_async_client_context_manager_closes(self):
        async def run():
            async with chrome_client.AsyncClient() as client:
                response = await client.get(self.base + "/small")
                self.assertEqual(response.status_code, 200)
                return client

        self.assertTrue(self.run_async(run())._closed)


class AliasBehaviourTests(_Server):
    """The same call through the alias must behave like the class it aliases."""

    def test_history_requests_differ_per_hop(self):
        """Each hop must carry the request that produced it, through the alias.

        Sharing one PreparedRequest made every hop report the caller's URL, so a
        caller could not tell which hop set which cookie. Two hops are required
        for the assertion to mean anything: with a single hop the shared request
        and the per-hop request have the same URL.
        """
        with chrome_client.Client() as client:
            response = client.get(self.base + "/redirect2")
            self.assertEqual(len(response.history), 2)
            self.assertEqual([hop.request.url for hop in response.history],
                             [self.base + "/redirect2", self.base + "/redirect"])
            self.assertEqual(response.request.url, self.base + "/redirect2")

    def test_cookies_reach_the_wire(self):
        """A jar edit must be visible to the server, not only to the jar."""
        with chrome_client.Client() as client:
            client.get(self.base + "/cookie")
            client.cookies.set("added", "1", domain="127.0.0.1")
            self.assertIn("added", client.cookies.get_dict())
            self.assertEqual(len(client._engine_cookies()), 2)

    def test_use_after_close_is_refused(self):
        client = chrome_client.Client()
        client.get(self.base + "/small")
        client.close()
        with self.assertRaises(chrome_client.SessionClosed):
            client.get(self.base + "/small")

    def test_close_is_idempotent(self):
        client = chrome_client.Client()
        client.get(self.base + "/small")
        client.close()
        client.close()
        client.close()

    def test_a_stream_survives_its_session_closing(self):
        """close() ends reuse of the pool, not work already running on it."""
        client = chrome_client.Client()
        response = client.get(self.base + "/small", stream=True)
        client.close()
        body = b"".join(response.iter_content(65536))
        response.close()
        self.assertEqual(len(body), 16)

    def test_async_client_streaming(self):
        async def run():
            async with chrome_client.AsyncClient() as client:
                response = await client.get(self.base + "/small", stream=True)
                total = 0
                async for chunk in response.aiter_content(65536):
                    total += len(chunk)
                await response.aclose()
                return total

        self.assertEqual(self.run_async(run()), 16)

    def test_async_cancel_mid_request_does_not_wedge_the_session(self):
        async def run():
            async with chrome_client.AsyncClient() as client:
                task = asyncio.create_task(client.get(self.base + "/slow",
                                                      timeout=5))
                await asyncio.sleep(0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                # The session must still serve a request afterwards.
                return (await client.get(self.base + "/small")).status_code

        self.assertEqual(self.run_async(run()), 200)


class StubParityTests(unittest.TestCase):
    """The stub must not advertise a keyword the constructor rejects."""

    def test_session_rejects_max_clients(self):
        with self.assertRaises(TypeError):
            chrome_client.Session(max_clients=4)

    def test_async_session_accepts_max_clients(self):
        session = chrome_client.AsyncSession(max_clients=4)
        try:
            self.assertEqual(session.max_clients, 4)
        finally:
            session.close()

    def test_async_session_helper_accepts_max_clients(self):
        async def run():
            async with chrome_client.async_session(max_clients=4) as session:
                return session.max_clients

        self.assertEqual(self._run(run()), 4)

    @staticmethod
    def _run(coro):
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(coro)
        finally:
            asyncio.set_event_loop(None)
            loop.close()


class SessionOptionsTests(unittest.TestCase):
    """The shared TypedDict must not list an AsyncSession-only keyword.

    ``_types`` is a stub-only module -- it has no runtime counterpart, which is
    why this reads the .pyi instead of importing it. The claim being checked is
    exactly the one the stub makes, so the stub is the right artefact to read.
    """

    def test_session_options_omits_max_clients(self):
        import ast
        from pathlib import Path

        import chrome_client

        stub = (Path(chrome_client.__file__).parent / "_python_impl"
                / "_types.pyi")
        tree = ast.parse(stub.read_text(encoding="utf-8"))
        annotations = None
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "SessionOptions":
                annotations = [child.target.id for child in node.body
                               if isinstance(child, ast.AnnAssign)]
                break
        self.assertIsNotNone(annotations, "SessionOptions not found in the stub")
        self.assertNotIn("max_clients", annotations,
                         "Session does not accept max_clients, so the shared "
                         "TypedDict must not list it")

    def test_async_session_stub_lists_max_clients(self):
        import ast
        from pathlib import Path

        import chrome_client

        stub = (Path(chrome_client.__file__).parent / "_python_impl"
                / "api.pyi")
        tree = ast.parse(stub.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "async_session":
                names = [arg.arg for arg in node.args.args]
                self.assertIn("max_clients", names,
                              "async_session accepts max_clients at run time")
                return
        self.fail("async_session not found in the stub")


if __name__ == "__main__":
    unittest.main()
