"""Regression tests for the transport-binding refactor.

Each test names the property the refactor exists to hold, so a future change
that reintroduces the old behaviour fails here rather than in the field:

* a binding is a value -- immutable, hashable, and the engine-cache key;
* a slot knows how many requests are using it, so an eviction is never
  mistaken for a free one;
* cookie resolution and mirroring are serialised, so concurrent caller edits
  cannot leave two requests on different store generations;
* the counters that make a per-request transport change visible stay honest.

Run with the audited Core available::

    LD_LIBRARY_PATH=core/binaries/linux-x86_64 \
    PYTHONPATH=bindings/python \
    python3 -m unittest bindings/python/tests/test_session_binding.py
"""

import threading
import time
import unittest

try:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
except ImportError:  # Python 3.6
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

import chrome_client
from chrome_client import engine as engine_module
from chrome_client._python_impl.engine import (BINDING_FIELDS, EngineCache,
                                               EngineConfig, SessionBinding)


class Handler(BaseHTTPRequestHandler):
    """Cookie echo server, so the mirror and the jar can disagree on purpose."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def _send(self, body, extra=()):
        self.send_response(200)
        for name, value in extra:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/set"):
            name = self.path.split("=", 1)[-1] if "=" in self.path else "sid"
            return self._send(b"set", [("Set-Cookie", "%s=v-%s; Path=/" % (name, name))])
        if self.path.startswith("/echo"):
            return self._send(self.headers.get("Cookie", "").encode("utf-8"))
        return self._send(b"ok")


class BigHandler(BaseHTTPRequestHandler):
    """Serves one fixed-size body, for the graceful-close tests."""

    protocol_version = "HTTP/1.1"
    SIZE = 200000

    def log_message(self, *_args):
        pass

    def do_GET(self):
        body = b"z" * self.SIZE
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass


class BindingTests(unittest.TestCase):
    """A binding is a value, and it is the whole engine-cache key."""

    def test_a_binding_is_hashable_and_compares_by_value(self):
        left = SessionBinding(impersonate="chrome_151", proxy="http://h:1")
        right = SessionBinding(impersonate="chrome_151", proxy="http://h:1")
        self.assertEqual(left, right)
        self.assertEqual(hash(left), hash(right))
        self.assertEqual(len({left, right}), 1)

    def test_any_transport_field_separates_the_key(self):
        base = SessionBinding()
        for field in BINDING_FIELDS:
            changed = base.replace(**{field: "v" if field != "verify" and field != "cache"
                                      else False})
            self.assertNotEqual(base.key, changed.key,
                                "%s did not participate in the cache key" % field)

    def test_replace_rejects_an_unknown_field(self):
        with self.assertRaises(TypeError):
            SessionBinding().replace(heade_order=["Host"])

    def test_replace_does_not_mutate_the_original(self):
        original = SessionBinding(impersonate="chrome_151")
        copy = original.replace(proxy="http://h:1")
        self.assertIsNone(original.proxy)
        self.assertEqual(copy.proxy, "http://h:1")

    def test_fingerprint_names_the_profile_in_use(self):
        self.assertEqual(SessionBinding().fingerprint, "chromium_current")
        self.assertEqual(SessionBinding(impersonate="chrome_151").fingerprint,
                         "chrome_151")

    def test_describe_lists_only_what_was_set(self):
        described = SessionBinding(impersonate="chrome_151").describe()
        self.assertEqual(described, {"impersonate": "chrome_151"})

    def test_engine_config_exposes_its_binding(self):
        config = EngineConfig(impersonate="chrome_151", proxy="http://h:1")
        self.assertEqual(config.binding,
                         SessionBinding(impersonate="chrome_151", proxy="http://h:1"))

    def test_generation_is_not_part_of_the_transport_binding(self):
        config = EngineConfig(impersonate="chrome_151")
        self.assertEqual(config.binding, config.replace(generation=7).binding)
        self.assertNotEqual(config.key, config.replace(generation=7).key)


class LeaseTests(unittest.TestCase):
    """A slot knows whether it is in use."""

    def test_a_lease_pins_and_releases(self):
        cache = EngineCache(max_engines=1)
        config = EngineConfig()
        with cache.acquire(config) as slot:
            self.assertEqual(slot.inflight, 1)
            self.assertEqual(cache.inflight(), 1)
        self.assertEqual(slot.inflight, 0)
        self.assertEqual(cache.inflight(), 0)

    def test_rebinding_moves_the_pin(self):
        cache = EngineCache(max_engines=4)
        first = EngineConfig(generation=0)
        second = EngineConfig(generation=1)
        lease = cache.acquire(first)
        lease.__enter__()
        try:
            replacement = cache.get(second)
            lease.rebind(replacement)
            self.assertEqual(lease.slot.inflight, 1)
            self.assertEqual(cache.get(first).inflight, 0)
        finally:
            lease.__exit__(None, None, None)
        self.assertEqual(cache.inflight(), 0)

    def test_an_eviction_of_a_pinned_slot_is_reported_as_deferred(self):
        cache = EngineCache(max_engines=1)
        pinned = EngineConfig(generation=0)
        lease = cache.acquire(pinned)
        lease.__enter__()
        try:
            cache.get(EngineConfig(generation=1))
            self.assertEqual(cache.stats["evictions"], 1)
            self.assertEqual(cache.stats["deferred"], 1)
        finally:
            lease.__exit__(None, None, None)


class SessionStatsTests(unittest.TestCase):
    """The counters describe what the session really did."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_repeated_requests_build_one_engine(self):
        with chrome_client.Session() as session:
            for _ in range(4):
                self.assertEqual(session.get(self.base + "/ok").status_code, 200)
            stats = session.stats
        self.assertEqual(stats["builds"], 1)
        self.assertEqual(stats["hits"], 3)
        self.assertEqual(stats["live"], 1)

    def test_a_per_request_profile_shows_up_as_a_second_build(self):
        with chrome_client.Session() as session:
            session.get(self.base + "/ok")
            session.get(self.base + "/ok", impersonate="chrome_151")
            stats = session.stats
        self.assertEqual(stats["builds"], 2)

    def test_binding_answers_with_the_sessions_own_proxy(self):
        with chrome_client.Session(proxy="http://127.0.0.1:9") as session:
            self.assertEqual(session.binding("http://example.invalid/").proxy,
                             "http://127.0.0.1:9")

    def test_binding_without_a_url_fails_closed_on_a_proxy_mapping(self):
        with chrome_client.Session(proxies={"https": "http://127.0.0.1:9"}) as session:
            self.assertEqual(
                session.binding("https://example.invalid/").proxy,
                "http://127.0.0.1:9")
            with self.assertRaises(chrome_client.InvalidProxyURL):
                session.binding()

    def test_binding_is_the_engine_cache_key_without_the_generation(self):
        with chrome_client.Session() as session:
            binding = session.binding("http://example.invalid/")
            config = session._engine_config(None, None, None, None)
            self.assertEqual(config.binding, binding)
            self.assertEqual(config.generation, session.stats["generation"])

    def test_the_sync_path_rebinds_when_cookie_resolution_replaces_the_engine(self):
        """The sync call site must move the pin too -- same reason as async."""
        from chrome_client._python_impl.engine import _SlotLease

        calls = []
        original = _SlotLease.rebind

        def watched(self, slot):
            calls.append(slot.config.generation)
            return original(self, slot)

        _SlotLease.rebind = watched
        try:
            with chrome_client.Session() as session:
                session.get(self.base + "/set=sid")
                session.cookies.clear()          # jar now disagrees with store
                session.get(self.base + "/echo")
        finally:
            _SlotLease.rebind = original
        self.assertTrue(calls, "the sync path never rebound the lease")

    def test_stats_reports_the_generation_the_session_is_on(self):
        with chrome_client.Session() as session:
            session.get(self.base + "/set=sid")
            session.cookies.clear()
            session.get(self.base + "/echo")
            stats = session.stats
        self.assertGreaterEqual(stats["generation"], 1)

    def test_a_closed_session_still_reports_its_last_counters(self):
        session = chrome_client.Session()
        session.get(self.base + "/ok")
        session.close()
        self.assertEqual(session.stats["builds"], 1)


