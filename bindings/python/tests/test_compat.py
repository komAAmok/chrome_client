"""Compatibility and lifecycle regression tests.

Run with the audited Core available:

    LD_LIBRARY_PATH=core/binaries/linux-x86_64 \
    PYTHONPATH=bindings/python:target/release \
    python -m unittest bindings/python/tests/test_compat.py
"""

import asyncio
import base64
import datetime
import gc
import hashlib
import json
import os
import shutil
import socket
import ssl
import struct
import sys
import tempfile
import threading
import time
import unittest

try:
    import cryptography  # noqa: F401
    _HAS_CRYPTOGRAPHY = True
except ImportError:  # the certificate tests generate their own chain
    _HAS_CRYPTOGRAPHY = False
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

try:
    from http.server import ThreadingHTTPServer
except ImportError:  # Python 3.6
    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

import chrome_client
from chrome_client import requests as cc_requests


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, body, status=200, extra=()):
        self.send_response(status)
        for name, value in extra:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/set-cookie"):
            return self._send(b"ok", extra=[("Set-Cookie", "sid=ABC; Path=/"),
                                            ("Set-Cookie", "taste=sweet; Path=/"),
                                            ("Content-Type", "text/plain")])
        if self.path.startswith("/echo-cookie"):
            return self._send(repr(self.headers.get_all("Cookie") or []).encode(),
                              extra=[("Content-Type", "text/plain")])
        if self.path.startswith("/redirect/"):
            depth = int(self.path.rsplit("/", 1)[-1])
            if depth > 0:
                self.send_response(302)
                self.send_header("Location", "/redirect/%d" % (depth - 1))
                self.send_header("Set-Cookie", "hop%d=y; Path=/" % depth)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._send(b"landed", extra=[("Content-Type", "text/plain")])
        if self.path.startswith("/status/"):
            return self._send(b"err", status=int(self.path.rsplit("/", 1)[-1]),
                              extra=[("Content-Type", "text/plain")])
        if self.path.startswith("/bytes/"):
            size = int(self.path.rsplit("/", 1)[-1])
            return self._send(b"y" * size,
                              extra=[("Content-Type", "application/octet-stream")])
        if self.path.startswith("/link"):
            return self._send(b"{}", extra=[
                ("Link", '<https://example.invalid/next>; rel="next"'),
                ("Content-Type", "application/json")])
        if self.path.startswith("/gbk"):
            return self._send("汉字".encode("gbk"),
                              extra=[("Content-Type", "text/html; charset=gbk")])
        if self.path.startswith("/slow"):
            time.sleep(1.0)
            return self._send(b"slow", extra=[("Content-Type", "text/plain")])
        return self._send(b'{"hello":"world"}',
                          extra=[("Content-Type", "application/json")])

    def do_POST(self):
        size = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(size) if size else b""
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            raw = b""
            while True:
                length = int(self.rfile.readline().strip(), 16)
                if length == 0:
                    self.rfile.readline()
                    break
                raw += self.rfile.read(length)
                self.rfile.readline()
        payload = json.dumps({
            "content_type": self.headers.get("Content-Type"),
            "authorization": self.headers.get("Authorization"),
            "referer": self.headers.get("Referer"),
            "body": raw.decode("utf-8", "replace"),
            "length": len(raw),
        }).encode()
        self._send(payload, extra=[("Content-Type", "application/json")])

    do_PUT = do_PATCH = do_DELETE = do_POST

    def do_HEAD(self):
        if self.path.startswith("/redirect/"):
            self.send_response(302)
            self.send_header("Location", "/redirect/0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", "17")
        self.end_headers()

    def log_message(self, *_args):
        pass


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen = []

    def do_GET(self):
        type(self).seen.append(self.path)
        body = ("proxied:" + self.path).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()
        cls.url = "http://127.0.0.1:%d/" % cls.server.server_address[1]

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


class SessionStateTests(Base):
    """The session has to stay a session: cookies and connections persist."""

    def test_cookies_persist_and_are_visible(self):
        with chrome_client.Session() as session:
            session.get(self.url + "set-cookie")
            # Duplicate Set-Cookie fields must both survive, which means the
            # header parser cannot collapse them.
            self.assertEqual(session.cookies.get_dict(),
                             {"sid": "ABC", "taste": "sweet"})
            echoed = session.get(self.url + "echo-cookie").text
            self.assertIn("sid=ABC", echoed)
            self.assertIn("taste=sweet", echoed)

    def test_response_cookies_are_separate_from_the_jar(self):
        with chrome_client.Session() as session:
            response = session.get(self.url + "set-cookie")
            self.assertEqual(response.cookies.get_dict(),
                             {"sid": "ABC", "taste": "sweet"})
            plain = session.get(self.url)
            self.assertEqual(plain.cookies.get_dict(), {})

    def test_per_request_override_keeps_the_session(self):
        """A per-request override must not silently start a new session.

        Overrides need a differently configured engine, and an engine owns the
        cookie store, so the facade has to carry the jar across.
        """
        with chrome_client.Session() as session:
            session.get(self.url + "set-cookie")
            for _ in range(3):
                echoed = session.get(self.url + "echo-cookie", verify=False).text
                self.assertIn("sid=ABC", echoed)
            # One engine for the default configuration, one for verify=False --
            # not one per request.
            self.assertEqual(len(session._engines), 2)

    def test_engine_is_reused_across_requests(self):
        built = []
        original = chrome_client.engine.EngineConfig.build

        def counting(config):
            built.append(config)
            return original(config)

        chrome_client.engine.EngineConfig.build = counting
        try:
            with chrome_client.Session() as session:
                for _ in range(5):
                    session.get(self.url)
                for _ in range(5):
                    session.get(self.url, impersonate="chrome_151")
        finally:
            chrome_client.engine.EngineConfig.build = original
        self.assertEqual(len(built), 2)

    def test_caller_cookie_edits_take_effect(self):
        with chrome_client.Session() as session:
            session.get(self.url + "set-cookie")
            session.cookies.set("extra", "1", domain="127.0.0.1", path="/")
            echoed = session.get(self.url + "echo-cookie").text
            self.assertIn("extra=1", echoed)
            self.assertIn("sid=ABC", echoed)

            del session.cookies["sid"]
            echoed = session.get(self.url + "echo-cookie").text
            self.assertNotIn("sid=ABC", echoed)
            self.assertIn("taste=sweet", echoed)

            session.cookies.clear()
            self.assertEqual(session.cookies.get_dict(), {})
            self.assertEqual(session.get(self.url + "echo-cookie").text, "[]")

    def test_cookie_jar_api(self):
        jar = chrome_client.CookieJar()
        jar.set("a", "1", domain="example.invalid", path="/")
        jar.set("b", "2", domain="other.invalid", path="/sub")
        self.assertEqual(jar["a"], "1")
        self.assertEqual(jar.get("missing", "fallback"), "fallback")
        self.assertEqual(sorted(jar.keys()), ["a", "b"])
        self.assertEqual(jar.get_dict(domain="other.invalid"), {"b": "2"})
        self.assertEqual(sorted(jar.list_domains()), ["example.invalid", "other.invalid"])
        self.assertEqual(jar.copy().get_dict(), jar.get_dict())
        jar.update({"c": "3"})
        self.assertIn("c", jar)
        del jar["c"]
        self.assertNotIn("c", jar)
        self.assertEqual(chrome_client.dict_from_cookiejar(jar), jar.get_dict())

    def test_module_level_calls_share_one_engine(self):
        built = []
        original = chrome_client.engine.EngineConfig.build

        def counting(config):
            built.append(config)
            return original(config)

        chrome_client.close_shared_session()
        chrome_client.engine.EngineConfig.build = counting
        try:
            for _ in range(4):
                self.assertEqual(chrome_client.get(self.url, timeout=5).status_code, 200)
        finally:
            chrome_client.engine.EngineConfig.build = original
            chrome_client.close_shared_session()
        self.assertEqual(len(built), 1)


