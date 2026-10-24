"""Regression tests for four response-body and retry defects.

Each test states the observable symptom the defect had, so a reintroduction
fails here instead of in the field. All four were reproduced against the
shipped build before being fixed, and each fix was verified against a slow
origin -- a fast origin completes the transfer before the defect can show,
which is exactly how one of these was missed on the first pass.

Run with the audited Core available:

    LD_LIBRARY_PATH=core/binaries/linux-x86_64 \
    PYTHONPATH=bindings/python \
    python3 -m unittest bindings/python/tests/test_body_and_retry_defects.py
"""

import asyncio
import threading
import time
import unittest

try:
    from http.server import ThreadingHTTPServer
except ImportError:  # Python 3.6
    from http.server import HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

from http.server import BaseHTTPRequestHandler

import chrome_client


#: Big enough that a per-chunk delay leaves a real cancellation window.
BODY_SIZE = 200_000
CHUNK = 1_000
CHUNK_DELAY = 0.002
#: Long enough for an uncancelled transfer of BODY_SIZE to finish. Waiting
#: only a few chunk-times proves nothing: measured, an uncancelled download
#: had sent 68000 of 200000 bytes after 0.15 s and all 200000 by 1.0 s.
SETTLE_SECONDS = 1.2


class SlowHandler(BaseHTTPRequestHandler):
    """Serves a body slowly so "was the download cancelled?" is observable.

    ``sent`` counts the bytes this handler actually put on the wire: a
    cancelled transfer stops early, an uncancelled one reaches BODY_SIZE.
    """

    protocol_version = "HTTP/1.1"
    sent = 0
    lock = threading.Lock()

    def do_GET(self):
        with SlowHandler.lock:
            SlowHandler.sent = 0
        self.send_response(200)
        self.send_header("Content-Length", str(BODY_SIZE))
        self.end_headers()
        try:
            for _ in range(BODY_SIZE // CHUNK):
                self.wfile.write(b"x" * CHUNK)
                self.wfile.flush()
                with SlowHandler.lock:
                    SlowHandler.sent += CHUNK
                time.sleep(CHUNK_DELAY)
        except Exception:
            pass

    def log_message(self, *args):
        pass


class DroppingHandler(BaseHTTPRequestHandler):
    """Records each attempt, then drops the connection.

    A transport failure is what makes the client retry; ``hits`` records how
    many attempts actually reached the origin.
    """

    protocol_version = "HTTP/1.1"
    hits = []
    lock = threading.Lock()

    def do_POST(self):
        with DroppingHandler.lock:
            DroppingHandler.hits.append(self.path)
        self.close_connection = True
        try:
            self.connection.close()
        except Exception:
            pass

    def log_message(self, *args):
        pass


class Base(unittest.TestCase):
    handler = SlowHandler

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), cls.handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()


class ClaimedReaderCancellationTests(Base):
    """A reader handed to a caller must still be cancellable.

    ``iter_content()`` moves the reader out of the response so the generator
    can own it. ``close()`` used to look only at the field the reader had just
    been moved out of, so closing after the first chunk silently did nothing
    and the whole body kept downloading.
    """

    def test_closing_after_the_reader_was_claimed_stops_the_download(self):
        with SlowHandler.lock:
            SlowHandler.sent = 0
        session = chrome_client.Session()
        try:
            response = session.get(self.base + "/", stream=True, timeout=10)
            iterator = response.iter_content(CHUNK)
            next(iterator)
            response.close()
            time.sleep(SETTLE_SECONDS)
            sent = SlowHandler.sent
        finally:
            session.close()
        self.assertLess(
            sent, BODY_SIZE,
            "close() after iter_content() took the reader did not cancel the "
            "download: the origin sent %d of %d bytes" % (sent, BODY_SIZE))

    def test_closing_before_the_reader_was_claimed_still_stops_the_download(self):
        # The case that already worked; kept so a fix for the one above cannot
        # silently regress this.
        with SlowHandler.lock:
            SlowHandler.sent = 0
        session = chrome_client.Session()
        try:
            response = session.get(self.base + "/", stream=True, timeout=10)
            response.close()
            time.sleep(SETTLE_SECONDS)
            sent = SlowHandler.sent
        finally:
            session.close()
        self.assertLess(sent, BODY_SIZE)

    def test_a_full_stream_still_reads_every_byte(self):
        # Cancellation must not shorten a stream the caller actually drains.
        session = chrome_client.Session()
        try:
            response = session.get(self.base + "/", stream=True, timeout=60)
            total = sum(len(chunk) for chunk in response.iter_content(CHUNK))
        finally:
            session.close()
        self.assertEqual(total, BODY_SIZE)


