"""Regression tests for the profile-driven request header set.

``impersonate=`` used to own only the ``User-Agent`` version string, so a
``chrome_155`` session sent four headers where that release sends twelve.  The
set now comes from ``profiles/chrome_<major>/h1/captures.json`` -- six
independent captures of that release's own build, all identical -- so these
tests assert against the evidence itself rather than against a copy of it.

Two properties matter beyond "the headers are there":

* the platform follows the host, because a ``"Windows"`` hint beside an
  ``X11; Linux`` User-Agent is a louder contradiction than a missing hint;
* nothing is injected when no profile is named, because a session that never
  claimed to be a browser must not start looking like Chrome.

Run with the audited Core available::

    LD_LIBRARY_PATH=core/binaries/linux-x86_64 \
    PYTHONPATH=bindings/python \
    python3 -m unittest bindings/python/tests/test_client_hints.py
"""

import asyncio
import json
import pathlib
import threading
import unittest

try:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
except ImportError:  # Python 3.6
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

import chrome_client
from chrome_client._python_impl import browser_headers as hints
from chrome_client._python_impl.impersonate import LATEST_CHROME, OLDEST_CHROME
from chrome_client._python_impl.client_hints import CAPTURED_HEADERS

PROFILES = pathlib.Path(__file__).resolve().parents[3] / "profiles"


def captured_lines(major):
    """Returns the raw header lines of one profile's first capture.

    Args:
        major: The Chrome major version.

    Returns:
        A list of ``"Name: value"`` strings.
    """
    path = PROFILES / ("chrome_%d" % major) / "h1" / "captures.json"
    first = json.loads([line for line in path.read_text().splitlines()
                        if line.strip()][0])
    block = first.get("http1")
    return list(block.get("headers") if isinstance(block, dict) else block)


def expected_pairs(major, port):
    """Returns what this profile should send to a server on *port*.

    The capture's ``Host`` and the two platform fields are rewritten the way the
    runtime rewrites them; everything else must match byte for byte.

    Args:
        major: The Chrome major version.
        port: The local server port, for the ``Host`` field.

    Returns:
        A list of (name, value) tuples in emission order.
    """
    token = hints._platform_token()
    platform_hint = hints._sec_ch_ua_platform()
    pairs = []
    for line in captured_lines(major):
        name, _, value = line.partition(":")
        name, value = name.strip(), value.strip()
        if name == "Host":
            value = "127.0.0.1:%d" % port
        elif name == "User-Agent":
            head, _, rest = value.partition("(")
            _group, _, tail = rest.partition(")")
            value = "%s(%s)%s" % (head, token, tail)
        elif name == "sec-ch-ua-platform":
            value = platform_hint
        pairs.append((name, value))
    return pairs


class Server(BaseHTTPRequestHandler):
    """Records the request headers it received, one list per request."""

    protocol_version = "HTTP/1.1"
    seen = []
    lock = threading.Lock()

    def log_message(self, *_args):
        pass

    def do_GET(self):
        with self.lock:
            self.seen.append(list(self.headers.items()))
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()
        cls.port = cls.server.server_address[1]
        cls.url = "http://127.0.0.1:%d/x" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def headers_of(self, **session_kwargs):
        """Sends one request and returns the headers the server saw."""
        Server.seen = []
        with chrome_client.Session(**session_kwargs) as session:
            session.get(self.url)
        return Server.seen[0]