class ProxyTests(Base):
    @classmethod
    def setUpClass(cls):
        super(ProxyTests, cls).setUpClass()
        ProxyHandler.seen = []
        cls.proxy = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
        cls.proxy_thread = threading.Thread(target=cls.proxy.serve_forever)
        cls.proxy_thread.daemon = True
        cls.proxy_thread.start()
        cls.proxy_url = "http://127.0.0.1:%d" % cls.proxy.server_address[1]
        # Chromium bypasses proxies for loopback, so routing can only be proven
        # against a name that does not resolve locally.
        cls.target = "http://proxy-target.test/hello"

    @classmethod
    def tearDownClass(cls):
        cls.proxy.shutdown()
        cls.proxy_thread.join(timeout=5)
        super(ProxyTests, cls).tearDownClass()

    @classmethod
    def _target_resolves(cls):
        """True when the resolver answers for a name that should not exist.

        Some resolvers synthesise addresses for unknown names. Where that
        happens, "fails without a proxy" is a property of the resolver, not of
        this code, so the assertion is skipped rather than reported as a bug.
        """
        try:
            socket.getaddrinfo("proxy-target.test", 80)
            return True
        except socket.gaierror:
            return False

    def test_proxies_mapping_is_mutable_and_routes(self):
        with chrome_client.Session() as session:
            if not self._target_resolves():
                with self.assertRaises(chrome_client.RequestException):
                    session.get(self.target, timeout=4)
            session.proxies.update({"http": self.proxy_url})
            response = session.get(self.target, timeout=10)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.text, "proxied:" + self.target)
            session.proxies.clear()
            if not self._target_resolves():
                with self.assertRaises(chrome_client.RequestException):
                    session.get(self.target, timeout=4)

    def test_per_request_proxy_and_scheme_keys(self):
        with chrome_client.Session() as session:
            self.assertEqual(
                session.get(self.target, proxy=self.proxy_url, timeout=10).status_code, 200)
            self.assertEqual(
                session.get(self.target, proxies={"all": self.proxy_url},
                            timeout=10).status_code, 200)
            self.assertLessEqual(len(session._engines), 2)

    def test_cookies_survive_a_proxy_change(self):
        with chrome_client.Session() as session:
            session.cookies.set("sid", "KEEP", domain="proxy-target.test", path="/")
            session.get(self.target, proxy=self.proxy_url, timeout=10)
            self.assertEqual(session.cookies.get_dict(), {"sid": "KEEP"})
            self.assertTrue(any("hello" in path for path in ProxyHandler.seen))


