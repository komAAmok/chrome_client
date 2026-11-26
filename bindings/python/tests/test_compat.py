"""Compatibility and lifecycle regression tests.

Run with the audited Core available:

    LD_LIBRARY_PATH=core/binaries/linux-x86_64 \
    PYTHONPATH=bindings/python:target/release \
    python -m unittest bindings/python/tests/test_compat.py
"""

import asyncio
import ast
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
import subprocess
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
from chrome_client._python_impl.impersonate import LATEST_CHROME, OLDEST_CHROME
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
        if self.path.startswith("/set-cookie-httponly"):
            return self._send(b"ok", extra=[("Set-Cookie", "ho=1; Path=/; HttpOnly")])
        if self.path.startswith("/set-cookie-a"):
            # Overwrites a cookie the caller shadowed for this one request.
            return self._send(b"ok", extra=[("Set-Cookie", "a=NEW; Path=/")])
        if self.path.startswith("/set-cookie"):
            return self._send(b"ok", extra=[("Set-Cookie", "sid=ABC; Path=/"),
                                            ("Set-Cookie", "taste=sweet; Path=/"),
                                            ("Content-Type", "text/plain")])
        if self.path.startswith("/echo-referer"):
            return self._send(repr(self.headers.get("Referer")).encode(),
                              extra=[("Content-Type", "text/plain")])
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


class AuthProxyHandler(BaseHTTPRequestHandler):
    """A proxy that requires Basic credentials before it forwards anything.

    ``ProxyHandler`` above never challenges, so it cannot exercise
    authentication; this one answers 407 with a ``realm`` -- the spelling
    Chromium has to decode through ICU -- until it sees the right
    ``Proxy-Authorization`` header.
    """

    protocol_version = "HTTP/1.1"
    username = "user"
    password = "pass"

    def do_GET(self):
        expected = "Basic " + base64.b64encode(
            ("%s:%s" % (type(self).username, type(self).password)).encode()
        ).decode()
        if self.headers.get("Proxy-Authorization") != expected:
            self.send_response(407)
            self.send_header("Proxy-Authenticate",
                             'Basic realm="%s"' % ProxyAuthenticationTests.REALM)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = ("authed:" + self.path).encode()
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


    def test_cookie_update_replaces_same_name(self):
        """A later write of the same name must win, not sit beside the old one.

        ``cookielib`` only overwrites on an exact ``(domain, path, name)``
        match, so a server-set cookie for ``.example.com`` plus a caller-written
        one for the default empty domain produced two entries: the request sent
        ``Cookie: a=server; a=1`` and ``jar["a"]`` raised
        ``CookieConflictError``. Measured before the fix.
        """
        jar = chrome_client.CookieJar()
        jar.set_cookie(chrome_client.create_cookie(
            "a", "server", domain=".example.com", path="/"))
        jar.update({"a": "1"})
        self.assertEqual(jar["a"], "1")
        self.assertEqual(jar.items(), [("a", "1")])
        self.assertEqual(jar.cookie_header("http://example.com/"), "a=1")

    def test_cookie_update_keeps_distinct_domains(self):
        """Two hosts may each hold their own cookie with the same name."""
        jar = chrome_client.CookieJar()
        jar.set("a", "one", domain="example.invalid", path="/")
        jar.set("a", "two", domain="other.invalid", path="/")
        self.assertEqual(len(list(jar)), 2)
        self.assertEqual(jar.cookie_header("http://example.invalid/"), "a=one")
        self.assertEqual(jar.cookie_header("http://other.invalid/"), "a=two")


    def test_cookie_set_with_domain_only_touches_that_domain(self):
        """A domain-bearing write must leave other hosts' cookies alone."""
        jar = chrome_client.CookieJar()
        jar.set("a", "one", domain="example.invalid", path="/")
        jar.set("a", "two", domain="other.invalid", path="/")
        jar.set("a", "1", domain="example.invalid", path="/")
        self.assertEqual(len(list(jar)), 2)
        self.assertEqual(jar.cookie_header("http://example.invalid/"), "a=1")
        self.assertEqual(jar.cookie_header("http://other.invalid/"), "a=two")

    def test_cookie_set_parent_domain_absorbs_subdomain(self):
        """Writing the parent scope retires the narrower entry it covers."""
        jar = chrome_client.CookieJar()
        jar.set("a", "sub", domain="sub.example.com", path="/")
        jar.set("a", "parent", domain=".example.com", path="/")
        self.assertEqual(len(list(jar)), 1)
        self.assertEqual(jar.cookie_header("http://sub.example.com/"), "a=parent")

    def test_cookie_set_subdomain_does_not_retire_parent(self):
        """The narrow write cannot claim hosts the parent still covers."""
        jar = chrome_client.CookieJar()
        jar.set("a", "parent", domain=".example.com", path="/")
        jar.set("a", "sub", domain="sub.example.com", path="/")
        self.assertEqual(len(list(jar)), 2)
        self.assertEqual(jar.cookie_header("http://other.example.com/"), "a=parent")

    def test_cookie_domainless_write_retires_scoped_entry(self):
        """The reported defect: a domain-less write must win over a host entry."""
        jar = chrome_client.CookieJar()
        jar.set("a", "one", domain="example.invalid", path="/")
        jar.set("a", "two", domain="other.invalid", path="/")
        jar.update({"a": "1"})
        self.assertEqual(jar["a"], "1")
        self.assertEqual(len(list(jar)), 1)

    def test_cookie_scoped_write_absorbs_domainless_entry(self):
        """A domain on either side means the scopes are the same cookie.

        Measured: this is the cost of one value per name. Writing with a domain
        retires an existing domain-less entry for the same name, because an
        unspecified domain matches everything and the two cannot be ranked.
        """
        jar = chrome_client.CookieJar()
        jar.update({"a": "loose"})
        jar.set("a", "scoped", domain="example.invalid", path="/")
        self.assertEqual(len(list(jar)), 1)
        self.assertEqual(jar["a"], "scoped")
        self.assertEqual(jar.cookie_header("http://example.invalid/"), "a=scoped")


    def test_set_cookie_merges_like_a_store(self):
        """A direct ``set_cookie`` is a store insert, not a caller assignment.

        It is the path a server's ``Set-Cookie`` takes (``extract_cookies`` ->
        ``set_cookie``), and a store must keep every distinct scope: RFC 6265
        §5.4 requires a request to ``/sub/x`` to send both ``a=sub; Path=/sub``
        and ``a=root; Path=/``. Retiring here collapsed them, and because the
        facade pushes the jar's ``Cookie`` line whenever it disagrees with the
        Core, the loss reached the wire. ``set()`` / ``update()`` still retire:
        see ``test_cookie_update_replaces_same_name``.
        """
        jar = chrome_client.CookieJar()
        jar.set_cookie(chrome_client.create_cookie(
            "a", "server", domain=".example.com", path="/"))
        jar.set_cookie(chrome_client.create_cookie("a", "1"))
        self.assertEqual(len(list(jar)), 2)
        self.assertIn("a=server", jar.cookie_header("http://example.com/"))
        self.assertIn("a=1", jar.cookie_header("http://example.com/"))

    def test_a_server_may_keep_two_paths_of_one_name(self):
        """RFC 6265 §5.4: / and /sub are two cookies and both are sent.

        Measured before the fix: the jar collapsed them to the last one and the
        next request carried ``Cookie: a=root`` instead of ``a=sub; a=root``.
        """
        jar = chrome_client.CookieJar()
        jar.absorb_set_cookie("http://h.invalid/sub/x",
                              ["a=sub; Path=/sub", "a=root; Path=/"])
        self.assertEqual(len(list(jar)), 2)
        self.assertEqual(jar.cookie_header("http://h.invalid/sub/x"),
                         "a=sub; a=root")
        self.assertEqual(jar.cookie_header("http://h.invalid/other"), "a=root")
    def test_set_cookie_keeps_distinct_domains(self):
        """Retirement is scope-aware no matter which method wrote the cookie."""
        jar = chrome_client.CookieJar()
        jar.set_cookie(chrome_client.create_cookie(
            "a", "one", domain="example.invalid", path="/"))
        jar.set_cookie(chrome_client.create_cookie(
            "a", "two", domain="other.invalid", path="/"))
        self.assertEqual(len(list(jar)), 2)
        self.assertEqual(jar.cookie_header("http://example.invalid/"), "a=one")
        self.assertEqual(jar.cookie_header("http://other.invalid/"), "a=two")

    def test_set_cookie_from_a_second_response_replaces(self):
        """Two Set-Cookie lines for one name leave one entry, not two."""
        jar = chrome_client.CookieJar()
        jar.absorb_set_cookie("http://example.com/", ["a=first; Path=/"])
        jar.absorb_set_cookie("http://example.com/", ["a=second; Path=/"])
        self.assertEqual(jar.items(), [("a", "second")])

    def test_cookie_update_keeps_distinct_paths(self):
        """RFC 6265 5.4: / and /sub are different cookies and both are sent."""
        jar = chrome_client.CookieJar()
        jar.set("a", "root", domain="h.invalid", path="/")
        jar.set("a", "sub", domain="h.invalid", path="/sub")
        self.assertEqual(len(list(jar)), 2)
        self.assertEqual(jar.cookie_header("http://h.invalid/"), "a=root")
        self.assertEqual(jar.cookie_header("http://h.invalid/sub/x"),
                         "a=sub; a=root")

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
            # ABI v9 reports the negotiated protocol, so a plain HTTP/1.1 server
            # is now identified as such instead of being reported as unknown.
            # The earlier version of this line asserted None because the status
            # line cannot tell "real HTTP/1.1" from "normalized HTTP/2"; the
            # value comes from HttpResponseInfo::connection_info now, so it can.
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

    def test_referer_reaches_the_wire(self):
        """Referer is routed to URLRequest::SetReferrer, so it is sent.

        The header is the only spelling: the `referer=` keyword was removed with
        the rest of the curl_cffi surface, because it did nothing the header
        does not.
        """
        with chrome_client.Session() as session:
            response = session.get(
                self.url + "echo-referer",
                headers={"Referer": "https://other.invalid/path"})
            self.assertEqual(ast.literal_eval(response.text),
                             "https://other.invalid/path")
            with self.assertRaises(TypeError):
                session.get(self.url + "echo-referer",
                            referer="https://example.invalid/")
        with chrome_client.Session(headers={"Referer": "https://session.invalid/"}) as session:
            response = session.get(self.url + "echo-referer")
            self.assertEqual(ast.literal_eval(response.text),
                             "https://session.invalid/")

    def test_facade_adds_no_headers_of_its_own(self):
        """The profile owns the default header set; the facade must not seed it."""
        self.assertEqual(dict(chrome_client.utils.default_headers()), {})
        with chrome_client.Session() as session:
            self.assertEqual(dict(session.headers), {})

    def test_profile_conflict_is_mapped(self):
        with self.assertRaises(chrome_client.ImpersonateError):
            chrome_client.Session(impersonate="chrome_151",
                                  user_agent="Conflicting/1.0").get(self.url)

    def test_timeout_tuple_with_one_deadline_is_accepted(self):
        """A pair whose halves agree is one deadline, so it is representable."""
        with chrome_client.Session() as session:
            self.assertEqual(session.get(self.url, timeout=(5.0, 5.0)).status_code, 200)
            with self.assertRaises(chrome_client.Timeout):
                session.get(self.url + "slow", timeout=(0.01, 0.01))

    def test_timeout_tuple_with_two_deadlines_fails_closed(self):
        """ABI v8 has one deadline, so the pair cannot be honoured faithfully.

        Measured before the fix, against a server that accepts and never
        answers: ``timeout=(0.2, 10)`` waited 10.0s where requests waits 0.2s,
        and came back as ``Timeout`` rather than ``ConnectTimeout``. Taking the larger
        half silently deletes the only thing the pair is for.
        """
        import time as _time
        with chrome_client.Session() as session:
            started = _time.monotonic()
            with self.assertRaises(chrome_client.UnsupportedFeature):
                session.get(self.url, timeout=(0.2, 10))
            self.assertLess(_time.monotonic() - started, 1.0)

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

    def test_every_hop_reports_the_request_that_produced_it(self):
        """history[i].request must describe hop i, not the original request.

        ``Response.history`` in requests is a list of responses, each carrying the
        request that produced it, so a caller can tell which hop set which cookie.
        Every entry used to share the caller's ``PreparedRequest``, so a 3-hop
        chain reported the same url three times -- measured: request.url was
        "redirect?n=3" on all three hops.
        """
        with chrome_client.Session() as session:
            response = session.get(self.url + "redirect/3")
            self.assertEqual(
                [hop.request.url for hop in response.history],
                [self.url + "redirect/3", self.url + "redirect/2",
                 self.url + "redirect/1"])
            self.assertEqual(response.request.url, self.url + "redirect/3")

    def test_allow_redirects_false_returns_the_redirect(self):
        with chrome_client.Session() as session:
            response = session.get(self.url + "redirect/1", allow_redirects=False)
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.is_redirect)
            self.assertEqual(response.headers["location"], "/redirect/0")
            self.assertEqual(response.url, self.url + "redirect/1")
            self.assertEqual(response.history, [])
            self.assertEqual(response.next.url, self.url + "redirect/0")

    def test_a_numeric_max_redirects_fails_closed(self):
        """ABI v8 cannot lower Chromium's ceiling, so a cap is refused.

        The facade used to drive the hops from Python to make a tighter cap
        true, which reimplemented redirect semantics in the binding layer: the
        previous hop's Set-Cookie was not sent, Referrer-Policy was not applied,
        redirect_count came back 0, and a 302 turned a PUT into a GET where
        Chromium only rewrites POST. Chromium owns the number now, and anything
        it cannot honour is an error rather than a different behaviour.
        """
        with chrome_client.Session() as session:
            with self.assertRaises(TypeError):
                session.get(self.url + "redirect/2", max_redirects=5)
            with self.assertRaises(TypeError):
                chrome_client.Session(max_redirects=5)
            response = session.get(self.url + "redirect/2")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.history), 2)
            self.assertEqual(response.redirect_count, 2)

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
        # The newest majors are asserted explicitly so a profile that stops
        # resolving is caught; the older aliases stay pinned to a fixed version
        # so they do not drift every time the newest major moves.
        for target, expected in (("chrome_157", "chrome_157"),
                                 ("chrome157", "chrome_157"),
                                 ("chrome_156", "chrome_156"),
                                 ("chrome156", "chrome_156"),
                                 ("chrome_155", "chrome_155"),
                                 ("chrome155", "chrome_155"),
                                 ("chrome_154", "chrome_154"),
                                 ("chrome154", "chrome_154")):
            self.assertEqual(chrome_client.normalize_impersonate(target), expected)
        for target in ("chrome152", "chrome_152", "chrome151"):
            with chrome_client.Session(impersonate=target) as session:
                self.assertEqual(session.get(self.url).status_code, 200)
        self.assertEqual(chrome_client.normalize_impersonate("chrome136"), "chrome_136")
        self.assertEqual(chrome_client.normalize_impersonate("chrome_136"), "chrome_136")
        # Derived from the constant, not spelled out: a literal here fails the
        # build for the right reason only once, and then silently has to be
        # edited again for every release after it.
        self.assertEqual(chrome_client.available_profiles()[-1],
                         "chrome_%d" % LATEST_CHROME)

    def test_bare_family_names_are_rejected(self):
        # A family name would resolve to whatever profile ships last, so the
        # caller's fingerprint would change when a profile is added -- silently,
        # and without the caller asking. Only exact majors are accepted.
        for target in ("chrome", "chromium", "Chrome", "CHROMIUM", " chrome "):
            with self.assertRaises(chrome_client.ImpersonateError) as caught:
                chrome_client.normalize_impersonate(target)
            self.assertIn("exact major", str(caught.exception))
            with self.assertRaises(chrome_client.ImpersonateError):
                chrome_client.Session(impersonate=target)
        self.assertFalse(hasattr(chrome_client.impersonate, "ALIASES"),
                         "the family alias table is gone; nothing may resolve it")

    def test_available_profiles_has_no_family_names(self):
        profiles = chrome_client.available_profiles()
        self.assertEqual(profiles[0], "chrome_%d" % OLDEST_CHROME)
        self.assertEqual(profiles[-1], "chrome_%d" % LATEST_CHROME)
        self.assertEqual(len(profiles), LATEST_CHROME - OLDEST_CHROME + 1)
        for name in profiles:
            self.assertRegex(name, r"^chrome_\d+$")

    def test_unavailable_profiles_fail_closed(self):
        for target in ("safari17", "firefox133", "edge101", "tor145", "chrome_9"):
            with self.assertRaises(chrome_client.ImpersonateError):
                chrome_client.Session(impersonate=target)

    def test_unknown_profile_names_fail_at_construction(self):
        # These were stored verbatim and only rejected later, when the engine was
        # built: Session(impersonate="notachrome") succeeded and the error
        # surfaced on the first request. A name outside the pinned set is a
        # caller mistake, so it has to be reported where it was made.
        for target in ("notachrome", "chrome_abc", "totally_bogus"):
            with self.assertRaises(chrome_client.ImpersonateError):
                chrome_client.Session(impersonate=target)

    def test_blank_impersonate_means_no_profile(self):
        # "" and whitespace are the "no profile" spelling, the same way
        # normalize_http_version reads "" as "let Chromium negotiate".
        for blank in ("", "   "):
            with chrome_client.Session(impersonate=blank) as session:
                self.assertIsNone(session.impersonate)
        self.assertIsNone(chrome_client.normalize_impersonate(None))

    def test_per_call_impersonate_accepts_the_same_spellings(self):
        # normalize_impersonate ran only in __init__, so "CHROME_153" and
        # "chrome-153" worked as constructor arguments but were rejected with
        # ProfileUnsupported when passed per request.
        for target in ("chrome153", "chrome_153", "CHROME_153", "chrome-153"):
            with chrome_client.Session() as session:
                self.assertEqual(
                    session.get(self.url, impersonate=target).status_code, 200)

    def test_fingerprint_overrides_are_gone_not_refused(self):
        """ja3/akamai/perk and 13 extra_fp fields were removed, not stubbed."""
        for name, value in (("ja3", "771,4865,0,29-23-24,0"),
                            ("akamai", "1:65536;2:0;4:6291456"),
                            ("perk", "1:65536")):
            with self.assertRaises(TypeError):
                chrome_client.Session(**{name: value})
        with self.assertRaises(TypeError):
            chrome_client.ExtraFingerprints(tls_permute_extensions=True)
        # A hand-built mapping still reaches the validator, which names the
        # field it cannot honour.
        with self.assertRaises(chrome_client.UnsupportedFeature) as caught:
            chrome_client.Session(extra_fp={"tls_grease": True})
        self.assertIn("tls_grease", str(caught.exception))
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


