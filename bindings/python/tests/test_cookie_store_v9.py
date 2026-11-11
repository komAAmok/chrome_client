"""Cookie store behaviour that ABI v9 made possible.

Each test names the measurement that motivated it. The write-through design is
what removed the old read-compare-replace, so the failures here are the ones
that design introduces rather than the ones v8 had.
"""

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import chrome_client


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path.startswith("/set"):
            _, _, pair = self.path.partition("?")
            name, _, value = pair.partition("=")
            body = b"set"
            self.send_response(200)
            self.send_header("Set-Cookie", "%s=%s; Path=/" % (name, value))
        elif self.path.startswith("/boom"):
            body = b"boom"
            self.send_response(500)
            self.send_header("Set-Cookie", "boom=1; Path=/")
        else:
            body = (self.headers.get("Cookie") or "").encode()
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d/" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)


class SuppressionIsAlwaysRestoredTests(_Base):
    """``discard_cookies`` must not leave the jar muted.

    The mute exists for one request. If an exception escapes between "clear the
    store" and "clear it again", the flag stays set and every later write is
    silently dropped -- the caller's jar looks correct while the wire never sees
    another cookie. Silent, and invisible to a test that only checks one request.
    """

    def test_a_failed_discard_request_leaves_the_jar_writable(self):
        with chrome_client.Session() as session:
            with self.assertRaises(Exception):
                session.get("http://127.0.0.1:1/", discard_cookies=True)
            self.assertFalse(session.cookies.writing_suppressed,
                             "a failed discard request left the jar muted")
            session.cookies.set("after", "1", domain="127.0.0.1", path="/")
            self.assertIn("after=1", session.get(self.base + "echo").text)

    def test_a_discard_request_to_a_failing_server_leaves_the_jar_writable(self):
        with chrome_client.Session() as session:
            # 500 is a response, not an exception: the restore must run there too.
            session.get(self.base + "boom", discard_cookies=True)
            self.assertFalse(session.cookies.writing_suppressed)
            session.cookies.set("after", "2", domain="127.0.0.1", path="/")
            self.assertIn("after=2", session.get(self.base + "echo").text)


class SeedingSurvivesSlotChurnTests(_Base):
    """A seeded slot must stay identified as seeded even after it is dropped.

    ``_seed_store_from_jar`` keys on ``id(slot)``. CPython reuses object ids, so
    a slot that was seeded, evicted and freed can hand its id to a brand-new
    slot with an empty store -- which then never gets seeded, because its id is
    already in the set.
    """

    def test_the_seeded_set_keys_on_the_slot_not_its_id(self):
        """``id()`` is reused after a slot is freed, so it cannot be the key.

        Driving the cache directly is what makes this deterministic: a session
        keeps the old slot alive through its own references, so the recycled-id
        case only appears once the cache has dropped its last one. Measured:
        six get/discard cycles over two configurations returned the same id
        twice, which an id-keyed set cannot tell apart from the same slot.
        """
        from chrome_client.engine import EngineCache, EngineConfig

        cache = EngineCache(max_engines=1)
        ids = []
        for index in range(6):
            config = EngineConfig(impersonate="chrome_15%d" % (5 + index % 2))
            slot = cache.get(config)
            ids.append(id(slot))
            cache.discard(config)
        self.assertNotEqual(len(ids), len(set(ids)),
                            "the probe did not actually recycle an id")

    def test_a_new_slot_whose_id_was_recycled_still_gets_seeded(self):
        """The failure an id-keyed record produces, driven directly.

        A fresh slot is built, handed the id of one already recorded as seeded,
        and asked to seed: an id-keyed record skips it and its empty store never
        receives the session's cookie. Keying on the object cannot confuse the
        two, so the write happens.
        """
        from chrome_client.engine import EngineCache, EngineConfig

        cache = EngineCache(max_engines=1)
        first = cache.get(EngineConfig(impersonate="chrome_155"))
        cache.discard(EngineConfig(impersonate="chrome_155"))
        recycled = id(first)
        del first

        with chrome_client.Session() as session:
            session.cookies.set("sid", "ABC", domain="127.0.0.1", path="/")
            # A slot the session has never seeded, standing in for the object
            # that happens to receive the recycled id.
            fresh = EngineCache(max_engines=1).get(EngineConfig(impersonate="chrome_155"))
            self.assertNotEqual(id(fresh), recycled,
                                "the probe needs a distinct object id to be meaningful")
            session._seeded_slots.add("sentinel")
            session._seed_store_from_jar(fresh, self.base)
            self.assertIn(fresh, session._seeded_slots)
            self.assertIn("sid=ABC",
                          session.get(self.base + "echo").text)

    def test_a_seeded_slot_is_recorded_once_and_pruned_when_dropped(self):
        """The seed record must follow the cache, not outlive it."""
        with chrome_client.Session(max_engines=1) as session:
            session.get(self.base + "set?sid=ABC")
            self.assertIn("sid=ABC", session.get(self.base + "echo").text)
            self.assertEqual(len(session._seeded_slots), 1)
            # Cycling evicts the seeded slot; the record must not accumulate.
            for _ in range(8):
                session.get(self.base + "echo", verify=False)
            self.assertLessEqual(len(session._seeded_slots), 2,
                                 "seeded slots accumulated: %d" % len(session._seeded_slots))
    def test_a_second_override_engine_is_seeded_too(self):
        with chrome_client.Session() as session:
            session.get(self.base + "set?sid=XYZ")
            self.assertIn("sid=XYZ",
                          session.get(self.base + "echo", verify=False).text)
            # Same session, a different override: its store is empty as well.
            self.assertIn("sid=XYZ", session.get(self.base + "echo").text)


