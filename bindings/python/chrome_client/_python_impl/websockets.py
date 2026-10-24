"""WebSocket wrappers.

Both classes expose the curl_cffi method names (``send_str``, ``recv_str``,
``send_json``, ``recv_json``, ``ping``, ``terminate``) and the shorter
``send``/``recv`` pair this package shipped earlier.
"""

import asyncio
import json as _json
import time
from collections import deque

from .exceptions import (WebSocketClosed, WebSocketError, WebSocketTimeout,
                         map_native_error, name_net_error)

#: Close code the ``close()`` default sends, matching ``WsCloseCode.OK``.
OK = 1000

#: Bounds on the queue an async socket buffers before it refuses more frames.
MAX_QUEUED_EVENTS = 1024
MAX_QUEUED_BYTES = 4 * 1024 * 1024


def _native(method, *args):
    """Calls one Core socket method, mapping a native failure onto the hierarchy.

    PyO3 surfaces a Core error as a bare ``RuntimeError`` (``'InvalidState'`` for a
    terminal socket). Only ``recv()`` mapped it, so ``send``/``close``/``ping``/``terminate``
    leaked that bare error past every ``except RequestException`` a caller writes.
    Everything that crosses the ABI goes through here.
    """
    try:
        return method(*args)
    except RuntimeError as error:
        raise map_native_error(error)


class WsCloseCode(object):
    OK = 1000
    GOING_AWAY = 1001
    PROTOCOL_ERROR = 1002
    UNSUPPORTED = 1003
    NO_STATUS = 1005
    ABNORMAL = 1006
    INVALID_DATA = 1007
    POLICY_VIOLATION = 1008
    TOO_LARGE = 1009
    MISSING_EXTENSION = 1010
    INTERNAL_ERROR = 1011
    SERVICE_RESTART = 1012
    TRY_AGAIN_LATER = 1013
    BAD_GATEWAY = 1014
    TLS_HANDSHAKE_ERROR = 1015


class CurlWsFrame(object):
    """Frame-type flags reported alongside a received payload."""

    TEXT = "text"
    BINARY = "binary"
    CLOSE = "closed"
    PING = "ping"