class TableTests(unittest.TestCase):
    """The generated table is the captures, not a retyped copy."""

    def test_every_pinned_profile_has_a_table_entry(self):
        majors = sorted(int(d.name.split("_")[1]) for d in PROFILES.glob("chrome_*"))
        self.assertEqual(sorted(CAPTURED_HEADERS), majors)

    def test_the_table_matches_the_first_capture_of_each_profile(self):
        for major, pairs in sorted(CAPTURED_HEADERS.items()):
            wanted = []
            for line in captured_lines(major):
                name, _, value = line.partition(":")
                name, value = name.strip(), value.strip()
                if name in ("Host", "Connection"):
                    continue
                wanted.append((name, value))
            self.assertEqual(list(pairs), wanted,
                             "chrome_%d: table drifted from the capture" % major)

    def test_a_profile_without_evidence_gets_no_headers(self):
        self.assertEqual(hints.browser_headers(None), [])
        self.assertEqual(hints.browser_headers("chrome_1"), [])
        self.assertEqual(hints.browser_headers("not_a_profile"), [])

    def test_the_platform_token_comes_from_the_core(self):
        """The token is read out of the Core, not derived in Python.

        This used to assert a prefix and the absence of parentheses, which is a
        shape check: it passed for any string that looked plausible and could not
        see the Core disagree. What matters is that the value the facade sends is
        the value the artifact reports, because the Core decides it from
        BUILDFLAG(IS_WIN/IS_MAC) plus ARCH_CPU_* while Python can only see the
        host -- on one x86_64 machine serving the 32-bit Core those differ.
        """
        from chrome_client._python_impl._native import native

        for platform in ("auto", "windows", "macos", "linux"):
            group, hint = hints._platform_identity(platform)
            self.assertEqual(native.PyEngine.platform_identity(platform),
                             (group, hint), platform)
            self.assertEqual(hints._platform_token(platform), group)
            self.assertEqual(hints._sec_ch_ua_platform(platform), hint)
            self.assertNotIn("(", group)
            self.assertTrue(hint.startswith(chr(34)) and hint.endswith(chr(34)), hint)

    def test_the_derivation_matches_the_cores_string_table(self):
        """The three platform strings obey the same rules the Core documents."""
        token = hints._platform_token()
        self.assertTrue(token.startswith(("Windows NT", "Macintosh", "X11")), token)
        self.assertNotIn(")", token)


class SyncWireTests(Base):
    """The synchronous path sends the captured set, in capture order."""

    def test_chrome_155_sends_the_captured_header_set(self):
        self.assertEqual(self.headers_of(impersonate="chrome_155"),
                         expected_pairs(155, self.port))

    def test_a_profile_that_never_sent_client_hints_sends_none(self):
        """chrome_86..89 predate client hints; inventing one would be a lie."""
        names = [name for name, _value in self.headers_of(impersonate="chrome_86")]
        self.assertNotIn("sec-ch-ua", names)
        self.assertNotIn("sec-ch-ua-platform", names)
        self.assertIn("Sec-Fetch-Site", names)

    def test_every_pinned_profile_reproduces_its_capture(self):
        """Every profile the ENGINE serves, byte for byte, in capture order.

        The table can be ahead of the Core: profiles/chrome_<major>/ and this
        table gain a release as soon as its captures are imported, while the
        Core only serves it after a rebuild raises its kLastProfileMajor.
        Those are two different facts and the test has to state which one it
        covers. A profile in the table but outside the engine range is
        asserted to be REFUSED, so the gap cannot silently widen: if the
        engine starts accepting it, this test fails and the loop above this
        line is what has to change.
        """
        served = {major for major in CAPTURED_HEADERS
                  if OLDEST_CHROME <= major <= LATEST_CHROME}
        pending = sorted(set(CAPTURED_HEADERS) - served)
        self.assertTrue(served, "no profile in the table is within the engine range")
        for major in sorted(served):
            self.assertEqual(self.headers_of(impersonate="chrome_%d" % major),
                             expected_pairs(major, self.port),
                             "chrome_%d did not reproduce its capture" % major)
        for major in pending:
            with self.assertRaises(chrome_client.ImpersonateError):
                chrome_client.Session(impersonate="chrome_%d" % major)
            self.assertGreater(major, LATEST_CHROME,
                               "chrome_%d is below the engine floor yet was "
                               "treated as pending" % major)

    def test_no_profile_means_no_browser_headers(self):
        names = [name for name, _value in self.headers_of()]
        self.assertEqual(names, ["Host", "Connection", "User-Agent", "Accept-Encoding"])

    def test_default_headers_false_opts_out_even_with_a_profile(self):
        names = [name for name, _value in
                 self.headers_of(impersonate="chrome_155", default_headers=False)]
        self.assertEqual(names, ["Host", "Connection", "User-Agent", "Accept-Encoding"])


