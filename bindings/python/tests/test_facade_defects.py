"""Regression tests for five facade defects.

Each test states the behaviour a caller observes, not the fix, so a regression
fails here rather than in a downstream consumer:

* **B1** -- ``AsyncResponse.aiter_content(decode_unicode=True)`` decoded every
  chunk on its own, so a multi-byte character split across a chunk boundary
  became U+FFFD.  The synchronous path had always used an incremental decoder.
* **B2** -- ``.content`` returned ``b""`` for a response whose body a stream
  reader had already taken, claiming an empty body for one that had bytes.
* **B3** -- a second ``iter_content()`` generator over one response died with
  ``AttributeError`` on its first ``next()``, because the generator body bound
  the reader lazily and the other generator had already cleared it.
* **B4** -- ``JSONDecodeError`` was not a ``json.JSONDecodeError``, so
  ``except json.JSONDecodeError`` missed it and ``.pos`` did not exist.
* **B5** -- ``send``/``send_str``/``send_bytes``/``ping``/``close``/
  ``terminate`` let the Core bare ``RuntimeError`` escape instead of mapping it
  onto the exception hierarchy.
"""

import asyncio
import base64
import hashlib
import json
import socket
import struct
import threading
import time
import unittest

from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

try:
    from http.server import ThreadingHTTPServer
except ImportError:  # Python 3.6
    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

import chrome_client


#: Text whose UTF-8 encoding puts a multi-byte character across the 10-byte
#: chunk boundary the tests below use.
TEXT = "h\u00e9llo w\u00f6rld \u2014 \u00fcn\u00efcode \u2713 \u6f22\u5b57 " * 20
UTF8_BODY = TEXT.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, body, content_type="application/octet-stream"):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/utf8"):
            return self._send(UTF8_BODY, "text/plain; charset=utf-8")
        if self.path.startswith("/bytes/"):
            return self._send(b"z" * int(self.path.rsplit("/", 1)[-1]))
        if self.path.startswith("/invalid-json"):
            return self._send(b'{"a": }', "application/json")
        if self.path.startswith("/empty"):
            return self._send(b"", "application/json")
        return self._send(b'{"hello": "world"}', "application/json")

    def log_message(self, *_args):
        pass


class WebSocketServer(object):
    """Minimal WebSocket server, so B5 needs no external endpoint."""

    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self):
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(8)
        self.port = self._socket.getsockname()[1]
        self._stop = False
        self._thread = threading.Thread(target=self._serve)
        self._thread.daemon = True
        self._thread.start()

    @property
    def url(self):
        return "ws://127.0.0.1:%d/" % self.port

    def _serve(self):
        while not self._stop:
            try:
                client, _address = self._socket.accept()
            except OSError:
                return
            worker = threading.Thread(target=self._session, args=(client,))
            worker.daemon = True
            worker.start()

    def _session(self, client):
        try:
            raw = b""
            while b"\r\n\r\n" not in raw:
                chunk = client.recv(4096)
                if not chunk:
                    return
                raw += chunk
            headers = {}
            for line in raw.split(b"\r\n\r\n", 1)[0].split(b"\r\n")[1:]:
                if b":" in line:
                    name, value = line.split(b":", 1)
                    headers[name.strip().lower()] = value.strip()
            key = headers.get(b"sec-websocket-key", b"")
            accept = base64.b64encode(
                hashlib.sha1(key + self.GUID.encode()).digest()).decode()
            client.sendall(("HTTP/1.1 101 Switching Protocols\r\n"
                            "Upgrade: websocket\r\n"
                            "Connection: Upgrade\r\n"
                            "Sec-WebSocket-Accept: %s\r\n\r\n" % accept).encode())
            client.sendall(struct.pack("!BB", 0x81, 5) + b"hello")
            client.settimeout(5.0)
            while True:
                if not client.recv(4096):
                    return
        except OSError:
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass

    def close(self):
        self._stop = True
        try:
            self._socket.close()
        except OSError:
            pass


