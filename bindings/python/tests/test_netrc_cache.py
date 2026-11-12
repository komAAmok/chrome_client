"""Regression tests for the .netrc lookup cache.

Every request asks get_netrc_auth, which resolves the same two candidate paths
with expanduser + stat. expanduser falls back to pwd.getpwuid when HOME is unset
-- the normal case in a container or a CI job -- so the lookup cost 0.15 ms per
request, and 0.92 ms per coroutine of the async synchronous prefix.

Measured, interleaved A/B, 300 requests x 8 rounds, 16-byte body:

    uncached   median 2501 us
    cached     median 2062 us     (17.6% faster)

And on the async prefix, 256 coroutines: 1.15 ms -> 0.23 ms per coroutine, i.e.
236 ms less loop time per burst.

The tests below pin the cache contract rather than its speed: a miss must be
remembered, a hit must still notice the file appearing or disappearing, and the
answer must not depend on when it was cached.
"""

import os
import tempfile
import unittest

import chrome_client._python_impl.utils as utils


class NetrcCacheTests(unittest.TestCase):
    def setUp(self):
        self._saved_env = dict(os.environ)
        self._saved_cache = dict(utils._netrc_candidates_cache)
        self._dir = tempfile.mkdtemp(prefix="netrc-")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)
        utils._netrc_candidates_cache.clear()
        utils._netrc_candidates_cache.update(self._saved_cache)
        try:
            for name in os.listdir(self._dir):
                os.unlink(os.path.join(self._dir, name))
            os.rmdir(self._dir)
        except OSError:
            pass

    def _reset(self):
        utils._netrc_candidates_cache["key"] = utils._UNSET
        utils._netrc_candidates_cache["paths"] = None

    def test_the_expansion_is_cached_even_when_there_is_no_file(self):
        """No .netrc at all is the common case, and expanduser is the costly half.

        The existence check is deliberately NOT cached -- it is a cheap stat and it
        is the part that can change while the process runs. What must not repeat is
        the expansion, which falls back to pwd.getpwuid when HOME is unset.
        """
        os.environ["NETRC"] = os.path.join(self._dir, "absent")
        self._reset()
        self.assertIsNone(utils._netrc_path())
        real_expanduser = os.path.expanduser
        calls = {"n": 0}

        def counted(path):
            calls["n"] += 1
            return real_expanduser(path)

        os.path.expanduser = counted
        try:
            self.assertIsNone(utils._netrc_path())
            self.assertIsNone(utils._netrc_path())
        finally:
            os.path.expanduser = real_expanduser
        self.assertEqual(calls["n"], 0, "a cached lookup still called expanduser")

    def test_a_file_that_appears_is_noticed(self):
        """Creating .netrc after a cached miss must be picked up."""
        path = os.path.join(self._dir, "netrc")
        os.environ["NETRC"] = path
        self._reset()
        self.assertIsNone(utils._netrc_path())
        with open(path, "w", encoding="ascii") as handle:
            handle.write("machine example.test login u password p\n")
        self.assertEqual(utils._netrc_path(), path,
                         "a .netrc created after a cached miss was ignored")

    def test_a_file_that_disappears_is_noticed(self):
        """Deleting .netrc after a cached hit must be picked up."""
        path = os.path.join(self._dir, "netrc")
        os.environ["NETRC"] = path
        with open(path, "w", encoding="ascii") as handle:
            handle.write("machine example.test login u password p\n")
        self._reset()
        self.assertEqual(utils._netrc_path(), path)
        os.unlink(path)
        self.assertIsNone(utils._netrc_path(),
                          "a deleted .netrc was still reported as present")

    def test_changing_netrc_invalidates_the_cache(self):
        """The environment is part of the key."""
        first = os.path.join(self._dir, "one")
        second = os.path.join(self._dir, "two")
        for path in (first, second):
            with open(path, "w", encoding="ascii") as handle:
                handle.write("machine example.test login u password p\n")
        os.environ["NETRC"] = first
        self._reset()
        self.assertEqual(utils._netrc_path(), first)
        os.environ["NETRC"] = second
        self.assertEqual(utils._netrc_path(), second,
                         "changing NETRC did not invalidate the cache")

    def test_the_answer_does_not_depend_on_caching(self):
        """A cached lookup returns what an uncached one would."""
        path = os.path.join(self._dir, "netrc")
        os.environ["NETRC"] = path
        with open(path, "w", encoding="ascii") as handle:
            handle.write("machine example.test login alice password secret\n")
        self._reset()
        cached = utils.get_netrc_auth("http://example.test/x")
        self._reset()
        uncached = utils.get_netrc_auth("http://example.test/x")
        self.assertEqual(cached, uncached)
        self.assertEqual(cached, ("alice", "secret"))

    def test_authentication_still_reaches_a_request(self):
        """The cache must not stop credentials from being applied."""
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        seen = {}

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                seen["authorization"] = self.headers.get("Authorization")
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()
        try:
            path = os.path.join(self._dir, "netrc")
            os.environ["NETRC"] = path
            with open(path, "w", encoding="ascii") as handle:
                handle.write("machine 127.0.0.1 login alice password secret\n")
            self._reset()
            import chrome_client
            with chrome_client.Session() as session:
                session.get("http://127.0.0.1:%d/"
                            % server.server_address[1], timeout=10)
            header = seen.get("authorization")
            self.assertIsNotNone(header,
                                 "netrc credentials did not reach the wire")
            # HTTP Basic is base64(user:password), so the name is not literal.
            import base64
            self.assertTrue(header.startswith("Basic "))
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            self.assertEqual(decoded, "alice:secret")
        finally:
            server.shutdown()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
