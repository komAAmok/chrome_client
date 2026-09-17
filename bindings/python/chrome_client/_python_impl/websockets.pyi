"""Type surface of :mod:`chrome_client.websockets`.

Both classes expose the curl_cffi method names (``send_str``, ``recv_str``,
``send_json``, ``recv_json``, ``ping``, ``terminate``) and the shorter
``send``/``recv`` pair this package shipped earlier.

The handshake is complete by the time a constructor or ``AsyncSession.websocket``
returns: the Core rejects ``send()`` and ``close()`` on a socket that is not open
yet, so returning earlier would hand back an unusable object.  That is why
``WebSocket(...)`` can raise instead of deferring the failure.
"""

import asyncio
from typing import (
    Any,
    Callable,
    Iterator,
    Optional,
    Sequence,
    Tuple,
    Type,
    TYPE_CHECKING,
    Union,
)

from typing_extensions import Literal, TypeAlias

from ._types import HeadersLike, Proxies, Timeout, Verify
from .impersonate import Impersonate

if TYPE_CHECKING:  # cyclic: sessions imports this module at runtime
    from .sessions import Session

#: Close code ``close()`` sends by default, matching :attr:`WsCloseCode.OK`.
OK: int

#: Maximum frames an async socket buffers before it refuses more.
MAX_QUEUED_EVENTS: int
#: Maximum bytes an async socket buffers before it refuses more.
MAX_QUEUED_BYTES: int

#: Frame kinds a receive can report, matching :class:`CurlWsFrame`.
FrameKind: TypeAlias = Literal["text", "binary", "closed", "ping"]

#: One received message: text as ``str``, binary as ``bytes``.
Message: TypeAlias = Union[str, bytes]

#: Callback signatures for :meth:`WebSocket.run_forever`.
OnMessage: TypeAlias = Callable[["WebSocket", Message], Any]
OnError: TypeAlias = Callable[["WebSocket", BaseException], Any]
OnOpen: TypeAlias = Callable[["WebSocket"], Any]
OnClose: TypeAlias = Callable[["WebSocket"], Any]


class WsCloseCode:
    """WebSocket close codes, spelled as curl_cffi spells them.

    Example:
        >>> socket.close(WsCloseCode.GOING_AWAY, b"bye")
        >>> WsCloseCode.OK
        1000
    """

    OK: Literal[1000]
    GOING_AWAY: Literal[1001]
    PROTOCOL_ERROR: Literal[1002]
    UNSUPPORTED: Literal[1003]
    NO_STATUS: Literal[1005]
    ABNORMAL: Literal[1006]
    INVALID_DATA: Literal[1007]
    POLICY_VIOLATION: Literal[1008]
    TOO_LARGE: Literal[1009]
    MISSING_EXTENSION: Literal[1010]
    INTERNAL_ERROR: Literal[1011]
    SERVICE_RESTART: Literal[1012]
    TRY_AGAIN_LATER: Literal[1013]
    BAD_GATEWAY: Literal[1014]
    TLS_HANDSHAKE_ERROR: Literal[1015]


class CurlWsFrame:
    """Frame-type flags reported alongside a received payload.

    ``recv_fragment()`` returns one of these as its second element, which is how
    a caller tells a text frame from a close or ping without decoding.

    Example:
        >>> data, kind = socket.recv_fragment()
        >>> kind == CurlWsFrame.CLOSE
        False
    """

    TEXT: Literal["text"]
    BINARY: Literal["binary"]
    CLOSE: Literal["closed"]
    PING: Literal["ping"]