class SeedCookieHandler(BaseHTTPRequestHandler):
    """Seeds one cookie into the Core store so a later handshake can disagree."""

    protocol_version = "HTTP/1.1"

    def do_GET(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Set-Cookie", "sid=core; Path=/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass

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
                                    impersonate="chrome_151") as socket_:
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
        with chrome_client.Session(impersonate="chrome_151") as session:
            with session.websocket(self.server.url) as socket_:
                socket_.recv_str()
        headers = self.server.handshakes[-1]["headers"]
        self.assertIn("Chrome/151.0.0.0", headers["user-agent"])

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

    def test_handshake_cookie_follows_the_caller_jar(self):
        """A jar edit must reach the handshake, not lose to the Core store.

        ``CookieMonster`` owns the ``Cookie`` header and re-attaches what it
        already holds, so stuffing the jar value into an extra header was not
        enough: measured before the fix, a rewritten jar still sent the store's
        old value (``sid=core`` while the jar held ``sid=caller``), and a
        cleared jar still sent the deleted cookie. The handshake now picks its
        engine the same way an HTTP request does.
        """
        # A plain HTTP server on the same host seeds the Core cookie store, so
        # the handshake has something to disagree with.
        http = ThreadingHTTPServer(("127.0.0.1", 0), SeedCookieHandler)
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        seed = "http://127.0.0.1:%d/seed" % http.server_address[1]

        with chrome_client.Session() as session:
            session.get(seed)
            session.cookies.set("sid", "caller", domain="127.0.0.1", path="/")
            with session.websocket(self.server.url) as socket_:
                socket_.recv_str()
        self.assertEqual(self.server.handshakes[-1]["headers"]["cookie"],
                         "sid=caller")

        session = chrome_client.Session()
        try:
            session.get(seed)
            session.cookies.clear()
            with session.websocket(self.server.url) as socket_:
                socket_.recv_str()
            self.assertNotIn("cookie", self.server.handshakes[-1]["headers"])
        finally:
            session.close()
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
            async with chrome_client.AsyncSession(impersonate="chrome_151") as session:
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



def _import_native_loader():
    """Returns the facade's _native module, not the extension it exposes.

    ``chrome_client._python_impl.__init__`` does ``from ._native import native as
    _native``, so ``chrome_client._python_impl._native`` is the loaded extension.
    The loader module itself has to be imported by path.
    """
    import importlib.util as _ilu
    import pathlib as _pathlib

    path = (_pathlib.Path(chrome_client.__file__).parent / "_python_impl"
            / "_native.py")
    spec = _ilu.spec_from_file_location("_cc_native_loader", str(path))
    module = _ilu.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

class NativeModuleSelectionTests(unittest.TestCase):
    """The facade must accept either extension, not only the one it prefers."""

    def test_both_module_names_are_known(self):
        """Both wheels declare the same requires-python, so pip may install either.

        Measured before the fix: a cp36-abi3 wheel installed into a 3.12 venv
        imported fine once the legacy name was accepted, and failed with "is not
        installed" when the facade only asked for ``chrome_client_native``.
        """
        _native = _import_native_loader()

        self.assertNotEqual(_native.MODULE_NAME, _native.FALLBACK_MODULE_NAME)
        self.assertEqual(
            {_native.MODULE_NAME, _native.FALLBACK_MODULE_NAME},
            {_native.LEGACY_MODULE_NAME, _native.MODERN_MODULE_NAME})

    def test_the_preferred_name_matches_the_interpreter(self):
        _native = _import_native_loader()

        expected = (_native.LEGACY_MODULE_NAME if sys.version_info < (3, 9)
                    else _native.MODERN_MODULE_NAME)
        self.assertEqual(_native.MODULE_NAME, expected)

    def test_both_names_resolve_to_a_loaded_module(self):
        """Neither name may be a guess: the extension that loaded is registered."""
        _native = _import_native_loader()

        # MODULE_NAME is the name this interpreter asks for *first*, not the one
        # it necessarily gets: both wheels ship the same package and declare the
        # same requires-python, so pip may install the cp36-abi3 wheel on 3.9+,
        # and _native.load() then falls back to it on purpose. Asserting only
        # MODULE_NAME made this test fail in that supported configuration --
        # measured with the modern extension absent: nothing was wrong, the
        # fallback had done exactly what its docstring says.
        loaded = _native.native
        self.assertIn(loaded.__name__, (_native.MODULE_NAME, _native.FALLBACK_MODULE_NAME))
        self.assertIs(sys.modules.get(loaded.__name__), loaded)
        self.assertEqual(loaded.abi_version, chrome_client.abi_version())

