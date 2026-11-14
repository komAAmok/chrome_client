"""Regression tests for releasing native resources on close().

Two defects were measured here, and both of them made a create/request/close loop
grow without bound:

1. ``_SessionJarSink`` held the session strongly, so session -> jar -> sink ->
   session was a reference cycle. An object in a cycle never reaches a refcount of
   zero, so its ``__del__` -- the only thing that releases a native request -- did
   not run until the cyclic collector visited that generation. With the collector
   disabled (which a host embedding Python may well do), 200 cycles left 200
   sessions and 403 open descriptors.

2. ``close()`` emptied the engine cache but left ``_seeded_slots``, which is a
   second strong reference to the same ``EngineSlot`` objects. ``stats["live"]`
   therefore reported 0 while the engine, its thread pool registration and its
   five sockets were all still alive, and they stayed alive until the next cache
   miss or a cyclic collection.

Both assertions below fail if either fix is reverted; the second one was checked by
restoring the strong reference and watching it fail.
"""

import gc
import os
import unittest

import chrome_client


def _open_sockets():
    """Counts socket descriptors, or returns None where /proc is unavailable."""
    if not os.path.isdir("/proc/self/fd"):
        return None
    count = 0
    for entry in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink("/proc/self/fd/" + entry)
        except OSError:
            continue
        if target.startswith("socket:"):
            count += 1
    return count


class _Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                body = b"x" * 64
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Set-Cookie", "sid=%s; Path=/" % self.path[-6:])
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()


class CloseReleasesTheEngineTests(_Server):
    """close() must drop every strong reference the session holds to an engine."""

    def test_close_empties_the_seeded_slot_set(self):
        """The seed record must not outlive the cache that produced it.

        It is a strong reference to the slot, so leaving it behind keeps the
        whole Chromium context -- and its sockets -- alive after close().
        """
        session = chrome_client.Session()
        try:
            session.get(self.base + "/a")
            self.assertEqual(len(session._seeded_slots), 1,
                             "a request did not seed its engine")
            session.close()
            self.assertEqual(session.stats["live"], 0)
            self.assertEqual(len(session._seeded_slots), 0,
                             "close() left a strong reference to the engine")
        finally:
            session.close()

    def test_a_closed_session_is_collectable_without_the_cyclic_collector(self):
        """The session graph must be acyclic, so refcounting alone frees it.

        A host that disables the cyclic collector -- or simply never triggers a
        gen-2 pass -- must still get its engines back on close().
        """
        import weakref

        gc.collect()
        session = chrome_client.Session()
        session.get(self.base + "/b")
        reference = weakref.ref(session)
        session.close()
        del session
        self.assertIsNone(reference(),
                          "the session survived by reference counting: it is in a "
                          "cycle, and its __del__ cannot run")

    def test_churn_with_the_collector_disabled_does_not_grow(self):
        """Sixty create/request/close cycles must leave the descriptor count flat.

        Measured before the fix with the collector disabled: +121 sockets and 60
        live engines. A threshold of eight descriptors absorbs the pool's own
        reuse behaviour without absorbing a per-cycle leak.
        """
        baseline = _open_sockets()
        if baseline is None:
            self.skipTest("/proc/self/fd is unavailable on this platform")
        gc.collect()
        baseline = _open_sockets()
        gc.disable()
        try:
            for index in range(60):
                session = chrome_client.Session()
                session.get("%s/c%d" % (self.base, index))
                session.close()
                del session
        finally:
            gc.enable()
        grown = _open_sockets() - baseline
        self.assertLessEqual(grown, 8,
                             "descriptors grew by %d over 60 closed sessions"
                             % grown)


class SlotOwnershipTests(_Server):
    """Only an engine no cache owns may be released with its request.

    ``discard_cookies=True`` runs on a throwaway engine and hands it to a retire
    worker as soon as the request ends. Deciding "throwaway" by asking the cache
    whether it still holds the slot is wrong, because EVICTION answers the same
    way: measured with ``max_engines=1``, building a second configuration evicts
    the first, the evicted slot is no longer findable, and the request still
    running on it would have its engine torn down underneath it. The slot now
    carries the answer instead of having it inferred.
    """

    def test_an_evicted_pooled_slot_is_not_single_use(self):
        session = chrome_client.Session(max_engines=1)
        try:
            first = session._engine_config(None, None, None, None)
            second = session._engine_config(None, None, False, None)
            pooled = session._engines.get(first)
            self.assertFalse(pooled.single_use)
            replacement = session._engines.get(second)
            self.assertIsNot(pooled, replacement)
            self.assertFalse(pooled.single_use,
                             "an evicted pooled slot was marked single-use, so the "
                             "request still running on it would lose its engine")
            self.assertIsNotNone(pooled.engine,
                                 "eviction released an engine a request may still use")
        finally:
            session.close()

    def test_a_throwaway_slot_is_single_use(self):
        session = chrome_client.Session()
        try:
            config = session._engine_config(None, None, None, None)
            session._engines.get(config)
            throwaway = session._throwaway_slot(config)
            self.assertTrue(throwaway.single_use)
            self.assertIsNot(throwaway, session._engines.get(config),
                             "a throwaway slot came from the cache")
        finally:
            session.close()

    def test_releasing_a_slot_twice_is_harmless(self):
        session = chrome_client.Session()
        try:
            config = session._engine_config(None, None, None, None)
            slot = session._throwaway_slot(config)
            slot.release_engine()
            slot.release_engine()
            self.assertIsNone(slot.engine)
        finally:
            session.close()

    def test_a_discard_request_leaves_a_pooled_engine_usable(self):
        """The pooled engine must survive a discard request that ran beside it."""
        with chrome_client.Session() as session:
            session.get(self.base + "/keep")
            pooled = session._engines.get(
                session._engine_config(None, None, None, None))
            session.get(self.base + "/discard", discard_cookies=True)
            self.assertIsNotNone(pooled.engine,
                                 "a discard request released the pooled engine")
            self.assertEqual(session.get(self.base + "/again").status_code, 200)


if __name__ == "__main__":
    unittest.main()
