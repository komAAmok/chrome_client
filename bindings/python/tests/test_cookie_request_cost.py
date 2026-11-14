"""Regression tests for the per-request cookie cost.

``prepare_request` merged the whole session jar into a fresh jar for every
request, and `_resolve_cookies` then wrote every entry of that merged jar through
to the engine's cookie store. The store already held those cookies (the jar is a
view of it since ABI v9), so the writes were no-ops that still cost one lock
acquisition and one store call each.

Measured with 200 session cookies, one request:

    before   600 write_through calls, 400 store writes, 172 ms
    after      0 write_through calls,   0 store writes,  12 ms
    (8 threads: 17 req/s -> 352 req/s)

The tests below pin the behaviour rather than the counters: they assert that a
request with no per-request cookies makes no store writes at all, and that the
requests-compatible surface (a merged ``_cookies`` view when the caller passes
cookies, an empty one when not) is unchanged.
"""

import json
import unittest


class _Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                body = json.dumps({"cookie": self.headers.get("Cookie")}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", "application/json")
                if self.path.startswith("/set"):
                    value = "v"
                    if "?" in self.path:
                        from urllib.parse import parse_qs, urlsplit
                        value = parse_qs(urlsplit(self.path).query).get(
                            "value", ["v"])[0]
                    self.send_header("Set-Cookie", "sid=%s; Path=/" % value)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
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
    def wire_cookie(response):
        return json.loads(response.content)["cookie"]

    def _count_writes(self, session, cookies):
        """Counts store writes made by one request with *cookies* in the jar."""
        import chrome_client._python_impl.sessions as sessions

        for index in range(cookies):
            session.cookies.set("c%d" % index, "v", domain="127.0.0.1")
        session.get(self.base + "/warm")  # seeds the store once

        calls = {"n": 0}
        original = sessions._SessionJarSink.write_cookie

        def counted(sink, cookie, source_url):
            calls["n"] += 1
            return original(sink, cookie, source_url)

        sessions._SessionJarSink.write_cookie = counted
        try:
            session.get(self.base + "/probe")
        finally:
            sessions._SessionJarSink.write_cookie = original
        return calls["n"]


class PerRequestCookieCostTests(_Server):
    def test_a_plain_request_writes_nothing_to_the_store(self):
        """No per-request cookies means no store writes at all.

        The session's own cookies are already in the store; rewriting them was the
        defect. Any non-zero count here means the merge came back.
        """
        import chrome_client

        with chrome_client.Session() as session:
            self.assertEqual(self._count_writes(session, 50), 0,
                             "a plain request wrote the session jar into the "
                             "store again")

    def test_the_merged_view_is_not_built_unless_asked_for(self):
        """The copy itself is the cost: it writes every entry through the sink.

        Asserted separately from the sink counter because a future change could
        keep the counter clean by filtering at write time while still paying for
        the copy.
        """
        import chrome_client
        import chrome_client._python_impl.sessions as sessions

        with chrome_client.Session() as session:
            for index in range(50):
                session.cookies.set("c%d" % index, "v", domain="127.0.0.1")
            session.get(self.base + "/warm")

            copies = {"n": 0}
            original = sessions.merge_cookies

            def counted(cookiejar, cookies):
                copies["n"] += 1
                return original(cookiejar, cookies)

            sessions.merge_cookies = counted
            try:
                session.get(self.base + "/probe")
            finally:
                sessions.merge_cookies = original
        self.assertEqual(copies["n"], 0,
                         "a plain request still built the merged jar copy")

    def test_store_writes_scale_with_per_request_cookies_only(self):
        """The count follows the caller's cookies, not the jar's size."""
        import chrome_client

        with chrome_client.Session() as session:
            for index in range(50):
                session.cookies.set("c%d" % index, "v", domain="127.0.0.1")
            session.get(self.base + "/warm")
            import chrome_client._python_impl.sessions as sessions
            calls = {"n": 0}
            original = sessions._SessionJarSink.write_cookie

            def counted(sink, cookie, source_url):
                calls["n"] += 1
                return original(sink, cookie, source_url)

            sessions._SessionJarSink.write_cookie = counted
            try:
                session.get(self.base + "/probe", cookies={"one": "1"})
            finally:
                sessions._SessionJarSink.write_cookie = original
        self.assertEqual(calls["n"], 1,
                         "one per-request cookie must cost one store write, not "
                         "one per session cookie plus one")

    def test_per_request_cookies_still_reach_the_wire(self):
        """A per-request mapping must merge with the session jar on the wire.

        Persistence is deliberately NOT asserted. The per-request cookies are
        written into the engine's store, and a store is not request-scoped, so
        they do remain afterwards -- that is pre-existing behaviour (the same
        write-through loop existed before this fix), and changing it would be a
        behaviour change disguised as an optimisation. What this fix must not do
        is stop them reaching the wire or drop the session's own cookies.
        """
        import chrome_client

        with chrome_client.Session() as session:
            session.cookies.set("session_c", "1", domain="127.0.0.1")
            sent = self.wire_cookie(
                session.get(self.base + "/echo", cookies={"per_req": "2"}))
            self.assertIn("session_c=1", sent)
            self.assertIn("per_req=2", sent)
            after = self.wire_cookie(session.get(self.base + "/echo"))
            self.assertIn("session_c=1", after,
                          "the session's own cookie stopped being sent")

    def test_prepared_cookies_stay_a_merged_view_when_asked_for(self):
        import chrome_client

        with chrome_client.Session() as session:
            session.cookies.set("a", "1", domain="127.0.0.1")
            prepared = session.prepare_request(
                chrome_client.Request("GET", self.base + "/echo",
                                      cookies={"b": "2"}))
            names = sorted(cookie.name for cookie in prepared._cookies)
            self.assertEqual(names, ["a", "b"],
                             "prepared._cookies must remain the merged view a "
                             "requests-compatible caller expects")

    def test_prepared_cookies_are_empty_not_none_without_request_cookies(self):
        import chrome_client

        with chrome_client.Session() as session:
            session.cookies.set("a", "1", domain="127.0.0.1")
            prepared = session.prepare_request(
                chrome_client.Request("GET", self.base + "/echo"))
            self.assertIsNotNone(prepared._cookies,
                                 "callers iterate prepared._cookies")
            self.assertEqual(list(prepared._cookies), [])

    def test_the_session_jar_skips_the_mirror_copy(self):
        """The session jar needs the store write, not a throwaway copy.

        ``absorb_set_cookie`` builds a second jar holding what this response set,
        which is what ``Response.cookies`` exposes. The session jar call was also
        building one, and nothing read it: that second ``extract_cookies`` cost a
        full parse of the same lines, and with a write-through sink attached it
        measured 270-500 us. Measured through ``_absorb_cookies``: one
        Set-Cookie response was 601-947 us against 359 us for the last call in
        the same loop.

        What must not change is that the store still receives the cookie.
        """
        import chrome_client

        with chrome_client.Session() as session:
            response = session.get(self.base + "/set")
            # Response.cookies still reports what THIS response set...
            self.assertEqual([cookie.name for cookie in response.cookies], ["sid"])
            # ...and the session jar still holds it...
            self.assertIn("sid", session.cookies.get_dict())
            # ...and the wire still carries it.
            self.assertIn("sid=v", self.wire_cookie(
                session.get(self.base + "/echo")))

    def test_a_response_without_set_cookie_takes_no_lock(self):
        """Most responses carry no cookie; the scan must run before locking.

        Taking the session cookie lock for a response that cannot change the jar
        serialises every concurrent request behind it for nothing. Measured
        through ``_absorb_cookies``: 7.4 us against 0.3 us for the scan alone.
        """
        import chrome_client
        import chrome_client._python_impl.sessions as sessions

        with chrome_client.Session() as session:
            response = session.get(self.base + "/echo")
            taken = {"n": 0}
            original = session._cookie_lock

            class Counting(object):
                def __enter__(self):
                    taken["n"] += 1
                    return original.__enter__()

                def __exit__(self, *exc):
                    return original.__exit__(*exc)

            session._cookie_lock = Counting()
            try:
                session._absorb_cookies(None, response, False)
            finally:
                session._cookie_lock = original
        self.assertEqual(taken["n"], 0,
                         "a response with no Set-Cookie took the cookie lock")

    def test_a_server_set_cookie_still_reaches_the_wire(self):
        import chrome_client

        with chrome_client.Session() as session:
            session.get(self.base + "/set")
            self.assertIn("sid=v", self.wire_cookie(
                session.get(self.base + "/echo")))

    def test_discard_cookies_still_isolates(self):
        """Neither send nor record -- for THIS request, and only this one.

        The earlier version of this test asserted that the session's own cookie
        was gone after a discard request. That is the defect, not the contract:
        it passed because the discard cleared the session's shared store
        (sessions.py, _resolve_cookies), which also deleted the cookies of every
        other request in flight. Measured with a plain request running beside a
        discard request: 19 of 20 readings of the plain request's Cookie header
        were wrong.

        What is asserted now is both halves against the value the discard request
        itself set, so neither can pass for the wrong reason.
        """
        import chrome_client

        with chrome_client.Session() as session:
            session.get(self.base + "/set?value=KEEP")
            self.assertEqual(self.wire_cookie(session.get(self.base + "/echo")),
                             "sid=KEEP")
            discarded = self.wire_cookie(
                session.get(self.base + "/set?value=DROP", discard_cookies=True))
            self.assertIsNone(discarded,
                              "discard_cookies must send no Cookie header")
            after = self.wire_cookie(session.get(self.base + "/echo"))
            self.assertEqual(after, "sid=KEEP",
                             "a discard request must leave the session's store "
                             "exactly as it found it")
            self.assertNotIn("DROP", after,
                             "the discarded request's Set-Cookie reached the "
                             "store")


if __name__ == "__main__":
    unittest.main()