class HopByHopHeaderTests(unittest.TestCase):
    """Fields only the connection layer can honour must fail, not be ignored."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _EchoHeaderHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d/" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_every_hop_by_hop_field_is_refused(self):
        """Measured before the fix: Connection: close was sent but ignored.

        Three requests still shared one socket, and on HTTP/2 the same field is
        dropped outright (net/spdy/spdy_http_utils.cc:214-221), so the behaviour
        depended on the negotiated protocol.
        """
        for name, value in (("Connection", "close"),
                            ("Connection", "keep-alive"),
                            ("Proxy-Connection", "keep-alive"),
                            ("Keep-Alive", "timeout=5"),
                            ("TE", "trailers"),
                            ("Trailer", "X-Checksum"),
                            ("Upgrade", "h2c")):
            with chrome_client.Session(header_policy="strict") as session:
                with self.assertRaises(chrome_client.UnsupportedFeature):
                    session.get(self.base, headers={name: value})

    def test_removal_still_fails_through_the_core_owned_path(self):
        with chrome_client.Session() as session:
            with self.assertRaises(chrome_client.UnsupportedFeature):
                session.get(self.base, headers={"Connection": None})

    def test_a_caller_transfer_encoding_is_passed_through(self):
        """A deliberate decision, pinned so it cannot drift silently.

        `Transfer-Encoding: chunked` on a bytes body produces the same framing
        pair `requests` produces (measured: both send Content-Length and
        Transfer-Encoding, and RFC 9112 section 6.3 says the recipient ignores
        Content-Length). Rewriting it here would make a request that works
        against requests fail, which this project does not do for a call the
        caller spelled out.
        """
        with chrome_client.Session() as session:
            session.post(self.base, headers={"Transfer-Encoding": "chunked"},
                         data=b"abc")
        self.assertEqual(_ECHOED[-1].get("Transfer-Encoding"), "chunked")
        self.assertEqual(_ECHOED[-1].get("Content-Length"), "3")

    def test_a_streamed_body_chunks_without_a_content_length(self):
        """The path that must stay clean: the facade owns the framing here."""
        with chrome_client.Session() as session:
            session.post(self.base,
                         headers={"Transfer-Encoding": "chunked"},
                         data=iter([b"aa", b"bb"]))
        self.assertEqual(_ECHOED[-1].get("Transfer-Encoding"), "chunked")
        self.assertIsNone(_ECHOED[-1].get("Content-Length"),
                          "a chunked body must not also declare a length")

    def test_a_file_part_carries_no_invented_content_type(self):
        """The facade must not guess a part type from the filename.

        requests -- and therefore curl_cffi, which this facade follows --
        renders a 2-tuple as a bare Content-Disposition and lets the caller add
        a type only by passing one explicitly. Guessing looked harmless and was
        not: measured against a server that stores what it receives,
        files={"f": ("a.html", b"<script>...")} arrived as text/html where
        requests sends no type at all, so an upload endpoint that echoes the
        stored type starts serving attacker HTML.
        """
        with chrome_client.Session() as session:
            session.post(self.base, files={"f": ("a.html", b"<script>x</script>")})
        content_type = _ECHOED[-1].get("Content-Type") or ""
        self.assertTrue(content_type.startswith("multipart/form-data;"))
        body = _POSTED[-1]
        self.assertNotIn(b"Content-Type: text/html", body)
        self.assertNotIn(b"Content-Type: application/octet-stream", body)

    def test_an_explicit_part_content_type_is_still_sent(self):
        """The other direction: what the caller asks for must reach the wire."""
        with chrome_client.Session() as session:
            session.post(self.base,
                         files={"f": ("a.txt", b"xy", "image/png")})
        self.assertIn(b"Content-Type: image/png", _POSTED[-1])

    def test_ordinary_headers_are_unaffected(self):
        """The guard must not swallow body framing or caller-owned fields."""
        with chrome_client.Session(impersonate="chrome_124") as session:
            session.get(self.base, headers={"X-Custom": "1",
                                            "Content-Type": "text/plain"})
        self.assertEqual(_ECHOED[-1]["X-Custom"], "1")
        self.assertEqual(_ECHOED[-1]["Content-Type"], "text/plain")

    def test_session_level_headers_are_refused_too(self):
        """The guard lived on request() only, and session.headers bypassed it.

        Measured before the fix: a session-level Connection, Keep-Alive,
        Upgrade, Trailer and TE all reached the wire on HTTP/1.1, which is the
        same degradation the per-request refusal exists to stop, only spelled
        differently.
        """
        for name, value in (("Connection", "close"),
                            ("Keep-Alive", "timeout=5"),
                            ("Upgrade", "h2c"),
                            ("Trailer", "X-Checksum"),
                            ("TE", "trailers")):
            with chrome_client.Session(header_policy="strict") as session:
                session.headers[name] = value
                with self.assertRaises(chrome_client.UnsupportedFeature) as caught:
                    session.get(self.base)
            self.assertIn(name.lower(), str(caught.exception))

    def test_a_hand_built_prepared_request_is_refused(self):
        """prepare_request + send is a second documented entry point.

        The same header sent down it reached the wire untouched, so the refusal
        has to sit on send(), where every path converges, not on request().
        """
        with chrome_client.Session(header_policy="strict") as session:
            prepared = chrome_client.PreparedRequest()
            prepared.prepare(method="GET", url=self.base,
                             headers={"Connection": "close"})
            with self.assertRaises(chrome_client.UnsupportedFeature):
                session.send(prepared)

    def test_the_none_spelling_survives_into_send(self):
        """``headers={"Connection": None}`` is requests' spelling of a removal.

        prepare_headers used to discard every None value without recording it,
        and the guard then saw a request with no opinion about Connection at
        all. Measured before the fix: the Core sent its own
        ``Connection: keep-alive`` and no UnsupportedFeature was raised.
        """
        with chrome_client.Session(header_policy="strict") as session:
            request = chrome_client.Request("GET", self.base,
                                            headers={"Connection": None})
            prepared = session.prepare_request(request)
            self.assertIn("connection", prepared.headers.deleted_names)
            with self.assertRaises(chrome_client.UnsupportedFeature):
                session.send(prepared)

    def test_a_headerless_prepared_request_still_sends(self):
        """The removal record must not become a reason to reject plain traffic."""
        with chrome_client.Session() as session:
            prepared = chrome_client.PreparedRequest()
            prepared.prepare(method="GET", url=self.base)
            self.assertEqual(session.send(prepared).status_code, 200)

    def test_the_async_entry_points_are_refused_too(self):
        """AsyncSession.send never touched the guard either."""
        async def run():
            async with chrome_client.AsyncSession(header_policy="strict") as session:
                prepared = chrome_client.PreparedRequest()
                prepared.prepare(method="GET", url=self.base,
                                 headers={"Upgrade": "h2c"})
                with self.assertRaises(chrome_client.UnsupportedFeature):
                    await session.send(prepared)
                request = chrome_client.Request("GET", self.base,
                                                headers={"Trailer": "X"})
                with self.assertRaises(chrome_client.UnsupportedFeature):
                    await session.send(session.prepare_request(request))

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(run())
        finally:
            asyncio.set_event_loop(None)
            loop.close()

class CoreOwnedHeaderTests(unittest.TestCase):
    """A removal the Core cannot honour must fail, not silently send the value."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _EchoHeaderHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d/" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_removing_the_user_agent_fails_closed(self):
        """Measured before the fix: the profile UA was sent anyway, no error."""
        with chrome_client.Session(impersonate="chrome_124") as session:
            with self.assertRaises(chrome_client.UnsupportedFeature) as caught:
                session.get(self.base, headers={"User-Agent": None})
        self.assertIn("user-agent", str(caught.exception).lower())

    def test_removing_accept_encoding_fails_closed(self):
        with chrome_client.Session(impersonate="chrome_124") as session:
            with self.assertRaises(chrome_client.UnsupportedFeature):
                session.get(self.base, headers={"Accept-Encoding": None})

    def test_a_session_level_removal_fails_closed_too(self):
        """session.headers.pop() reached no check, so the value came back.

        Measured before the fix: after
        Session(impersonate="chrome_124").headers.pop("User-Agent"), the
        request carried the profile UA and no error was raised -- the silent
        no-op this class exists to remove, spelled the session-level way.
        """
        with chrome_client.Session(impersonate="chrome_124") as session:
            session.headers["User-Agent"] = "mine/1.0"
            session.headers.pop("User-Agent")
            with self.assertRaises(chrome_client.UnsupportedFeature) as caught:
                session.get(self.base)
        self.assertIn("user-agent", str(caught.exception).lower())

    def test_removing_the_cookie_header_fails_closed(self):
        """``Cookie`` is attached by CookieMonster, so removing it does nothing.

        Measured before the fix: a store holding ``c=3`` still sent
        ``Cookie: c=3`` after the request asked to delete the header, and
        nothing raised -- the silent no-op this class exists to remove, in the
        one header the guard had left out.
        """
        with chrome_client.Session() as session:
            session.cookies.set("c", "3", domain="127.0.0.1")
            with self.assertRaises(chrome_client.UnsupportedFeature) as caught:
                session.get(self.base, headers={"Cookie": None})
        self.assertIn("cookie", str(caught.exception).lower())

    def test_the_requests_spelling_of_a_session_removal_fails_too(self):
        with chrome_client.Session(impersonate="chrome_124") as session:
            session.headers["Accept-Encoding"] = None
            with self.assertRaises(chrome_client.UnsupportedFeature):
                session.get(self.base)

    def test_a_streaming_body_keeps_its_own_content_length_removal(self):
        """prepare_content_length() drops Content-Length on purpose.

        That deletion is internal and must not be read as the caller asking to
        remove a Core-owned header, or every streaming upload would fail.
        """
        with chrome_client.Session() as session:
            response = session.post(self.base, data=iter([b"a", b"b"]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(_ECHOED[-1].get("Transfer-Encoding"), "chunked")

    def test_several_removals_are_all_named(self):
        with chrome_client.Session(impersonate="chrome_124") as session:
            with self.assertRaises(chrome_client.UnsupportedFeature) as caught:
                session.get(self.base, headers={"User-Agent": None,
                                                "Accept-Encoding": None})
        message = str(caught.exception)
        self.assertIn("user-agent", message)
        self.assertIn("accept-encoding", message)

    def test_a_caller_owned_removal_still_works(self):
        """Only Core-owned names fail; everything else keeps requests semantics."""
        with Session(headers={"X-Gone": "yes"}) as session:
            session.get(self.base, headers={"X-Gone": None})
        self.assertNotIn("X-Gone", _ECHOED[-1])

    def test_overriding_a_core_owned_header_still_works(self):
        with chrome_client.Session(impersonate="chrome_124") as session:
            session.get(self.base, headers={"User-Agent": "mine/1.0"})
        self.assertEqual(_ECHOED[-1]["User-Agent"], "mine/1.0")


_ECHOED = []
_POSTED = []

Session = chrome_client.Session


class _EchoHeaderHandler(BaseHTTPRequestHandler):
    """Records the request headers so a test can assert what reached the wire."""

    protocol_version = "HTTP/1.1"

    def do_GET(self):
        _ECHOED.append({name: value for name, value in self.headers.items()})
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_POST(self):
        """Accepts both framings so a streaming upload can be observed.

        Chromium sends a caller-supplied iterator as chunked, which this
        handler has to consume before it can answer; without it the streaming
        test only ever saw the fixture's own 501.
        """
        _ECHOED.append({name: value for name, value in self.headers.items()})
        # RFC 9112 section 6.3 (and RFC 7230 before it): a message with both
        # framing fields is a smuggling-shaped input that a server SHOULD reject
        # outright. A test server cannot reject, so it takes the ONE reading that
        # does not need to guess -- again ``Content-Length``, which is what the
        # ``Transfer-Encoding``-wins rule exists to stop -- and, more to the
        # point here, reads exactly the bytes the body actually is. Reading it as
        # chunks hangs on ``abc``, which is how this decision showed up in the
        # first place.
        chunked = ("chunked" in self.headers.get("Transfer-Encoding", "").lower()
                   and "content-length" not in self.headers)
        if chunked:
            while True:
                line = self.rfile.readline().strip()
                if not line:
                    break
                size = int(line.split(b";", 1)[0], 16)
                if size == 0:
                    self.rfile.readline()
                    break
                _POSTED.append(self.rfile.read(size))
                self.rfile.readline()
        else:
            length = int(self.headers.get("Content-Length") or 0)
            _POSTED.append(self.rfile.read(length) if length else b"")
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass

class NetErrorMappingTests(unittest.TestCase):
    """Every declared exception class must have an entry that reaches it."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _NetErrorHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_a_failed_content_decoding_raises_ContentDecodingError(self):
        """-330 is what GzipSourceStream returns when inflate fails.

        The table only had -371 (decoder creation), so every real decoding
        failure surfaced as ``ConnectionError`` and ``except ContentDecodingError`` was
        dead code.
        """
        with chrome_client.Session() as session:
            with self.assertRaises(chrome_client.ContentDecodingError) as caught:
                session.get(self.base + "/bad-gzip", timeout=5)
        self.assertIn("-330", str(caught.exception))

    def test_only_if_cached_without_an_entry_raises_CacheMiss(self):
        """-400 used to map to None, so the miss was a bare RequestException."""
        with chrome_client.Session() as session:
            with self.assertRaises(chrome_client.CacheMiss) as caught:
                session.get(self.base + "/cached", cache_mode="only_if_cached")
        self.assertIn("-400", str(caught.exception))

    def test_only_if_cached_with_an_entry_succeeds(self):
        with chrome_client.Session() as session:
            session.get(self.base + "/cached")
            again = session.get(self.base + "/cached", cache_mode="only_if_cached")
        self.assertEqual(again.status_code, 200)


class _NetErrorHandler(BaseHTTPRequestHandler):
    """Serves one badly-encoded body and one cacheable one."""

    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path == "/bad-gzip":
            body = b"this-is-not-gzip-and-here-is-padding"
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
        else:
            body = b"ok"
            self.send_response(200)
            self.send_header("Cache-Control", "max-age=600")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass

class SilentLossRegressionTests(unittest.TestCase):
    """Regressions for defects that returned a wrong result without raising.

    Every case below was reproduced against the shipped build before it was
    fixed. None of them produced an exception, which is exactly why they needed
    a test rather than a crash report.
    """

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_async_streaming_keeps_the_body_without_native_redirects(self):
        """A streaming state buffers into chunks, so reading body gave b"".

        Forced by allow_redirects=False, which is now the only way to hold a
        request in the Core's manual redirect mode: a numeric max_redirects is
        refused up front, so it cannot reach this state any more.
        """
        url = self.base + "/bytes/10918"

        async def run():
            async with chrome_client.AsyncSession() as session:
                response = await session.get(url, stream=True, allow_redirects=False)
                body = await response.acontent()
                self.assertEqual(len(body), 10918)
                await response.aclose()
        asyncio.run(run())

    def test_sync_and_async_streaming_agree(self):
        """The synchronous path was always correct; they must not diverge."""
        url = self.base + "/bytes/10918"
        with chrome_client.Session() as session:
            sync = session.get(url, stream=True, allow_redirects=False)
            self.assertEqual(len(sync.content), 10918)

        async def run():
            async with chrome_client.AsyncSession() as session:
                response = await session.get(url, stream=True,
                                            allow_redirects=False)
                self.assertEqual(len(await response.acontent()), 10918)
                await response.aclose()
        asyncio.run(run())

    def test_discard_cookies_does_not_send_stored_cookies(self):
        """Dropping the header was not enough: the Core store re-attached them."""
        with chrome_client.Session() as session:
            session.get(self.base + "/set-cookie")
            self.assertTrue(session.cookies.get_dict())
            echoed = session.get(self.base + "/echo-cookie").text
            self.assertIn("sid=ABC", echoed)
            discarded = session.get(self.base + "/echo-cookie",
                                    discard_cookies=True).text
            self.assertNotIn("sid=ABC", discarded)
            self.assertNotIn("taste=sweet", discarded)

    def test_a_cookie_supplied_while_discarding_fails_closed(self):
        """Both options promise different things, and one used to lose silently.

        Measured before the refusal: `get(url, cookies={"d": "4"},
        discard_cookies=True)` reached the origin with no Cookie header and
        raised nothing, so the caller got a request it had not described.
        """
        with chrome_client.Session() as session:
            with self.assertRaises(chrome_client.UnsupportedFeature) as caught:
                session.get(self.base + "/echo-cookie", cookies={"d": "4"},
                            discard_cookies=True)
        self.assertIn("discard_cookies", str(caught.exception))

    def test_a_literal_cookie_header_still_loses_to_discard_cookies(self):
        """The loud option wins here: discard promise beats a supplied header.

        Pinned by test_session_defects.DiscardDoesNotRewriteTheCallersRequest,
        which also requires the caller's own object to keep its header -- the
        field is dropped from a copy, never from the request that was passed in.
        """
        with chrome_client.Session() as session:
            body = session.get(self.base + "/echo-cookie",
                               headers={"Cookie": "raw=1"},
                               discard_cookies=True).text
            self.assertEqual(ast.literal_eval(body), [])

    def test_a_session_cookie_is_still_suppressed_by_discard_cookies(self):
        """The option exists to suppress stored cookies; that must keep working."""
        with chrome_client.Session(cookies={"stored": "S"}) as session:
            body = session.get(self.base + "/echo-cookie",
                               discard_cookies=True).text
            self.assertEqual(ast.literal_eval(body), [],
                             "discard_cookies must send no stored cookie")
            self.assertEqual(session.cookies.get_dict(), {"stored": "S"},
                             "discard_cookies must not record either")

    def test_discard_cookies_does_not_suppress_a_plain_request(self):
        with chrome_client.Session() as session:
            body = session.get(self.base + "/echo-cookie",
                               cookies={"d": "4"}).text
            self.assertEqual(ast.literal_eval(body), ["d=4"])

    def test_a_per_request_cookie_does_not_outlive_its_request(self):
        """``cookies=`` is request-scoped, and the store is not.

        Measured before the fix: ``get(url, cookies={"per": "P"})`` followed by
        a plain ``get(url)`` sent ``Cookie: per=P`` again. requests sends
        nothing on the second call, so a value the caller scoped to one request
        leaked into every later one.
        """
        with chrome_client.Session() as session:
            first = session.get(self.base + "/echo-cookie",
                                cookies={"per": "P"}).text
            self.assertEqual(ast.literal_eval(first), ["per=P"])
            after = session.get(self.base + "/echo-cookie").text
            self.assertEqual(ast.literal_eval(after), [],
                             "a request-scoped cookie reached the next request")

    def test_a_caller_set_cookie_is_not_http_only(self):
        """``cookies.set()`` is a script write; the store must not see HttpOnly.

        ``create_cookie`` seeds ``{"HttpOnly": None}`` for every cookie, and
        ``has_nonstandard_attr`` only asks whether the key exists, so every
        caller-set cookie was mirrored into the store as HttpOnly. Chromium
        refuses to replace an HttpOnly cookie with a non-HttpOnly one, so the
        wrong flag turned an ordinary response into ``InvalidURL``: measured,
        a server answering ``Set-Cookie: a=NEW`` for a name the caller had
        already set failed outright.
        """
        with chrome_client.Session() as session:
            session.cookies.set("mine", "1", domain="127.0.0.1")
            stored = [row for row in session._store_slot().cookies(None)
                      if row[0] == "mine"]
        self.assertEqual(len(stored), 1)
        self.assertFalse(stored[0][7], "a caller-set cookie reached the store "
                                        "marked HttpOnly")

    def test_a_server_http_only_cookie_is_still_http_only(self):
        """The flag has to keep working in the direction that matters."""
        with chrome_client.Session() as session:
            session.get(self.base + "/set-cookie-httponly")
            stored = [row for row in session._store_slot().cookies(None)
                      if row[0] == "ho"]
        self.assertEqual(len(stored), 1)
        self.assertTrue(stored[0][7], "a server HttpOnly cookie lost its flag")

    def test_a_server_set_cookie_outranks_the_restore(self):
        """A response that sets the shadowed name has the last word.

        Measured before the fix, and this was a regression from the restore
        itself: the response set ``a=NEW``, the jar recorded ``NEW``, and then
        the restore wrote ``OLD`` back into the store, so the next request sent
        a value the jar no longer described.
        """
        with chrome_client.Session() as session:
            session.cookies.set("a", "OLD", domain="127.0.0.1")
            session.get(self.base + "/set-cookie-a", cookies={"a": "OVERRIDE"})
            self.assertEqual(session.cookies.get_dict().get("a"), "NEW")
            body = session.get(self.base + "/echo-cookie").text
            self.assertEqual(ast.literal_eval(body), ["a=NEW"],
                             "the restore resurrected a value the server had "
                             "already replaced")

    def test_a_per_request_cookie_does_not_delete_the_session_cookie(self):
        """Restoring matters as much as deleting.

        Deleting the name outright left the store empty while the jar still held
        the session value, so every later request sent nothing at all -- the jar
        and the store had diverged, which is the failure this module exists to
        remove.
        """
        with chrome_client.Session() as session:
            session.cookies.set("sess", "S", domain="127.0.0.1")
            overridden = session.get(self.base + "/echo-cookie",
                                     cookies={"sess": "OVR"}).text
            self.assertEqual(ast.literal_eval(overridden), ["sess=OVR"])
            restored = session.get(self.base + "/echo-cookie").text
            self.assertEqual(ast.literal_eval(restored), ["sess=S"])

    def test_a_per_request_cookie_covers_every_redirect_hop(self):
        """One URLRequest follows the chain, so the retire must wait for it."""
        with chrome_client.Session() as session:
            response = session.get(self.base + "/redirect/0",
                                   cookies={"per": "P"})
            self.assertEqual(response.status_code, 200)
            inner = session.get(self.base + "/echo-cookie").text
            self.assertEqual(ast.literal_eval(inner), [],
                             "the per-request cookie outlived the request")

    def test_editing_the_cookie_jar_does_not_fill_the_engine_cache(self):
        """A caller edit must not cost a whole Chromium context per request.

        When the jar and the Core's store disagree, the store has to be replaced
        by a fresh generation -- that part is correct and unavoidable. The defect
        was that the *superseded* generation was left in the bounded cache even
        though nothing can ever select it again (its mirror no longer matches the
        jar). It therefore evicted the engine the next ordinary request needs.

        Measured on the shipped build: eight caller edits filled all eight cache
        slots with dead generations, so every later request that merely looked
        like a reuse rebuilt a context. The assertion is on the cache, not on a
        build counter: a rebuild per *edit* is correct, a cache full of corpses is
        not.
        """
        with chrome_client.Session() as session:
            # The server sets a cookie, so the mirror is non-empty and a caller
            # edit actually creates the jar-vs-store disagreement.
            session.get(self.base + "/set-cookie")
            self.assertTrue(session.cookies.get_dict())

            for index in range(8):
                session.cookies.clear()
                self.assertEqual(
                    session.get(self.base + "/set-cookie?n=%d" % index).status_code, 200)

            self.assertEqual(
                len(session._engines), 1,
                "eight caller edits left %d engines cached; the superseded "
                "generations are unreachable and must be discarded, not left to "
                "evict the reusable one" % len(session._engines))

    def test_send_honours_session_defaults(self):
        """send() bypassed session defaults, including max_response_bytes."""
        with chrome_client.Session(max_response_bytes=1000) as session:
            prepared = session.prepare_request(
                chrome_client.Request("GET", self.base + "/bytes/20000"))
            with self.assertRaises(chrome_client.ResponseTooLarge):
                session.send(prepared)

    def test_send_rejects_a_non_prepared_request_with_valueerror(self):
        """requests.Session.send raises ValueError for a non-PreparedRequest.

        Verified against requests 2.31.0 rather than assumed: its message is
        "You can only send PreparedRequests." and the exception is ValueError.
        """
        with chrome_client.Session() as session:
            with self.assertRaises(ValueError):
                session.send(self.base + "/")

    def test_http_version_reports_the_negotiated_protocol(self):
        """ABI v9 reports the protocol; the status line is no longer consulted.

        This test used to assert ``None``, because ABI v8 carried no protocol
        and a normalized HTTP/2 response is indistinguishable from HTTP/1.1 by
        status line alone. v9 answers the question directly, so the honest
        assertion is the real value -- and the guard that matters moved: a
        response the Core cannot classify must still come back as ``None``
        rather than being upgraded to HTTP/1.1. That half is covered by
        ``test_response_attributes``.
        """
        with chrome_client.Session() as session:
            response = session.get(self.base + "/")
            self.assertEqual(response.http_version, "HTTP/1.1")

    def test_priority_rejects_an_int_with_a_clear_error(self):
        """The stub advertised Optional[int]; the Core parses strings."""
        with chrome_client.Session() as session:
            with self.assertRaises(TypeError):
                session.get(self.base + "/", priority=5)

    def test_default_priority_follows_the_profile_version(self):
        """Chrome 124+ must ask for "highest", earlier profiles for nothing.

        The wire value is what matters: net::LOWEST (the Core default)
        serialises as "priority: i" because urgency 3 is the protocol default
        and gets omitted, so a navigation-claiming header set would advertise
        a subresource hint.  Chrome itself sends "u=0, i" from 124 on, and no
        priority header at all before that.
        """
        from chrome_client._python_impl.impersonate import default_priority

        for major, expected in ((99, None), (123, None), (124, "highest"),
                                (155, "highest")):
            profile = "chrome_%d" % major
            self.assertEqual(default_priority(profile), expected, profile)
        self.assertIsNone(default_priority(None))
        # An unparseable name must not be pre-empted here: the Core owns that
        # rejection, and returning a priority first would mask it.
        for bogus in ("garbage", "chrome_x", "chrome_"):
            self.assertIsNone(default_priority(bogus), bogus)

    def test_explicit_priority_still_wins_over_the_profile_default(self):
        """A caller asking for "low" must not be upgraded to "highest"."""
        with chrome_client.Session(impersonate="chrome_155") as session:
            request, options = session._prepare_call({
                "method": "GET", "url": "https://example.com/",
                "impersonate": None, "priority": "low",
            })
        self.assertEqual(options["priority"], "low")

    def test_profile_default_priority_reaches_the_transport(self):
        """The resolved default has to be what the Core is asked to send."""
        with chrome_client.Session(impersonate="chrome_155") as session:
            request, options = session._prepare_call({
                "method": "GET", "url": "https://example.com/",
                "impersonate": None,
            })
        self.assertEqual(options["priority"], "highest")
        with chrome_client.Session(impersonate="chrome_123") as session:
            request, options = session._prepare_call({
                "method": "GET", "url": "https://example.com/",
                "impersonate": None,
            })
        self.assertIsNone(options["priority"])


class RefererEchoHandler(BaseHTTPRequestHandler):
    """Echoes the Referer the request actually carried.

    The assertion has to be about what the server received, not about what the
    facade stored: the defect this covers was a caller-supplied Referer that
    never reached the wire, which no amount of inspecting session state reveals.
    """

    protocol_version = "HTTP/1.1"
    seen = []

    def _reply(self):
        type(self).seen.append((self.command, self.path,
                                self.headers.get("Referer")))
        body = json.dumps({"referer": self.headers.get("Referer")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _reply
    do_POST = _reply

    def log_message(self, *_args):
        pass


class RefererTests(unittest.TestCase):
    """A caller-supplied Referer must travel, and nothing else may change.

    Both halves matter. The defect was that ``headers={"Referer": ...}`` was
    silently dropped: Chromium's ``URLRequestHttpJob`` strips Referer out of
    the extra headers and serializes ``referrer()`` instead, so the value
    vanished with no error. The Core now routes it to
    ``URLRequest::SetReferrer`` (core/source/request.cc). The other half is
    that a request which asks for no Referer must still send none -- fixing the
    first half must not make the binding invent one.
    """

    REFERER = "https://referrer.example/page"

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), RefererEchoHandler)
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_every_entry_point_puts_the_referer_on_the_wire(self):
        """Session-level and per-call, keyword and header, sync and async."""
        cases = {}

        with chrome_client.Session(headers={"Referer": self.REFERER}) as session:
            cases["session headers="] = session.get(
                self.base + "/1", timeout=10).json()["referer"]
        with chrome_client.Session() as session:
            cases["per-call headers="] = session.get(
                self.base + "/3", headers={"Referer": self.REFERER},
                timeout=10).json()["referer"]
        with chrome_client.Session() as session:
            session.headers.update({"Referer": self.REFERER})
            cases["session.headers.update"] = session.get(
                self.base + "/4", timeout=10).json()["referer"]
        with chrome_client.Session() as session:
            cases["prepare/send"] = session.send(
                session.prepare_request(
                    chrome_client.Request("GET", self.base + "/5",
                                          headers={"Referer": self.REFERER})),
                timeout=10).json()["referer"]
        with chrome_client.Session(headers={"Referer": self.REFERER}) as session:
            cases["POST"] = session.post(
                self.base + "/6", data={"a": "1"}, timeout=10).json()["referer"]

        async def run():
            async with chrome_client.AsyncSession(
                    headers={"Referer": self.REFERER}) as session:
                cases["async session headers="] = (
                    await session.get(self.base + "/7", timeout=10)
                ).json()["referer"]
            async with chrome_client.AsyncSession() as session:
                with self.assertRaises(TypeError):
                    await session.get(self.base + "/8", referer=self.REFERER,
                                      timeout=10)
        asyncio.run(run())

        for label, received in cases.items():
            self.assertEqual(received, self.REFERER, label)

    def test_no_referer_is_invented_when_none_was_asked_for(self):
        """The native behaviour: Chromium sends no Referer on a plain request."""
        with chrome_client.Session() as session:
            response = session.get(self.base + "/plain", timeout=10)
        self.assertIsNone(response.json()["referer"])

    def test_a_caller_referer_survives_a_redirect(self):
        """The caller owns the header, so the default policy must not clear it.

        ``URLRequest::SetReferrer`` with Chromium's default policy clears an
        https referrer on an http target
        (CLEAR_ON_TRANSITION_FROM_SECURE_TO_INSECURE), which would drop the very
        value the caller supplied; the Core sets NEVER_CLEAR for exactly this
        reason.
        """
        with chrome_client.Session(headers={"Referer": self.REFERER}) as session:
            response = session.get(self.base + "/redirect/1", timeout=10)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["referer"], self.REFERER)

    def test_an_explicit_header_beats_the_session_default(self):
        per_call = "https://per-call.example/"
        with chrome_client.Session(headers={"Referer": self.REFERER}) as session:
            received = session.get(self.base + "/override",
                                   headers={"Referer": per_call},
                                   timeout=10).json()["referer"]
        self.assertEqual(received, per_call)


class ProxyAuthenticationTests(unittest.TestCase):
    """The 407 path, which no fixture exercised before.

    ``ProxyHandler`` answers every request with 200 and never sends a
    ``Proxy-Authenticate`` challenge, so the entire authentication path --
    Core delegate, challenge parsing, credential injection -- had no test. A
    defect lived there: ``Basic realm="x"`` never authenticated at all.

    The proxy here demands credentials and answers 407 until it gets them, and
    the assertion is on the response body, so "the request went direct" and
    "the credentials were never sent" cannot pass.
    """

    REALM = "micronet-proxy"

    @classmethod
    def setUpClass(cls):
        cls.proxy = ThreadingHTTPServer(("127.0.0.1", 0), AuthProxyHandler)
        cls.proxy.daemon_threads = True
        cls.proxy_thread = threading.Thread(target=cls.proxy.serve_forever)
        cls.proxy_thread.daemon = True
        cls.proxy_thread.start()
        cls.proxy_url = "http://127.0.0.1:%d" % cls.proxy.server_address[1]
        cls.target = "http://proxy-auth-target.test/hello"

    @classmethod
    def tearDownClass(cls):
        cls.proxy.shutdown()
        cls.proxy_thread.join(timeout=5)

    def test_challenge_with_a_realm_authenticates(self):
        """A realm is the normal spelling; it must not disable the handler.

        Chromium decodes the realm with
        ``base::ConvertToUtf8AndNormalize(..., kCharsetLatin1, ...)``, which
        opens ICU's ISO-8859-1 converter. When the Core's ICU dataset left
        ``conversion_mappings`` out, that open failed, no auth handler was
        created, ``GetAuthChallengeInfo`` returned null, and
        ``OnAuthRequired`` was never called -- the 407 was simply returned to
        the caller with the credentials unspent.
        """
        with chrome_client.Session(proxy=self.proxy_url,
                                   proxy_auth=("user", "pass")) as session:
            response = session.get(self.target, timeout=20)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "authed:" + self.target)

    def test_credentials_embedded_in_the_proxy_url_are_used(self):
        """``http://user:pass@host`` must authenticate too.

        Chromium's proxy rule parser rejects userinfo outright, so the facade
        has to move it into the Engine's proxy_username/proxy_password fields.
        """
        authenticated = self.proxy_url.replace("http://", "http://user:pass@")
        with chrome_client.Session(proxy=authenticated) as session:
            response = session.get(self.target, timeout=20)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "authed:" + self.target)

    def test_wrong_credentials_do_not_succeed(self):
        """The positive cases above must not pass on a bypassed proxy.

        Chromium re-tries the challenge inside the same URLRequest and gives up
        after kMaxRestarts (32), so rejected credentials arrive as
        ERR_TOO_MANY_RETRIES (-375) rather than as a 407 the caller could
        inspect. Asserting "not 200" rather than the exact spelling keeps the
        test about the outcome: the credentials were rejected.
        """
        with chrome_client.Session(proxy=self.proxy_url,
                                   proxy_auth=("user", "wrong")) as session:
            try:
                response = session.get(self.target, timeout=20)
            except chrome_client.ProxyError as error:
                self.assertIn("ERR_TOO_MANY_RETRIES", str(error))
            else:
                self.assertNotEqual(response.status_code, 200)
                self.assertNotIn("authed:", response.text)


class ProxyRegressionTests(unittest.TestCase):
    """Regressions for the proxy path.

    Both defects here produced a *successful-looking* response, which is why
    they survived: nothing raised, the request simply did not go where the
    caller asked.
    """

    @classmethod
    def setUpClass(cls):
        cls.proxy = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
        cls.proxy_thread = threading.Thread(target=cls.proxy.serve_forever)
        cls.proxy_thread.daemon = True
        cls.proxy_thread.start()
        cls.proxy_url = "http://127.0.0.1:%d" % cls.proxy.server_address[1]
        # A name that only resolves inside the proxy, so a direct connection
        # cannot succeed and a bypass cannot be mistaken for success.
        cls.target = "http://proxy-target.test/hello"

    @classmethod
    def tearDownClass(cls):
        cls.proxy.shutdown()
        cls.proxy_thread.join(timeout=5)

    def test_session_proxy_constructor_is_used(self):
        """Session(proxy=...) stored the value and never read it.

        Every request went direct. The target below only exists behind the
        proxy, so a direct connection fails and the assertion is about the
        response body rather than about a counter.
        """
        with chrome_client.Session(proxy=self.proxy_url) as session:
            self.assertEqual(
                session._effective_proxy(self.target, None, None), self.proxy_url)
            response = session.get(self.target, timeout=10)
            self.assertEqual(response.status_code, 200)
            # An HTTP proxy receives the absolute URI, as requests-style proxying
            # requires; the fixture echoes it back.
            self.assertEqual(response.text, "proxied:http://proxy-target.test/hello")

    def test_per_request_proxy_still_wins(self):
        with chrome_client.Session() as session:
            response = session.get(self.target, proxy=self.proxy_url, timeout=10)
            # An HTTP proxy receives the absolute URI, as requests-style proxying
            # requires; the fixture echoes it back.
            self.assertEqual(response.text, "proxied:http://proxy-target.test/hello")

    def test_session_proxies_mapping_still_works(self):
        with chrome_client.Session() as session:
            session.proxies.update({"http": self.proxy_url})
            response = session.get(self.target, timeout=10)
            # An HTTP proxy receives the absolute URI, as requests-style proxying
            # requires; the fixture echoes it back.
            self.assertEqual(response.text, "proxied:http://proxy-target.test/hello")

    def test_proxy_error_names_come_from_chromium(self):
        """A proxy failure must name the Chromium error, not a bare code.

        The table had drifted: -111 is ERR_TUNNEL_CONNECTION_FAILED (not
        ERR_TUNNEL_CONNECTION_FAILED at -336, which is ERR_NO_SUPPORTED_PROXIES)
        and -111/-115 were missing entirely, so callers saw "Proxy (net error
        -111)" with nothing to act on.
        """
        from chrome_client._python_impl.exceptions import (_NET_ERRORS, ProxyError,
                                                           map_native_error)
        self.assertEqual(_NET_ERRORS[-111][0], "ERR_TUNNEL_CONNECTION_FAILED")
        self.assertEqual(_NET_ERRORS[-115][0], "ERR_PROXY_AUTH_UNSUPPORTED")
        self.assertEqual(_NET_ERRORS[-120][0], "ERR_SOCKS_CONNECTION_FAILED")
        self.assertEqual(_NET_ERRORS[-127][0], "ERR_PROXY_AUTH_REQUESTED")
        self.assertEqual(_NET_ERRORS[-336][0], "ERR_NO_SUPPORTED_PROXIES")
        self.assertEqual(_NET_ERRORS[-324][0], "ERR_EMPTY_RESPONSE")
        for code, (name, kind) in _NET_ERRORS.items():
            self.assertTrue(name.startswith("ERR_"), (code, name))

        # Every proxy-class error Chromium can report must be mapped. These five
        # were missing, so a PAC failure or a proxy-delegate cancellation
        # surfaced as a bare RequestException carrying only "net error -N": the
        # caller could neither catch ProxyError nor see the name.
        for code, name in (
            (-186, "ERR_PROXY_UNABLE_TO_CONNECT_TO_DESTINATION"),
            (-187, "ERR_PROXY_DELEGATE_CANCELED_CONNECT_REQUEST"),
            (-188, "ERR_PROXY_DELEGATE_CANCELED_CONNECT_RESPONSE"),
            (-327, "ERR_PAC_SCRIPT_FAILED"),
            (-367, "ERR_PAC_SCRIPT_TERMINATED"),
        ):
            self.assertIn(code, _NET_ERRORS, "%d (%s) is unmapped" % (code, name))
            self.assertEqual(_NET_ERRORS[code][0], name)
            self.assertIs(_NET_ERRORS[code][1], ProxyError)
            # It must reach the caller as a ProxyError, not a bare
            # RequestException carrying a numeric message.
            with self.assertRaises(ProxyError) as caught:
                raise map_native_error(RuntimeError("net error %d" % code))
            self.assertIn(name, str(caught.exception))


class ProxyURLNormalisationTests(unittest.TestCase):
    """Pool strings Chromium rejects must be repaired or refused, not passed on.

    ``ProxyRules::ParseFromString`` is strict: a rule it cannot parse leaves the
    rule list empty, which the Core reports as ``ERR_NO_SUPPORTED_PROXIES``
    (-336). That message reads like "no proxy is configured" rather than "your
    proxy string has a trailing newline", which is how a proxy-pool endpoint
    read from a file failed for every request.
    """

    def test_rule_shapes_that_used_to_fail_now_parse(self):
        from chrome_client._python_impl.sessions import normalize_proxy_url
        for raw in ("http://127.0.0.1:8080/", "http://127.0.0.1:8080/path",
                    "http://127.0.0.1:8080#frag", "http://127.0.0.1:8080\n",
                    "  http://127.0.0.1:8080  ", "HTTP://127.0.0.1:8080",
                    "127.0.0.1:8080"):
            rule, user, password = normalize_proxy_url(raw)
            self.assertEqual(rule, "http://127.0.0.1:8080", raw)
            self.assertIsNone(user, raw)
            self.assertIsNone(password, raw)

    def test_socks5h_is_a_spelling_not_a_mode(self):
        """Chromium's SOCKS5 client always sends the hostname to the proxy.

        ``socks5h`` is therefore the behaviour of plain ``socks5``, and the alias
        is all that is needed. GetSchemeFromUriScheme (net/base/proxy_string_util
        .cc) has no ``socks5h`` entry, so passing the spelling through produced
        -336 ERR_NO_SUPPORTED_PROXIES.
        """
        from chrome_client._python_impl.sessions import normalize_proxy_url
        self.assertEqual(normalize_proxy_url("socks5h://1.2.3.4:1080")[0],
                         "socks5://1.2.3.4:1080")
        self.assertEqual(normalize_proxy_url("socks5://1.2.3.4:1080")[0],
                         "socks5://1.2.3.4:1080")

    def test_socks4a_fails_closed(self):
        """SOCKS4a asks for remote name resolution, which the Core cannot do.

        Chromium's SOCKS4 client resolves locally (socks_client_socket.cc
        DoResolveHost), so accepting the spelling would silently send the
        caller's DNS out through this machine instead of the proxy.
        """
        from chrome_client._python_impl.sessions import normalize_proxy_url
        with self.assertRaises(chrome_client.UnsupportedFeature):
            normalize_proxy_url("socks4a://1.2.3.4:1080")

    def test_doubled_scheme_is_refused(self):
        """A pool entry that already carries a scheme, prefixed again by the caller.

        Splitting on the first "://" would leave a host literally named "socks5",
        so the request would be sent to a machine named after a protocol instead
        of failing.
        """
        from chrome_client._python_impl.sessions import normalize_proxy_url
        for raw in ("http://socks5://1.2.3.4:1080", "http://http://1.2.3.4:8080"):
            with self.assertRaises(chrome_client.InvalidProxyURL):
                normalize_proxy_url(raw)

    def test_unknown_scheme_and_query_are_refused(self):
        from chrome_client._python_impl.sessions import normalize_proxy_url
        with self.assertRaises(chrome_client.InvalidProxyURL):
            normalize_proxy_url("ftp://1.2.3.4:21")
        with self.assertRaises(chrome_client.InvalidProxyURL):
            normalize_proxy_url("http://1.2.3.4:8080?x=1")

    def test_credentials_are_split_out_of_the_rule(self):
        from chrome_client._python_impl.sessions import normalize_proxy_url
        rule, user, password = normalize_proxy_url("http://u%40x:p%3Ass@1.2.3.4:8080")
        self.assertEqual(rule, "http://1.2.3.4:8080")
        self.assertEqual(user, "u@x")
        self.assertEqual(password, "p:ss")

    def test_empty_means_no_proxy(self):
        from chrome_client._python_impl.sessions import normalize_proxy_url
        self.assertEqual(normalize_proxy_url("")[0], "")
        self.assertEqual(normalize_proxy_url("   ")[0], "")
        self.assertIsNone(normalize_proxy_url(None)[0])


class NativeProxyRuleTests(unittest.TestCase):
    """Chromium's own rule syntax must reach the Core untouched.

    `ProxyRules::ParseFromString` already understands per-scheme lists
    (`http=h:p;https=h:p`, split on `;` then `=`), comma-separated fallback lists
    and the `direct://` token. The facade used to rewrite every one of them as a
    single `scheme://host` URI, which turned `http=h:p` into a rule naming a host
    literally called "http" and made a valid configuration unroutable.
    """

    def test_per_scheme_lists_are_passed_through(self):
        from chrome_client._python_impl.sessions import normalize_proxy_url
        for raw in ("http=127.0.0.1:8080",
                    "http=127.0.0.1:8080;https=127.0.0.1:8081",
                    "http=127.0.0.1:8080 ; https=127.0.0.1:8081",
                    "ftp=127.0.0.1:21"):
            self.assertEqual(normalize_proxy_url(raw)[0], raw.strip(), raw)

    def test_fallback_lists_are_passed_through(self):
        """A comma-separated list is Chromium's own failover mechanism."""
        from chrome_client._python_impl.sessions import normalize_proxy_url
        raw = "http://127.0.0.1:8080,http://127.0.0.1:8081"
        self.assertEqual(normalize_proxy_url(raw)[0], raw)

    def test_direct_token_means_no_proxy(self):
        from chrome_client._python_impl.sessions import normalize_proxy_url
        self.assertEqual(normalize_proxy_url("direct://")[0], "direct://")
        self.assertEqual(normalize_proxy_url("DIRECT://")[0], "DIRECT://")

    def test_direct_mixed_with_a_server_is_refused(self):
        """Chromium only accepts direct:// as the sole entry of a list."""
        from chrome_client._python_impl.sessions import normalize_proxy_url
        with self.assertRaises(chrome_client.InvalidProxyURL):
            normalize_proxy_url("direct://,http://127.0.0.1:8080")

    def test_unknown_rule_key_is_refused_with_the_accepted_set(self):
        from chrome_client._python_impl.sessions import normalize_proxy_url
        with self.assertRaises(chrome_client.InvalidProxyURL) as caught:
            normalize_proxy_url("socks5=127.0.0.1:1080")
        self.assertIn("http", str(caught.exception))

    def test_credentials_inside_a_rule_list_fail_closed(self):
        """A rule list carries no userinfo, so say so instead of dropping it."""
        from chrome_client._python_impl.sessions import normalize_proxy_url
        with self.assertRaises(chrome_client.InvalidProxyURL) as caught:
            normalize_proxy_url("http=user:pass@127.0.0.1:8080")
        self.assertIn("proxy_auth", str(caught.exception))

    def test_quic_is_accepted_because_the_core_reports_the_capability(self):
        """quic:// parses when -- and only when -- the Core reports the bit.

        The scheme used to be refused here unconditionally, which was right for
        an artifact built without ENABLE_QUIC_PROXY_SUPPORT and wrong for one
        built with it. The answer now comes from mn_engine_capabilities, so this
        test asserts the pair rather than the old constant: the facade accepts
        quic:// exactly when the loaded Core advertises the capability.
        """
        from chrome_client._python_impl import engine
        from chrome_client._python_impl.sessions import normalize_proxy_url

        if not chrome_client.capabilities() & engine.CAPABILITY_QUIC_PROXY:
            self.skipTest("this Core artifact was built without QUIC proxies")
        self.assertEqual("quic://127.0.0.1:443",
                         normalize_proxy_url("quic://127.0.0.1:443")[0])

    def test_socks_is_accepted_as_a_socks5_spelling(self):
        """`socks://` is Chromium's own short spelling for SOCKS5.

        engine.cc listed socks5/socks4/https/quic/http but not "socks", so
        `proxy_chain="socks://h:9"` reached a branch that returned early and the
        caller saw InitializationFailed from a context that was never built --
        while `proxy="socks://h:9"` worked, because that path goes through
        ProxyRules::ParseFromString, which does know the spelling. Two options
        naming one connection path must not disagree.
        """
        from chrome_client._python_impl.sessions import normalize_proxy_chain, normalize_proxy_url
        self.assertEqual(("socks://127.0.0.1:9", None, None),
                         normalize_proxy_url("socks://127.0.0.1:9"))
        self.assertEqual("socks://127.0.0.1:9",
                         normalize_proxy_chain("socks://127.0.0.1:9"))

    def test_a_rule_list_that_yields_no_proxy_is_refused(self):
        """An accepted rule that resolves to nothing is a direct connection.

        ProxyList::AddProxyChain documents "Silently discard malformed inputs"
        (proxy_list.cc:58), and ParseFromString comma-splits before calling it,
        so "[a,b]" loses both halves, "garbage://x" loses its only one, and
        "http=garbage://x;https=garbage://y" empties every per-scheme list.
        Before this check all three were ACCEPTED and then connected directly:
        measured as a 200 from a local origin while the proxy port recorded zero
        connections. That is the accepted-and-ignored downgrade section 8 bans.
        """
        from chrome_client._python_impl.sessions import normalize_proxy_url
        for rule in ("[https://a:1,https://b:2]", "garbage://x",
                     "http=garbage://x;https=garbage://y", "socks=garbage://x"):
            with self.assertRaises(chrome_client.InvalidProxyURL, msg=rule):
                normalize_proxy_url(rule)

    def test_a_chain_that_splits_into_nothing_is_refused(self):
        """",," and "   " name a proxy and produce none: Engine sees a direct path.

        The Python layer already refused these, so the ABI check is what keeps a
        direct Go/Node/cgo caller from the same silent downgrade.
        """
        from chrome_client._python_impl.sessions import normalize_proxy_chain
        for value in (",,", "   ", ",https://127.0.0.1:9,"):
            with self.assertRaises(chrome_client.UnsupportedFeature, msg=value):
                normalize_proxy_chain(value)

    def test_module_level_impersonate_reaches_the_engine(self):
        """The verb helpers must honour impersonate= the way Session does.

        ``chrome_client.get(url, impersonate="chrome_158")`` used to send four
        headers: the option was not in the api module's _SESSION_ONLY list, so it
        travelled as a *request* option into a shared session built with no
        profile. _browser_headers returns early when there is no profile, so the
        captured set was never injected and the call looked like a bare
        transport with a Chrome User-Agent bolted on.

        Verified by decoding the wire, not by inspecting the option: a private
        session is built from the constructor list, so the count is what proves
        the value arrived.
        """
        from chrome_client._python_impl import api

        self.assertIn("impersonate", api._SESSION_ONLY)
        self.assertIn("platform", api._SESSION_ONLY)

    def test_a_module_level_call_with_no_profile_stays_a_bare_transport(self):
        """Adding impersonate to the list must not make every call look like Chrome.

        ``chrome_client.get(url)`` keeps using the shared session, and that
        session has no profile, so the captured set stays out. This is the
        property that would break if _SESSION_ONLY gained a default value.
        """
        from chrome_client._python_impl import api

        shared = api.shared_session()
        self.assertIsNone(shared.impersonate)

    def test_core_overridden_headers_are_stripped(self):
        """Accept-Encoding/Accept-Language are Chromium's, not the caller's.

        Chromium rewrites both after the caller's block is built
        (url_request_http_job.cc AddExtraHeaders), so forwarding a value would
        put something on the wire the caller did not ask for. Stripping keeps the
        profile value, which is what a fingerprint client wants.
        """
        from chrome_client._python_impl.sessions import (
            _strip_core_overridden_headers)
        from chrome_client._python_impl.structures import Headers

        kept, stripped = _strip_core_overridden_headers(Headers([
            ("Accept-Encoding", "identity"),
            ("Accept-Language", "en-US"),
            ("Accept", "application/json"),
            ("User-Agent", "PAYLOAD/1.0"),
        ]))
        self.assertEqual({"accept-encoding", "accept-language"}, stripped)
        self.assertEqual({"Accept", "User-Agent"}, {n for n, _ in kept.items()})

    def test_headers_no_capture_mentions_keep_chromium_position(self):
        """Pragma/Cache-Control precede the captured block, not follow it.

        BuildRequestHeaders sets them before MergeFrom(extra_headers)
        (http_network_transaction.cc:1483-1497), and the captured block *is*
        extra_headers. Appending them after Accept-Language would put them past
        every captured field, which the wire comparison in this file would not
        catch because it only compares names.
        """
        from chrome_client._python_impl.browser_headers import (
            full_header_order, header_order)

        captured = header_order("chrome_158")
        full = full_header_order("chrome_158")
        self.assertEqual(["pragma", "cache-control"], full[:2])
        self.assertEqual([n.lower() for n in captured], full[2:])
        self.assertEqual([], full_header_order(None))

    def test_a_core_without_the_bit_refuses_the_scheme(self):
        """The refusal path stays reachable, and it names the bit.

        Patching the reader rather than the artifact is the only way to cover
        it on a machine whose Core has the flag on; without this, deleting the
        check would leave every test green.
        """
        from chrome_client._python_impl import engine
        from chrome_client._python_impl import sessions

        original = engine.capabilities
        try:
            sessions.capabilities = lambda: 0
            with self.assertRaises(chrome_client.UnsupportedFeature) as caught:
                sessions._require_quic_proxy_support()
        finally:
            sessions.capabilities = original
        self.assertIn("MN_CAPABILITY_QUIC_PROXY", str(caught.exception))

    def test_a_native_rule_list_still_works_through_the_core(self):
        """Pass-through must be real routing, not just string preservation.

        The proxy echoes the absolute URI, so a direct connection cannot pass.
        """
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
        proxy.daemon_threads = True
        proxy_thread = threading.Thread(target=proxy.serve_forever)
        proxy_thread.daemon = True
        proxy_thread.start()
        url = "127.0.0.1:%d" % proxy.server_address[1]
        target = "http://proxy-target.test/hello"
        try:
            for rule in ("http=" + url, "http://" + url + ",http://" + url):
                with chrome_client.Session(proxy=rule) as session:
                    response = session.get(target, timeout=10)
                self.assertEqual(response.status_code, 200, rule)
                self.assertEqual(response.text, "proxied:" + target, rule)
        finally:
            proxy.shutdown()
            proxy_thread.join(timeout=5)