class WebSocket:
    """Blocking WebSocket.

    Construct it directly, or let :meth:`Session.websocket` build it.  Either way
    the handshake has completed when the constructor returns.

    Attributes:
        closed: ``True`` once the socket has been closed, terminated, or finished
            by the peer.

    Example:
        >>> with WebSocket(url="wss://echo.example.com",
        ...                impersonate="chrome_153") as socket:
        ...     socket.send_str("ping")
        ...     print(socket.recv_str())
    """

    def __init__(
        self,
        socket: Any = None,
        url: Optional[str] = None,
        session: Optional[Session] = None,
        impersonate: Optional[Impersonate] = None,
        proxy: Optional[str] = None,
        proxies: Optional[Proxies] = None,
        verify: Verify = True,
        origin: str = "",
        headers: Optional[HeadersLike] = None,
        timeout: Timeout = None,
        protocols: Optional[Sequence[str]] = None,
        await_open: bool = False,
    ) -> None:
        """Connects, or wraps an already-connected native socket.

        Args:
            socket: An existing native socket. When given, every other argument
                is ignored except ``timeout``.
            url: ``ws://`` or ``wss://`` URL to connect to. Required when
                ``socket`` is not given.
            session: An existing session to borrow. When ``None``, a private
                session is created for this socket and closed with it, so
                ``WebSocket(url=...)`` does not leak a Chromium engine.
            impersonate: Profile for the handshake; only used when this
                constructor creates the session.
            proxy: Proxy URL for the handshake.
            proxies: Proxy mapping for the handshake.
            verify: Verification setting for ``wss://``.
            origin: Handshake ``Origin``; defaults to the URL's own origin.
            headers: Extra handshake headers. ``Host``, ``Origin``,
                ``Connection``, ``Upgrade``, ``User-Agent`` and
                ``Sec-WebSocket-*`` are rejected because Chromium derives them
                itself and the UA's placement is part of the fingerprint.
            timeout: Handshake deadline.
            protocols: Subprotocols to offer in ``Sec-WebSocket-Protocol``.
            await_open: Accepted for source compatibility. The handshake is
                always awaited here; passing ``True`` simply states the intent.

        Raises:
            ValueError: Neither ``socket`` nor ``url`` was given.
            UnsupportedFeature: A forbidden handshake header was supplied.
            WebSocketTimeout: The handshake did not complete in time.
            WebSocketError: The handshake failed at the network or TLS layer.
            WebSocketClosed: The peer closed before the handshake completed.
        """
        ...

    def connect(self, url: str = "", **_kwargs: Any) -> "WebSocket":
        """Present for curl_cffi parity; construction already connects.

        Args:
            url: Ignored.
            **_kwargs: Ignored.

        Returns:
            The socket itself, so ``WebSocket(...).connect()`` chains.
        """
        ...

    # -- sending ------------------------------------------------------------
    def send(self, data: Union[str, bytes, bytearray, memoryview]) -> None:
        """Sends ``str`` as a text frame and anything else as binary.

        Args:
            data: The payload. ``str`` goes out as text; bytes-like values are
                sent as binary.

        Raises:
            WebSocketError: The socket is closed or the Core rejected the frame.

        Example:
            >>> socket.send("hello")      # text frame
            >>> socket.send(b"\\x00\\x01")  # binary frame
        """
        ...

    def send_str(self, text: str) -> None:
        """Sends a text frame.

        Args:
            text: The text payload; encoded as UTF-8 by the Core.

        Raises:
            WebSocketError: The socket is closed or the frame was rejected.
        """
        ...

    def send_bytes(self, data: Any) -> None:
        """Sends a binary frame.

        Args:
            data: The payload; anything buffer-protocol compatible.

        Raises:
            WebSocketError: The socket is closed or the frame was rejected.
        """
        ...

    def send_binary(self, data: Any) -> None:
        """Alias of :meth:`send_bytes`.

        Args:
            data: The payload.
        """
        ...

    def send_json(self, value: Any, dumps: Callable[[Any], str] = ...) -> None:
        """Serialises ``value`` and sends it as one text frame.

        Args:
            value: Any JSON-serialisable object.
            dumps: Serialiser, ``json.dumps`` by default.

        Raises:
            TypeError: ``value`` is not serialisable by ``dumps``.
            WebSocketError: The socket is closed or the frame was rejected.

        Example:
            >>> socket.send_json({"op": "ping", "id": 1})
        """
        ...

    def ping(self, payload: Any = b"") -> None:
        """Sends an application-level keepalive.

        The Core answers protocol-level pings itself, so there is no ping frame
        to send here; an application ping is a zero-length text frame, which is
        the closest honest equivalent.

        Args:
            payload: Optional text or bytes to send.
        """
        ...

    # -- receiving ----------------------------------------------------------
    def recv_fragment(self) -> Tuple[bytes, str]:
        """Receives one raw frame without decoding it.

        Returns:
            ``(data, kind)`` where ``kind`` is a :class:`CurlWsFrame` value.
            ``(b"", "closed")`` when the peer closed the socket.

        Raises:
            WebSocketError: The socket failed.
            WebSocketClosed: The peer closed the connection.

        Example:
            >>> data, kind = socket.recv_fragment()
        """
        ...

    def recv(self) -> Optional[Message]:
        """Receives one message, decoded when it is text.

        Returns:
            ``str`` for a text frame, ``bytes`` for a binary frame, and ``None``
            when the socket ended cleanly.

        Raises:
            WebSocketClosed: The peer closed the connection.
            WebSocketError: The socket failed.

        Example:
            >>> message = socket.recv()
        """
        ...

    def recv_str(self) -> str:
        """Receives one message and decodes it to text.

        Returns:
            The message as ``str``; binary payloads are decoded as UTF-8 with
            invalid sequences replaced.

        Raises:
            WebSocketClosed: The peer closed the connection.
            WebSocketError: The socket failed.
        """
        ...

    def recv_bytes(self) -> bytes:
        """Receives one message and returns it as bytes.

        Returns:
            The message as ``bytes``; text payloads are UTF-8 encoded.

        Raises:
            WebSocketClosed: The peer closed the connection.
            WebSocketError: The socket failed.
        """
        ...

    def recv_json(self, loads: Callable[[Union[str, bytes]], Any] = ...) -> Any:
        """Receives one message and parses it as JSON.

        Args:
            loads: Deserialiser, ``json.loads`` by default.

        Returns:
            The decoded value.

        Raises:
            ValueError: The payload is not valid JSON.
            WebSocketClosed: The peer closed the connection.

        Example:
            >>> socket.recv_json()["op"]
            'pong'
        """
        ...

    def run_forever(
        self,
        url: str = "",
        on_message: Optional[OnMessage] = None,
        on_error: Optional[OnError] = None,
        on_open: Optional[OnOpen] = None,
        on_close: Optional[OnClose] = None,
        **_kwargs: Any,
    ) -> None:
        """Dispatches events to callbacks until the socket closes.

        Args:
            url: Ignored; the socket is already connected.
            on_message: Called with ``(socket, message)`` per message. ``message``
                is ``str`` for text frames and ``bytes`` for binary ones.
            on_error: Called with ``(socket, error)`` when the socket fails.
            on_open: Called once with ``(socket)`` before the read loop starts.
            on_close: Called once with ``(socket)`` in a ``finally`` block; the
                socket is closed afterwards.
            **_kwargs: Ignored, for curl_cffi parity.

        Example:
            >>> socket.run_forever(
            ...     on_message=lambda ws, msg: print(msg),
            ...     on_close=lambda ws: print("closed"))
        """
        ...

    def close(self, code: int = ..., message: Union[str, bytes] = b"") -> None:
        """Closes the socket with a status code and reason.

        When this socket created its own session, that session's Chromium engine
        is released here -- which is what keeps ``WebSocket(url=...)`` from
        leaking one. Calling it twice is a no-op.

        Args:
            code: Close code, e.g. :attr:`WsCloseCode.OK`. ``1000`` by default.
            message: Reason string; bytes are decoded as UTF-8.

        Example:
            >>> socket.close(WsCloseCode.GOING_AWAY, "shutdown")
        """
        ...

    def terminate(self) -> None:
        """Cancels the socket without a close handshake.

        Use this when the peer is unresponsive: no close frame is sent, the
        connection is torn down immediately.
        """
        ...

    @property
    def closed(self) -> bool:
        """Reports whether the socket is finished.

        Returns:
            ``True`` once closed, terminated or finished by the peer."""
        ...

    def __iter__(self) -> Iterator[Message]:
        """Iterates incoming messages until the socket closes.

        Yields:
            ``str`` for text frames and ``bytes`` for binary ones."""
        ...

    def __enter__(self) -> "WebSocket":
        """Returns the socket, so ``with WebSocket(...) as socket:`` works.

        Returns:
            This socket."""
        ...
    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]] = ...,
        exc: Optional[BaseException] = ...,
        tb: Any = ...,
    ) -> None:
        """Closes the socket on the way out.

        Args:
            exc_type: Exception type, if any.
            exc: Exception instance, if any.
            tb: Traceback, if any."""
        ...


