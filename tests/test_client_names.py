import inspect
import asyncio
import ast
import json
import sys
import types
import unittest
import tempfile
import threading
from collections import UserDict
from unittest.mock import patch
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

native = types.ModuleType("chrome_client.cronet_cloak")


class PyCronetClient:
    calls = []
    create_calls = []

    def create_session(self, *args):
        self.create_calls.append(args)
        return "test-session"

    def request_sync(self, session_id, url, method, headers, body, allow_redirects):
        self.calls.append((url, method, dict(headers), body))
        if "/set-cookies" in url:
            return {
                "status_code": 200,
                "headers": [
                    ("set-cookie", "root=1; Path=/"),
                    ("set-cookie", "scoped=1; Path=/app"),
                ],
                "body": b"ok",
            }
        if "/delete-cookie" in url:
            return {
                "status_code": 200,
                "headers": [("set-cookie", "scoped=; Path=/app; Max-Age=0")],
                "body": b"ok",
            }
        if url.startswith("https://example.test/redirect"):
            return {
                "status_code": 302,
                "headers": [("location", "https://other.test/final")],
                "body": b"redirect",
            }
        return {
            "status_code": 200,
            "headers": [("content-type", "application/json")],
            "body": b'{"ok": true}',
        }

    async def request(self, session_id, url, method, headers, body, allow_redirects):
        return self.request_sync(session_id, url, method, headers, body, allow_redirects)

    def request_stream_sync(self, session_id, url, method, headers, body, allow_redirects):
        return FakeStreamReader([b"a\r", b"\nb\n", b"last"])

    async def request_stream(self, session_id, url, method, headers, body, allow_redirects):
        return FakeStreamReader([b"a\r", b"\nb\n", b"last"])

    def websocket_connect(self, session_id, url, headers=None, sub_protocols=None, origin=None):
        self.calls.append((url, sub_protocols, origin, headers))
        return FakeWebSocket()

    def close_session(self, session_id):
        return True


class FakeWebSocket:
    def __init__(self):
        self.events = [
            {"type": "open"},
            {"type": "message", "data": "hello"},
            {"type": "close", "code": 1000, "reason": "done"},
        ]

    def recv_timeout(self, timeout_ms):
        return self.events.pop(0)

    def send(self, message):
        pass

    def send_bytes(self, data):
        pass

    def close(self, code, reason):
        pass


class FakeStreamReader:
    status_code = 200
    headers = [("Content-Type", "text/plain; charset=utf-8")]

    def __init__(self, chunks):
        self.chunks = list(chunks)

    def next_chunk_sync(self):
        return self.chunks.pop(0) if self.chunks else None

    async def next_chunk(self):
        return self.next_chunk_sync()

    def close(self):
        self.chunks.clear()


native.PyCronetClient = PyCronetClient
sys.modules["chrome_client.cronet_cloak"] = native

import chrome_client