class ProxyURLPortTests(unittest.TestCase):
    """Port handling has to match Chromium, including the falsy port 0.

    `ProxyServer::ParsePort` returns 0 for ":0" -- a real port -- and
    `ProxySchemeHostAndPortToProxyServer` rejects a rule whose port is present but
    empty. Falsy-testing the port collapsed both cases into the scheme default,
    so an input Chromium refuses became a working request and a request to port 0
    silently went to port 80.
    """

    def test_port_zero_survives(self):
        from chrome_client._python_impl.sessions import normalize_proxy_url
        self.assertEqual(normalize_proxy_url("http://h:0")[0], "http://h:0")
        self.assertEqual(normalize_proxy_url("http://[::1]:0")[0], "http://[::1]:0")

    def test_absent_port_is_left_to_chromium(self):
        """No colon at all means "scheme default", which Chromium applies itself.

        http=80, socks4/socks5=1080, https=443 come from
        ProxyServer::GetDefaultPortForScheme; repeating them here would be a
        repeated here would be a second definition of the same table.
        """
        from chrome_client._python_impl.sessions import normalize_proxy_url
        for raw in ("http://h", "https://h", "socks5://h", "socks4://h"):
            self.assertEqual(normalize_proxy_url(raw)[0], raw)

    def test_empty_port_is_refused(self):
        from chrome_client._python_impl.sessions import normalize_proxy_url
        with self.assertRaises(chrome_client.InvalidProxyURL):
            normalize_proxy_url("http://h:")

    def test_trailing_text_is_reported_as_trailing_text(self):
        """A pool comment is not a bad port; say which part is wrong."""
        from chrome_client._python_impl.sessions import normalize_proxy_url
        with self.assertRaises(chrome_client.InvalidProxyURL) as caught:
            normalize_proxy_url("http://h:8080 comment")
        self.assertIn("trailing text", str(caught.exception))
        self.assertNotIn("invalid port", str(caught.exception))

    def test_a_hash_comment_is_stripped_like_any_other_fragment(self):
        from chrome_client._python_impl.sessions import normalize_proxy_url
        self.assertEqual(normalize_proxy_url("http://h:8080 # rot=2")[0],
                         "http://h:8080")

    def test_normalisation_is_idempotent(self):
        """Re-normalising a rule must not change it.

        The rule is the value that reaches the Core; if a second pass altered it,
        the cached Engine keys and the proxy actually used could disagree.
        """
        from chrome_client._python_impl.sessions import normalize_proxy_url
        for raw in ("http://h:8080", "socks5h://h:1080", "h:80",
                    "HTTP://u:p@h:80/", "http://[::1]:8080", "http://h:0"):
            once = normalize_proxy_url(raw)[0]
            self.assertEqual(normalize_proxy_url(once)[0], once, raw)