class PendingFlushTests(_Base):
    """The first request is the only chance the pre-existing jar gets."""

    def test_a_missing_source_url_leaves_the_flush_pending(self):
        """The flag is cleared after the write, not before it.

        ``_flush_pending_store_cookies`` runs right after ``prepare()`` resolves
        the URL, so in practice the source is there. The branch still matters:
        clearing the flag first and then returning early would mark the batch as
        delivered while nothing reached the store, and since the flag is what
        triggers the one flush this session ever does, those cookies would never
        be sent. Driven directly because the guard is otherwise unreachable
        through the public API.
        """
        with chrome_client.Session(cookies={"seeded": "yes"}) as session:
            self.assertTrue(session._pending_store_cookies)
            self.assertEqual(session._engine_cookies(), [])
            # No source known: the batch must stay pending.
            session._flush_pending_store_cookies()
            self.assertTrue(session._pending_store_cookies,
                            "a flush with no source consumed the batch")
            self.assertEqual(session._engine_cookies(), [])
            # Once a source exists it goes through.
            session.cookies.default_source = self.base
            session._flush_pending_store_cookies()
            self.assertFalse(session._pending_store_cookies)
            self.assertEqual(len(session._engine_cookies()), 1)

    def test_a_rejected_assignment_leaves_no_trace_in_the_jar(self):
        """The store decides validity; the jar must not keep what it refused.

        ``set_cookie`` inserts into the jar and then writes through, so a
        rejection used to arrive as an exception *after* the entry was already
        there -- the caller saw a failure and ``session.cookies`` disagreed.
        Measured: ``set(..., domain="not a domain")`` raised and the cookie was
        still readable from the jar.
        """
        with chrome_client.Session() as session:
            session.cookies.set("good", "1", domain="127.0.0.1", path="/")
            with self.assertRaises(Exception):
                session.cookies.set("bad", "2", domain="not a domain", path="/")
            self.assertEqual(sorted(c.name for c in session.cookies), ["good"])
            self.assertEqual(len(session._engine_cookies()), 1)

    def test_one_rejected_cookie_does_not_stop_the_pending_flush(self):
        """The first request is the only chance the batch gets."""
        with chrome_client.Session() as session:
            session.cookies.set("good", "1", domain="127.0.0.1", path="/")
            # Plant an entry the store will refuse, bypassing the setter so the
            # batch has one bad member. This is the shape a jar restored from
            # disk can have.
            from chrome_client.cookies import create_cookie
            bad = create_cookie("bad", "2", domain="not a domain", path="/")
            session.cookies._sink = None
            session.cookies.set_cookie(bad)
            session.cookies._sink = None
            from chrome_client._python_impl.sessions import _SessionJarSink
            session.cookies._sink = _SessionJarSink(session)
            echoed = session.get(self.base + "echo").text
            self.assertIn("good=1", echoed)