class RequestsSurfaceTests(Base):
    """Names and behaviours code written against ``requests`` relies on."""

    def test_requests_namespace_exposes_session(self):
        self.assertIs(cc_requests.Session, chrome_client.Session)
        self.assertIs(chrome_client.requests.Session, chrome_client.Session)
        with cc_requests.Session() as session:
            session.get(self.url + "set-cookie")
            self.assertIn("sid=ABC", session.get(self.url + "echo-cookie").text)
        from chrome_client.requests.exceptions import Timeout
        self.assertIs(Timeout, chrome_client.Timeout)

    def test_exception_hierarchy(self):
        self.assertTrue(issubclass(chrome_client.RequestException, IOError))
        self.assertTrue(issubclass(chrome_client.HTTPError, chrome_client.RequestException))
        self.assertTrue(issubclass(chrome_client.ProxyError, chrome_client.ConnectionError))
        self.assertTrue(issubclass(chrome_client.SSLError, chrome_client.ConnectionError))
        self.assertTrue(issubclass(chrome_client.ConnectTimeout, chrome_client.Timeout))
        self.assertTrue(issubclass(chrome_client.ConnectTimeout,
                                   chrome_client.ConnectionError))
        self.assertTrue(issubclass(chrome_client.ReadTimeout, chrome_client.Timeout))
        self.assertTrue(issubclass(chrome_client.JSONDecodeError, ValueError))
        self.assertTrue(issubclass(chrome_client.MissingSchema, ValueError))
        self.assertIs(chrome_client.RequestsError, chrome_client.RequestException)

    def test_raise_for_status_and_ok(self):
        with chrome_client.Session() as session:
            response = session.get(self.url + "status/404")
            self.assertFalse(response.ok)
            self.assertFalse(bool(response))
            with self.assertRaises(chrome_client.HTTPError) as caught:
                response.raise_for_status()
            self.assertIs(caught.exception.response, response)
            self.assertIs(session.get(self.url).raise_for_status().status_code.__class__,
                          int)

    def test_response_attributes(self):
        with chrome_client.Session() as session:
            response = session.get(self.url + "link")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.reason, "OK")
            self.assertEqual(response.http_version, "HTTP/1.1")
            self.assertEqual(response.url, self.url + "link")
            self.assertEqual(response.history, [])
            self.assertEqual(list(response.links), ["next"])
            self.assertEqual(response.links["next"]["url"],
                             "https://example.invalid/next")
            self.assertGreaterEqual(response.elapsed.total_seconds(), 0)
            self.assertIsNotNone(response.request)
            self.assertEqual(response.request.method, "GET")
            self.assertFalse(response.is_redirect)
            self.assertFalse(response.is_permanent_redirect)
            self.assertIsNone(response.next)
            self.assertEqual(response.json(), {})
            self.assertEqual(repr(response), "<Response [200]>")

    def test_headers_preserve_case_and_duplicates(self):
        with chrome_client.Session() as session:
            headers = session.get(self.url + "set-cookie").headers
            self.assertEqual(headers["CONTENT-TYPE"], "text/plain")
            self.assertIn("Content-Type", list(headers))
            self.assertEqual(len(headers.get_list("set-cookie")), 2)
            self.assertEqual(headers["set-cookie"], "sid=ABC; Path=/, taste=sweet; Path=/")
            self.assertIsInstance(headers, chrome_client.CaseInsensitiveDict)

    def test_case_insensitive_dict(self):
        mapping = chrome_client.CaseInsensitiveDict({"Content-Type": "text/plain"})
        self.assertEqual(list(mapping.keys()), ["Content-Type"])
        self.assertEqual(mapping["content-type"], "text/plain")
        self.assertEqual(mapping.pop("CONTENT-TYPE"), "text/plain")
        self.assertEqual(len(mapping), 0)
        mapping["A"] = "1"
        self.assertEqual(mapping.copy(), {"a": "1"})
        self.assertEqual(mapping.lower_items(), [("a", "1")])

    def test_bodies(self):
        with chrome_client.Session() as session:
            payload = session.post(self.url, json={"a": 1}).json()
            self.assertEqual(payload["content_type"], "application/json")
            self.assertEqual(payload["body"], '{"a": 1}')

            payload = session.post(self.url, data={"a": "1", "b": ["2", "3"]}).json()
            self.assertEqual(payload["content_type"],
                             "application/x-www-form-urlencoded")
            self.assertEqual(payload["body"], "a=1&b=2&b=3")

            # A sequence of pairs is form data, not an iterator to stream.
            payload = session.post(self.url, data=[("a", "1"), ("b", "2")]).json()
            self.assertEqual(payload["body"], "a=1&b=2")

            payload = session.post(self.url, data=b"raw-bytes").json()
            self.assertEqual(payload["body"], "raw-bytes")

    def test_files_multipart(self):
        with chrome_client.Session() as session:
            payload = session.post(self.url, data={"title": "t"},
                                   files={"f": ("a.txt", b"PAYLOAD", "text/plain")}).json()
            self.assertTrue(payload["content_type"].startswith("multipart/form-data;"))
            self.assertIn("PAYLOAD", payload["body"])
            self.assertIn('filename="a.txt"', payload["body"])

    def test_auth(self):
        with chrome_client.Session() as session:
            payload = session.post(self.url, auth=("user", "pass")).json()
            self.assertEqual(payload["authorization"], "Basic dXNlcjpwYXNz")
            handler = chrome_client.HTTPBasicAuth("user", "pass")
            payload = session.post(self.url, auth=handler).json()
            self.assertEqual(payload["authorization"], "Basic dXNlcjpwYXNz")

    def test_referer_fails_closed(self):
        """Chromium strips a caller-set Referer, so the facade refuses it.

        Accepting it would leave the request looking unreferred with no way for
        the caller to tell.
        """
        with chrome_client.Session() as session:
            with self.assertRaises(chrome_client.UnsupportedFeature):
                session.get(self.url, referer="https://example.invalid/")
            with self.assertRaises(chrome_client.UnsupportedFeature):
                session.get(self.url, headers={"Referer": "https://example.invalid/"})
        with self.assertRaises(chrome_client.UnsupportedFeature):
            chrome_client.Session(headers={"Referer": "https://example.invalid/"})

    def test_facade_adds_no_headers_of_its_own(self):
        """The profile owns the default header set; the facade must not seed it."""
        self.assertEqual(dict(chrome_client.utils.default_headers()), {})
        with chrome_client.Session() as session:
            self.assertEqual(dict(session.headers), {})

    def test_profile_conflict_is_mapped(self):
        with self.assertRaises(chrome_client.ImpersonateError):
            chrome_client.Session(impersonate="chrome_152",
                                  user_agent="Conflicting/1.0").get(self.url)

    def test_timeout_tuple_is_accepted(self):
        with chrome_client.Session() as session:
            self.assertEqual(session.get(self.url, timeout=(1.0, 5.0)).status_code, 200)
            with self.assertRaises(chrome_client.Timeout):
                session.get(self.url + "slow", timeout=(0.01, 0.01))

    def test_params_encoding(self):
        with chrome_client.Session() as session:
            response = session.get(self.url, params={"a": "1", "b": None, "c": ["x", "y"]})
            self.assertIn("a=1", response.url)
            self.assertNotIn("b=", response.url)
            self.assertIn("c=x&c=y", response.url)

    def test_request_prepare_and_send(self):
        with chrome_client.Session() as session:
            request = chrome_client.Request("POST", self.url, json={"k": "v"})
            prepared = session.prepare_request(request)
            self.assertIsInstance(prepared, chrome_client.PreparedRequest)
            self.assertEqual(prepared.method, "POST")
            self.assertEqual(prepared.headers["content-type"], "application/json")
            self.assertEqual(prepared.path_url, "/")
            self.assertEqual(session.send(prepared).json()["body"], '{"k": "v"}')
            with self.assertRaises(ValueError):
                session.send(request)

    def test_hooks_and_adapters(self):
        seen = []
        with chrome_client.Session(hooks={"response": lambda r: seen.append(r.status_code)}) \
                as session:
            session.get(self.url)
            self.assertEqual(seen, [200])
            self.assertIsInstance(session.get_adapter(self.url),
                                  chrome_client.HTTPAdapter)
            with self.assertRaises(chrome_client.InvalidSchema):
                session.get_adapter("ftp://example.invalid")

    def test_custom_adapter_is_used(self):
        class Recorded(chrome_client.BaseAdapter):
            def __init__(self):
                chrome_client.BaseAdapter.__init__(self)
                self.calls = []

            def send(self, request, **kwargs):
                self.calls.append(request.url)
                response = chrome_client.Response()
                response.status_code = 418
                response.url = request.url
                response.content = b"stub"
                return response

            def close(self):
                pass

        adapter = Recorded()
        with chrome_client.Session() as session:
            session.mount("http://", adapter)
            response = session.get(self.url)
        self.assertEqual(response.status_code, 418)
        self.assertEqual(response.content, b"stub")
        self.assertEqual(len(adapter.calls), 1)

    def test_encoding_and_text(self):
        with chrome_client.Session() as session:
            response = session.get(self.url + "gbk")
            self.assertEqual(response.text, "汉字")
            self.assertEqual(response.charset, "gbk")
            response.encoding = "gbk"
            # Setting `encoding` must not rewrite the response headers.
            self.assertEqual(response.headers["content-type"], "text/html; charset=gbk")
            self.assertEqual(response.text, "汉字")

    def test_codes_lookup(self):
        self.assertEqual(chrome_client.codes.ok, 200)
        self.assertEqual(chrome_client.codes.not_found, 404)
        self.assertEqual(chrome_client.codes.too_many_requests, 429)
        self.assertEqual(chrome_client.codes.OK, 200)

    def test_head_does_not_follow_redirects_by_default(self):
        with chrome_client.Session() as session:
            response = session.head(self.url + "redirect/1")
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.content, b"")


