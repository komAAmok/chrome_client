"""Engine-level options that reached the extension but not the facade.

The PyO3 constructor has accepted dns_nameservers, doh_template, dns_secure_mode,
dns_over_tls, dns_rotate, dns_padding, hsts_bypass, proxy_chain, platform and
max_sockets_per_group since ABI v9/v11, but EngineConfig._build() forwarded only
eleven positional arguments, so Session() could not set any of them. Two of the
tests below cover defects found while wiring them:

* platform= was ignored twice over -- not forwarded to the binding, and then
  overridden by a Python copy of the logic that derived the User-Agent group and
  sec-ch-ua-platform from the host architecture instead of the option. The Core
  already derives both from mn_platform_t; that duplication is what made
  platform="windows" send a Linux User-Agent.
* hsts_bypass entries must be single labels. Chromium matches them by label and
  silently ignores a dotted one, so a broken entry would look accepted.
"""

import socket
import threading
import unittest

import chrome_client


class _Recorder(object):
    """A local HTTP server that keeps the request head it received."""

    def __init__(self):
        self.heads = []
        self.socket = socket.socket()
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(16)
        self.port = self.socket.getsockname()[1]
        self._closed = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while not self._closed:
            try:
                raw, _ = self.socket.accept()
            except OSError:
                return
            try:
                data = raw.recv(8192).decode("latin-1")
                self.heads.append(data)
                raw.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                            b"Connection: close\r\n\r\nok")
                raw.close()
            except Exception:
                try:
                    raw.close()
                except Exception:
                    pass

    def url(self):
        return "http://127.0.0.1:%d/" % self.port

    def last_headers(self):
        head = self.heads[-1]
        found = {}
        for line in head.split("\r\n")[1:]:
            if ":" in line:
                name, _, value = line.partition(":")
                found[name.strip().lower()] = value.strip()
        return found

    def close(self):
        self._closed = True
        try:
            self.socket.close()
        except OSError:
            pass


class PlatformIdentityTest(unittest.TestCase):
    """platform= decides the User-Agent group and the client hint.

    The Core derives both from mn_platform_t, so asking for a Windows identity
    on a Linux build must send a Windows User-Agent. Deriving them from the host
    instead is the defect this class exists to catch.
    """

    def setUp(self):
        self.server = _Recorder()

    def tearDown(self):
        self.server.close()

    def _headers_for(self, platform):
        with chrome_client.Session(impersonate="chrome_155",
                                   platform=platform) as session:
            code = session.get(self.server.url(), timeout=8).status_code
        self.assertEqual(code, 200)
        return self.server.last_headers()

    def test_windows_platform_sends_a_windows_user_agent(self):
        headers = self._headers_for("windows")
        self.assertIn("Windows NT 10.0", headers["user-agent"])
        self.assertNotIn("Linux", headers["user-agent"])
        self.assertEqual(headers["sec-ch-ua-platform"], chr(34) + "Windows" + chr(34))

    def test_macos_platform_sends_a_macintosh_user_agent(self):
        headers = self._headers_for("macos")
        self.assertIn("Macintosh", headers["user-agent"])
        self.assertEqual(headers["sec-ch-ua-platform"], chr(34) + "macOS" + chr(34))

    def test_linux_platform_sends_a_linux_user_agent(self):
        headers = self._headers_for("linux")
        self.assertIn("X11; Linux", headers["user-agent"])
        self.assertEqual(headers["sec-ch-ua-platform"], chr(34) + "Linux" + chr(34))

    def test_unknown_platform_is_rejected(self):
        with self.assertRaises(chrome_client.UnsupportedFeature):
            chrome_client.Session(platform="solaris")

    def test_platform_is_part_of_the_engine_key(self):
        a = chrome_client.Session(platform="windows")
        b = chrome_client.Session(platform="linux")
        self.assertNotEqual(
            a._engine_config(None, a.proxy, a.verify, a.http_version).key,
            b._engine_config(None, b.proxy, b.verify, b.http_version).key)
        a.close()
        b.close()


class ResolverOptionTest(unittest.TestCase):
    """The DNS, HSTS and proxy-chain options build an engine when coherent.

    Resolving through a custom nameserver needs a second host, so these assert
    that the engine accepts the configuration: the failure being prevented is
    "accepted by the facade, rejected by the Core", which is what an unforwarded
    option produces.
    """

    def _build(self, **kwargs):
        session = chrome_client.Session(**kwargs)
        try:
            return session._engines.get(
                session._engine_config(None, session.proxy, session.verify,
                                       session.http_version))
        finally:
            session.close()

    def test_nameservers_accept_a_list(self):
        self.assertIsNotNone(
            self._build(dns_nameservers=["1.1.1.1", "8.8.8.8:53"]))

    def test_doh_needs_a_secure_mode(self):
        self.assertIsNotNone(self._build(
            doh_template="https://dns.google/dns-query", dns_secure_mode=2))

    def test_doh_without_a_mode_is_rejected(self):
        with self.assertRaises(chrome_client.UnsupportedFeature):
            chrome_client.Session(doh_template="https://dns.google/dns-query")

    def test_dns_secure_mode_range(self):
        with self.assertRaises(chrome_client.UnsupportedFeature):
            chrome_client.Session(dns_secure_mode=3)

    def test_hsts_bypass_rejects_a_dotted_entry(self):
        with self.assertRaises(chrome_client.UnsupportedFeature):
            chrome_client.Session(hsts_bypass="example.com")

    def test_hsts_bypass_accepts_a_single_label(self):
        self.assertIsNotNone(self._build(hsts_bypass="example"))

    def test_proxy_chain_accepts_entries(self):
        self.assertIsNotNone(self._build(proxy_chain="http://127.0.0.1:9"))

    def test_max_sockets_ceiling(self):
        with self.assertRaises(chrome_client.UnsupportedFeature):
            chrome_client.Session(max_sockets_per_group=4096)

    def test_max_sockets_accepts_zero_and_a_small_value(self):
        self.assertIsNotNone(self._build(max_sockets_per_group=0))
        self.assertIsNotNone(self._build(max_sockets_per_group=32))


if __name__ == "__main__":
    unittest.main()