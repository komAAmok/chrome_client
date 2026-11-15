"""Client certificates (mTLS) through the Python facade.

The Core has carried ``mn_engine_set_client_certificate`` since ABI v9 but no
binding exposed it, so connecting to an mTLS endpoint failed with
``ERR_SSL_CLIENT_AUTH_CERT_NEEDED`` and no way to fix it from Python. These
tests cover the four claims the facade now makes:

  1. ``cert=`` as a (cert, key) pair and as one combined PEM both authenticate;
  2. no certificate still fails, and fails loudly;
  3. replacing a certificate at runtime takes effect on the next request --
     this is the one that catches a missing connection refresh, because a
     pooled connection opened under the old certificate would be reused and the
     server would keep seeing the previous CN;
  4. a certificate the Core cannot use is rejected at the call site instead of
     being accepted and failing on the first request.

Each server is a real TLS listener with ``CERT_REQUIRED``, and the assertion is
the common name the server actually saw, not merely that a response arrived.
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
class _Certificates(object):
    """One CA, one server certificate and two client certificates."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="chrome-client-mtls-")
        j = lambda name: os.path.join(self.dir, name)
        self.ca = j("ca.pem")
        _openssl("req", "-x509", "-newkey", "rsa:2048", "-keyout", j("ca.key"),
                 "-out", self.ca, "-days", "2", "-nodes", "-subj", "/CN=Test CA")
        self.server_cert, self.server_key = j("srv.pem"), j("srv.key")
        _openssl("req", "-newkey", "rsa:2048", "-keyout", self.server_key,
                 "-out", j("srv.csr"), "-nodes", "-subj", "/CN=localhost")
        ext = j("srv.ext")
        with open(ext, "w") as handle:
            handle.write("subjectAltName=DNS:localhost\nextendedKeyUsage=serverAuth\n")
        _openssl("x509", "-req", "-in", j("srv.csr"), "-CA", self.ca,
                 "-CAkey", j("ca.key"), "-CAcreateserial", "-out", self.server_cert,
                 "-days", "2", "-extfile", ext)
        self.client_cert, self.client_key = self._client(j, "cli", "client.test")
        self.second_cert, self.second_key = self._client(j, "cli2", "second.test")
        self.encrypted_key = j("cli_enc.key")
        _openssl("rsa", "-in", self.client_key, "-aes256", "-passout", "pass:secret",
                 "-out", self.encrypted_key)
        self.combined = j("combined.pem")
        with open(self.combined, "wb") as handle:
            for part in (self.client_cert, self.client_key):
                with open(part, "rb") as source:
                    handle.write(source.read())

    def _client(self, j, stem, cn):
        cert, key = j(stem + ".pem"), j(stem + ".key")
        _openssl("req", "-newkey", "rsa:2048", "-keyout", key,
                 "-out", j(stem + ".csr"), "-nodes", "-subj", "/CN=" + cn)
        ext = j(stem + ".ext")
        with open(ext, "w") as handle:
            handle.write("extendedKeyUsage=clientAuth\n")
        _openssl("x509", "-req", "-in", j(stem + ".csr"), "-CA", self.ca,
                 "-CAkey", j("ca.key"), "-CAcreateserial", "-out", cert,
                 "-days", "2", "-extfile", ext)
        return cert, key

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class _MtlsServer(object):
    """A TLS server that demands a client certificate and records its CN."""

    def __init__(self, certs):
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(certs.server_cert, certs.server_key)
        self.context.verify_mode = ssl.CERT_REQUIRED
        self.context.load_verify_locations(certs.ca)
        self.socket = socket.socket()
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(16)
        self.port = self.socket.getsockname()[1]
        self.seen = []
        self._closed = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self._closed:
            try:
                raw, _ = self.socket.accept()
            except OSError:
                return
            try:
                tls = self.context.wrap_socket(raw, server_side=True)
                subject = dict(x[0] for x in tls.getpeercert()["subject"])
                self.seen.append(subject["commonName"])
                tls.recv(8192)
                tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                            b"Connection: close\r\n\r\nok")
                tls.close()
            except Exception as error:  # noqa: BLE001 - recorded, not raised
                self.seen.append("FAIL:" + type(error).__name__)
                try:
                    raw.close()
                except Exception:
                    pass

    def url(self):
        return "https://localhost:%d/" % self.port

    def close(self):
        self._closed = True
        try:
            self.socket.close()
        except OSError:
            pass
        self.thread.join(timeout=2)


class ClientCertificateTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.certs = _Certificates()

    @classmethod
    def tearDownClass(cls):
        cls.certs.close()

    def setUp(self):
        self.server = _MtlsServer(self.certs)

    def tearDown(self):
        self.server.close()

    def test_cert_pair_authenticates(self):
        with chrome_client.Session(verify=self.certs.ca,
                                   cert=(self.certs.client_cert,
                                         self.certs.client_key)) as session:
            response = session.get(self.server.url(), timeout=10)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.server.seen, ["client.test"])

    def test_combined_pem_authenticates(self):
        with chrome_client.Session(verify=self.certs.ca,
                                   cert=self.certs.combined) as session:
            response = session.get(self.server.url(), timeout=10)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.server.seen, ["client.test"])

    def test_without_certificate_fails_loudly(self):
        with chrome_client.Session(verify=self.certs.ca) as session:
            with self.assertRaises(chrome_client.SSLError):
                session.get(self.server.url(), timeout=10)

    def test_runtime_swap_takes_effect(self):
        # The regression this exists for: without the Core refreshing that
        # origin's connection group, the second request reuses the connection
        # opened under client.test and the server never sees second.test.
        with chrome_client.Session(verify=self.certs.ca,
                                   cert=(self.certs.client_cert,
                                         self.certs.client_key)) as session:
            session.get(self.server.url(), timeout=10)
            session.set_client_certificate(
                "localhost", self.server.port,
                self.certs.second_cert, self.certs.second_key)
            session.get(self.server.url(), timeout=10)
        self.assertEqual(self.server.seen, ["client.test", "second.test"])

    def test_encrypted_key_is_rejected_at_the_call_site(self):
        # Accepted-then-failed would report success here and break at the first
        # request, which is the fail-closed promise inverted.
        with chrome_client.Session(verify=self.certs.ca,
                                   cert=(self.certs.client_cert,
                                         self.certs.client_key)) as session:
            with self.assertRaises(chrome_client.SSLError):
                session.set_client_certificate(
                    "localhost", self.server.port,
                    self.certs.client_cert, self.certs.encrypted_key)

    def test_missing_file_is_rejected(self):
        with self.assertRaises(chrome_client.UnsupportedFeature):
            chrome_client.Session(cert="/nonexistent/cert.pem")

    def test_wrong_shape_is_rejected(self):
        with self.assertRaises(chrome_client.UnsupportedFeature):
            chrome_client.Session(cert=123)


class _RecordingEngine(object):
    """Stands in for the native engine and counts the calls it receives.

    The registration memo is invisible to a functional test: with it disabled
    every assertion about WHICH certificate the server saw still passes, because
    the Core call it skips produces the same state. What changes is how often
    the Core is asked, and each ask closes that origin's idle connections
    (net/socket/ssl_client_socket.cc:274-287). So the observation has to be the
    call count, not the outcome -- measured by disabling the memo on the shipped
    tree: all seven functional tests stayed green.
    """

    def __init__(self):
        self.calls = []

    def set_client_certificate(self, host, port, cert, key):
        self.calls.append(("set", host, port, cert, key))

    def clear_client_certificate(self, host, port):
        self.calls.append(("clear", host, port))