class RedirectTests(Base):
    def test_history_and_final_url(self):
        with chrome_client.Session() as session:
            response = session.get(self.url + "redirect/3")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.text, "landed")
            self.assertEqual(response.url, self.url + "redirect/0")
            self.assertEqual([hop.status_code for hop in response.history], [302, 302, 302])
            self.assertEqual(response.history[0].url, self.url + "redirect/3")
            self.assertEqual(response.redirect_count, 3)

    def test_hop_cookies_are_mirrored(self):
        with chrome_client.Session() as session:
            session.get(self.url + "redirect/2")
            self.assertEqual(session.cookies.get_dict(), {"hop2": "y", "hop1": "y"})

    def test_allow_redirects_false_returns_the_redirect(self):
        with chrome_client.Session() as session:
            response = session.get(self.url + "redirect/1", allow_redirects=False)
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.is_redirect)
            self.assertEqual(response.headers["location"], "/redirect/0")
            self.assertEqual(response.url, self.url + "redirect/1")
            self.assertEqual(response.history, [])
            self.assertEqual(response.next.url, self.url + "redirect/0")

    def test_max_redirects_is_enforced(self):
        with chrome_client.Session() as session:
            with self.assertRaises(chrome_client.TooManyRedirects):
                session.get(self.url + "redirect/5", max_redirects=2)
            response = session.get(self.url + "redirect/2", max_redirects=5)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.history), 2)

    def test_resolve_redirects_generator(self):
        with chrome_client.Session() as session:
            first = session.get(self.url + "redirect/2", allow_redirects=False)
            chain = list(session.resolve_redirects(first, first.request))
            self.assertEqual(chain[-1].status_code, 200)
            self.assertEqual(chain[-1].text, "landed")

    def test_async_allow_redirects_false(self):
        async def run():
            async with chrome_client.AsyncSession() as session:
                response = await session.get(self.url + "redirect/1",
                                             allow_redirects=False)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["location"], "/redirect/0")
                followed = await session.get(self.url + "redirect/2")
                self.assertEqual(followed.status_code, 200)
                self.assertEqual(len(followed.history), 2)

        self.run_async(run())


class CurlCffiSurfaceTests(Base):
    def test_impersonate_aliases(self):
        for target in ("chrome", "chrome152", "chrome_152", "chrome151"):
            with chrome_client.Session(impersonate=target) as session:
                self.assertEqual(session.get(self.url).status_code, 200)
        self.assertEqual(chrome_client.normalize_impersonate("chrome136"), "chrome_136")
        self.assertEqual(chrome_client.normalize_impersonate("chrome_136"), "chrome_136")
        self.assertIn("chrome_152", chrome_client.available_profiles())

    def test_unavailable_profiles_fail_closed(self):
        for target in ("safari17", "firefox133", "edge101", "tor145", "chrome_9"):
            with self.assertRaises(chrome_client.ImpersonateError):
                chrome_client.Session(impersonate=target)

    def test_fingerprint_overrides_fail_closed(self):
        with self.assertRaises(chrome_client.UnsupportedFeature):
            chrome_client.Session(ja3="771,4865,0,29-23-24,0")
        with self.assertRaises(chrome_client.UnsupportedFeature):
            chrome_client.Session(akamai="1:65536;2:0;4:6291456")
        with self.assertRaises(chrome_client.UnsupportedFeature):
            chrome_client.Session(extra_fp=chrome_client.ExtraFingerprints(
                tls_permute_extensions=True))
        with self.assertRaises(chrome_client.UnsupportedFeature):
            chrome_client.Session(cert="/tmp/client.pem")

    def test_extra_fp_header_order_is_honoured(self):
        order = ["Host", "X-Second", "X-First"]
        fingerprint = chrome_client.ExtraFingerprints(header_order=order)
        with chrome_client.Session(extra_fp=fingerprint) as session:
            prepared = session.prepare_request(chrome_client.Request(
                "GET", self.url, headers={"X-First": "1", "X-Second": "2"}))
            emitted = [name for name, _value in prepared.wire_headers()]
            self.assertLess(emitted.index("X-Second"), emitted.index("X-First"))

    def test_http_version_pinning(self):
        for value in ("v1", "http/1.1", "h1", chrome_client.CurlHttpVersion.V1_1):
            with chrome_client.Session(http_version=value) as session:
                self.assertEqual(session.get(self.url).status_code, 200)
        self.assertEqual(chrome_client.normalize_http_version("h2"), "v2")
        self.assertEqual(chrome_client.normalize_http_version("http/3"), "v3")
        with self.assertRaises(chrome_client.UnsupportedFeature):
            chrome_client.Session(http_version="quic")

    def test_curlmime(self):
        mime = chrome_client.CurlMime()
        mime.addpart(name="title", data="hello")
        mime.addpart(name="photo", filename="p.jpg", content_type="image/jpeg",
                     data=b"\x01\x02")
        with chrome_client.Session() as session:
            payload = session.post(self.url, multipart=mime).json()
        self.assertTrue(payload["content_type"].startswith("multipart/form-data;"))
        self.assertIn('name="photo"', payload["body"])
        mime.close()

    def test_content_kwarg_and_callback(self):
        chunks = []
        with chrome_client.Session() as session:
            payload = session.post(self.url, content=b"exact-bytes",
                                   content_callback=chunks.append).json()
        self.assertEqual(payload["body"], "exact-bytes")
        self.assertEqual(len(chunks), 1)

    def test_stream_context_manager_and_flag(self):
        with chrome_client.Session() as session:
            # `session.stream` is both the requests flag and curl_cffi's helper.
            self.assertFalse(session.stream)
            with session.stream("GET", self.url + "bytes/50000") as response:
                total = sum(len(chunk) for chunk in response.iter_content(4096))
            self.assertEqual(total, 50000)
            session.stream = True
            self.assertTrue(session.stream)
            response = session.get(self.url + "bytes/1000")
            self.assertEqual(len(b"".join(response.iter_content(128))), 1000)

    def test_raise_for_status_session_flag(self):
        with chrome_client.Session(raise_for_status=True) as session:
            with self.assertRaises(chrome_client.HTTPError):
                session.get(self.url + "status/500")

    def test_retry_strategy(self):
        attempts = chrome_client.RetryStrategy(count=2, delay=0.01,
                                              backoff="exponential")
        self.assertEqual(attempts.sleep_for(1), 0.01)
        self.assertEqual(attempts.sleep_for(2), 0.02)
        with chrome_client.Session(retry=attempts) as session:
            self.assertEqual(session.get(self.url + "status/503").status_code, 503)

    def test_discard_cookies(self):
        with chrome_client.Session(discard_cookies=True) as session:
            session.get(self.url + "set-cookie")
            self.assertEqual(session.cookies.get_dict(), {})

    def test_base_url(self):
        with chrome_client.Session(base_url=self.url) as session:
            self.assertEqual(session.get("link").status_code, 200)

    def test_async_stream_failure_surfaces(self):
        """A streaming failure must wake the consumer.

        The streaming consumer waits on an event, not on the response future, so
        resolving only the future left it asleep forever.
        """
        async def run():
            async with chrome_client.AsyncSession() as session:
                response = await session.get(self.url + "bytes/40000", stream=True,
                                             max_response_bytes=1024)
                with self.assertRaises(chrome_client.ResponseTooLarge):
                    async for _chunk in response.aiter_content():
                        pass
                with self.assertRaises(chrome_client.ResponseTooLarge):
                    await session.get(self.url + "bytes/40000", max_response_bytes=1024)
                with self.assertRaises(chrome_client.Timeout):
                    await session.get(self.url + "slow", stream=True, timeout=0.05)

        self.run_async(run())

    def test_async_stream_helpers(self):
        async def run():
            async with chrome_client.AsyncSession() as session:
                response = await session.get(self.url + "bytes/40000", stream=True)
                total = 0
                async for chunk in response.aiter_content(4096):
                    total += len(chunk)
                self.assertEqual(total, 40000)
                buffered = await session.get(self.url)
                self.assertEqual(await buffered.atext(), '{"hello":"world"}')
                async with session.stream("GET", self.url + "bytes/2048") as streamed:
                    lines = [line async for line in streamed.aiter_lines()]
                self.assertEqual(sum(len(line) for line in lines), 2048)

        self.run_async(run())


