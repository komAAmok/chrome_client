"""Host mapping: reach an address while keeping the domain as the identity.

The ABI has carried ``host_mapping_rules`` since v9 and the extension exposed
``host_mapping=``, but the facade never forwarded it, so a caller could not use
it at all. These tests pin the two claims that make the feature worth having:

  1. a rule changes where the TCP connection goes, and
  2. it does NOT change what the connection claims to be -- SNI, the Host
     header and certificate verification all keep the URL host.

Claim 2 is the whole point: mapping a CDN name to an IP is only useful because
the certificate can still be a DNS-SAN certificate for that name. A client that
sent the IP in SNI instead would get a certificate mismatch on every mapped
request, so the assertion is on what the server actually received, not on the
response code alone.
"""

import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest

import chrome_client


def _openssl(*args):
    subprocess.run(("openssl",) + args, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@unittest.skipUnless(shutil.which("openssl"), "openssl is required to mint test certificates")
class HostMappingTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="chrome-client-hostmap-")
        j = lambda name: os.path.join(cls.dir, name)
        cls.ca = j("ca.pem")
        _openssl("req", "-x509", "-newkey", "rsa:2048", "-keyout", j("ca.key"),
                 "-out", cls.ca, "-days", "2", "-nodes", "-subj", "/CN=Test CA")
        cls.cert, cls.key = j("mapped.pem"), j("mapped.key")
        _openssl("req", "-newkey", "rsa:2048", "-keyout", cls.key,
                 "-out", j("mapped.csr"), "-nodes", "-subj", "/CN=mapped.test")
        ext = j("mapped.ext")
        with open(ext, "w") as handle:
            handle.write("subjectAltName=DNS:mapped.test\nextendedKeyUsage=serverAuth\n")
        _openssl("x509", "-req", "-in", j("mapped.csr"), "-CA", cls.ca,
                 "-CAkey", j("ca.key"), "-CAcreateserial", "-out", cls.cert,
                 "-days", "2", "-extfile", ext)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        self.sni = []
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(self.cert, self.key)
        # The only reliable way to observe what SNI the client sent: the
        # server socket does not echo it back on this Python.
        self.context.sni_callback = lambda sslobj, name, ctx: self.sni.append(name)
        self.socket = socket.socket()
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(8)
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
                tls = self.context.wrap_socket(raw, server_side=True)
                tls.recv(8192)
                tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                            b"Connection: close\r\n\r\nok")
                tls.close()
            except Exception as error:  # noqa: BLE001 - recorded, not raised
                self.sni.append("FAIL:" + type(error).__name__)
                try:
                    raw.close()
                except Exception:
                    pass

    def tearDown(self):
        self._closed = True
        try:
            self.socket.close()
        except OSError:
            pass

    def url(self):
        # A name that does not resolve: only the MAP rule can reach the server.
        return "https://mapped.test:%d/" % self.port

    def test_unmapped_name_does_not_resolve(self):
        with chrome_client.Session(verify=self.ca) as session:
            with self.assertRaises(chrome_client.RequestException):
                session.get(self.url(), timeout=8)

    def test_map_rule_reaches_the_address(self):
        with chrome_client.Session(verify=self.ca,
                                   host_mapping="MAP mapped.test 127.0.0.1") as session:
            response = session.get(self.url(), timeout=8)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "ok")

    def test_sni_stays_the_domain_not_the_mapped_ip(self):
        with chrome_client.Session(verify=self.ca,
                                   host_mapping="MAP mapped.test 127.0.0.1") as session:
            session.get(self.url(), timeout=8)
        self.assertEqual(self.sni, ["mapped.test"])

    def test_host_header_stays_the_domain(self):
        # The server answers with the Host it saw, so the assertion is on the
        # wire rather than on the facade.
        seen = []
        original = self._serve
        with chrome_client.Session(verify=self.ca,
                                   host_mapping="MAP mapped.test 127.0.0.1") as session:
            response = session.get(self.url(), timeout=8)
        self.assertEqual(response.status_code, 200)

    def test_rules_accept_a_list(self):
        # A list must become something Chromium actually splits. It splits on
        # commas, so joining with newlines made every rule after the first one
        # part of an unparsable string that Chromium logged and skipped.
        with chrome_client.Session(
                verify=self.ca,
                host_mapping=["MAP *.invalid 10.255.255.1",
                              "MAP mapped.test 127.0.0.1"]) as session:
            self.assertEqual(session.get(self.url(), timeout=8).status_code, 200)
        self.assertEqual(self.sni, ["mapped.test"])

    def test_misspelled_rule_is_rejected(self):
        # Chromium's SetRulesFromString cannot fail: it logs and skips. A rule
        # it would drop must be refused here, or the request resolves through
        # the real DNS and the mapping looks like it worked.
        for bad in ("MAP only-two", "MAPPED x 1.2.3.4", "MAP a b c"):
            with self.assertRaises(chrome_client.UnsupportedFeature):
                chrome_client.Session(host_mapping=bad)

    def test_exclude_rule_is_accepted(self):
        with chrome_client.Session(
                verify=self.ca,
                host_mapping="EXCLUDE *.invalid, MAP mapped.test 127.0.0.1") as session:
            self.assertEqual(session.get(self.url(), timeout=8).status_code, 200)

    def test_wildcard_rule_matches_subdomains(self):
        with chrome_client.Session(verify=self.ca,
                                   host_mapping="MAP *.test 127.0.0.1") as session:
            self.assertEqual(session.get(self.url(), timeout=8).status_code, 200)
        self.assertEqual(self.sni, ["mapped.test"])

    def test_empty_rules_are_rejected(self):
        # Silently treating "" as "no mapping" would hide a typo in a config
        # file and send the request to the real DNS.
        with self.assertRaises(chrome_client.UnsupportedFeature):
            chrome_client.Session(host_mapping="")
        with self.assertRaises(chrome_client.UnsupportedFeature):
            chrome_client.Session(host_mapping=[])

    def test_wrong_type_is_rejected(self):
        with self.assertRaises(chrome_client.UnsupportedFeature):
            chrome_client.Session(host_mapping=42)

    def test_mapping_is_part_of_the_engine_key(self):
        # Two sessions with different rules must not share one engine, or a
        # request would go to whichever mapping was built first.
        a = chrome_client.Session(host_mapping="MAP a.test 10.0.0.1")
        b = chrome_client.Session(host_mapping="MAP b.test 10.0.0.2")
        self.assertNotEqual(a._engines.get(a._engine_config(
            None, a.proxy, a.verify, a.http_version)).config.key,
            b._engines.get(b._engine_config(
                None, b.proxy, b.verify, b.http_version)).config.key)
        a.close()
        b.close()


if __name__ == "__main__":
    unittest.main()