class WebSocket(object):
    """Blocking WebSocket."""

    def __init__(self, socket=None, url=None, session=None, impersonate=None,
                 proxy=None, proxies=None, verify=True, origin="", headers=None,
                 timeout=None, protocols=None, await_open=False):
        self._owned_session = None
        self._opened = False
        self._pending = deque()
        if socket is None:
            if url is None:
                raise ValueError("WebSocket needs either a native socket or a url")
            from .sessions import Session
            owned = session is None
            session = session or Session(impersonate=impersonate, proxy=proxy,
                                        proxies=proxies, verify=verify)
            inner = session.websocket(url, origin, headers, timeout, proxy, proxies,
                                      impersonate, protocols, None)
            socket = inner._socket
            self._opened = inner._opened
            self._pending = inner._pending
            self._owned_session = session if owned else None
        self._socket = socket
        self._closed = False
        if await_open:
            self._await_open(timeout)

    def _await_open(self, timeout):
        """Consumes events until the handshake opens.

        `send()` and `close()` are rejected by the Core before the socket is
        open, so a synchronous constructor has to block here. Frames that arrive
        with or before the open event are queued rather than dropped.
        """
        deadline = None if not timeout else time.monotonic() + timeout
        while not self._opened:
            if deadline is not None and time.monotonic() > deadline:
                self.terminate()
                raise WebSocketTimeout("WebSocket handshake timed out")
            event = _native(self._socket.recv)
            if event is None:
                self._closed = True
                raise WebSocketClosed("WebSocket closed before the handshake completed")
            kind, data, code, error = event
            if kind == "open":
                self._opened = True
                return
            if kind == "error":
                self._closed = True
                raise WebSocketError(
                    name_net_error(error) if error else "WebSocket handshake failed")
            if kind in ("closed", "closing"):
                self._closed = True
                raise WebSocketClosed("WebSocket closed before the handshake completed",
                                      code)
            self._pending.append(event)

    def connect(self, url="", **_kwargs):
        """Present for curl_cffi parity; construction already connects."""
        return self

    # -- sending ------------------------------------------------------------
    def send(self, data):
        if isinstance(data, str):
            return _native(self._socket.send_text, data)
        return _native(self._socket.send_bytes, bytes(data))

    def send_str(self, text):
        return _native(self._socket.send_text, text)

    def send_bytes(self, data):
        return _native(self._socket.send_bytes, bytes(data))

    send_binary = send_bytes

    def send_json(self, value, dumps=_json.dumps):
        return _native(self._socket.send_text, dumps(value))

    def ping(self, payload=b""):
        # The Core answers protocol-level pings itself; an application ping is a
        # zero-length text frame, which is the closest honest equivalent.
        return self.send(payload)

    # -- receiving ----------------------------------------------------------
    def _next(self):
        # Every other call that crosses the ABI goes through _native(); recv()
        # did not, so an unconnected or already-released socket raised a bare
        # RuntimeError("WebSocket is not connected") past every
        # ``except RequestException``. Measured on a native socket built by
        # _open_websocket() but never connected: recv()/recv_str()/
        # recv_fragment() all leaked it.
        event = self._pending.popleft() if self._pending \
            else _native(self._socket.recv)
        if event is None:
            self._closed = True
            return None
        kind, data, code, error = event
        if kind == "error":
            self._closed = True
            raise WebSocketError(
                name_net_error(error) if error else "WebSocket failure")
        if kind in ("closed", "closing"):
            self._closed = True
            if kind == "closed":
                raise WebSocketClosed("WebSocket closed", code)
            return None
        return kind, data

    def recv_fragment(self):
        result = self._next()
        if result is None:
            return b"", CurlWsFrame.CLOSE
        kind, data = result
        return data, kind

    def recv(self):
        while True:
            result = self._next()
            if result is None:
                return None
            kind, data = result
            if kind == "open":
                continue
            return data.decode("utf-8", "replace") if kind == "text" else data

    def recv_str(self):
        value = self.recv()
        return value if isinstance(value, str) else (value or b"").decode("utf-8", "replace")

    def recv_bytes(self):
        value = self.recv()
        return value.encode("utf-8") if isinstance(value, str) else value

    def recv_json(self, loads=_json.loads):
        return loads(self.recv_str())

    def run_forever(self, url="", on_message=None, on_error=None, on_open=None,
                    on_close=None, **_kwargs):
        """Dispatches events to callbacks until the socket closes."""
        if on_open is not None:
            on_open(self)
        try:
            while True:
                try:
                    message = self.recv()
                except WebSocketClosed:
                    break
                except WebSocketError as error:
                    if on_error is not None:
                        on_error(self, error)
                    break
                if message is None:
                    break
                if on_message is not None:
                    on_message(self, message)
        finally:
            if on_close is not None:
                on_close(self)
            self.close()

    def close(self, code=OK, message=b""):
        if self._closed:
            return None
        reason = message.decode("utf-8", "replace") if isinstance(message, bytes) \
            else (message or "")
        # Ask the Core first, as terminate() does: setting _closed before a call
        # that can raise left this object reporting a state the native socket
        # never reached, and turned every later close() into a silent no-op, so
        # a failed close could never be retried. Measured on a terminal socket:
        # close() raised RequestException while _closed was already True.
        try:
            result = _native(self._socket.close, code, reason)
        finally:
            # The Session is released even when the close fails: the C++
            # WebSocket holds its own scoped_refptr<Engine>
            # (core/source/websocket.cc), so dropping the Python owner cannot
            # tear down a context the socket still uses.
            self._release_owned_session()
        self._closed = True
        return result

    def terminate(self):
        # The Core is asked first: marking the socket closed before a call that
        # raises left this object claiming a state the native socket never
        # reached.
        result = _native(self._socket.cancel)
        self._closed = True
        # close() released the Session this socket owns; terminate() did not,
        # so `WebSocket(url=...)` followed by terminate() leaked a whole
        # Chromium Engine. Released only after the Core accepted the cancel, so
        # a failed terminate never tears down an Engine still in use.
        self._release_owned_session()
        return result

    def _release_owned_session(self):
        """Drops the Session this socket created for itself, if any.

        A Session owns a whole Chromium ``URLRequestContext``, so a
        ``WebSocket(url=...)`` that never releases it leaks one Engine per
        socket. The native socket keeps its own reference to the context, so
        the in-flight close is unaffected.
        """
        session, self._owned_session = self._owned_session, None
        if session is not None:
            session.close()

    @property
    def closed(self):
        return self._closed or self._socket.is_finished()

    def __iter__(self):
        while True:
            try:
                message = self.recv()
            except WebSocketClosed:
                return
            if message is None:
                return
            yield message

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class AsyncWebSocket(object):
    """Asyncio WebSocket.

    The Core wakes the loop and this drains a batch of frames per wakeup, so a
    chatty socket does not cost one loop round trip per frame.
    """

    def __init__(self, socket, loop):
        self._socket = socket
        self._loop = loop
        self._events = deque()
        self._queued_bytes = 0
        self._wake = asyncio.Event()
        self._recv_active = False
        self._closed = False
        self._opened = False

    @classmethod
    async def open(cls, socket, timeout=None):
        """Starts the socket and waits for the handshake to open.

        Same reason as the synchronous path: the Core rejects `send()` and
        `close()` until the socket is open.
        """
        loop = asyncio.get_running_loop() if hasattr(asyncio, "get_running_loop") \
            else asyncio.get_event_loop()
        instance = cls(socket, loop)
        socket.start_async(loop, instance._drain)
        try:
            await instance._await_open(timeout)
        except BaseException:
            try:
                socket.cancel()
            except RuntimeError:
                pass
            raise
        return instance

    async def _await_open(self, timeout):
        deadline = None if not timeout else self._loop.time() + timeout
        while not self._opened:
            while self._events:
                kind, data, code, error = self._events[0]
                if kind == "open":
                    self._events.popleft()
                    self._opened = True
                    return
                if kind == "error":
                    self._events.popleft()
                    self._closed = True
                    raise WebSocketError(
                    name_net_error(error) if error else "WebSocket handshake failed")
                if kind in ("closed", "closing"):
                    self._events.popleft()
                    self._closed = True
                    raise WebSocketClosed(
                        "WebSocket closed before the handshake completed", code)
                # A data frame that raced ahead of the open event stays queued.
                self._opened = True
                return
            if self._closed:
                raise WebSocketClosed("WebSocket closed before the handshake completed")
            self._wake.clear()
            if deadline is None:
                await self._wake.wait()
            else:
                remaining = deadline - self._loop.time()
                if remaining <= 0:
                    raise WebSocketTimeout("WebSocket handshake timed out")
                try:
                    await asyncio.wait_for(self._wake.wait(), remaining)
                except asyncio.TimeoutError:
                    raise WebSocketTimeout("WebSocket handshake timed out")

    # ``_open`` was the historical name.
    _open = open

    def _drain(self):
        if self._closed:
            return
        try:
            events = self._socket.poll_events(64)
        except RuntimeError as error:
            self._events.append(("error", b"", None, str(error)))
            self._wake.set()
            return
        for event in events:
            self._enqueue(event)
        if len(events) == 64:
            self._loop.call_soon(self._drain)

    def _enqueue(self, event):
        if self._closed:
            return
        _kind, data, _code, _error = event
        size = len(data)
        if len(self._events) >= MAX_QUEUED_EVENTS or \
                self._queued_bytes + size > MAX_QUEUED_BYTES:
            self._closed = True
            try:
                self._socket.cancel()
            except RuntimeError:
                pass
            self._events.append(("error", b"", None,
                                 "WebSocket event buffer limit exceeded"))
        else:
            self._events.append(event)
            self._queued_bytes += size
        self._wake.set()

    async def _next(self):
        if self._recv_active:
            raise WebSocketError("concurrent recv() is not supported")
        self._recv_active = True
        try:
            while not self._events:
                if self._closed:
                    return None
                self._wake.clear()
                await self._wake.wait()
            kind, data, code, error = self._events.popleft()
            self._queued_bytes = max(0, self._queued_bytes - len(data))
        finally:
            self._recv_active = False
        if kind == "error":
            self._closed = True
            raise WebSocketError(
                name_net_error(error) if error else "WebSocket failure")
        if kind in ("closed", "closing"):
            self._closed = True
            if kind == "closed":
                raise WebSocketClosed("WebSocket closed", code)
            return None
        return kind, data

    async def recv_fragment(self):
        result = await self._next()
        if result is None:
            return b"", CurlWsFrame.CLOSE
        kind, data = result
        return data, kind

    async def recv(self):
        while True:
            result = await self._next()
            if result is None:
                return None
            kind, data = result
            if kind == "open":
                continue
            return data.decode("utf-8", "replace") if kind == "text" else data

    async def recv_str(self):
        value = await self.recv()
        return value if isinstance(value, str) else (value or b"").decode("utf-8", "replace")

    async def recv_bytes(self):
        value = await self.recv()
        return value.encode("utf-8") if isinstance(value, str) else value

    async def recv_json(self, loads=_json.loads):
        return loads(await self.recv_str())

    async def send(self, data):
        if isinstance(data, str):
            return _native(self._socket.send_text, data)
        return _native(self._socket.send_bytes, bytes(data))

    async def send_str(self, text):
        return _native(self._socket.send_text, text)

    async def send_bytes(self, data):
        return _native(self._socket.send_bytes, bytes(data))

    send_binary = send_bytes

    async def send_json(self, value, dumps=_json.dumps):
        return _native(self._socket.send_text, dumps(value))

    async def ping(self, payload=b""):
        return await self.send(payload)

    async def close(self, code=OK, message=b""):
        if self._closed:
            return None
        # Same order as the synchronous path: the Core is asked first, so a
        # failed close leaves _closed reporting the state the socket is really
        # in and the caller can retry, instead of the socket silently claiming
        # to be closed while the Core never closed it.
        try:
            self._socket.detach_callback()
        except RuntimeError:
            pass
        reason = message.decode("utf-8", "replace") if isinstance(message, bytes) \
            else (message or "")
        result = _native(self._socket.close, code, reason)
        self._closed = True
        self._wake.set()
        return result

    aclose = close

    async def terminate(self):
        # Same order as the synchronous path: the Core is asked first, so a
        # failure leaves _closed reporting the state the socket is really in.
        result = _native(self._socket.cancel)
        self._closed = True
        self._wake.set()
        return result

    @property
    def closed(self):
        return self._closed

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            message = await self.recv()
        except WebSocketClosed:
            raise StopAsyncIteration
        if message is None:
            raise StopAsyncIteration
        return message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        await self.close()