class LifecycleTests(Base):
    """Ownership: nothing may stay alive after a request finishes."""

    def _rss_kb(self):
        with open("/proc/self/statm") as handle:
            return int(handle.read().split()[1]) * (os.sysconf("SC_PAGE_SIZE") // 1024)

    @unittest.skipUnless(sys.platform.startswith("linux"), "reads /proc/self/statm")
    def test_async_requests_do_not_leak(self):
        """Completed async requests must release their asyncio bridge.

        The bridge installs a Core callback holding the event loop and the notify
        callable, and notify holds the request, so the cycle runs through Rust
        where Python's collector cannot see it. Before the terminal transition
        cleared the callback this leaked about 12 KiB per request.

        A leak is linear in request count, so this compares two equal batches
        rather than testing an absolute number: an allocator that is still
        warming up grows much less on the second batch, while a leak grows the
        same amount again. That distinction holds on any machine, whereas an
        absolute KiB threshold depends on core count and allocator arenas.
        """
        async def drive(session, count):
            for _ in range(count):
                response = await session.get(self.url)
                response.content

        batch = 400
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            session = chrome_client.AsyncSession()
            loop.run_until_complete(drive(session, 200))   # warm up
            gc.collect()
            start = self._rss_kb()
            loop.run_until_complete(drive(session, batch))
            gc.collect()
            first = self._rss_kb() - start
            middle = self._rss_kb()
            loop.run_until_complete(drive(session, batch))
            gc.collect()
            second = self._rss_kb() - middle
            loop.run_until_complete(session.aclose())
        finally:
            asyncio.set_event_loop(None)
            loop.close()
        # At the old rate each batch cost ~4.7 MiB, and the second cost as much
        # as the first. Allow generous absolute slack for allocator behaviour but
        # require the growth to stop scaling with request count.
        message = ("first %d KiB, second %d KiB over %d requests each"
                   % (first, second, batch))
        self.assertLess(second, 2048, message)
        self.assertLess(second, max(first, 512), message)

    def test_abandoned_stream_is_released(self):
        with chrome_client.Session() as session:
            for _ in range(5):
                session.get(self.url + "bytes/100000", stream=True)
            gc.collect()
            # A later request on the same engine must still complete promptly; an
            # abandoned paused body used to hold a Core callback slot.
            begin = time.time()
            self.assertEqual(session.get(self.url, timeout=5).status_code, 200)
            self.assertLess(time.time() - begin, 5.0)

    def test_close_is_idempotent_and_fails_closed(self):
        session = chrome_client.Session()
        session.close()
        session.close()
        with self.assertRaises(chrome_client.RequestException):
            session.get(self.url)

    def test_engine_cache_is_bounded(self):
        with chrome_client.Session(max_engines=2) as session:
            for major in (150, 151, 152):
                session.get(self.url, impersonate="chrome_%d" % major)
            self.assertLessEqual(len(session._engines), 2)

    def test_response_raw_is_file_like(self):
        with chrome_client.Session() as session:
            streamed = session.get(self.url + "bytes/5000", stream=True)
            self.assertEqual(len(streamed.raw.read(10)), 10)
            self.assertEqual(len(streamed.raw.read(90)), 90)
            self.assertEqual(len(streamed.raw.read()), 4900)
            chunked = session.get(self.url + "bytes/5000", stream=True)
            self.assertEqual(sum(len(block) for block in chunked.raw.stream(1024)), 5000)
            buffered = session.get(self.url + "bytes/512")
            self.assertEqual(len(buffered.raw.read()), 512)

    def test_net_errors_map_to_named_exceptions(self):
        from chrome_client.exceptions import describe_net_error, map_native_error
        self.assertEqual(describe_net_error(-105), "ERR_NAME_NOT_RESOLVED")
        self.assertEqual(describe_net_error(-202), "ERR_CERT_AUTHORITY_INVALID")
        self.assertIsNone(describe_net_error(1))
        mapped = map_native_error("Network (Chromium net error -105)")
        self.assertIsInstance(mapped, chrome_client.DNSError)
        self.assertIn("ERR_NAME_NOT_RESOLVED", str(mapped))
        self.assertIsInstance(map_native_error("Tls (Chromium net error -201)"),
                              chrome_client.CertificateVerifyError)
        self.assertIsInstance(map_native_error("Network (Chromium net error -118)"),
                              chrome_client.ConnectTimeout)
        self.assertIsInstance(map_native_error("Proxy (Chromium net error -130)"),
                              chrome_client.ProxyError)
        self.assertIsInstance(map_native_error("Network (Chromium net error -324)"),
                              chrome_client.ConnectionError)
        self.assertIsInstance(map_native_error("Protocol (Chromium net error -321)"),
                              chrome_client.ChunkedEncodingError)
        # An unknown code keeps the coarse category rather than guessing.
        self.assertIsInstance(map_native_error("Network (Chromium net error -99999)"),
                              chrome_client.ConnectionError)

    def test_unreachable_host_is_a_connection_error(self):
        """Any transport failure lands under ``ConnectionError``.

        The precise cause is environment-dependent -- a resolver that hijacks
        NXDOMAIN answers turns ERR_NAME_NOT_RESOLVED into ERR_EMPTY_RESPONSE --
        so this pins the branch of the hierarchy, and
        ``test_net_errors_map_to_named_exceptions`` pins each code.
        """
        try:
            socket.getaddrinfo("this-host-does-not-exist.invalid", 80)
        except socket.gaierror:
            pass
        else:
            self.skipTest("the resolver answers for .invalid names")
        with chrome_client.Session() as session:
            with self.assertRaises(chrome_client.ConnectionError):
                session.get("http://this-host-does-not-exist.invalid/", timeout=8)

    def test_response_is_picklable(self):
        import pickle
        with chrome_client.Session() as session:
            response = session.get(self.url)
            restored = pickle.loads(pickle.dumps(response))
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.content, response.content)