class FakeReader(object):
    """Stand-in for the native body reader: yields prepared chunks."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    def __iter__(self):
        return iter(self._chunks)

    def close(self, cancel=False):
        self.closed = True


class FakeAsyncReader(FakeReader):
    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self, cancel=False):
        self.closed = True


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()
        cls.url = "http://127.0.0.1:%d/" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=5)

    @staticmethod
    def run_async(coro):
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(coro)
        finally:
            asyncio.set_event_loop(None)
            loop.close()
class AsyncStreamDecodingTests(Base):
    """B1: a split multi-byte character must not become U+FFFD."""

    def test_ten_byte_chunks_decode_without_replacement_characters(self):
        async def run():
            async with chrome_client.AsyncSession() as session:
                response = await session.get(self.url + "utf8", stream=True)
                pieces = []
                async for chunk in response.aiter_content(10, decode_unicode=True):
                    pieces.append(chunk)
                return "".join(pieces)

        decoded = self.run_async(run())
        self.assertEqual(decoded.count("\ufffd"), 0)
        self.assertEqual(decoded, TEXT)

    def test_the_synchronous_and_asynchronous_paths_agree(self):
        """Both decode through one incremental decoder, so both must match."""
        from chrome_client._python_impl.models import AsyncResponse, Response

        chunks = [UTF8_BODY[offset:offset + 10]
                  for offset in range(0, len(UTF8_BODY), 10)]

        sync = Response()
        sync.encoding = "utf-8"
        sync._body_reader = FakeReader(chunks)
        sync_text = "".join(sync.iter_content(10, decode_unicode=True))

        async def run():
            response = AsyncResponse()
            response.encoding = "utf-8"
            response._async_body_reader = FakeAsyncReader(chunks)
            return "".join([piece async for piece in
                            response.aiter_content(10, decode_unicode=True)])

        async_text = self.run_async(run())
        self.assertEqual(async_text, sync_text)
        self.assertEqual(async_text, TEXT)

    def test_byte_mode_still_yields_the_exact_bytes(self):
        async def run():
            async with chrome_client.AsyncSession() as session:
                response = await session.get(self.url + "utf8", stream=True)
                return b"".join([chunk async for chunk in
                                 response.aiter_content(10)])

        self.assertEqual(self.run_async(run()), UTF8_BODY)


class PartialStreamContentTests(Base):
    """B2: ``.content`` must not claim an empty body it never had."""

    def test_content_after_partial_stream_raises(self):
        with chrome_client.Session() as session:
            response = session.get(self.url + "bytes/5000", stream=True)
            reader = response.iter_content(10)
            self.assertEqual(len(next(reader)), 10)
            with self.assertRaises(chrome_client.StreamConsumedError):
                response.content

    def test_content_after_close_raises(self):
        with chrome_client.Session() as session:
            response = session.get(self.url + "bytes/5000", stream=True)
            response.close()
            with self.assertRaises(chrome_client.StreamConsumedError):
                response.content

    def test_a_fully_read_stream_is_unaffected(self):
        with chrome_client.Session() as session:
            response = session.get(self.url + "bytes/5000", stream=True)
            self.assertEqual(len(b"".join(response.iter_content(7))), 5000)
            buffered = session.get(self.url + "bytes/5000")
            self.assertEqual(len(buffered.content), 5000)
            # Accessing a buffered body twice stays free.
            self.assertEqual(buffered.content, buffered.content)


class RepeatedIterContentTests(Base):
    """B3: the second iterator must fail cleanly, not with AttributeError."""

    def test_a_second_iterator_raises_stream_consumed(self):
        """The second reader must fail as StreamConsumedError, never AttributeError.

        The reader is claimed when `iter_content` is called, so the refusal
        may land at the call or at the first `next()`; both are correct, an
        `AttributeError` on a cleared reader is not.
        """
        with chrome_client.Session() as session:
            response = session.get(self.url + "bytes/5000", stream=True)
            first = response.iter_content(16)
            try:
                second = response.iter_content(16)
            except chrome_client.StreamConsumedError:
                second = None
            self.assertEqual(len(next(first)), 16)
            if second is not None:
                with self.assertRaises(chrome_client.StreamConsumedError):
                    next(second)

    def test_the_first_iterator_still_finishes(self):
        with chrome_client.Session() as session:
            response = session.get(self.url + "bytes/1000", stream=True)
            first = response.iter_content(64)
            try:
                response.iter_content(64)
            except chrome_client.StreamConsumedError:
                pass
            self.assertEqual(len(next(first)), 64)
            self.assertEqual(len(b"".join(first)), 1000 - 64)

    def test_two_generators_over_a_buffered_body_are_independent(self):
        """A body already in memory has no reader to fight over."""
        with chrome_client.Session() as session:
            response = session.get(self.url + "bytes/64")
            first = response.iter_content(16)
            second = response.iter_content(16)
            self.assertEqual(len(next(first)), 16)
            self.assertEqual(len(next(second)), 16)


class ClosedResponseTests(Base):
    """The state `close()` leaves behind has to be stated, not guessed.

    requests raises a bare ``RuntimeError("The content for this response was
    already consumed")`` here. That is not a ``RequestException``, so
    ``except RequestException`` catches ours and not requests'; ours is the
    stricter of the two and is the one this package promises.
    """

    def test_content_after_close_raises_stream_consumed(self):
        with chrome_client.Session() as session:
            response = session.get(self.url + "bytes/5000", stream=True)
            response.close()
            with self.assertRaises(chrome_client.StreamConsumedError) as caught:
                response.content
        error = caught.exception
        # Both bases matter: RequestException is what callers catch, TypeError
        # is what requests users may already be catching.
        self.assertIsInstance(error, chrome_client.RequestException)
        self.assertIsInstance(error, TypeError)
        self.assertNotIsInstance(error, RuntimeError)

    def test_iter_content_after_close_raises_stream_consumed(self):
        """Not an empty iterator: an empty one would claim a zero-length body."""
        with chrome_client.Session() as session:
            response = session.get(self.url + "bytes/5000", stream=True)
            response.close()
            with self.assertRaises(chrome_client.StreamConsumedError):
                response.iter_content(16)

    def test_iter_lines_after_close_raises_stream_consumed(self):
        with chrome_client.Session() as session:
            response = session.get(self.url + "bytes/5000", stream=True)
            response.close()
            with self.assertRaises(chrome_client.StreamConsumedError):
                list(response.iter_lines())

    def test_close_after_a_partial_read_raises_on_both_reads(self):
        with chrome_client.Session() as session:
            response = session.get(self.url + "bytes/5000", stream=True)
            reader = response.iter_content(16)
            self.assertEqual(len(next(reader)), 16)
            response.close()
            for name, call in (("content", lambda: response.content),
                               ("iter_content",
                                lambda: response.iter_content(16))):
                with self.subTest(read=name):
                    with self.assertRaises(chrome_client.StreamConsumedError):
                        call()

    def test_a_closed_streamed_response_still_reports_its_metadata(self):
        """Closing the body must not erase the headers it already received."""
        with chrome_client.Session() as session:
            response = session.get(self.url + "bytes/5000", stream=True)
            response.close()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers.get("Content-Length"), "5000")


class AssignedContentTests(Base):
    """Assigning `.content` must replace the body for every reader.

    A streamed response keeps its native reader alive, so assigning the body
    used to leave `.content`/`.text` answering the assigned bytes while
    `iter_content()`/`iter_lines()` kept streaming the discarded native one:
    the same object reporting two different bodies.
    """

    def test_iter_content_slices_the_assigned_bytes(self):
        with chrome_client.Session() as session:
            response = session.get(self.url + "bytes/5000", stream=True)
            response.content = b"x" * 100
            self.assertEqual(len(response.content), 100)
            self.assertEqual(len(b"".join(response.iter_content(30))), 100)

    def test_every_reader_agrees_after_the_assignment(self):
        with chrome_client.Session() as session:
            response = session.get(self.url + "bytes/5000", stream=True)
            response.content = b"x" * 100
            self.assertEqual(len(response.text), 100)
            self.assertEqual(len(b"".join(response.iter_content(30))), 100)
            self.assertEqual(len(response.raw.read()), 100)

    def test_iter_lines_reads_the_assigned_bytes(self):
        with chrome_client.Session() as session:
            response = session.get(self.url + "bytes/5000", stream=True)
            response.content = b"a\nb\nc\n"
            self.assertEqual(list(response.iter_lines()), [b"a", b"b", b"c"])

    def test_the_async_readers_agree_after_the_assignment(self):
        async def run():
            async with chrome_client.AsyncSession() as session:
                response = await session.get(self.url + "bytes/5000",
                                             stream=True)
                response.content = b"x" * 100
                streamed = b"".join([chunk async for chunk in
                                     response.aiter_content(30)])
                return (len(response.content), len(streamed),
                        len(await response.atext()))

        buffered, streamed, text = self.run_async(run())
        self.assertEqual((buffered, streamed, text), (100, 100, 100))

    def test_assigning_after_close_still_works(self):
        """The body was cancelled, then replaced: the replacement is readable."""
        with chrome_client.Session() as session:
            response = session.get(self.url + "bytes/5000", stream=True)
            response.close()
            response.content = b"w" * 40
            self.assertEqual(len(response.content), 40)
            self.assertEqual(len(b"".join(response.iter_content(15))), 40)

    def test_a_buffered_response_is_unaffected(self):
        with chrome_client.Session() as session:
            response = session.get(self.url + "bytes/5000")
            response.content = b"y" * 50
            self.assertEqual(len(b"".join(response.iter_content(20))), 50)
class JSONDecodeErrorTests(Base):
    """B4: the class has to be a ``json.JSONDecodeError``, as in requests."""

    def test_the_class_is_a_stdlib_json_decode_error(self):
        self.assertTrue(issubclass(chrome_client.JSONDecodeError,
                                   json.JSONDecodeError))
        self.assertTrue(issubclass(chrome_client.JSONDecodeError, ValueError))
        self.assertTrue(issubclass(chrome_client.JSONDecodeError,
                                   chrome_client.InvalidJSONError))
        self.assertTrue(issubclass(chrome_client.JSONDecodeError,
                                   chrome_client.RequestException))

    def test_a_bad_body_raises_the_stdlib_shape(self):
        with chrome_client.Session() as session:
            response = session.get(self.url + "invalid-json")
            with self.assertRaises(json.JSONDecodeError) as caught:
                response.json()
        error = caught.exception
        self.assertIsInstance(error, chrome_client.JSONDecodeError)
        self.assertEqual(error.msg, "Expecting value")
        self.assertEqual(error.doc, '{"a": }')
        self.assertEqual(error.pos, 6)
        self.assertEqual((error.lineno, error.colno), (1, 7))
        self.assertEqual(str(error), "Expecting value: line 1 column 7 (char 6)")
        self.assertIs(error.response, response)

    def test_an_empty_body_reports_position_zero(self):
        with chrome_client.Session() as session:
            response = session.get(self.url + "empty")
            with self.assertRaises(json.JSONDecodeError) as caught:
                response.json()
        self.assertEqual(caught.exception.pos, 0)
        self.assertEqual(str(caught.exception),
                         "Expecting value: line 1 column 1 (char 0)")

    def test_a_valid_body_still_decodes(self):
        with chrome_client.Session() as session:
            self.assertEqual(session.get(self.url).json(), {"hello": "world"})


class WebSocketTerminalErrorTests(unittest.TestCase):
    """B5: a Core failure must reach the caller as a mapped exception.

    The Core reports a terminal socket as the string ``InvalidState``, which
    PyO3 raises as a bare ``RuntimeError``. Every method that crosses the ABI
    has to map it, or it escapes past the ``except RequestException`` a caller
    writes.
    """

    #: How long to wait for the Core to settle the socket into a terminal state.
    SETTLE = 0.4

    def setUp(self):
        self.server = WebSocketServer()
        self.addCleanup(self.server.close)

    @staticmethod
    def run_async(coro):
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(coro)
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    def _terminal_sync_socket(self):
        """A socket the Core has finished, reached through the public API."""
        socket_ = chrome_client.WebSocket(url=self.server.url)
        socket_.recv_str()
        socket_.terminate()
        time.sleep(self.SETTLE)
        # The Core is already terminal, so releasing the socket may itself
        # raise the very InvalidState these tests exercise; cleanup is not
        # what is under test here.
        self.addCleanup(self._release, socket_)
        return socket_

    @staticmethod
    def _release(socket_):
        try:
            socket_.close()
        except chrome_client.RequestException:
            pass

    def _assert_mapped(self, name, call):
        """Asserts ``call`` raises a mapped exception, not a bare RuntimeError."""
        try:
            call()
        except chrome_client.RequestException:
            return
        except RuntimeError as error:
            self.fail("%s leaked a bare %s: %r"
                      % (name, type(error).__name__, error))
        self.fail("%s did not fail on a terminal socket" % name)

    def test_every_sending_method_maps_the_native_error(self):
        socket_ = self._terminal_sync_socket()
        for name, call in (("send", lambda ws: ws.send("x")),
                           ("send_str", lambda ws: ws.send_str("x")),
                           ("send_bytes", lambda ws: ws.send_bytes(b"x")),
                           ("send_json", lambda ws: ws.send_json({"a": 1})),
                           ("ping", lambda ws: ws.ping(b"x"))):
            with self.subTest(method=name):
                self._assert_mapped(name, lambda: call(socket_))

    def test_close_maps_the_native_error(self):
        socket_ = self._terminal_sync_socket()

        def close_again():
            socket_._closed = False
            socket_.close()

        self._assert_mapped("close", close_again)

    def test_terminate_maps_the_native_error(self):
        socket_ = self._terminal_sync_socket()

        def terminate_again():
            socket_._closed = False
            socket_.terminate()

        self._assert_mapped("terminate", terminate_again)

    def test_a_failed_terminate_does_not_fake_a_closed_state(self):
        """The Core is asked first; only success may set ``_closed``."""
        socket_ = self._terminal_sync_socket()
        socket_._closed = False
        with self.assertRaises(chrome_client.RequestException):
            socket_.terminate()
        self.assertFalse(socket_._closed)

    def test_a_successful_terminate_marks_the_socket_closed(self):
        socket_ = chrome_client.WebSocket(url=self.server.url)
        socket_.recv_str()
        socket_.terminate()
        self.assertTrue(socket_.closed)

    def test_the_async_socket_maps_the_same_errors(self):
        async def open_terminal_socket():
            async with chrome_client.AsyncSession() as session:
                socket_ = await session.websocket(self.server.url)
                await socket_.recv_str()
                await socket_.terminate()
                await asyncio.sleep(self.SETTLE)
                return socket_

        socket_ = self.run_async(open_terminal_socket())

        def attempt(name, call):
            socket_._closed = False
            try:
                self.run_async(call())
            except chrome_client.RequestException:
                return
            except RuntimeError as error:
                self.fail("%s leaked a bare %s: %r"
                          % (name, type(error).__name__, error))
            self.fail("%s did not fail on a terminal socket" % name)

        for name, call in (("send", lambda: socket_.send("x")),
                           ("send_str", lambda: socket_.send_str("x")),
                           ("send_bytes", lambda: socket_.send_bytes(b"x")),
                           ("ping", lambda: socket_.ping(b"x"))):
            with self.subTest(method=name):
                attempt(name, call)

        attempt("close", lambda: socket_.close())
        attempt("terminate", lambda: socket_.terminate())

    def test_a_failed_async_terminate_does_not_fake_a_closed_state(self):
        async def open_terminal_socket():
            async with chrome_client.AsyncSession() as session:
                socket_ = await session.websocket(self.server.url)
                await socket_.recv_str()
                await socket_.terminate()
                await asyncio.sleep(self.SETTLE)
                return socket_

        socket_ = self.run_async(open_terminal_socket())
        socket_._closed = False
        with self.assertRaises(chrome_client.RequestException):
            self.run_async(socket_.terminate())
        self.assertFalse(socket_._closed)


class PickledResponseSurfaceTests(Base):
    """A restored response must expose every attribute a fresh one does.

    ``__attrs__`` is the serialisable payload, not the whole object: live
    handles are deliberately left out. Before this was enforced, ``__setstate__``
    restored only ``__attrs__``, so ``.text``/``.charset`` raised AttributeError
    on ``default_encoding`` and ``http_version``/``status_line``/``infos``/
    ``redirect_count``/``redirect_url``/``history_headers`` were absent -- even
    though the stub declares all of them. Comparing against a fresh instance
    rather than a hardcoded list means a field added later cannot silently
    reintroduce the gap.
    """

    def test_unpickled_response_has_the_same_attributes_as_a_fresh_one(self):
        import pickle

        with chrome_client.Session() as session:
            response = session.get(self.url)
            restored = pickle.loads(pickle.dumps(response))

        fresh = type(restored)()
        missing = sorted(set(fresh.__dict__) - set(restored.__dict__))
        self.assertEqual(
            [], missing,
            "pickle round-trip dropped attributes a fresh instance has: %s"
            % (missing,))
        # The three surfaces that used to raise, named so a failure says what
        # a caller would have hit.
        self.assertIsInstance(restored.text, str)
        self.assertEqual(restored.charset, "utf-8")
        self.assertIsNone(restored.http_version)


class WhitespaceStatusLineTests(unittest.TestCase):
    """A status line with no token yields None instead of raising.

    The guard used to test only for the empty string, so a line of spaces or
    tabs reached ``split(None, 1)[0]`` on an empty list and raised IndexError.
    ``reason_from_status_line`` was always safe because it length-checks; this
    is the same shape.
    """

    def test_whitespace_only_lines_return_none(self):
        from chrome_client._python_impl.models import http_version_from_status_line

        for line in ("", " ", "   ", "\t", "\t\n", " \r\n "):
            with self.subTest(line=line):
                self.assertIsNone(http_version_from_status_line(line))

    def test_a_real_status_line_still_maps(self):
        from chrome_client._python_impl.models import http_version_from_status_line

        self.assertEqual(http_version_from_status_line("HTTP/2 200"), "HTTP/2")
        self.assertEqual(http_version_from_status_line("HTTP/1.1 200 OK"), "HTTP/1.1")

if __name__ == "__main__":
    unittest.main()