class ProxyCredentialScopeTests(unittest.TestCase):
    """``proxy_auth`` belongs to the session's proxy, not to every proxy.

    An Engine -- and therefore a proxy -- is chosen per request, while
    ``proxy_auth`` is session state. Applying it unconditionally sent the
    session's username and password to whatever endpoint a rotating pool picked
    for one call.
    """

    @classmethod
    def setUpClass(cls):
        cls.proxy = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
        cls.proxy.daemon_threads = True
        cls.proxy_thread = threading.Thread(target=cls.proxy.serve_forever)
        cls.proxy_thread.daemon = True
        cls.proxy_thread.start()
        cls.proxy_url = "http://127.0.0.1:%d" % cls.proxy.server_address[1]
        cls.target = "http://proxy-target.test/hello"

    @classmethod
    def tearDownClass(cls):
        cls.proxy.shutdown()
        cls.proxy_thread.join(timeout=5)

    def test_credentials_follow_only_the_session_proxy(self):
        with chrome_client.Session(proxy=self.proxy_url,
                                   proxy_auth=("pooluser", "poolpass")) as session:
            # The session's own proxy gets the credentials.
            self.assertTrue(session._proxy_auth_applies(self.proxy_url))
            # Any other endpoint does not.
            self.assertFalse(session._proxy_auth_applies("http://127.0.0.1:9"))
        # A session with no proxy of its own has nothing to scope against, so
        # the per-request proxy is the only candidate.
        with chrome_client.Session() as session:
            self.assertTrue(session._proxy_auth_applies("http://127.0.0.1:9"))

    def test_pool_string_shapes_actually_route_through_the_core(self):
        """The normalised rule must work on the wire, not only in the parser.

        Each of these spellings used to fail with ERR_NO_SUPPORTED_PROXIES,
        which reads like "no proxy configured". The assertion is on the response
        body the proxy echoed, so a direct connection cannot pass.
        """
        for raw in (self.proxy_url + "/", self.proxy_url + "\n",
                    self.proxy_url + "/path", self.proxy_url + "#frag",
                    "  " + self.proxy_url + "  "):
            with chrome_client.Session(proxy=raw) as session:
                response = session.get(self.target, timeout=10)
            self.assertEqual(response.status_code, 200, raw)
            self.assertEqual(response.text, "proxied:" + self.target, raw)

    def test_socks4a_raises_before_any_socket_is_opened(self):
        """Fail-closed must be a construction-time error, not a -336.

        The Core cannot do SOCKS4a, so the caller has to learn that from an
        exception naming the feature rather than from a proxy error code.
        """
        with self.assertRaises(chrome_client.UnsupportedFeature):
            with chrome_client.Session(proxy="socks4a://127.0.0.1:1080") as session:
                session.get(self.target, timeout=5)

    def test_doubled_scheme_raises_before_any_socket_is_opened(self):
        with self.assertRaises(chrome_client.InvalidProxyURL):
            with chrome_client.Session(proxy="http://socks5://127.0.0.1:1080") as session:
                session.get(self.target, timeout=5)
    def test_a_per_request_proxy_does_not_receive_the_session_credentials(self):
        """A pool endpoint must not be handed the session's password.

        The session is configured with proxy A plus ``proxy_auth``; the request is
        pinned to proxy B, which answers 407 with a realm. Before the fix the
        Engine built for B carried the session credentials anyway and sent them
        to B unprompted, so B saw a ``Proxy-Authorization`` header it should
        never have received (and, answering 407 again, the call ended in
        ERR_TOO_MANY_RETRIES instead of returning the challenge).

        Asserting on the header B actually received is what makes this a test:
        the status code alone cannot distinguish "B was challenged" from "B
        never got credentials".
        """
        class Challenger(BaseHTTPRequestHandler):
            """Always answers 407; records any credentials it was given."""

            protocol_version = "HTTP/1.1"
            seen = []

            def do_GET(self):
                type(self).seen.append(self.headers.get("Proxy-Authorization"))
                self.send_response(407)
                self.send_header("Proxy-Authenticate", 'Basic realm="p"')
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args):
                pass

        challenger = ThreadingHTTPServer(("127.0.0.1", 0), Challenger)
        challenger.daemon_threads = True
        challenger_thread = threading.Thread(target=challenger.serve_forever)
        challenger_thread.daemon = True
        challenger_thread.start()
        challenger_url = "http://127.0.0.1:%d" % challenger.server_address[1]
        try:
            with chrome_client.Session(proxy=self.proxy_url,
                                       proxy_auth=("pooluser", "poolpass")) as session:
                try:
                    session.get(self.target, proxy=challenger_url, timeout=15)
                except chrome_client.RequestException:
                    pass
        finally:
            challenger.shutdown()
            challenger_thread.join(timeout=5)
        self.assertTrue(Challenger.seen, "the per-request proxy was never used")
        self.assertEqual([value for value in Challenger.seen if value is not None], [])

    def test_the_session_proxies_mapping_also_scopes_the_credentials(self):
        """The session can name its proxy through ``proxies`` instead of ``proxy=``.

        Scoping only on ``proxy=`` left the mapping form unscoped, so a request
        pinned to a different proxy still received the session credentials.
        ``ProxyHandler`` never challenges, so this asserts on the header each
        proxy actually received.
        """
        class Challenger(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            seen = []

            def do_GET(self):
                type(self).seen.append(self.headers.get("Proxy-Authorization"))
                self.send_response(407)
                self.send_header("Proxy-Authenticate", 'Basic realm="p"')
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args):
                pass

        challenger = ThreadingHTTPServer(("127.0.0.1", 0), Challenger)
        challenger.daemon_threads = True
        challenger_thread = threading.Thread(target=challenger.serve_forever)
        challenger_thread.daemon = True
        challenger_thread.start()
        challenger_url = "http://127.0.0.1:%d" % challenger.server_address[1]
        try:
            with chrome_client.Session(
                    proxies={"http": self.proxy_url, "https": self.proxy_url},
                    proxy_auth=("pooluser", "poolpass")) as session:
                try:
                    session.get(self.target, proxy=challenger_url, timeout=15)
                except chrome_client.RequestException:
                    pass
        finally:
            challenger.shutdown()
            challenger_thread.join(timeout=5)
        self.assertTrue(Challenger.seen, "the per-request proxy was never used")
        self.assertEqual([value for value in Challenger.seen if value is not None], [])

    def test_a_per_request_proxies_mapping_does_not_receive_them_either(self):
        """Both override spellings must be scoped, not only `proxy=`.

        Fixing only `proxy=` left `proxies={...}` leaking: the session named its
        proxy through the mapping, the call replaced it via `proxies=`, and the
        Engine for the replacement still carried the session credentials.
        """
        class Challenger(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            seen = []

            def do_GET(self):
                type(self).seen.append(self.headers.get("Proxy-Authorization"))
                self.send_response(407)
                self.send_header("Proxy-Authenticate", 'Basic realm="p"')
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args):
                pass

        challenger = ThreadingHTTPServer(("127.0.0.1", 0), Challenger)
        challenger.daemon_threads = True
        challenger_thread = threading.Thread(target=challenger.serve_forever)
        challenger_thread.daemon = True
        challenger_thread.start()
        challenger_url = "http://127.0.0.1:%d" % challenger.server_address[1]
        try:
            with chrome_client.Session(proxies={"http": self.proxy_url},
                                       proxy_auth=("pooluser", "poolpass")) as session:
                try:
                    session.get(self.target, proxies={"http": challenger_url},
                                timeout=15)
                except chrome_client.RequestException:
                    pass
        finally:
            challenger.shutdown()
            challenger_thread.join(timeout=5)
        self.assertTrue(Challenger.seen, "the per-request proxy was never used")
        self.assertEqual([value for value in Challenger.seen if value is not None], [])

    def test_the_session_proxy_still_authenticates_when_overridden_by_itself(self):
        """Scoping must not disable the credentials for the session proxy itself.

        The session is pinned to a challenging proxy and the call repeats it; the
        credentials have to be attached, or every request would 407 forever.
        """
        received = []
        original = AuthProxyHandler.do_GET

        def capturing(handler):
            received.append(handler.headers.get("Proxy-Authorization"))
            return original(handler)

        proxy = ThreadingHTTPServer(("127.0.0.1", 0), AuthProxyHandler)
        proxy.daemon_threads = True
        proxy_thread = threading.Thread(target=proxy.serve_forever)
        proxy_thread.daemon = True
        proxy_thread.start()
        url = "http://127.0.0.1:%d" % proxy.server_address[1]
        AuthProxyHandler.do_GET = capturing
        try:
            with chrome_client.Session(proxy=url,
                                       proxy_auth=("user", "pass")) as session:
                response = session.get(self.target, proxy=url, timeout=20)
        finally:
            AuthProxyHandler.do_GET = original
            proxy.shutdown()
            proxy_thread.join(timeout=5)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any(value for value in received), received)

    def test_the_session_proxy_still_gets_its_credentials(self):
        """The scoping must not disable authentication for the session proxy."""
        self.assertTrue(chrome_client.Session(
            proxy=self.proxy_url, proxy_auth=("pooluser", "poolpass")
        )._proxy_auth_applies(self.proxy_url))