class ConcurrencyTests(Base):
    def test_thread_pool(self):
        with chrome_client.Session() as session:
            with ThreadPoolExecutor(max_workers=32) as pool:
                codes = list(pool.map(lambda _: session.get(self.url).status_code,
                                      range(200)))
        self.assertEqual(len(codes), 200)
        self.assertEqual(set(codes), {200})

    def test_thread_pool_with_one_session_per_thread(self):
        local = threading.local()

        def fetch(_index):
            if not hasattr(local, "session"):
                local.session = chrome_client.Session()
            return local.session.get(self.url).status_code

        with ThreadPoolExecutor(max_workers=8) as pool:
            codes = list(pool.map(fetch, range(120)))
        self.assertEqual(set(codes), {200})

    def test_async_gather_scales(self):
        """2000 requests in flight must not cost more than the transport does.

        Each target carries a distinct query string. Requests that share an HTTP
        cache key serialise inside Chromium's ``HttpCache``, so hammering one URL
        measures that lock rather than the async path -- see
        ``test_same_cache_key_requests_serialize``.
        """
        counts = (64, 512, 2000) if os.environ.get("CHROME_CLIENT_TIMING_TESTS") == "1" \
            else (64, 512)

        async def run():
            async with chrome_client.AsyncSession() as session:
                for count in counts:
                    targets = ["%s?i=%d" % (self.url, index) for index in range(count)]
                    # Chromium allows 6 concurrent HTTP/1.1 connections per host
                    # group, so the floor is count/6 round trips however fast the
                    # machine is; budget generously rather than assume core count.
                    responses = await asyncio.wait_for(
                        asyncio.gather(*[session.get(url) for url in targets]),
                        60 + count / 4)
                    self.assertEqual(len(responses), count)
                    self.assertTrue(all(r.status_code == 200 for r in responses))

        self.run_async(run())

    def test_same_cache_key_requests_serialize(self):
        """Records a Chromium behaviour that looks like a client bottleneck.

        With the cache on, concurrent requests for one URL queue behind a single
        ``HttpCache`` entry, so a burst is much slower than the same burst spread
        over distinct keys. ``cache=False`` removes the difference, which is what
        identifies the cache rather than the binding as the cause.

        The gap widens with burst size -- measured 1.3x at 400 requests, 2.4x at
        1000, 3.9x at 2000 on a 12-core machine.

        The timing comparison only runs when CHROME_CLIENT_TIMING_TESTS=1: a
        wall-clock ratio is a property of the host, not of this code, so it must
        not gate CI. What always runs is the functional half -- that a same-key
        burst completes correctly.
        """
        async def measure(session, count, distinct):
            targets = ["%s?i=%d" % (self.url, index) if distinct else self.url
                       for index in range(count)]
            began = time.monotonic()
            await asyncio.gather(*[session.get(url) for url in targets])
            return time.monotonic() - began

        count = 1000 if os.environ.get("CHROME_CLIENT_TIMING_TESTS") == "1" else 200

        async def run():
            async with chrome_client.AsyncSession() as cached:
                same = await measure(cached, count, False)
                spread = await measure(cached, count, True)
            async with chrome_client.AsyncSession(cache=False) as uncached:
                without = await measure(uncached, count, False)
            if os.environ.get("CHROME_CLIENT_TIMING_TESTS") != "1":
                return
            # Distinct keys, and a disabled cache, both run at the transport's
            # own rate; only same-key-with-cache pays the serialisation.
            self.assertLess(spread * 1.8, same)
            self.assertLess(without * 1.8, same)

        self.run_async(run())

    def test_async_max_clients_bounds_inflight(self):
        async def run():
            async with chrome_client.AsyncSession(max_clients=4) as session:
                responses = await asyncio.gather(
                    *[session.get(self.url) for _ in range(50)])
                self.assertEqual(len(responses), 50)

        self.run_async(run())

    def test_large_body_batched_polling(self):
        async def run():
            async with chrome_client.AsyncSession() as session:
                response = await session.get(self.url + "bytes/4194304")
                self.assertEqual(len(response.content), 4194304)

        self.run_async(run())

    def test_process_pool(self):
        """Process pools must use a spawning start method.

        Once an engine exists the process is multi-threaded with Chromium threads
        holding locks, so ``fork()`` can deadlock the child before any Python code
        runs -- nothing the binding does in the child can prevent that. ``spawn``
        (or ``forkserver``) starts a clean interpreter that builds its own engine.
        """
        import multiprocessing
        from concurrent.futures import ProcessPoolExecutor
        context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=2, mp_context=context) as pool:
            codes = list(pool.map(_fetch_in_subprocess, [self.url] * 4))
        self.assertEqual(set(codes), {200})

    def test_engine_cache_rebuilds_after_a_fork(self):
        """A child that does reach Python must not reuse the parent's engine."""
        cache = chrome_client.engine.EngineCache()
        config = chrome_client.EngineConfig()
        cache.get(config)
        self.assertEqual(len(cache), 1)
        cache._pid = -1  # simulate having crossed a fork
        cache.get(config)
        self.assertEqual(len(cache), 1)
        self.assertEqual(cache._pid, os.getpid())


class WebSocketServer(object):
    """Minimal WebSocket server that records handshake headers.

    A real endpoint is not needed to test the handshake, the frame path, or the
    header rules, so these tests do not depend on ``MINICRONET_WS_URL``.
    """

    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, reject=False):
        self.handshakes = []
        self.received = []
        self._reject = reject
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(16)
        self.port = self._socket.getsockname()[1]
        self._stop = False
        self._thread = threading.Thread(target=self._serve)
        self._thread.daemon = True
        self._thread.start()

    @property
    def url(self):
        return "ws://127.0.0.1:%d/" % self.port

    def _serve(self):
        while not self._stop:
            try:
                client, _address = self._socket.accept()
            except OSError:
                return
            worker = threading.Thread(target=self._session, args=(client,))
            worker.daemon = True
            worker.start()

    def _session(self, client):
        try:
            raw = b""
            while b"\r\n\r\n" not in raw:
                chunk = client.recv(4096)
                if not chunk:
                    return
                raw += chunk
            request = raw.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1")
            lines = request.split("\r\n")
            headers = {}
            order = []
            for line in lines[1:]:
                if ":" in line:
                    name, value = line.split(":", 1)
                    headers[name.strip().lower()] = value.strip()
                    order.append(name.strip())
            self.handshakes.append({"request_line": lines[0], "headers": headers,
                                    "order": order})
            if self._reject:
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                return
            key = headers.get("sec-websocket-key", "")
            accept = base64.b64encode(
                hashlib.sha1((key + self.GUID).encode()).digest()).decode()
            client.sendall(("HTTP/1.1 101 Switching Protocols\r\n"
                            "Upgrade: websocket\r\n"
                            "Connection: Upgrade\r\n"
                            "Sec-WebSocket-Accept: %s\r\n\r\n" % accept).encode())
            client.sendall(self._text_frame(b"hello"))
            client.settimeout(3.0)
            try:
                self.received.append(client.recv(4096))
            except OSError:
                pass
        finally:
            try:
                client.close()
            except OSError:
                pass

    @staticmethod
    def _text_frame(payload):
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(length)
        else:
            header.append(126)
            header += struct.pack("!H", length)
        return bytes(header) + payload

    def close(self):
        self._stop = True
        try:
            self._socket.close()
        except OSError:
            pass