class ClientNamesTest(unittest.TestCase):
    def test_sessions_can_be_recreated_after_threaded_close(self):
        errors = []

        def create_and_close():
            try:
                for _ in range(25):
                    session = chrome_client.Session(impersonate=None)
                    session.close()
                    session.close()  # close remains idempotent under contention
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=create_and_close) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        with chrome_client.Session(impersonate=None) as session:
            self.assertEqual(session.get("https://example.test/new").status_code, 200)

    def test_impersonate_hints_match_bundled_profiles(self):
        package = Path(__file__).resolve().parents[1] / "python" / "chrome_client"
        tree = ast.parse((package / "_typing.pyi").read_text(encoding="utf-8"))
        literal = next(
            node.value for node in tree.body
            if isinstance(node, ast.Assign)
            and any(getattr(target, "id", None) == "BrowserTypeLiteral"
                    for target in node.targets)
        )
        values = literal.slice.value if isinstance(literal.slice, ast.Index) else literal.slice
        hinted = [value.value if hasattr(value, "value") else value.s
                  for value in values.elts]
        profiles = json.loads(
            (package / "tls_profiles.json").read_text(encoding="utf-8")
        )
        self.assertEqual(set(hinted), set(profiles))
        self.assertIs(chrome_client.BrowserTypeLiteral, str)

        requests_stub = ast.parse(
            (package / "requests.pyi").read_text(encoding="utf-8")
        )
        functions = {
            node.name: node for node in requests_stub.body
            if isinstance(node, ast.FunctionDef)
        }
        for name in (
            "request", "get", "options", "head", "post", "put", "patch",
            "delete", "trace", "query",
        ):
            self.assertIsNone(functions[name].args.kwarg, name)
        self.assertTrue((package / "py.typed").is_file())

    def test_public_client_names(self):
        self.assertFalse(hasattr(chrome_client, "CronetClient"))
        self.assertFalse(hasattr(chrome_client, "AsyncCronetClient"))
        self.assertFalse(hasattr(chrome_client, "PyCronetClient"))
        self.assertTrue(issubclass(chrome_client.Session, chrome_client.Client))
        self.assertTrue(issubclass(chrome_client.AsyncSession, chrome_client.AsyncClient))
        self.assertIs(chrome_client.requests.Session, chrome_client.Session)
        self.assertIsInstance(chrome_client.Client(impersonate=None), chrome_client.Client)
        self.assertIsInstance(
            chrome_client.AsyncClient(impersonate=None), chrome_client.AsyncClient
        )

        expected = [
            "verify",
            "proxies",
            "timeout",
            "impersonate",
            "headers",
            "cookies",
            "auth",
            "proxy",
            "base_url",
            "params",
            "allow_redirects",
            "max_redirects",
            "default_headers",
            "timeout_ms",
            "default_domain",
            "random_tls_extension_order",
        ]
        self.assertEqual(list(inspect.signature(chrome_client.Client).parameters), expected)
        self.assertEqual(
            list(inspect.signature(chrome_client.AsyncClient).parameters), expected
        )

    def test_random_tls_extension_order_is_explicit(self):
        with chrome_client.Client(impersonate=None) as client:
            self.assertFalse(client.random_tls_extension_order)
        with chrome_client.Client(
            impersonate=None, random_tls_extension_order=True
        ) as client:
            self.assertTrue(client.random_tls_extension_order)

        from chrome_client import _client as client_module
        original_profiles = client_module._TLS_PROFILES_CACHE
        profile = {"tls_extensions": ["first", "second", "third"]}
        client_module._TLS_PROFILES_CACHE = {"test": profile}

        class ReverseRandom:
            def shuffle(self, values):
                values.reverse()

        try:
            PyCronetClient.create_calls.clear()
            with patch.object(client_module.random, "SystemRandom", return_value=ReverseRandom()):
                with chrome_client.Client(
                    impersonate="test", random_tls_extension_order=True
                ):
                    pass
            self.assertEqual(
                PyCronetClient.create_calls[-1][5],
                ["third", "second", "first"],
            )
            self.assertEqual(profile["tls_extensions"], ["first", "second", "third"])
        finally:
            client_module._TLS_PROFILES_CACHE = original_profiles

        for name in ("request", "get", "post", "put", "patch", "delete",
                     "head", "options", "trace", "query", "close"):
            self.assertTrue(hasattr(chrome_client.Client, name), name)
            self.assertTrue(hasattr(chrome_client.AsyncClient, name), name)

    def test_tls_profile_lists_are_not_shared_with_callers(self):
        from chrome_client import _client as client_module
        original_profiles = client_module._TLS_PROFILES_CACHE
        try:
            source = {"test": {"tls_extensions": ["a", "b"]}}
            chrome_client.set_tls_profiles(source)
            source["test"]["tls_extensions"].reverse()
            exported = chrome_client.get_tls_profiles()
            exported["test"]["tls_extensions"].reverse()
            self.assertEqual(
                client_module._load_tls_profile("test")["tls_extensions"],
                ["a", "b"],
            )
        finally:
            client_module._TLS_PROFILES_CACHE = original_profiles

    def test_tls_profile_registry_is_safe_during_concurrent_reads_and_writes(self):
        from concurrent.futures import ThreadPoolExecutor

        from chrome_client import _client as client_module
        original_profiles = client_module._TLS_PROFILES_CACHE
        try:
            chrome_client.set_tls_profiles({"test": {"tls_extensions": ["a", "b"]}})

            def read_profile(_):
                return client_module._load_tls_profile("test")["tls_extensions"]

            def write_profile(index):
                chrome_client.add_tls_profile(
                    "test", {"tls_extensions": ["a", "b", str(index)]}
                )

            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(read_profile, i) for i in range(64)]
                futures += [pool.submit(write_profile, i) for i in range(64)]
                for future in futures:
                    future.result()
        finally:
            client_module._TLS_PROFILES_CACHE = original_profiles

    def test_requests_namespace_and_session_attributes(self):
        session = chrome_client.requests.Session(impersonate=None)
        self.assertIs(session.headers, session.headers)
        self.assertIs(session.cookies, session.cookies)
        self.assertEqual(session.params, {})
        self.assertIsNone(session.timeout)
        self.assertIsNone(session.impersonate)
        session.close()

    def test_requests_first_request_and_response_surface(self):
        with chrome_client.Session(
            headers={"X-Session": "yes"},
            params={"a": "1"},
            impersonate=None,
        ) as session:
            response = session.post(
                "https://example.test/api",
                data={"x": "y"},
                params=[("b", "2"), ("b", "3")],
                auth=("user", "pass"),
            )

        self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(response.request.method, "POST")
        self.assertIn("a=1", response.request.url)
        self.assertIn("b=2", response.request.url)
        self.assertEqual(response.request.headers["X-Session"], "yes")
        self.assertTrue(response.ok)
        self.assertFalse(response.is_redirect)
        self.assertEqual(b"".join(response.iter_content(3)), response.content)
        self.assertEqual(list(response.iter_content(None)), [response.content])

        with chrome_client.Session(impersonate=None) as session:
            prepared = session.prepare_request(chrome_client.Request(
                "POST", "https://example.test/prepared",
                json={"value": 1}, params={"q": "x"},
            ))
            sent = session.send(prepared)
        self.assertEqual(sent.request.body, b'{"value": 1}')

        PyCronetClient.calls.clear()
        with chrome_client.Session(
            impersonate=None, params={"session": "1"}
        ) as session:
            prepared = session.prepare_request(chrome_client.Request(
                "POST", "https://example.test/prepared",
                json=[1, "two"], params={"q": "x"},
            ))
            session.send(prepared)
        url, _, _, body = PyCronetClient.calls[-1]
        self.assertEqual(url.count("session=1"), 1)
        self.assertEqual(url.count("q=x"), 1)
        self.assertEqual(body, b'[1, "two"]')

    def test_json_accepts_every_json_value(self):
        PyCronetClient.calls.clear()
        with chrome_client.Client(impersonate=None) as client:
            client.post("https://example.test/api", json=[1, True, None])
            self.assertEqual(PyCronetClient.calls[-1][3], b'[1, true, null]')
            client.post("https://example.test/api", json="value")
            self.assertEqual(PyCronetClient.calls[-1][3], b'"value"')

    def test_cookie_jar_accepts_mapping_protocol(self):
        jar = chrome_client.CookieJar()
        jar.update(UserDict({"token": "value"}))
        self.assertEqual(jar["token"], "value")
        with self.assertRaises(TypeError):
            jar.update([("token", "other")])
        jar.update(jar)

        PyCronetClient.calls.clear()
        with chrome_client.Client(impersonate=None, cookies={"global": "yes"}) as client:
            client.get("https://example.test/path")
        self.assertEqual(PyCronetClient.calls[-1][2]["cookie"], "global=yes")

    def test_session_cookie_jar_can_be_assigned(self):
        jar = chrome_client.CookieJar()
        jar["token"] = "value"

        with chrome_client.Session(impersonate=None) as session:
            session.cookies = jar
            self.assertIs(session.cookies, jar)
            session.cookies = UserDict({"other": "cookie"})
            self.assertEqual(session.cookies.get_dict(), {"other": "cookie"})

        async_session = chrome_client.AsyncSession(impersonate=None)
        async_session.cookies = jar
        self.assertIs(async_session.cookies, jar)
        asyncio.run(async_session.close())

    def test_stale_cookie_response_cannot_roll_back_newer_state(self):
        with chrome_client.Session(impersonate=None) as session:
            session._update_cookies_from_response(
                {"set-cookie": ["token=new; Path=/"]},
                "https://example.test/api",
                2,
            )
            session._update_cookies_from_response(
                {"set-cookie": ["token=old; Path=/"]},
                "https://example.test/api",
                1,
            )
            self.assertEqual(session.cookies.get("token"), "new")

            session._update_cookies_from_response(
                {"set-cookie": ["other=old; Path=/"]},
                "https://example.test/api",
                1,
            )
            self.assertEqual(session.cookies.get("other"), "old")

    def test_cookie_jar_is_safe_for_concurrent_updates(self):
        jar = chrome_client.CookieJar(default_domain="example.test")
        errors = []

        def worker(worker_id):
            try:
                for index in range(100):
                    jar.set("token-%d-%d" % (worker_id, index), "value")
                    jar.update_from_set_cookie(
                        ["server-%d-%d=ok; Path=/" % (worker_id, index)],
                        "https://example.test/api",
                    )
                    jar.cookies_for_request("https://example.test/api")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(jar.get_dict()), 1600)

    def test_cookie_path_expires_and_max_age_semantics(self):
        jar = chrome_client.CookieJar()
        with patch("chrome_client._cookies.time.time", return_value=1000):
            jar.update_from_set_cookie([
                "root=1; Path=/",
                "id=base; Path=/app",
                "id=deep; Path=/app/admin",
                "default_path=yes; SameSite=Lax",
                "wide=1; Domain=.example.test; Path=/",
                "foreign=bad; Domain=other.test; Path=/",
                "future=yes; Expires=Wed, 09 Jun 2038 10:18:14 GMT",
                "old=no; Expires=Thu, 01 Jan 1970 00:00:00 GMT",
                "short=alive; Max-Age=10; Expires=Thu, 01 Jan 1970 00:00:00 GMT",
                "locked=yes; Path=/; Secure",
            ], "https://example.test/app/login")

            values = [
                (cookie.name, cookie.value)
                for cookie in jar.cookies_for_request(
                    "https://example.test/app/admin/page"
                )
            ]
            self.assertEqual(values[:2], [("id", "deep"), ("id", "base")])
            self.assertIn(("default_path", "yes"), values)
            self.assertIn(("locked", "yes"), values)
            self.assertNotIn(("foreign", "bad"), values)
            self.assertNotIn(("old", "no"), values)

            self.assertEqual(
                [(cookie.name, cookie.value) for cookie in jar.cookies_for_request(
                    "https://sub.example.test/app/admin/page"
                )],
                [("wide", "1")],
            )
            self.assertNotIn(
                "locked",
                [cookie.name for cookie in jar.cookies_for_request(
                    "http://example.test/app/admin/page"
                )],
            )
            self.assertNotIn(
                "default_path",
                [cookie.name for cookie in jar.cookies_for_request(
                    "https://example.test/application"
                )],
            )

        with patch("chrome_client._cookies.time.time", return_value=1011):
            self.assertNotIn("short", jar)
            jar.clear_expired_cookies()

        with patch("chrome_client._cookies.time.time", return_value=1020):
            jar.update_from_set_cookie([
                "id=deleted; Path=/app; Max-Age=0; "
                "Expires=Wed, 09 Jun 2038 10:18:14 GMT"
            ], "https://example.test/app/login")
            remaining_ids = [
                cookie.value for cookie in jar.cookies_for_request(
                    "https://example.test/app/admin/page"
                ) if cookie.name == "id"
            ]
            self.assertEqual(remaining_ids, ["deep"])

        ipv6 = chrome_client.CookieJar()
        ipv6.update_from_set_cookie(["v6=yes; Path=/"], "https://[::1]/index")
        self.assertEqual(
            [(cookie.name, cookie.value) for cookie in ipv6.cookies_for_request(
                "https://[::1]/next"
            )],
            [("v6", "yes")],
        )

        session_only = chrome_client.CookieJar()
        session_only.set("session", "yes")
        session_only.set("persistent", "yes", expires=4102444800)
        session_only.clear_session_cookies()
        self.assertNotIn("session", session_only)
        self.assertIn("persistent", session_only)

    def test_cookie_header_preserves_path_order_and_request_override(self):
        jar = chrome_client.CookieJar()
        jar.set("root", "1", domain="example.test", path="/", host_only=True)
        jar.set("id", "base", domain="example.test", path="/app", host_only=True)
        jar.set("id", "deep", domain="example.test", path="/app/admin", host_only=True)

        PyCronetClient.calls.clear()
        with chrome_client.Client(impersonate=None, cookies=jar) as client:
            client.get("https://example.test/app/admin/page")
            cookie = PyCronetClient.calls[-1][2]["cookie"]
            self.assertEqual(cookie, "id=deep; id=base; root=1")

            client.get(
                "https://example.test/app/admin/page",
                cookies={"id": "request"},
            )
            cookie = PyCronetClient.calls[-1][2]["cookie"]
            self.assertEqual(cookie, "root=1; id=request")

    def test_response_set_cookie_path_and_deletion_flow(self):
        PyCronetClient.calls.clear()
        with chrome_client.Client(impersonate=None) as client:
            client.get("https://example.test/set-cookies")
            client.get("https://example.test/app/page")
            self.assertEqual(
                PyCronetClient.calls[-1][2]["cookie"],
                "scoped=1; root=1",
            )
            client.get("https://example.test/delete-cookie")
            client.get("https://example.test/app/page")
            self.assertEqual(PyCronetClient.calls[-1][2]["cookie"], "root=1")
            client.get("https://example.test/other")
            self.assertEqual(PyCronetClient.calls[-1][2]["cookie"], "root=1")

    def test_async_cookie_path_header_flow(self):
        async def run():
            async with chrome_client.AsyncClient(impersonate=None) as client:
                client.cookies.set(
                    "scoped", "yes", domain="example.test",
                    path="/app", host_only=True,
                )
                await client.get("https://example.test/app/page")
                matched = PyCronetClient.calls[-1][2].get("cookie")
                await client.get("https://example.test/other")
                missed = PyCronetClient.calls[-1][2].get("cookie")
                return matched, missed

        PyCronetClient.calls.clear()
        self.assertEqual(asyncio.run(run()), ("scoped=yes", None))

    def test_invalid_parameters_fail_loudly(self):
        with self.assertRaises(TypeError):
            chrome_client.Client(impersonate=None, unknown=True)
        with self.assertRaises(TypeError):
            chrome_client.Client(chrometls="chrome_150")
        with self.assertRaises(TypeError):
            chrome_client.get("https://example.test", chrometls="chrome_150")

        async def reject_old_parameter():
            await chrome_client.async_get(
                "https://example.test", chrometls="chrome_150"
            )

        with self.assertRaises(TypeError):
            asyncio.run(reject_old_parameter())
        with self.assertRaises(TypeError):
            chrome_client.Client(proxy="http://localhost:1", proxies={})
        with self.assertRaises(ValueError):
            chrome_client.Client(impersonate=None, timeout=(-1, 1))
        with chrome_client.Client(impersonate=None) as client:
            with self.assertRaises(chrome_client.RequestError):
                client.request("G\x00ET", "https://example.test")
            with self.assertRaises(chrome_client.RequestError):
                client.get("https://example.test/\x00")
            with self.assertRaises(ValueError):
                client.get("https://example.test", headers={"Bad\nName": "x"})
            with self.assertRaises(ValueError):
                client.get("https://example.test", cookies={"token": "x\r\ny"})

    def test_per_request_transport_overrides_use_compatible_client(self):
        with chrome_client.Client(impersonate=None, timeout=30) as client:
            response = client.get("https://example.test", timeout=5)
        self.assertEqual(response.status_code, 200)

    def test_async_client_uses_the_same_surface(self):
        async def run():
            async with chrome_client.AsyncSession(impersonate=None) as client:
                response = await client.post(
                    "https://example.test", json={"ok": True}, timeout=5
                )
                streamed = await client.get("https://example.test/stream", stream=True)
                lines = [line async for line in streamed.aiter_lines(chunk_size=2)]
                await streamed.aclose()
                prepared = client.prepare_request(chrome_client.Request(
                    "POST", "https://example.test/prepared", json=[1, 2]
                ))
                sent = await client.send(prepared)
                return response, lines, sent

        response, lines, sent = asyncio.run(run())
        self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(response.request.method, "POST")
        self.assertEqual(lines, [b"a", b"b", b"last"])
        self.assertEqual(sent.request.body, b'[1, 2]')

    def test_websocket_call_chain_matches_native_signature(self):
        events = []
        PyCronetClient.calls.clear()
        with chrome_client.Client(impersonate=None) as client:
            ws = client.websocket(
                "wss://example.test/ws",
                on_open=lambda app: events.append("open"),
                on_message=lambda app, message: events.append(message),
                on_close=lambda app, code, reason: events.append((code, reason)),
                sub_protocols=["chat", "json"],
                origin="https://example.test",
                headers={"X-Test": "yes"},
            )
            ws.run_forever()
        self.assertEqual(events, ["open", "hello", (1000, "done")])
        self.assertEqual(
            PyCronetClient.calls[-1],
            ("wss://example.test/ws", "chat, json", "https://example.test", [("X-Test", "yes")]),
        )

    def test_stream_line_splitting_and_download_chunking(self):
        with chrome_client.Client(impersonate=None) as client:
            response = client.get("https://example.test/stream", stream=True)
            self.assertEqual(list(response.iter_lines(chunk_size=2)), [b"a", b"b", b"last"])

            response = client.get("https://example.test/stream", stream=True)
            cached = response.content
            self.assertEqual(b"".join(response.iter_content(2)), cached)

            response = client.get("https://example.test/stream", stream=True)
            with self.assertRaises(ValueError):
                next(response.iter_content(0))
            response.close()

            with tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / "download.bin")
                result = client.download_file(
                    "https://example.test/file", path, chunk_size=2
                )
                self.assertEqual(Path(path).read_bytes(), b"a\r\nb\nlast")
                self.assertEqual(result["size"], 9)

    def test_redirect_history_method_and_auth_safety(self):
        PyCronetClient.calls.clear()
        with chrome_client.Client(impersonate=None) as client:
            response = client.post(
                "https://example.test/redirect",
                data={"x": "1"}, auth=("user", "pass"),
            )
        self.assertEqual(response.request.method, "GET")
        self.assertEqual(len(response.history), 1)
        self.assertEqual(response.history[0].status_code, 302)
        self.assertNotIn("Authorization", PyCronetClient.calls[-1][2])

        PyCronetClient.calls.clear()
        with chrome_client.Client(impersonate=None, auth=("user", "pass")) as client:
            client.get("https://example.test/redirect")
        self.assertNotIn("Authorization", PyCronetClient.calls[-1][2])

        PyCronetClient.calls.clear()
        with chrome_client.Client(impersonate=None, params={"session": "1"}) as client:
            client.get("https://example.test/redirect")
        self.assertNotIn("session=1", PyCronetClient.calls[-1][0])


if __name__ == "__main__":
    unittest.main()