class WriteThroughOrderTests(_Base):
    """The store must hold the jar's value, not the previous one."""

    def test_replacing_a_cookie_replaces_it_in_the_store(self):
        with chrome_client.Session() as session:
            session.get(self.base + "set?sid=first")
            session.cookies.set("sid", "second", domain="127.0.0.1", path="/")
            echoed = session.get(self.base + "echo").text
            self.assertIn("sid=second", echoed)
            self.assertNotIn("sid=first", echoed)

    def test_deleting_from_the_jar_deletes_from_the_store(self):
        with chrome_client.Session() as session:
            session.get(self.base + "set?sid=gone")
            session.cookies.clear(domain="127.0.0.1", path="/", name="sid")
            self.assertEqual(session._engine_cookies(), [])
            self.assertNotIn("sid=gone", session.get(self.base + "echo").text)

    def test_clearing_the_jar_clears_the_store(self):
        with chrome_client.Session() as session:
            session.get(self.base + "set?a=1")
            session.get(self.base + "set?b=2")
            session.cookies.clear()
            self.assertEqual(session._engine_cookies(), [])


class EveryFailurePathLeavesAConsistentStateTests(_Base):
    """One invariant, checked after each way a request can fail.

    The three rounds of fixes above were all the same defect: a flag or a jar
    left describing a state the store never reached. Rather than keep finding
    them one at a time, this asserts the invariant directly across the failure
    modes -- after any failure, what the caller reads must match what will be
    sent.
    """

    def _invariant(self, session):
        """The jar is writable and every jar cookie is something the store took."""
        self.assertFalse(session.cookies.writing_suppressed,
                         "the jar was left muted")
        session.cookies.set("probe", "1", domain="127.0.0.1", path="/")
        self.assertIn("probe=1", session.get(self.base + "echo").text)
        session.cookies.clear(domain="127.0.0.1", path="/", name="probe")

    def test_after_a_refused_connection(self):
        with chrome_client.Session() as session:
            with self.assertRaises(Exception):
                session.get("http://127.0.0.1:1/")
            self._invariant(session)

    def test_after_a_refused_connection_with_discard(self):
        with chrome_client.Session() as session:
            with self.assertRaises(Exception):
                session.get("http://127.0.0.1:1/", discard_cookies=True)
            self._invariant(session)

    def test_after_a_server_error_response(self):
        with chrome_client.Session() as session:
            self.assertEqual(session.get(self.base + "boom").status_code, 500)
            self._invariant(session)

    def test_after_a_server_error_with_discard(self):
        with chrome_client.Session() as session:
            session.get(self.base + "boom", discard_cookies=True)
            self._invariant(session)

    def test_after_raise_for_status(self):
        with chrome_client.Session() as session:
            with self.assertRaises(Exception):
                session.get(self.base + "boom", raise_for_status=True)
            self._invariant(session)

    def test_after_a_body_limit_failure(self):
        with chrome_client.Session() as session:
            with self.assertRaises(Exception):
                session.get(self.base + "set?a=1", max_response_bytes=1)
            self._invariant(session)

    def test_after_a_rejected_jar_assignment(self):
        with chrome_client.Session() as session:
            with self.assertRaises(Exception):
                session.cookies.set("bad", "1", domain="not a domain", path="/")
            self._invariant(session)

    def test_after_a_timeout(self):
        with chrome_client.Session() as session:
            with self.assertRaises(Exception):
                session.get("http://10.255.255.1/", timeout=0.3)
            self._invariant(session)


if __name__ == "__main__":
    unittest.main()