class WebSocketTests(unittest.TestCase):
    def setUp(self):
        self.server = WebSocketServer()
        self.addCleanup(self.server.close)

    @staticmethod
    def run_async(coro):
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(coro)
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    def test_sync_handshake_send_and_close(self):
        """The constructor must return an already-open socket.

        The Core rejects ``send()`` and ``close()`` before the handshake
        completes, so returning early would hand back an unusable object.
        """
        with chrome_client.WebSocket(url=self.server.url,
                                    impersonate="chrome_152") as socket_:
            self.assertEqual(socket_.recv_str(), "hello")
            socket_.send_str("ping")
        self.assertEqual(len(self.server.handshakes), 1)

    def test_origin_defaults_to_the_url(self):
        """An empty Origin is rejected by the Core, so one has to be derived."""
        with chrome_client.WebSocket(url=self.server.url) as socket_:
            socket_.recv_str()
        origin = self.server.handshakes[-1]["headers"]["origin"]
        self.assertEqual(origin, "http://127.0.0.1:%d" % self.server.port)

    def test_explicit_origin_is_used(self):
        with chrome_client.Session() as session:
            with session.websocket(self.server.url,
                                   origin="https://example.invalid") as socket_:
                socket_.recv_str()
        self.assertEqual(self.server.handshakes[-1]["headers"]["origin"],
                         "https://example.invalid")

    def test_user_agent_comes_from_the_engine(self):
        """The handshake UA is the impersonated UA, and it is not a per-call header.

        Chromium places User-Agent itself, in its own position in the handshake;
        that placement is part of the fingerprint.
        """
        with chrome_client.Session(impersonate="chrome_152") as session:
            with session.websocket(self.server.url) as socket_:
                socket_.recv_str()
        headers = self.server.handshakes[-1]["headers"]
        self.assertIn("Chrome/152.0.0.0", headers["user-agent"])

        with chrome_client.Session(user_agent="Engine/1.0") as session:
            with session.websocket(self.server.url) as socket_:
                socket_.recv_str()
        self.assertEqual(self.server.handshakes[-1]["headers"]["user-agent"],
                         "Engine/1.0")

    def test_forbidden_handshake_headers_fail_closed(self):
        """Headers Chromium derives itself cannot be supplied.

        ``mn_websocket_create`` rejects Connection, Host, Origin, User-Agent,
        Upgrade and Sec-WebSocket-*. Passing one would fail inside the Core with
        a bare argument error, so the facade explains it instead.
        """
        with chrome_client.Session() as session:
            for name, value in (("User-Agent", "Injected/1.0"),
                                ("Origin", "http://example.invalid"),
                                ("Host", "example.invalid"),
                                ("Upgrade", "websocket"),
                                ("Sec-WebSocket-Key", "x")):
                with self.assertRaises(chrome_client.UnsupportedFeature):
                    session.websocket(self.server.url, headers={name: value})
        # A session-level User-Agent is an HTTP override; it must not silently
        # produce a handshake whose UA differs from every other request.
        with chrome_client.Session(headers={"User-Agent": "Injected/1.0"}) as session:
            with self.assertRaises(chrome_client.UnsupportedFeature):
                session.websocket(self.server.url)

    def test_ordinary_headers_are_forwarded(self):
        with chrome_client.Session(headers={"X-Session": "s"}) as session:
            with session.websocket(self.server.url,
                                   headers={"X-Call": "c"}) as socket_:
                socket_.recv_str()
        headers = self.server.handshakes[-1]["headers"]
        self.assertEqual(headers["x-session"], "s")
        self.assertEqual(headers["x-call"], "c")

    def test_cookies_are_sent_on_the_handshake(self):
        with chrome_client.Session() as session:
            session.cookies.set("sid", "WS", domain="127.0.0.1", path="/")
            with session.websocket(self.server.url) as socket_:
                socket_.recv_str()
        self.assertEqual(self.server.handshakes[-1]["headers"]["cookie"], "sid=WS")

    def test_failures_name_the_net_error(self):
        with chrome_client.Session() as session:
            with self.assertRaises(chrome_client.WebSocketError) as caught:
                session.websocket("ws://127.0.0.1:1/")       # a blocked port
            self.assertIn("ERR_UNSAFE_PORT", str(caught.exception))
            with self.assertRaises(chrome_client.WebSocketError) as caught:
                session.websocket("ws://127.0.0.1:9/")
            self.assertIn("net error", str(caught.exception))

    def test_rejected_handshake_raises(self):
        rejecting = WebSocketServer(reject=True)
        self.addCleanup(rejecting.close)
        with chrome_client.Session() as session:
            with self.assertRaises(chrome_client.WebSocketError):
                session.websocket(rejecting.url)

    def test_run_forever_dispatches_callbacks(self):
        seen = []
        socket_ = chrome_client.WebSocket(url=self.server.url)
        socket_.run_forever(on_open=lambda ws: seen.append("open"),
                            on_message=lambda ws, message: (seen.append(message),
                                                            ws.close()),
                            on_close=lambda ws: seen.append("close"))
        self.assertEqual(seen[0], "open")
        self.assertIn("hello", seen)
        self.assertEqual(seen[-1], "close")

    def test_async_handshake_and_iteration(self):
        async def run():
            async with chrome_client.AsyncSession(impersonate="chrome_152") as session:
                socket_ = await session.websocket(self.server.url)
                async with socket_:
                    self.assertEqual(await socket_.recv_str(), "hello")
                    await socket_.send_json({"op": "ping"})

        self.run_async(run())
        self.assertEqual(len(self.server.handshakes), 1)

    def test_async_failure_raises_before_use(self):
        async def run():
            async with chrome_client.AsyncSession() as session:
                with self.assertRaises(chrome_client.WebSocketError) as caught:
                    await session.websocket("ws://127.0.0.1:1/")
                self.assertIn("ERR_UNSAFE_PORT", str(caught.exception))

        self.run_async(run())

    def test_websocket_url_does_not_leak_a_session(self):
        """``WebSocket(url=...)`` owns a Session, so it must release it."""
        socket_ = chrome_client.WebSocket(url=self.server.url)
        self.assertIsNotNone(socket_._owned_session)
        socket_.close()
        self.assertIsNone(socket_._owned_session)


def _utc_now():
    """UTC now as a naive datetime.

    ``cryptography`` wants naive UTC for certificate validity, ``utcnow()`` is
    deprecated on 3.12+, and ``datetime.UTC`` does not exist before 3.11.
    """
    try:
        return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    except AttributeError:  # pragma: no cover
        return datetime.datetime.utcnow()