class OverrideTests(Base):
    """A caller overrides one field without losing the other eleven."""

    def test_a_subresource_fetch_can_replace_the_sec_fetch_triplet(self):
        Server.seen = []
        with chrome_client.Session(impersonate="chrome_155") as session:
            session.get(self.url, headers={"Sec-Fetch-Site": "same-origin",
                                           "Sec-Fetch-Mode": "no-cors",
                                           "Sec-Fetch-Dest": "image"})
        seen = dict(Server.seen[0])
        self.assertEqual(seen["Sec-Fetch-Site"], "same-origin")
        self.assertEqual(seen["Sec-Fetch-Mode"], "no-cors")
        self.assertEqual(seen["Sec-Fetch-Dest"], "image")
        # The fields the caller did not touch keep their captured values.
        self.assertEqual(seen["Accept"], dict(expected_pairs(155, self.port))["Accept"])
        self.assertIn("sec-ch-ua", seen)

    def test_a_none_value_removes_an_injected_header(self):
        Server.seen = []
        with chrome_client.Session(impersonate="chrome_155") as session:
            session.get(self.url, headers={"Sec-Fetch-User": None})
        names = [name for name, _value in Server.seen[0]]
        self.assertNotIn("Sec-Fetch-User", names)
        self.assertIn("Sec-Fetch-Site", names)

    def test_a_none_value_removes_a_session_header(self):
        """Measured before the fix: this sent the literal "X-Gone: None"."""
        Server.seen = []
        with chrome_client.Session(headers={"X-Keep": "1", "X-Gone": "yes"}) as session:
            session.get(self.url, headers={"X-Gone": None})
        seen = dict(Server.seen[0])
        self.assertNotIn("X-Gone", seen)
        self.assertEqual(seen["X-Keep"], "1")

    def test_a_user_agent_override_is_not_duplicated(self):
        Server.seen = []
        with chrome_client.Session(impersonate="chrome_155") as session:
            session.get(self.url, headers={"User-Agent": "Mine/1.0"})
        agents = [value for name, value in Server.seen[0]
                  if name.lower() == "user-agent"]
        self.assertEqual(agents, ["Mine/1.0"])

    def test_accept_language_follows_the_session_setting(self):
        seen = dict(self.headers_of(impersonate="chrome_155",
                                   accept_language="de-DE,de;q=0.9"))
        self.assertEqual(seen["Accept-Language"], "de-DE,de;q=0.9")


class AsyncWireTests(Base):
    """The asynchronous path shares one implementation and must agree."""

    def headers_async(self, **session_kwargs):
        """Sends one request through AsyncSession; returns what was seen."""
        Server.seen = []

        async def run():
            async with chrome_client.AsyncSession(**session_kwargs) as session:
                await session.get(self.url)

        asyncio.run(run())
        return Server.seen[0]

    def test_the_async_path_sends_the_same_set(self):
        self.assertEqual(self.headers_async(impersonate="chrome_155"),
                         expected_pairs(155, self.port))

    def test_the_async_path_injects_nothing_without_a_profile(self):
        names = [name for name, _value in self.headers_async()]
        self.assertEqual(names, ["Host", "Connection", "User-Agent", "Accept-Encoding"])

    def test_the_async_path_honours_default_headers_false(self):
        names = [name for name, _value in
                 self.headers_async(impersonate="chrome_155", default_headers=False)]
        self.assertEqual(names, ["Host", "Connection", "User-Agent", "Accept-Encoding"])


if __name__ == "__main__":
    unittest.main()