class ProxyRetryPolicyTests(unittest.TestCase):
    """A proxy failure that never reached the origin is retried once.

    A rotating pool produces ERR_PROXY_CONNECTION_FAILED (-130) and
    ERR_TUNNEL_CONNECTION_FAILED (-111) on healthy traffic: the endpoint chosen
    for this call was momentarily unreachable or refused the CONNECT. Since the
    origin never saw the request, one extra attempt cannot duplicate an effect --
    but the retry must not apply to a request whose body cannot be replayed, nor
    to credentials the proxy already rejected.
    """

    TARGET = "http://proxy-target.test/hello"

    def test_replayability_guards_the_retry(self):
        from chrome_client._python_impl.models import PreparedRequest
        from chrome_client._python_impl.sessions import _request_is_replayable

        get = PreparedRequest()
        get.method, get.url = "GET", self.TARGET
        self.assertTrue(_request_is_replayable(get))

        post = PreparedRequest()
        post.method, post.url = "POST", self.TARGET
        self.assertFalse(_request_is_replayable(post))

        streamed = PreparedRequest()
        streamed.method, streamed.url = "GET", self.TARGET
        streamed.stream_body = True
        self.assertFalse(_request_is_replayable(streamed))

    def test_only_unreachable_or_tunnelled_proxy_errors_are_retried(self):
        from chrome_client._python_impl.sessions import _is_retriable_proxy_failure

        self.assertTrue(_is_retriable_proxy_failure(
            chrome_client.ProxyError("ERR_PROXY_CONNECTION_FAILED (net error -130)")))
        self.assertTrue(_is_retriable_proxy_failure(
            chrome_client.ProxyError("ERR_TUNNEL_CONNECTION_FAILED (net error -111)")))
        # Rejected credentials: another attempt just spends 32 more restarts.
        self.assertFalse(_is_retriable_proxy_failure(
            chrome_client.ProxyError("ERR_TOO_MANY_RETRIES (net error -375)")))
        self.assertFalse(_is_retriable_proxy_failure(
            chrome_client.ProxyError("ERR_NO_SUPPORTED_PROXIES (net error -336)")))
        self.assertFalse(_is_retriable_proxy_failure(RuntimeError("not a proxy error")))

    def test_a_dead_proxy_is_attempted_twice_for_a_get_and_once_for_a_post(self):
        """Attempt counting is the only way to see the retry from outside.

        Nothing listens on the port, so every attempt fails with -130 and the
        call still raises -- the difference is how many connection attempts were
        made, which the proxy cannot report because it does not exist.
        """
        from chrome_client._python_impl import sessions as sessions_module

        attempts = []
        original = sessions_module.Session._send_once

        def counting(self, prepared, options):
            attempts.append(prepared.method)
            return original(self, prepared, options)

        sessions_module.Session._send_once = counting
        try:
            with chrome_client.Session(proxy="http://127.0.0.1:1") as session:
                with self.assertRaises(chrome_client.ProxyError):
                    session.get(self.TARGET, timeout=5)
                self.assertEqual(len(attempts), 2)

            del attempts[:]
            with chrome_client.Session(proxy="http://127.0.0.1:1") as session:
                with self.assertRaises(chrome_client.ProxyError):
                    session.post(self.TARGET, data=b"x", timeout=5)
                self.assertEqual(len(attempts), 1)

            # An explicit policy replaces the default instead of stacking on it.
            del attempts[:]
            policy = chrome_client.RetryStrategy(count=2, delay=0.01)
            with chrome_client.Session(proxy="http://127.0.0.1:1",
                                       retry=policy) as session:
                with self.assertRaises(chrome_client.ProxyError):
                    session.get(self.TARGET, timeout=5)
                self.assertEqual(len(attempts), 3)
        finally:
            sessions_module.Session._send_once = original