class RegistrationMemoTests(unittest.TestCase):
    """The memo, the collapse of duplicate calls, and the clear handshake."""

    def _slot(self, engine):
        from chrome_client._python_impl.engine import EngineSlot
        slot = EngineSlot.__new__(EngineSlot)
        slot.engine = engine
        slot.config = None
        slot._lock = threading.RLock()
        slot._inflight = 0
        slot._applied_certs = {}
        slot._pending_certs = {}
        slot._cert_cond = threading.Condition(slot._lock)
        return slot

    def test_the_same_bytes_reach_the_core_once(self):
        engine = _RecordingEngine()
        slot = self._slot(engine)
        for _ in range(3):
            slot.set_client_certificate("example.test", 443, b"CERT", b"KEY")
        self.assertEqual(len(engine.calls), 1,
                         "every repeat refreshed the origin's connection group")

    def test_a_rotation_reaches_the_core(self):
        engine = _RecordingEngine()
        slot = self._slot(engine)
        slot.set_client_certificate("example.test", 443, b"CERT", b"KEY")
        slot.set_client_certificate("example.test", 443, b"OTHER", b"KEY")
        self.assertEqual([call[3] for call in engine.calls], [b"CERT", b"OTHER"])

    def test_clearing_makes_the_next_registration_reach_the_core(self):
        engine = _RecordingEngine()
        slot = self._slot(engine)
        slot.set_client_certificate("example.test", 443, b"CERT", b"KEY")
        slot.clear_client_certificate("example.test", 443)
        slot.set_client_certificate("example.test", 443, b"CERT", b"KEY")
        self.assertEqual([call[0] for call in engine.calls], ["set", "clear", "set"],
                         "a cleared certificate was skipped as if it were still live")

    def test_a_second_server_is_not_covered_by_the_first(self):
        engine = _RecordingEngine()
        slot = self._slot(engine)
        slot.set_client_certificate("a.test", 443, b"CERT", b"KEY")
        slot.set_client_certificate("b.test", 443, b"CERT", b"KEY")
        self.assertEqual(len(engine.calls), 2)

    def test_overlapping_identical_calls_collapse(self):
        engine = _RecordingEngine()
        slot = self._slot(engine)
        threads = [threading.Thread(
            target=lambda: slot.set_client_certificate("example.test", 443,
                                                       b"CERT", b"KEY"))
                   for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(engine.calls), 1,
                         "8 concurrent identical registrations reached the Core "
                         "%d times" % len(engine.calls))

    def test_the_record_follows_completion_order(self):
        """Two rotations that finish out of order must leave the Core's value.

        A sequence number taken before the Core call names whichever thread
        entered last; Chromium keeps whichever call LANDED last. When those
        differ the record describes a certificate the Core does not hold, and
        the next identical registration is then skipped while the stale one
        stays live.
        """
        engine = _RecordingEngine()
        original = engine.set_client_certificate

        def set_with_reordered_completion(host, port, cert, key):
            if cert == b"SLOW":
                threading.Event().wait(0.05)
                original(host, port, cert, key)
                return
            original(host, port, cert, key)

        engine.set_client_certificate = set_with_reordered_completion
        slot = self._slot(engine)
        slow = threading.Thread(target=lambda: slot.set_client_certificate(
            "example.test", 443, b"SLOW", b"KEY"))
        slow.start()
        threading.Event().wait(0.01)
        slot.set_client_certificate("example.test", 443, b"FAST", b"KEY")
        slow.join()
        held = [call[3] for call in engine.calls][-1]
        recorded = slot._applied_certs[("example.test", 443)][0]
        self.assertEqual(recorded, held,
                         "the record names %r while the Core holds %r"
                         % (recorded, held))

    def test_clearing_waits_for_an_in_flight_registration(self):
        engine = _RecordingEngine()
        original = engine.set_client_certificate

        def slow_set(host, port, cert, key):
            threading.Event().wait(0.05)
            original(host, port, cert, key)

        engine.set_client_certificate = slow_set
        slot = self._slot(engine)
        thread = threading.Thread(target=lambda: slot.set_client_certificate(
            "example.test", 443, b"CERT", b"KEY"))
        thread.start()
        threading.Event().wait(0.01)
        slot.clear_client_certificate("example.test", 443)
        thread.join()
        self.assertIsNone(slot._applied_certs.get(("example.test", 443)))
        self.assertEqual(engine.calls[-1][0], "clear",
                         "the clear did not perform the last Core call")

    def test_an_interrupted_registration_does_not_wedge_the_engine(self):
        """One KeyboardInterrupt used to park the claim for that server forever.

        The claim was released only by the ``except RuntimeError`` handler, so a
        BaseException raised while the caller waited on the Core left
        ``_pending_certs`` populated. Every later registration and every
        ``clear_client_certificate`` for the same (host, port) then waited on a
        condition nothing would notify: measured, both blocked indefinitely, so
        one Ctrl-C made that engine permanently unable to register a
        certificate for that server.
        """
        engine = _RecordingEngine()

        def interrupted(host, port, cert, key):
            raise KeyboardInterrupt("caller interrupted the wait")

        engine.set_client_certificate = interrupted
        slot = self._slot(engine)
        with self.assertRaises(KeyboardInterrupt):
            slot.set_client_certificate("example.test", 443, b"CERT", b"KEY")
        self.assertEqual(slot._pending_certs, {},
                         "the interrupted call left its claim behind")

        # The engine has to be usable again: without the release this call waits
        # on a condition nobody will ever notify.
        engine.set_client_certificate = _RecordingEngine().set_client_certificate
        done = []
        worker = threading.Thread(
            target=lambda: (slot.set_client_certificate("example.test", 443,
                                                        b"CERT", b"KEY"),
                            done.append("returned")))
        worker.daemon = True
        worker.start()
        worker.join(5.0)
        self.assertEqual(done, ["returned"],
                         "the next registration never returned: the engine is wedged")

    def test_an_interrupted_registration_does_not_block_a_clear(self):
        """The same claim also blocked ``clear_client_certificate`` forever."""
        engine = _RecordingEngine()
        class _Cancelled(BaseException):
            pass

        engine.set_client_certificate = lambda *args: (_ for _ in ()).throw(
            _Cancelled("cancelled while waiting"))
        slot = self._slot(engine)
        with self.assertRaises(_Cancelled):
            slot.set_client_certificate("example.test", 443, b"CERT", b"KEY")
        done = []
        worker = threading.Thread(
            target=lambda: (slot.clear_client_certificate("example.test", 443),
                            done.append("returned")))
        worker.daemon = True
        worker.start()
        worker.join(5.0)
        self.assertEqual(done, ["returned"],
                         "the clear waited for a claim nobody would release")


if __name__ == "__main__":
    unittest.main()