class AsyncWebSocket:
    """Asyncio WebSocket.

    The Core wakes the loop and this drains a batch of frames per wakeup, so a
    chatty socket does not cost one loop round trip per frame.  A socket that
    produces frames faster than they are consumed is capped at
    :data:`MAX_QUEUED_EVENTS` frames or :data:`MAX_QUEUED_BYTES` bytes, after
    which the connection is closed with an error rather than growing without
    bound.

    Attributes:
        closed: ``True`` once the socket has been closed or terminated.

    Example:
        >>> socket = await session.websocket("wss://echo.example.com")
        >>> async with socket:
        ...     await socket.send_json({"op": "ping"})
        ...     print(await socket.recv_json())
    """

    def __init__(self, socket: Any, loop: asyncio.AbstractEventLoop) -> None:
        """Wraps a native socket.

        Prefer :meth:`open` (or :meth:`AsyncSession.websocket`), which starts the
        socket and waits for the handshake.

        Args:
            socket: The native socket to wrap.
            loop: The running event loop the Core will wake.
        """
        ...

    @classmethod
    async def open(cls, socket: Any, timeout: Timeout = None) -> "AsyncWebSocket":
        """Starts the socket and waits for the handshake to open.

        Args:
            socket: The native socket to drive.
            timeout: Handshake deadline. On failure the native socket is
                cancelled before the exception propagates.

        Returns:
            The open :class:`AsyncWebSocket`.

        Raises:
            WebSocketTimeout: The handshake did not complete in time.
            WebSocketError: The handshake failed.
            WebSocketClosed: The peer closed before the handshake completed.
        """
        ...

    # ``_open`` was the historical name.
    _open: Any

    async def recv_fragment(self) -> Tuple[bytes, str]:
        """Receives one raw frame without decoding it.

        Returns:
            ``(data, kind)`` with ``kind`` a :class:`CurlWsFrame` value;
            ``(b"", "closed")`` when the socket ended.

        Raises:
            WebSocketError: Concurrent ``recv`` calls, or a socket failure.
            WebSocketClosed: The peer closed the connection.
        """
        ...

    async def recv(self) -> Optional[Message]:
        """Receives one message, decoded when it is text.

        Returns:
            ``str`` for text, ``bytes`` for binary, ``None`` when the socket
            ended cleanly.

        Raises:
            WebSocketError: Another ``recv`` is already awaiting, or the socket
                failed.
            WebSocketClosed: The peer closed the connection.

        Example:
            >>> message = await socket.recv()
        """
        ...

    async def recv_str(self) -> str:
        """Receives one message and decodes it to text.

        Returns:
            The message as ``str``.

        Raises:
            WebSocketClosed: The peer closed the connection.
            WebSocketError: The socket failed.
        """
        ...

    async def recv_bytes(self) -> bytes:
        """Receives one message and returns it as bytes.

        Returns:
            The message as ``bytes``.
        """
        ...

    async def recv_json(self, loads: Callable[[Union[str, bytes]], Any] = ...) -> Any:
        """Receives one message and parses it as JSON.

        Args:
            loads: Deserialiser, ``json.loads`` by default.

        Returns:
            The decoded value.

        Raises:
            ValueError: The payload is not valid JSON.
        """
        ...

    async def send(self, data: Union[str, bytes, bytearray, memoryview]) -> None:
        """Sends ``str`` as a text frame and anything else as binary.

        Args:
            data: The payload.

        Raises:
            WebSocketError: The socket is closed.
        """
        ...

    async def send_str(self, text: str) -> None:
        """Sends a text frame.

        Args:
            text: The text payload.
        """
        ...

    async def send_bytes(self, data: Any) -> None:
        """Sends a binary frame.

        Args:
            data: The payload.
        """
        ...

    async def send_binary(self, data: Any) -> None:
        """Alias of :meth:`send_bytes`.

        Args:
            data: The payload.
        """
        ...

    async def send_json(self, value: Any, dumps: Callable[[Any], str] = ...) -> None:
        """Serialises ``value`` and sends it as one text frame.

        Args:
            value: Any JSON-serialisable object.
            dumps: Serialiser, ``json.dumps`` by default.

        Raises:
            TypeError: ``value`` is not serialisable.
        """
        ...

    async def ping(self, payload: Any = b"") -> None:
        """Sends an application-level keepalive.

        Args:
            payload: Optional text or bytes to send.
        """
        ...

    async def close(self, code: int = ..., message: Union[str, bytes] = b"") -> None:
        """Closes the socket with a status code and reason.

        Args:
            code: Close code; ``1000`` by default.
            message: Reason string; bytes are decoded as UTF-8.
        """
        ...

    async def aclose(self, code: int = ..., message: Union[str, bytes] = b"") -> None:
        """Alias of :meth:`close`.

        Args:
            code: Close code.
            message: Reason string.
        """
        ...

    async def terminate(self) -> None:
        """Cancels the socket without a close handshake."""
        ...

    @property
    def closed(self) -> bool:
        """Reports whether the socket is finished.

        Returns:
            ``True`` once closed or terminated."""
        ...

    def __aiter__(self) -> "AsyncWebSocket":
        """Returns the socket itself, which is its own async iterator.

        Returns:
            This socket."""
        ...

    async def __anext__(self) -> Message:
        """Returns the next message, or ends iteration when the socket closes.

        Returns:
            The next message: ``str`` for text, ``bytes`` for binary.

        Raises:
            StopAsyncIteration: The socket closed cleanly or ended."""
        ...

    async def __aenter__(self) -> "AsyncWebSocket":
        """Returns the socket, so ``async with socket:`` works.

        Returns:
            This socket."""
        ...
    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]] = ...,
        exc: Optional[BaseException] = ...,
        tb: Any = ...,
    ) -> None:
        """Awaits :meth:`AsyncWebSocket.close` on the way out.

        Args:
            exc_type: Exception type, if any.
            exc: Exception instance, if any.
            tb: Traceback, if any."""
        ...