class ManualRedirectErrorNamingTests(unittest.TestCase):
    """Every redirect mode must name the Chromium net error, not a bare category.

    The manual-redirect path (`allow_redirects=False`, which `Session.head` also
    uses) went through `ResponseFuture` and lost the numeric code, so a tunnel
    failure surfaced as the single word `Proxy` while the follow-redirect path
    reported `ERR_TUNNEL_CONNECTION_FAILED (net error -111)`. Callers act on the
    name; `Proxy` alone says nothing about what to fix.
    """

    @classmethod
    def setUpClass(cls):
        # A proxy that refuses every CONNECT with 502. Chromium turns any
        # non-200/407 CONNECT response into ERR_TUNNEL_CONNECTION_FAILED
        # (http_proxy_client_socket.cc DoProcessResponseCode).
        cls.target = "https://tunnel-target.test/"

    def test_manual_and_follow_modes_report_the_same_error_name(self):
        from chrome_client._python_impl.exceptions import map_native_error
        # The mapping itself is the contract; the transport path that produced
        # the string is covered by the live fixture below.
        for raw in ("Proxy (Chromium net error -111)",
                    "Proxy (net error -111)"):
            mapped = map_native_error(raw)
            self.assertIsInstance(mapped, chrome_client.ProxyError)
            self.assertIn("ERR_TUNNEL_CONNECTION_FAILED", str(mapped))
            self.assertNotEqual(str(mapped), "Proxy")

    def test_head_and_no_redirect_name_the_net_error(self):
        """Live check: a refused tunnel reports its code in every mode.

        A loopback proxy would be implicitly bypassed by Chromium, so this uses
        a dead port, whose failure mode is ERR_PROXY_CONNECTION_FAILED (-130).
        The point is identical: the code must survive the manual-redirect path.
        """
        dead = "http://127.0.0.1:1"
        for call in (lambda s: s.head(self.target, timeout=5),
                     lambda s: s.get(self.target, timeout=5, allow_redirects=False),
                     lambda s: s.get(self.target, timeout=5)):
            with chrome_client.Session(proxy=dead) as session:
                with self.assertRaises(chrome_client.ProxyError) as caught:
                    call(session)
            message = str(caught.exception)
            self.assertIn("net error", message)
            self.assertTrue(message.startswith("ERR_"), message)

    def test_requests_namespace_session_behaves_identically(self):
        """`from chrome_client import requests` must not have a separate path.

        The shim re-exports the same Session class, so a proxy failure has to
        arrive with the same name and the same exception type.
        """
        from chrome_client import requests as cc_requests

        self.assertIs(cc_requests.Session, chrome_client.Session)
        session = cc_requests.Session()
        session.proxies = {"http": "http://127.0.0.1:1",
                           "https": "http://127.0.0.1:1"}
        try:
            with self.assertRaises(chrome_client.ProxyError) as caught:
                session.head(self.target, timeout=5)
        finally:
            session.close()
        self.assertIn("ERR_PROXY_CONNECTION_FAILED", str(caught.exception))

class ProxyErrorClassificationTests(unittest.TestCase):
    """Every proxy failure must be a ProxyError, whatever the Core category says.

    `core/source/minicronet/error_mapping.h` decides the coarse category the
    Core sends; the facade table decides the exception class. Rows that were
    missing from BOTH sides (-170, -323, -336, -364, -366) fell through to
    ConnectionError, and -336 -- the one a misconfigured proxy rule produces --
    arrived as a generic network error.
    """

    def test_every_proxy_code_maps_to_proxy_error(self):
        from chrome_client._python_impl.exceptions import map_native_error
        codes = {-111, -115, -120, -121, -127, -130, -131, -136,
                 -170, -323, -336, -364, -366, -375}
        for code in sorted(codes):
            # The Core used to send "Network" for the ones missing from its own
            # switch; both spellings must reach the same class.
            for category in ("Proxy", "Network"):
                mapped = map_native_error(
                    "%s (Chromium net error %d)" % (category, code))
                self.assertIsInstance(mapped, chrome_client.ProxyError,
                                      (code, category, type(mapped).__name__))
                self.assertIn("net error %d" % code, str(mapped))

    def test_no_supported_proxies_is_named(self):
        from chrome_client._python_impl.exceptions import map_native_error
        mapped = map_native_error("Proxy (Chromium net error -336)")
        self.assertIn("ERR_NO_SUPPORTED_PROXIES", str(mapped))

    def test_a_rule_list_of_only_separators_is_refused(self):
        """Empty ProxyRules means "direct" to Chromium, not an error.

        The Core rejects non-empty input that parses to no rule, so the facade
        has to say what is wrong instead of letting it surface as InvalidURL.
        """
        from chrome_client._python_impl.sessions import normalize_proxy_url
        for raw in (" ; ", ";", "  ;  "):
            with self.assertRaises(chrome_client.InvalidProxyURL):
                normalize_proxy_url(raw)

    def test_an_empty_option_still_means_direct(self):
        from chrome_client._python_impl.sessions import normalize_proxy_url
        self.assertEqual(normalize_proxy_url("")[0], "")
        self.assertEqual(normalize_proxy_url("   ")[0], "")


class TimeoutBoundaryTests(unittest.TestCase):
    """An out-of-range timeout must raise, not abort the interpreter.

    `Duration::from_secs_f64` panics above u64::MAX seconds. pyo3 turns that
    into PanicException, which derives from BaseException -- so it escaped every
    `except Exception` and killed the process rather than the call.
    """

    def test_huge_and_non_finite_timeouts_are_rejected(self):
        from chrome_client._python_impl.engine import EngineConfig
        engine = EngineConfig().build()
        for value in (1e20, 1e300, float("inf"), float("nan"), -1.0):
            with self.assertRaises((RuntimeError, ValueError)) as caught:
                engine.request("GET", "http://127.0.0.1:1/", [], None, value,
                               True, None, None, None, False)
            self.assertNotIsInstance(caught.exception, KeyboardInterrupt)

    def test_a_large_but_representable_timeout_still_works(self):
        from chrome_client._python_impl.engine import EngineConfig
        engine = EngineConfig().build()
        request = engine.request("GET", "http://127.0.0.1:1/", [], None, 1e19,
                                 True, None, None, None, False)
        with self.assertRaises(RuntimeError):
            request.start_stream()

class Python36SyntaxFloorTests(unittest.TestCase):
    """The cp36-abi3 wheel claims 3.6, so the pure-Python half must honour it.

    The extension is built against `abi3-py36`, but the package also ships
    `chrome_client/**/*.py`, which is parsed by whatever interpreter installs it.
    A 3.7-only stdlib call in that code therefore fails on a supported version
    even though the wheel installed fine.
    """

    @staticmethod
    def _package_root():
        import chrome_client
        import pathlib
        return pathlib.Path(chrome_client.__file__).parent

    def test_shipped_modules_parse_with_the_36_grammar(self):
        import ast
        root = self._package_root()
        files = sorted(root.rglob("*.py"))
        self.assertTrue(files, "no Python sources found to check")
        for path in files:
            with self.subTest(path=str(path)):
                ast.parse(path.read_text(encoding="utf-8"),
                          filename=str(path), feature_version=(3, 6))

    def test_stdlib_attributes_absent_on_36_are_guarded(self):
        """Calling a 3.7+ attribute must be guarded, not assumed.

        `asyncio.get_running_loop` is the one that bit: it is 3.7+, and on 3.6
        the AttributeError slipped past an `except RuntimeError`, leaking the
        coroutine it was about to schedule.
        """
        import ast
        root = self._package_root()
        gated = {"asyncio": ("get_running_loop", "create_task", "all_tasks",
                             "current_task", "run")}
        offenders = []
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute):
                    continue
                if not isinstance(node.value, ast.Name):
                    continue
                names = gated.get(node.value.id)
                if names and node.attr in names:
                    # A guarded call is a string lookup (hasattr/getattr) or an
                    # attribute of a module object fetched under try/except.
                    offenders.append("%s:%d asyncio.%s"
                                     % (path.name, node.lineno, node.attr))
        # Every current use is either hasattr-guarded or inside try/except
        # AttributeError; assert that by looking for the guard in the file.
        for entry in offenders:
            name, lineno = entry.split(":")[0], int(entry.split(":")[1].split()[0])
            source = (root / "_python_impl" / name)
            if not source.exists():
                source = next(root.rglob(name))
            lines = source.read_text(encoding="utf-8").splitlines()
            window = chr(10).join(lines[max(0, lineno - 8):lineno + 2])
            guarded = (
                "hasattr(asyncio" in window
                # Fetching the attribute itself inside try/except is the other
                # accepted form: `except AttributeError` has to guard the
                # lookup, not merely appear somewhere nearby.
                or ("= asyncio." in window and "except AttributeError" in window)
            )
            self.assertTrue(
                guarded,
                "%s calls a 3.7+ attribute without a 3.6 guard" % entry)


class AsyncReleaseWithoutLoopTests(unittest.TestCase):
    """An abandoned async response must release itself with no running loop.

    `release_without_loop` exists because scheduling the teardown coroutine on
    a loop that will never run it left the native request -- and its socket and
    buffered body -- alive for the process lifetime.
    """

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()
        cls.url = "http://127.0.0.1:%d/big" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=5)

    def test_close_after_the_loop_stopped(self):
        import warnings

        async def scenario():
            async with chrome_client.AsyncSession() as session:
                return await session.get(self.url, stream=True)

        loop = asyncio.new_event_loop()
        try:
            response = loop.run_until_complete(scenario())
        finally:
            loop.close()
        with warnings.catch_warnings():
            # An unawaited coroutine is the symptom of the old behaviour.
            warnings.simplefilter("error", RuntimeWarning)
            response.close()

    def test_the_reader_exposes_a_synchronous_release(self):
        from chrome_client._python_impl.sessions import _AsyncBodyReader
        self.assertTrue(callable(getattr(_AsyncBodyReader,
                                       "release_without_loop", None)))

class StreamingErrorOwnershipTests(unittest.TestCase):
    """A deadline belongs to the request; a size limit belongs to the consumer.

    These two live in different test modules and disagreed once: suppressing
    every streaming error at the head broke the deadline contract, and raising
    every one broke the size contract.
    """

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_size_limit_surfaces_while_iterating_not_at_get(self):
        async def run():
            async with chrome_client.AsyncSession() as session:
                response = await session.get(self.base + "/bytes/40000",
                                            stream=True, max_response_bytes=1024)
                with self.assertRaises(chrome_client.ResponseTooLarge):
                    async for _chunk in response.aiter_content():
                        pass
        asyncio.run(run())

    def test_deadline_surfaces_at_get_even_when_streaming(self):
        async def run():
            async with chrome_client.AsyncSession() as session:
                with self.assertRaises(chrome_client.Timeout):
                    await session.get(self.base + "/slow", stream=True, timeout=0.05)
        asyncio.run(run())