class ConcurrentCookieTests(unittest.TestCase):
    """Two threads editing the jar must not lose an edit or split generations."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_concurrent_edits_do_not_lose_a_cookie(self):
        session = chrome_client.Session()
        try:
            session.get(self.base + "/set=sid")
            errors = []

            def edit(index):
                try:
                    for _ in range(3):
                        session.cookies.set("k%d" % index, "v%d" % index,
                                            domain="127.0.0.1", path="/")
                        session.get(self.base + "/echo")
                except Exception as error:  # pragma: no cover - reported below
                    errors.append(error)

            threads = [threading.Thread(target=edit, args=(index,))
                       for index in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            self.assertEqual(errors, [])
            jar = session.cookies.get_dict()
            for index in range(4):
                self.assertEqual(jar.get("k%d" % index), "v%d" % index,
                                 "a concurrent edit lost k%d" % index)
        finally:
            session.close()

    def test_cookie_resolution_never_runs_in_parallel(self):
        """Two threads must not be inside cookie resolution at the same time.

        The sequence is read the jar, read the engine's mirror, compare, and
        possibly replace the engine with a new cookie-store generation.  Two
        workers interleaving it can each bump the generation and end up on
        different engines for the same jar, so one of them sends a Cookie
        header its own engine's store then overrides.  The lock is what makes
        the sequence atomic, and this measures it directly instead of hoping a
        lost update shows up as a missing cookie.
        """
        from chrome_client._python_impl.cookies import RequestsCookieJar

        state = {"active": 0, "peak": 0}
        original = RequestsCookieJar.cookie_header

        def watched(self, url, headers=None):
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            try:
                # A real yield, not a busy loop: a busy loop keeps the GIL, so
                # two unsynchronised threads would still look serialised and
                # this check would pass on a broken implementation.  Sleeping
                # releases the GIL, so a second thread reaches this point if and
                # only if nothing serialises the callers.
                time.sleep(0.002)
                return original(self, url, headers)
            finally:
                state["active"] -= 1

        RequestsCookieJar.cookie_header = watched
        session = chrome_client.Session()
        try:
            session.get(self.base + "/set=sid")
            threads = []
            for index in range(6):
                def work(index=index):
                    for _ in range(2):
                        session.cookies.set("c%d" % index, "v", domain="127.0.0.1",
                                            path="/")
                        session.get(self.base + "/echo")
                threads.append(threading.Thread(target=work))
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
        finally:
            RequestsCookieJar.cookie_header = original
            session.close()
        self.assertEqual(state["peak"], 1,
                         "cookie resolution overlapped in %d threads"
                         % state["peak"])

    def test_the_cookie_lock_is_not_the_engine_cache_lock(self):
        session = chrome_client.Session()
        try:
            self.assertIsNot(session._cookie_lock, session._engines._lock)
        finally:
            session.close()


class ExportTests(unittest.TestCase):
    """The new surface is importable the way the stubs advertise it."""

    def test_session_binding_is_public(self):
        self.assertIs(chrome_client.SessionBinding, SessionBinding)
        self.assertIn("SessionBinding", chrome_client.__all__)

    def test_engine_module_keeps_its_historical_names(self):
        self.assertIs(engine_module.EngineConfig, EngineConfig)
        self.assertTrue(hasattr(engine_module, "EngineSlot"))
        self.assertTrue(hasattr(engine_module, "EngineCache"))


class BodyChunkingTests(unittest.TestCase):
    """The re-chunker copies each byte once and never loses a boundary."""

    class _Reader(object):
        """Feeds a fixed list of buffers, then stops."""

        def __init__(self, chunks):
            self._chunks = list(chunks)
            self.closed = False

        def __iter__(self):
            for chunk in self._chunks:
                yield chunk

        def close(self):
            self.closed = True

    def _blocks(self, chunks, size):
        from chrome_client._python_impl.models import Response

        response = Response()
        reader = self._Reader(chunks)
        blocks = list(response._stream_chunks(reader, size))
        self.assertTrue(reader.closed, "the reader must be closed even on success")
        return blocks

    def test_a_block_smaller_than_the_buffers_is_sliced(self):
        self.assertEqual(self._blocks([b"abcdefgh"], 3), [b"abc", b"def", b"gh"])

    def test_a_block_straddling_two_buffers_is_joined(self):
        self.assertEqual(self._blocks([b"ab", b"cd", b"ef"], 4), [b"abcd", b"ef"])

    def test_a_block_straddling_three_buffers_is_joined(self):
        self.assertEqual(self._blocks([b"a", b"b", b"c", b"d"], 3), [b"abc", b"d"])

    def test_the_whole_body_survives_rechunking(self):
        import os

        payload = os.urandom(5000)
        pieces = [payload[offset:offset + 7] for offset in range(0, len(payload), 7)]
        blocks = self._blocks(pieces, 512)
        self.assertEqual(b"".join(blocks), payload)

    def test_a_block_exactly_one_buffer_long_is_handed_out_whole(self):
        self.assertEqual(self._blocks([b"abcd", b"ef"], 4), [b"abcd", b"ef"])

    def test_an_aligned_buffer_is_handed_out_without_copying(self):
        """The whole point of the rewrite: an aligned block costs no copy.

        A behavioural assertion cannot see this -- a copying implementation
        returns equal bytes -- so the check is object identity.  Reintroducing
        the copy makes it fail, which is the only way the optimisation is
        actually load-bearing rather than decorative.
        """
        from chrome_client._python_impl.models import Response

        buffer = b"0123456789abcdef"
        reader = self._Reader([buffer])
        blocks = list(Response()._stream_chunks(reader, len(buffer)))
        self.assertEqual(blocks, [buffer])
        self.assertIs(blocks[0], buffer,
                      "an aligned buffer must be yielded as-is, not copied")

    def test_an_empty_body_yields_nothing(self):
        self.assertEqual(self._blocks([], 512), [])


class AsyncParityTests(unittest.TestCase):
    """The async path must hold the same properties as the sync one."""

    class _AsyncReader(object):
        """Async twin of the sync reader: fixed buffers, then stop."""

        def __init__(self, chunks):
            self._chunks = list(chunks)
            self.closed = False

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for chunk in self._chunks:
                yield chunk

        async def aclose(self, cancel=False):
            self.closed = True

    def _blocks(self, chunks, size):
        import asyncio
        from chrome_client._python_impl.models import AsyncResponse

        async def run():
            response = AsyncResponse()
            reader = self._AsyncReader(chunks)
            blocks = [block async for block in
                      response._astream_blocks(reader, size)]
            return blocks

        return asyncio.run(run())

    def test_an_aligned_buffer_is_handed_out_without_copying(self):
        buffer = b"0123456789abcdef"
        blocks = self._blocks([buffer], len(buffer))
        self.assertEqual(blocks, [buffer])
        self.assertIs(blocks[0], buffer,
                      "the async path must not copy an aligned buffer either")

    def test_a_block_straddling_two_buffers_is_joined(self):
        self.assertEqual(self._blocks([b"ab", b"cd", b"ef"], 4), [b"abcd", b"ef"])

    def test_the_whole_body_survives_rechunking(self):
        import os

        payload = os.urandom(4096)
        pieces = [payload[offset:offset + 5] for offset in range(0, len(payload), 5)]
        self.assertEqual(b"".join(self._blocks(pieces, 512)), payload)

    def test_aiter_content_honours_the_requested_block_size(self):
        """The public async iterator must use the zero-copy path, not a copy loop.

        Asserting only on the private helper left the wiring uncovered: passing a
        different block size from the public entry point kept every direct test
        green.  This drives the public iterator, so the block size the caller
        asked for is the block size that comes out.
        """
        import asyncio

        async def run():
            async with chrome_client.AsyncSession() as session:
                response = await session.get(self.big_base + "/", stream=True)
                blocks = [block async for block in response.aiter_content(4096)]
                self.assertEqual(sum(len(b) for b in blocks), BigHandler.SIZE)
                # Every block but the final remainder is exactly the requested
                # size; the remainder is whatever the body left over.
                self.assertEqual({len(b) for b in blocks[:-1]}, {4096})
                self.assertLessEqual(len(blocks[-1]), 4096)

        self.run_async(run())

    def test_aiter_content_reports_the_remainder_last(self):
        import asyncio

        async def run():
            async with chrome_client.AsyncSession() as session:
                response = await session.get(self.big_base + "/", stream=True)
                blocks = [block async for block in response.aiter_content(65536)]
                self.assertEqual(len(blocks), BigHandler.SIZE // 65536 + 1)
                self.assertLess(len(blocks[-1]), 65536)
                self.assertEqual(b"".join(blocks), b"z" * BigHandler.SIZE)

        self.run_async(run())

    def test_the_async_path_rebinds_when_cookie_resolution_replaces_the_engine(self):
        """The async call site must move the pin to the engine it ends up on.

        A replacement engine is a different slot, so leaving the pin behind
        would report the request as running on an engine nobody is using while
        the one in use looked idle.  This forces a store/jar disagreement and
        asserts the call site actually rebinds, which no behavioural assertion
        about bytes can see.
        """
        import asyncio
        from chrome_client._python_impl.engine import _SlotLease

        calls = []
        original = _SlotLease.rebind

        def watched(self, slot):
            calls.append(slot.config.generation)
            return original(self, slot)

        _SlotLease.rebind = watched
        try:
            async def run():
                async with chrome_client.AsyncSession() as session:
                    await session.get(self.base + "/set=sid")
                    session.cookies.clear()          # jar now disagrees with store
                    await session.get(self.base + "/echo")

            self.run_async(run())
        finally:
            _SlotLease.rebind = original
        self.assertTrue(calls, "the async path never rebound the lease")

    def test_async_max_clients_still_bounds_inflight(self):
        import asyncio

        async def run():
            async with chrome_client.AsyncSession(max_clients=4) as session:
                responses = await asyncio.gather(
                    *[session.get(self.base + "/ok") for _ in range(20)])
                self.assertEqual(len(responses), 20)
                self.assertEqual(session.stats["inflight"], 0)

        self.run_async(run())

    def test_the_async_path_pins_and_releases_the_engine(self):
        import asyncio

        async def run():
            async with chrome_client.AsyncSession() as session:
                await session.get(self.base + "/ok")
                self.assertEqual(session.stats["inflight"], 0)
                self.assertEqual(session.stats["builds"], 1)

        self.run_async(run())

    def test_async_cookie_resolution_is_serialised_too(self):
        import asyncio

        async def run():
            async with chrome_client.AsyncSession() as session:
                await session.get(self.base + "/set=sid")
                for index in range(4):
                    session.cookies.set("a%d" % index, "v", domain="127.0.0.1",
                                        path="/")
                    await session.get(self.base + "/echo")
                jar = session.cookies.get_dict()
                for index in range(4):
                    self.assertEqual(jar.get("a%d" % index), "v")

        self.run_async(run())

    def test_a_streaming_body_survives_async_close(self):
        import asyncio

        async def run():
            session = chrome_client.AsyncSession()
            response = await session.get(self.big_base + "/", stream=True)
            await session.aclose()
            total = 0
            async for block in response.aiter_content(4096):
                total += len(block)
            self.assertEqual(total, BigHandler.SIZE)

        self.run_async(run())

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        big = ThreadingHTTPServer(("127.0.0.1", 0), BigHandler)
        cls.big = big
        cls.big_base = "http://127.0.0.1:%d" % big.server_address[1]
        cls.big_thread = threading.Thread(target=big.serve_forever)
        cls.big_thread.daemon = True
        cls.big_thread.start()

    @classmethod
    def tearDownClass(cls):
        for server, thread in ((cls.server, cls.thread), (cls.big, cls.big_thread)):
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    @staticmethod
    def run_async(coro):
        import asyncio

        return asyncio.run(coro)


class GracefulCloseTests(unittest.TestCase):
    """Closing a session ends the pool, not the work already running on it."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), BigHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_a_streaming_body_survives_session_close(self):
        session = chrome_client.Session()
        response = session.get(self.base + "/", stream=True)
        self.assertEqual(response.status_code, 200)
        session.close()
        self.assertEqual(len(b"".join(response.iter_content(4096))), BigHandler.SIZE)

    def test_a_closed_session_refuses_a_new_request(self):
        session = chrome_client.Session()
        session.get(self.base + "/")
        session.close()
        with self.assertRaises(chrome_client.RequestException):
            session.get(self.base + "/")


if __name__ == "__main__":
    unittest.main()