class AsyncClaimedReaderCancellationTests(Base):
    """The asynchronous twin, including the ``content`` setter.

    The async path had the same blind spot in three places at once:
    ``aclose()``, the ``content`` setter, and ``_release_async_body()``.
    """

    def _sent_after(self, close_call):
        with SlowHandler.lock:
            SlowHandler.sent = 0

        async def run():
            session = chrome_client.AsyncSession()
            try:
                response = await session.get(self.base + "/", stream=True, timeout=10)
                iterator = response.aiter_content(CHUNK)
                try:
                    await iterator.__anext__()
                except StopAsyncIteration:
                    pass
                await close_call(response)
                await asyncio.sleep(SETTLE_SECONDS)
            finally:
                await session.aclose()
            return SlowHandler.sent

        return asyncio.run(asyncio.wait_for(run(), timeout=90))

    def test_aclose_after_the_reader_was_claimed_stops_the_download(self):
        sent = self._sent_after(lambda r: r.aclose())
        self.assertLess(
            sent, BODY_SIZE,
            "aclose() after aiter_content() took the reader did not cancel the "
            "download: the origin sent %d of %d bytes" % (sent, BODY_SIZE))

    def test_assigning_content_after_the_reader_was_claimed_stops_the_download(self):
        async def assign(response):
            response.content = b"replacement"

        sent = self._sent_after(assign)
        self.assertLess(
            sent, BODY_SIZE,
            "assigning .content after aiter_content() took the reader did not "
            "cancel the download: the origin sent %d of %d bytes" % (sent, BODY_SIZE))

    def test_a_full_async_stream_still_reads_every_byte(self):
        async def run():
            session = chrome_client.AsyncSession()
            try:
                response = await session.get(self.base + "/", stream=True, timeout=60)
                total = 0
                async for chunk in response.aiter_content(CHUNK):
                    total += len(chunk)
                return total
            finally:
                await session.aclose()

        self.assertEqual(asyncio.run(asyncio.wait_for(run(), timeout=90)), BODY_SIZE)


class AsyncChunkSizeValidationTests(Base):
    """``aiter_content`` must reject a non-positive chunk size.

    The synchronous twin always raised. The async one rewrote 0 to the default
    and let a negative value through, where the buffer never shrank while the
    loop condition stayed true, so it yielded empty blocks forever.
    """

    def test_a_zero_chunk_size_is_rejected(self):
        self._assert_rejected(0)

    def test_a_negative_chunk_size_is_rejected(self):
        self._assert_rejected(-5)

    def _assert_rejected(self, chunk_size):
        async def run():
            session = chrome_client.AsyncSession()
            try:
                response = await session.get(self.base + "/", stream=True, timeout=10)
                async for _ in response.aiter_content(chunk_size):
                    self.fail("aiter_content(%r) yielded a block instead of "
                              "rejecting the size" % (chunk_size,))
                self.fail("aiter_content(%r) completed instead of rejecting the "
                          "size" % (chunk_size,))
            finally:
                await session.aclose()

        with self.assertRaises(ValueError):
            asyncio.run(asyncio.wait_for(run(), timeout=30))


class StreamedBodyRetryTests(Base):
    """A one-shot streamed body must never be resent on a transport retry.

    Retrying unconditionally replayed whatever the generator still had, so the
    origin received only the tail of the body while the caller was told the
    request had failed. A bytes body, which can be produced again, must still
    be retried: the fix narrows retry rather than disabling it.
    """

    handler = DroppingHandler

    def _attempts_for(self, data, path):
        with DroppingHandler.lock:
            DroppingHandler.hits = []
        session = chrome_client.Session(retry=2)
        try:
            try:
                session.post(self.base + path, data=data, timeout=5)
            except Exception:
                pass
        finally:
            session.close()
        with DroppingHandler.lock:
            return len(DroppingHandler.hits)

    def test_a_streamed_body_is_sent_at_most_once(self):
        def once():
            yield b"PART1"
            yield b"PART2"

        attempts = self._attempts_for(once(), "/streamed")
        self.assertEqual(
            attempts, 1,
            "a one-shot streamed body was resent %d times; the origin would see "
            "a fragment of the body instead of the whole thing" % attempts)

    def test_a_bytes_body_is_still_retried(self):
        attempts = self._attempts_for(b"PAYLOAD", "/bytes")
        self.assertGreater(
            attempts, 1,
            "a bytes body was not retried at all: the streamed-body fix must "
            "narrow retry, not disable it")


class EnvironmentProxySettingsTests(Base):
    """``merge_environment_settings`` accepts ``proxies=None``.

    It used to call ``proxies.setdefault`` directly, so passing ``None`` --
    which the signature allows -- raised ``AttributeError`` whenever the
    environment named a proxy.
    """

    def test_proxies_none_is_accepted(self):
        session = chrome_client.Session()
        try:
            merged = session.merge_environment_settings(
                self.base + "/", proxies=None, stream=False, verify=True, cert=None)
        finally:
            session.close()
        self.assertIn("proxies", merged)


if __name__ == "__main__":
    unittest.main()