def _tls_authority(directory, filename="ca.pem"):
    """Builds a throwaway CA so a leaf can be valid except for one defect."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "chrome-client-test-ca")])
    now = _utc_now()
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    path = os.path.join(directory, filename)
    with open(path, "wb") as handle:
        handle.write(certificate.public_bytes(serialization.Encoding.PEM))
    return {"key": key, "certificate": certificate, "path": path}


def _issue_certificate(authority, host, valid_from, valid_to):
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    try:
        alt_names = [x509.IPAddress(ipaddress.ip_address(host))]
    except ValueError:
        alt_names = [x509.DNSName(host)]
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
        .issuer_name(authority["certificate"].subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(valid_from)
        .not_valid_after(valid_to)
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .sign(authority["key"], hashes.SHA256())
    )
    return key, certificate


class TlsServer(object):
    """HTTPS server whose certificate carries exactly one chosen defect."""

    def __init__(self, defect, authority, directory):
        from cryptography.hazmat.primitives import serialization

        now = _utc_now()
        host = "127.0.0.1"
        if defect == "self-signed":
            # A different CA, written to a different file: overwriting the
            # trusted CA's pem would make `verify=<path>` trust the rogue issuer.
            rogue = _tls_authority(directory, filename="rogue-ca.pem")
            key, certificate = _issue_certificate(
                rogue, host, now - datetime.timedelta(days=1),
                now + datetime.timedelta(days=365))
        elif defect == "expired":
            key, certificate = _issue_certificate(
                authority, host, now - datetime.timedelta(days=30),
                now - datetime.timedelta(days=1))
        elif defect == "wrong-host":
            key, certificate = _issue_certificate(
                authority, "other.invalid", now - datetime.timedelta(days=1),
                now + datetime.timedelta(days=365))
        else:
            key, certificate = _issue_certificate(
                authority, host, now - datetime.timedelta(days=1),
                now + datetime.timedelta(days=365))

        suffix = defect or "valid"
        key_path = os.path.join(directory, "%s-key.pem" % suffix)
        cert_path = os.path.join(directory, "%s-cert.pem" % suffix)
        with open(key_path, "wb") as handle:
            handle.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption()))
        with open(cert_path, "wb") as handle:
            handle.write(certificate.public_bytes(serialization.Encoding.PEM))

        self._server = ThreadingHTTPServer((host, 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        self._server.socket = context.wrap_socket(self._server.socket,
                                                  server_side=True)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever)
        self._thread.daemon = True
        self._thread.start()

    @property
    def url(self):
        return "https://127.0.0.1:%d/" % self.port

    def close(self):
        self._server.shutdown()


@unittest.skipUnless(_HAS_CRYPTOGRAPHY, "needs the cryptography package")
class CertificateErrorTests(unittest.TestCase):
    """A rejected certificate must report which check failed.

    Chromium hands the certificate error to
    ``URLRequest::Delegate::OnSSLCertificateError`` and relies on the delegate to
    end the request. The base implementation calls ``URLRequest::Cancel()``, which
    is ``DoCancel(ERR_ABORTED, SSLInfo())`` -- it discards both the error code and
    the SSLInfo. The Core therefore overrides the method and cancels with
    ``CancelWithSSLError``, the same call ``services/network/url_loader.cc`` makes.

    Before that override every certificate failure surfaced as
    ``ERR_ABORTED (net error -3)``, indistinguishable from a caller cancellation.
    """

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.mkdtemp(prefix="chrome-client-tls-")
        cls.authority = _tls_authority(cls.directory)
        cls.servers = {}
        for defect in ("self-signed", "expired", "wrong-host", None):
            cls.servers[defect or "valid"] = TlsServer(defect, cls.authority,
                                                       cls.directory)

    @classmethod
    def tearDownClass(cls):
        for server in cls.servers.values():
            server.close()
        shutil.rmtree(cls.directory, ignore_errors=True)

    def test_each_defect_reports_its_own_error_code(self):
        # The private CA is not in the Chrome Root Store, so trust it explicitly;
        # otherwise authority-invalid masks the defect under test.
        with chrome_client.Session(verify=self.authority["path"]) as session:
            expectations = (
                ("expired", "ERR_CERT_DATE_INVALID", -201),
                ("wrong-host", "ERR_CERT_COMMON_NAME_INVALID", -200),
                ("self-signed", "ERR_CERT_AUTHORITY_INVALID", -202),
            )
            for name, symbol, code in expectations:
                with self.assertRaises(chrome_client.CertificateVerifyError) as caught:
                    session.get(self.servers[name].url, timeout=10)
                message = str(caught.exception)
                self.assertIn(symbol, message)
                self.assertIn("net error %d" % code, message)
                # The regression this guards: ERR_ABORTED means the code was lost.
                self.assertNotIn("ERR_ABORTED", message)

    def test_certificate_errors_are_connection_errors(self):
        with chrome_client.Session(verify=self.authority["path"]) as session:
            with self.assertRaises(chrome_client.SSLError):
                session.get(self.servers["expired"].url, timeout=10)
            with self.assertRaises(chrome_client.ConnectionError):
                session.get(self.servers["expired"].url, timeout=10)
            with self.assertRaises(chrome_client.RequestException):
                session.get(self.servers["expired"].url, timeout=10)

    def test_custom_ca_accepts_an_otherwise_valid_certificate(self):
        with chrome_client.Session(verify=self.authority["path"]) as session:
            response = session.get(self.servers["valid"].url, timeout=10)
            self.assertEqual(response.status_code, 200)
            # The shared Handler answers "/" with JSON.
            self.assertEqual(response.json(), {"hello": "world"})

    def test_default_trust_store_rejects_the_private_ca(self):
        with chrome_client.Session() as session:
            with self.assertRaises(chrome_client.CertificateVerifyError) as caught:
                session.get(self.servers["valid"].url, timeout=10)
            self.assertIn("ERR_CERT_AUTHORITY_INVALID", str(caught.exception))

    def test_verify_false_still_proceeds(self):
        """The fix must not turn `verify=False` into a failure.

        `verify=False` is implemented by an always-OK cert verifier plus
        `ignore_certificate_errors`, neither of which routes through the delegate,
        so no certificate error is reported for the override to cancel on.
        """
        with chrome_client.Session(verify=False) as session:
            for name in ("self-signed", "expired", "wrong-host"):
                response = session.get(self.servers[name].url, timeout=10)
                self.assertEqual(response.status_code, 200)

    def test_cancellation_is_still_reported_as_cancellation(self):
        """ERR_ABORTED must keep meaning "the caller stopped this"."""
        async def run():
            async with chrome_client.AsyncSession(verify=False) as session:
                task = asyncio.ensure_future(
                    session.get(self.servers["valid"].url, timeout=30))
                await asyncio.sleep(0.01)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(run())
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    def test_async_path_reports_the_same_code(self):
        async def run():
            async with chrome_client.AsyncSession(
                    verify=self.authority["path"]) as session:
                with self.assertRaises(chrome_client.CertificateVerifyError) as caught:
                    await session.get(self.servers["expired"].url, timeout=10)
                self.assertIn("ERR_CERT_DATE_INVALID", str(caught.exception))

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(run())
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    def test_websocket_over_tls_reports_the_code(self):
        with chrome_client.Session() as session:
            url = self.servers["self-signed"].url.replace("https://", "wss://")
            with self.assertRaises(chrome_client.WebSocketError) as caught:
                session.websocket(url, timeout=10)
            self.assertIn("ERR_CERT_AUTHORITY_INVALID", str(caught.exception))


def _fetch_in_subprocess(url):
    import chrome_client as client
    return client.get(url, timeout=20).status_code


if __name__ == "__main__":
    unittest.main()