class NetErrorTableShapeTests(unittest.TestCase):
    """The table is generated data; keep it shaped like it.

    tools/audit-net-error-names.py checks the names against Chromium's
    net_error_list.h, which needs a checkout. These assertions hold without
    one, so a plain `python -m unittest` still catches a malformed row.
    """

    def test_every_row_is_a_known_chromium_code(self):
        from chrome_client._python_impl.exceptions import _NET_ERRORS
        self.assertGreater(len(_NET_ERRORS), 55)
        for code, row in _NET_ERRORS.items():
            self.assertIsInstance(code, int)
            self.assertEqual(len(row), 2, code)
            name, kind = row
            self.assertTrue(name.startswith("ERR_"), (code, name))
            self.assertIsNone(kind) if kind is None else self.assertTrue(
                isinstance(kind, type) and issubclass(kind, Exception), (code, kind))

    def test_transport_errors_are_not_reported_as_tls_or_chunked(self):
        """HTTP/2 and QUIC failures used to raise SSLError / ChunkedEncodingError.

        An h2 stream error is neither a certificate problem (the handshake
        succeeded) nor an HTTP/1.1 chunked-framing fault, so both classes sent
        callers to the wrong place. The Core folds every HTTP/2 and QUIC protocol
        error onto one "Protocol" category, so that category has to mean
        "transport broke", not "chunked framing".
        """
        from chrome_client._python_impl import exceptions as exc
        table = exc._NET_ERRORS
        self.assertIs(table[-337][1], exc.ConnectionError)   # ERR_HTTP2_PROTOCOL_ERROR
        self.assertIs(table[-358][1], exc.ConnectionError)   # ERR_QUIC_HANDSHAKE_FAILED
        self.assertIs(table[-321][1], exc.ChunkedEncodingError)
        category = dict((name, kind) for name, kind in exc._NATIVE_ERRORS)
        self.assertIs(category["Protocol"], exc.ConnectionError)
        self.assertIsNot(category["Protocol"], exc.SSLError)

class HeaderTableOwnershipTests(unittest.TestCase):
    """The Core owns the reserved-name table; the facade asks and keeps no copy.

    Before ABI 15 the facade held three of them -- _HOP_BY_HOP_HEADERS,
    _CORE_OWNED_HEADERS and _FORBIDDEN_WS_HEADERS -- and Go or Node would have
    had to hold the same three again. The migration is only real if the Core
    answers on its own, so these tests break the facade's own checks on purpose
    and require the request to still be refused or cleaned.
    """

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _EchoHeaderHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d/" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        del _ECHOED[:]

    def test_the_facade_keeps_no_header_table(self):
        """A table here would be the second definition the gate forbids."""
        from chrome_client._python_impl import sessions
        for name in ("_HOP_BY_HOP_HEADERS", "_CORE_OWNED_HEADERS",
                     "_DROPPED_IN_DROP_POLICY", "_FORBIDDEN_WS_HEADERS"):
            self.assertFalse(hasattr(sessions, name),
                             "%s is a second copy of the Core's table" % name)

    def test_the_core_refuses_a_reserved_name_by_itself(self):
        """With the facade's check disabled, mn_request_create is what refuses.

        The facade normally words this error, so the exception here is the
        generic Core mapping; what the test pins is that the refusal happens at
        all when the Python check is gone, which is what makes the rule one
        contract instead of two.
        """
        import unittest.mock as mock
        # header_policy="strict" so the Core gets MN_HEADER_POLICY_REJECT: with
        # the facade's pre-check patched out, the refusal has to come from
        # mn_request_create for this test to mean anything.
        with mock.patch.object(sessions_base(), "_check_header_policy",
                               lambda self, headers: None):
            with self.assertRaises(chrome_client.RequestException):
                chrome_client.Session(header_policy="strict").get(
                    self.base, headers={"Connection": "keep-alive"})

    def test_the_core_drops_by_itself(self):
        """With both facade-side steps disabled, the field still does not ship."""
        import unittest.mock as mock
        with mock.patch.object(sessions_base(), "_check_header_policy",
                               lambda self, headers: None):
            with mock.patch.object(sessions_base(), "_apply_header_policy",
                                   lambda self, headers: headers):
                with chrome_client.Session(header_policy="drop") as session:
                    response = session.get(self.base, headers={
                        "Upgrade": "h2c", "TE": "trailers", "X-Kept": "1"})
                    self.assertEqual(response.status_code, 200)
                    # Nothing was counted, because the facade's counting step is
                    # the one that was disabled.
                    self.assertEqual(dict(session.dropped_headers), {})
        seen = _ECHOED[-1]
        self.assertNotIn("Upgrade", seen)
        self.assertNotIn("TE", seen)
        self.assertEqual(seen.get("X-Kept"), "1")


def sessions_base():
    """The class both session flavours share, for patching one method on both."""
    from chrome_client._python_impl.sessions import BaseSession
    return BaseSession


class HeaderPolicyTests(unittest.TestCase):
    """Three policies; "compat" is the default.

    The same field can mean different things to different callers. A hop-by-hop
    field (Connection, Keep-Alive, TE, Trailer, Upgrade, Proxy-Connection) can
    never be written -- Chromium owns the connection on HTTP/1.1 and h2/h3 forbid
    them -- so the default removes it and counts it. A field Chromium overwrites
    (Host, Cookie, Content-Length, Transfer-Encoding, Accept-Encoding) is a
    different promise: removing *those* makes the profile's value win, which is
    what "drop" is for and why it is not the default.
    """

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _EchoHeaderHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d/" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        del _ECHOED[:]

    @staticmethod
    def _last():
        return _ECHOED[-1]

    def test_compat_is_the_default(self):
        with chrome_client.Session() as session:
            self.assertEqual(session.header_policy, "compat")
            response = session.get(self.base, headers={
                'Connection': 'keep-alive',
                'Cookie': 'caller=1',
                'X-Kept': '1',
            })
            self.assertEqual(response.status_code, 200)
            # Only the unsettable name goes; a field Chromium merely overwrites
            # keeps the behaviour it had before this policy existed.
            self.assertEqual(sorted(session.dropped_headers), ["connection"])
            seen = self._last()
            self.assertEqual(seen.get("Cookie"), "caller=1")
            self.assertEqual(seen.get("X-Kept"), "1")

    def test_explicit_strict_still_refuses(self):
        with chrome_client.Session(header_policy="strict") as session:
            self.assertEqual(session.header_policy, "strict")
            with self.assertRaises(chrome_client.UnsupportedFeature):
                session.get(self.base, headers={'Connection': 'keep-alive'})

    def test_drop_also_removes_what_chromium_would_overwrite(self):
        with chrome_client.Session(header_policy="drop") as session:
            response = session.get(self.base, headers={
                "Connection": "keep-alive",
                "Host": "spoofed.example",
                "Cookie": "a=1",
                "Content-Length": "5",
                "Accept-Language": "zh-CN,zh;q=0.9",
            })
            self.assertEqual(response.status_code, 200)
            seen = self._last()
            # Host is Chromium's own (net/http/http_network_transaction.cc:1451)
            self.assertNotEqual(seen.get("Host"), "spoofed.example")
            self.assertNotIn("Cookie", seen)
            self.assertEqual(sorted(session.dropped_headers),
                             ["connection", "content-length", "cookie"])
            # Accept-Language is not in the drop set: Chromium keeps it.
            self.assertEqual(seen.get("Accept-Language"), "zh-CN,zh;q=0.9")

    def test_accept_language_survives_without_a_profile(self):
        with chrome_client.Session() as session:
            session.get(self.base, headers={"Accept-Language": "zh-CN,zh;q=0.9"})
        self.assertEqual(self._last().get("Accept-Language"), "zh-CN,zh;q=0.9")

    def test_the_captured_accept_language_wins_with_a_profile(self):
        with chrome_client.Session(impersonate="chrome_158") as session:
            session.get(self.base, headers={"Accept-Language": "fr-FR,fr;q=0.9"})
        self.assertEqual(self._last().get("Accept-Language"), "zh-CN,zh;q=0.9")

    def test_cache_control_lands_in_chromiums_prefix_slot(self):
        with chrome_client.Session(impersonate="chrome_158") as session:
            session.get(self.base, headers={"Cache-Control": "no-cache"})
        order = list(self._last())
        # BuildRequestHeaders inserts it before MergeFrom(extra_headers)
        # (net/http/http_network_transaction.cc:1479-1497), so it belongs in
        # front of every captured field.
        self.assertLess(order.index("Cache-Control"), order.index("sec-ch-ua"))

    def test_the_module_level_shorthand_can_choose_the_policy(self):
        """The refusal message recommends a policy the shorthand could not set.

        ``chrome_client.requests.post(..., header_policy='strict')`` was forwarded
        to Session.request, which has no such keyword: the advice in the
        UnsupportedFeature message was unreachable from the module-level API.
        Measured on the 0.2.18 wheel: TypeError instead of a request.
        """
        response = chrome_client.requests.post(
            self.base, data={'a': '1'}, headers={'Connection': 'keep-alive'},
            timeout=10)
        self.assertEqual(response.status_code, 200)
        # The shared session is not reconfigured: the option builds a private
        # one for this call, so the process-wide default is untouched.
        with self.assertRaises(chrome_client.UnsupportedFeature):
            chrome_client.requests.post(
                self.base, headers={'Connection': 'keep-alive'},
                header_policy='strict')

    def test_the_environment_variable_moves_the_default(self):
        """A shell or CI job sets it once; no code changes at all."""
        code = 'import chrome_client; print(chrome_client.Session().header_policy)'
        environment = dict(os.environ, CHROME_CLIENT_HEADER_POLICY='strict')
        done = subprocess.run([sys.executable, '-c', code], env=environment,
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), 'strict')
        environment['CHROME_CLIENT_HEADER_POLICY'] = 'clean'
        done = subprocess.run([sys.executable, '-c', code], env=environment,
                              capture_output=True, text=True, timeout=120)
        self.assertNotEqual(done.returncode, 0, 'a typo must not pick a policy')
        self.assertIn('header_policy must be', done.stderr)

    def test_the_process_default_can_be_moved_without_touching_every_call(self):
        """set_default_header_policy() moves it; an explicit value still wins."""
        import unittest.mock as mock
        from chrome_client._python_impl import sessions as impl
        with mock.patch.object(impl, "_DEFAULT_HEADER_POLICY", {"value": "strict"}):
            with chrome_client.Session() as session:
                self.assertEqual(session.header_policy, "strict")
                with self.assertRaises(chrome_client.UnsupportedFeature):
                    session.get(self.base, headers={'Connection': 'keep-alive'})
            with chrome_client.Session(header_policy="compat") as session:
                response = session.get(self.base,
                                        headers={'Connection': 'keep-alive'})
                self.assertEqual(response.status_code, 200)

    def test_quote_is_gone(self):
        """The URL is always percent-encoded; the keyword no longer exists."""
        with chrome_client.Session() as session:
            with self.assertRaises(TypeError):
                session.request('GET', self.base, quote=False)
            with self.assertRaises(TypeError):
                session.request('GET', self.base, quote=True)
    def test_an_unknown_policy_is_refused(self):
        with self.assertRaises(ValueError):
            chrome_client.Session(header_policy="clean")

if __name__ == "__main__":
    unittest.main()

class AbandonedAsyncStreamTests(unittest.TestCase):
    """An async stream the caller never closed must release its socket.

    Every explicit path (``aclose()``, the ``content`` setter) requires the
    caller to act. A response that is simply dropped runs the generator
    ``finally`` during GC, and that path used to call ``aclose()`` without
    ``cancel=True``: the reader detached its callback but never cancelled the
    Core request, so the socket stayed in the engine pool.

    Measured over a never-ending chunked body: 20 abandoned responses leaked
    46 file descriptors and left 20 server connections open for the life of
    the process. A finite body hides this, because the transfer completes and
    the socket is reused before the leak can show.
    """

    @classmethod
    def setUpClass(cls):
        cls.server, cls.port = _endless_chunked_server()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def _fds(self):
        return len(os.listdir("/proc/self/fd"))

    @unittest.skipUnless(os.path.isdir("/proc/self/fd"),
                         "needs /proc to count descriptors")
    def test_an_abandoned_async_stream_releases_its_socket(self):
        url = "http://127.0.0.1:%d/" % self.port

        async def abandon(count):
            for _ in range(count):
                session = chrome_client.AsyncSession()
                response = await session.get(url, stream=True, timeout=10)
                iterator = response.aiter_content(4096)
                try:
                    await iterator.__anext__()
                except StopAsyncIteration:
                    pass
                # No aclose(): dropping everything is the case under test.

        gc.collect()
        time.sleep(0.2)
        before = self._fds()
        asyncio.run(abandon(20))
        gc.collect()
        time.sleep(1.5)
        after = self._fds()
        self.assertLess(
            after - before, 10,
            "20 abandoned async streams grew the descriptor count by %d; the "
            "generator finally must cancel the Core request, not just detach "
            "its callback" % (after - before))

    def test_closing_properly_also_releases(self):
        """The fix must not regress the explicit path."""
        url = "http://127.0.0.1:%d/" % self.port

        async def closed(count):
            for _ in range(count):
                session = chrome_client.AsyncSession()
                response = await session.get(url, stream=True, timeout=10)
                iterator = response.aiter_content(4096)
                try:
                    await iterator.__anext__()
                except StopAsyncIteration:
                    pass
                await response.aclose()
                await session.aclose()

        gc.collect()
        time.sleep(0.2)
        before = self._fds()
        asyncio.run(closed(10))
        gc.collect()
        time.sleep(1.0)
        after = self._fds()
        self.assertLess(after - before, 10,
                        "closing properly grew the descriptor count by %d"
                        % (after - before))


def _endless_chunked_server():
    """A server that streams chunked data forever, so an uncancelled request
    cannot finish on its own and the leak becomes observable."""
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(64)
    port = listener.getsockname()[1]
    stop = threading.Event()

    def handle(conn):
        try:
            conn.settimeout(30)
            buf = b""
            while b"\r\n\r\n" not in buf:
                data = conn.recv(65536)
                if not data:
                    return
                buf += data
            conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
            while not stop.is_set():
                conn.sendall(b"400\r\n" + b"z" * 1024 + b"\r\n")
                time.sleep(0.001)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def serve():
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()

    class _Server(object):
        def close(self):
            stop.set()
            try:
                listener.close()
            except Exception:
                pass

    return _Server(), port
