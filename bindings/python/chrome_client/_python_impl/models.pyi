"""Type surface of :mod:`chrome_client.models`.

``Response`` carries the requests surface (``reason``, ``history``, ``elapsed``,
``cookies``, ``links``, ``apparent_encoding``) plus the curl_cffi additions
(``http_version``, ``charset``, ``redirect_count``, ``infos``).  Anything ABI v8
cannot report is derived here and documented rather than faked: the Core sends a
numeric status with no reason phrase, so ``reason`` comes from the standard table.

The streaming methods are overloaded on ``decode_unicode`` so an IDE narrows
``for chunk in response.iter_content(n)`` to ``bytes`` and
``iter_content(n, decode_unicode=True)`` to ``str``.
"""

import datetime
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Tuple,
    Union,
)

from typing_extensions import Literal, Never, TypeAlias, overload

from ._types import Body, CookiesLike, HeadersLike, Hooks, ParamsLike, StrOrBytes
from .cookies import RequestsCookieJar
from .structures import Headers

#: Chunk size ``Response.iter_content`` uses when the caller passes ``None``.
CONTENT_CHUNK_SIZE: int
#: Chunk size ``Response.iter_lines`` uses by default.
ITER_CHUNK_SIZE: int
#: Statuses that carry a redirect, as requests defines them.
REDIRECT_STATI: Tuple[int, ...]
#: requests' own redirect cap; Chromium's internal cap is 20.
DEFAULT_REDIRECT_LIMIT: int

#: A status line and its headers, as parsed out of the Core's raw header block.
RawHeaderBlock: TypeAlias = Union[bytes, bytearray, memoryview, str]
#: ``(url, headers)`` for one redirect hop.
HistoryHeaders: TypeAlias = List[Tuple[str, Headers]]
#: One parsed ``Link`` header entry.
HeaderLink: TypeAlias = Dict[str, str]


def parse_raw_headers(
    raw: RawHeaderBlock, encoding: str = ...
) -> Tuple[str, Headers]:
    """Parses the Core's raw header block into ``(status_line, Headers)``.

    The Core hands over Chromium's ``raw_headers()`` with NULs turned into
    newlines, so the first line is the status line and duplicate fields arrive as
    separate lines -- which is why ``Set-Cookie`` survives intact.

    Args:
        raw: The raw header block, bytes or str.
        encoding: Codec used to decode bytes; ``iso-8859-1`` by default so no
            byte is ever lost.

    Returns:
        ``(status_line, headers)``. ``status_line`` is ``""`` when the block has
        none, which happens for a manually finished request.

    Example:
        >>> status_line, headers = parse_raw_headers(
        ...     b"HTTP/1.1 302 Found\\x00Location: /next\\x00")
        >>> status_line, headers["location"]
        ('HTTP/1.1 302 Found', '/next')
    """
    ...


def reason_from_status_line(status_line: str, status_code: int) -> str:
    """Returns the reason phrase, falling back to the standard table.

    Args:
        status_line: The status line the Core reported, possibly empty.
        status_code: The numeric status, used when the line carries no phrase.

    Returns:
        The server's own phrase when present, otherwise the standard one, and
        ``""`` for a code with no known phrase.

    Example:
        >>> reason_from_status_line("", 404)
        'Not Found'
        >>> reason_from_status_line("HTTP/1.1 200 All Good", 200)
        'All Good'
    """
    ...


def http_version_from_status_line(status_line: str) -> Optional[str]:
    """Maps the status line's protocol token onto a display name.

    Args:
        status_line: e.g. ``"HTTP/2 200"``.

    Returns:
        ``"HTTP/1.0"``, ``"HTTP/1.1"``, ``"HTTP/2"``, ``"HTTP/3"``, or ``None``
        when the line is empty.

    Example:
        >>> http_version_from_status_line("HTTP/2 200")
        'HTTP/2'
    """
    ...


def build_url(url: StrOrBytes, params: Optional[ParamsLike], encoding: str = ..., quote: Optional[bool] = ...) -> str:
    """Appends ``params`` to ``url`` with requests' encoding rules.

    Args:
        url: The URL, as str or bytes. Opaque schemes (``mailto:``, ``data:``)
            are passed through untouched so the Core rejects them with its own
            message.
        params: Query parameters to append.
        encoding: Codec used to encode parameter values.
        quote: ``False`` skips the percent-encoding pass, matching curl_cffi's
            escape hatch for URLs that must reach the wire exactly as given.

    Returns:
        The rebuilt, percent-encoded URL.

    Raises:
        URLRequired: ``url`` is empty.
        MissingSchema: ``url`` has no scheme.
        InvalidURL: ``url`` has no host.

    Example:
        >>> build_url("https://example.com/search", {"q": "a b"})
        'https://example.com/search?q=a%20b'
    """
    ...


class Request:
    """User-facing request description, as in ``requests.models.Request``.

    Attributes:
        method: The HTTP method, upper-cased by :meth:`prepare`.
        url: The target URL.
        headers: Headers, merged with the session's when prepared.
        files: Multipart file parts.
        data: Form body.
        params: Query parameters.
        auth: Auth callable, ``AuthBase`` or 2-tuple.
        cookies: Cookies for this request.
        json: JSON body.
        hooks: ``{"response": [...]}`` hooks.
    """

    method: Optional[str]
    url: Optional[str]
    headers: Optional[HeadersLike]
    files: Any
    data: Any
    params: Any
    auth: Any
    cookies: Optional[CookiesLike]
    json: Any
    hooks: Dict[str, List[Callable[[Any], Any]]]

    def __init__(
        self,
        method: Optional[str] = None,
        url: Optional[str] = None,
        headers: Optional[HeadersLike] = None,
        files: Any = None,
        data: Any = None,
        params: Any = None,
        auth: Any = None,
        cookies: Optional[CookiesLike] = None,
        hooks: Optional[Hooks] = None,
        json: Any = None,
    ) -> None:
        """Describes a request that has not been prepared yet.

        Args:
            method: HTTP method, e.g. ``"GET"``.
            url: Target URL; may be relative when the session has ``base_url``.
            headers: Headers for this request.
            files: Multipart file parts.
            data: Form or raw body.
            params: Query parameters.
            auth: Auth callable, ``AuthBase`` or ``(user, password)``.
            cookies: Cookies for this request.
            hooks: ``{"response": callable}``.
            json: JSON body.

        Example:
            >>> request = Request(method="GET", url="https://example.com")
            >>> request.prepare().url
            'https://example.com/'
        """
        ...

    def __repr__(self) -> str:
        """Returns ``"<Request [GET]>"``.

        Returns:
            The repr string."""
        ...

    def register_hook(self, event: str, hook: Union[Callable[[Any], Any], Iterable[Callable[[Any], Any]]]) -> None:
        """Registers a hook for an event.

        Args:
            event: Event name; only ``"response"`` exists.
            hook: One callable, or an iterable of them (non-callables are
                skipped).

        Raises:
            ValueError: ``event`` is not ``"response"``.
        """
        ...

    def deregister_hook(self, event: str, hook: Callable[[Any], Any]) -> bool:
        """Removes a previously registered hook.

        Args:
            event: The event the hook was registered under.
            hook: The callable to remove.

        Returns:
            ``True`` when the hook was present and removed.
        """
        ...

    def prepare(self) -> "PreparedRequest":
        """Builds the wire-ready request.

        Returns:
            A :class:`PreparedRequest` with the URL encoded, the body serialised
            and ``Content-Length`` set.

        Example:
            >>> Request(method="GET", url="https://example.com").prepare().path_url
            '/'
        """
        ...


class PreparedRequest:
    """The wire-ready request the Core is handed.

    ``body`` is either ``bytes`` (fixed upload) or a chunk iterator/file object,
    in which case ``stream_body`` is set and the Core uploads chunked.

    Attributes:
        method: Upper-cased method.
        url: Fully encoded URL.
        headers: A :class:`~chrome_client.Headers`, duplicate-preserving.
        body: The serialised body.
        hooks: Response hooks.
        stream_body: ``True`` when the body must be uploaded chunk by chunk.
        header_order: Emission order for headers, when a caller pinned one.
        quote: ``False`` disables percent-encoding the URL.
    """

    method: Optional[str]
    url: Optional[str]
    headers: Optional[Headers]
    body: Any
    hooks: Dict[str, List[Callable[[Any], Any]]]
    stream_body: bool
    header_order: Optional[List[str]]
    quote: Optional[bool]

    def __init__(self) -> None:
        """Creates an empty prepared request; fill it with :meth:`prepare`."""
        ...

    def __repr__(self) -> str:
        """Returns ``"<PreparedRequest [GET]>"``.

        Returns:
            The repr string."""
        ...

    def copy(self) -> "PreparedRequest":
        """Returns a shallow copy, used when following a redirect.

        Returns:
            A new request sharing the same ``body`` (so a streamed upload is not
            re-read) with copied headers and cookie jar.

        Example:
            >>> clone = prepared.copy()
            >>> clone.url = "https://elsewhere.example/"
        """
        ...

    def prepare(
        self,
        method: Optional[str] = None,
        url: Optional[str] = None,
        headers: Optional[HeadersLike] = None,
        files: Any = None,
        data: Optional[Body] = None,
        params: Optional[ParamsLike] = None,
        auth: Any = None,
        cookies: Optional[CookiesLike] = None,
        hooks: Optional[Hooks] = None,
        json: Any = None,
    ) -> None:
        """Fills in the request: method, URL, headers, cookies, body and auth.

        Args:
            method: HTTP method; upper-cased.
            url: Target URL; encoded with ``params`` appended.
            headers: Headers; ``None`` values are dropped.
            files: Multipart parts.
            data: Form or raw body.
            params: Query parameters.
            auth: Auth to apply.
            cookies: Cookies merged into the jar.
            hooks: Response hooks.
            json: JSON body, used when ``data`` is empty.
        """
        ...

    def prepare_method(self, method: Optional[str]) -> None:
        """Sets the upper-cased method.

        Args:
            method: The method, or ``None`` to leave it unset.
        """
        ...

    def prepare_url(self, url: Optional[str], params: Optional[ParamsLike]) -> None:
        """Encodes the URL and appends ``params``.

        Args:
            url: Target URL.
            params: Query parameters.

        Raises:
            URLRequired: ``url`` is empty.
            MissingSchema: ``url`` has no scheme.
            InvalidURL: ``url`` has no host.
        """
        ...

    def prepare_headers(self, headers: Optional[HeadersLike]) -> None:
        """Normalises headers into a duplicate-preserving mapping.

        Args:
            headers: A mapping, an iterable of pairs, or another
                :class:`~chrome_client.Headers`; ``None`` values are dropped.
        """
        ...

    def prepare_body(self, data: Optional[Body], files: Any, json: Any = None) -> None:
        """Serialises the body and sets ``Content-Type``/``Content-Length``.

        Args:
            data: Form data, raw bytes, or a file/iterator for chunked upload.
            files: Multipart parts; implies ``multipart/form-data``.
            json: JSON body, used only when ``data`` is empty.
        """
        ...

    def prepare_content_length(self, body: Any) -> None:
        """Sets ``Content-Length``, or ``Transfer-Encoding: chunked``.

        Args:
            body: The serialised body, or ``None``. A streamed body gets
                ``Transfer-Encoding: chunked`` and no length.
        """
        ...

    def prepare_auth(self, auth: Any, url: str = ...) -> None:
        """Applies an auth callable, expanding a 2-tuple to ``HTTPBasicAuth``.

        Args:
            auth: ``None``, a callable, an ``AuthBase``, or a
                ``(username, password)`` tuple.
            url: The URL the auth applies to.

        Raises:
            TypeError: ``auth`` is neither a 2-tuple nor callable.
        """
        ...

    def prepare_cookies(self, cookies: Optional[CookiesLike]) -> None:
        """Stores the cookie jar for this request.

        Args:
            cookies: A :class:`~chrome_client.RequestsCookieJar`, a mapping, or
                ``None``.
        """
        ...

    def prepare_hooks(self, hooks: Optional[Hooks]) -> None:
        """Registers the response hooks.

        Args:
            hooks: ``{"response": callable}``, a callable, or an iterable of
                callables.
        """
        ...

    @property
    def path_url(self) -> str:
        """Path plus query, as requests exposes it.

        Returns:
            e.g. ``"/search?q=a"``; ``"/"`` when the URL has an empty path.

        Example:
            >>> prepared.path_url
            '/api?page=1'
        """
        ...

    def wire_headers(self) -> List[Tuple[str, str]]:
        """Header pairs in emission order.

        ``header_order`` only reorders what the caller already supplied; it never
        invents a header, because the Chromium profile owns the default set and
        their ordering.

        Returns:
            ``[(name, value), ...]`` including duplicates.

        Example:
            >>> prepared.wire_headers()
            [('Accept', 'application/json')]
        """
        ...


class RawStream:
    """Minimal file-like view over a streaming body.

    ``requests`` exposes ``response.raw`` as a urllib3 ``HTTPResponse``.  There is
    no urllib3 here, so this provides the part callers actually use -- ``read``,
    ``stream`` and ``close`` -- over the Core's body stream.  urllib3-specific
    attributes are absent rather than faked: this is deliberately *not* an
    ``IO[bytes]``, so nothing here promises ``seek``, ``write`` or a real
    ``fileno``.

    Attributes:
        decode_content: Accepted for urllib3 parity; decompression already
            happened inside Chromium, so it changes nothing.
    """

    decode_content: bool

    def __init__(self, response: "Response") -> None:
        """Wraps a response's body stream.

        Args:
            response: The response whose body to expose.
        """
        ...

    def read(self, amt: Optional[int] = None, decode_content: Optional[bool] = None, cache_content: bool = ...) -> bytes:
        """Reads up to ``amt`` bytes, or the whole remaining body.

        Args:
            amt: Byte count, or ``None`` for ``read()``-to-EOF semantics, which
                also closes the stream.
            decode_content: Accepted for urllib3 parity and ignored.
            cache_content: Accepted for urllib3 parity and ignored.

        Returns:
            The bytes read; ``b""`` once closed.

        Example:
            >>> chunk = response.raw.read(8192)
        """
        ...

    def stream(self, amt: int = ..., decode_content: Optional[bool] = None) -> Iterator[bytes]:
        """Yields the body in ``amt``-sized blocks.

        Args:
            amt: Block size.
            decode_content: Accepted for urllib3 parity and ignored.

        Yields:
            Non-empty byte blocks until the body is exhausted.
        """
        ...

    def readinto(self, target: Any) -> int:
        """Reads into a pre-allocated buffer.

        Args:
            target: A writable buffer supporting slice assignment.

        Returns:
            The number of bytes written.
        """
        ...

    def readable(self) -> bool:
        """Reports that the stream can be read.

        Returns:
            Always ``True``."""
        ...
    def writable(self) -> bool:
        """Reports that the stream cannot be written.

        Returns:
            Always ``False``."""
        ...
    def seekable(self) -> bool:
        """Reports that the stream cannot be rewound.

        Returns:
            Always ``False``; the Core body stream is forward-only."""
        ...

    def tell(self) -> Never:
        """Always raises: the Core body stream cannot be rewound.

        Raises:
            IOError: Always. Consume the body forward-only with ``read`` and
                ``iter_content``, or buffer it into ``io.BytesIO`` first.
        """
        ...

    @property
    def closed(self) -> bool:
        """Reports whether the body has been fully read or closed.

        Returns:
            ``True`` once :meth:`read` reached EOF or :meth:`close` ran."""
        ...

    def close(self) -> None:
        """Closes the stream, cancelling an unread native request."""
        ...


class Response:
    """HTTP response with the requests and curl_cffi read surfaces.

    Attributes:
        status_code: The numeric status.
        status_line: The raw status line, e.g. ``"HTTP/2 200"``.
        reason: The reason phrase, from the server when it sent one and from the
            standard table otherwise.
        headers: A duplicate-preserving :class:`~chrome_client.Headers`.
        cookies: The cookies this response's hops set.
        url: The final URL after redirects.
        history: One :class:`Response` per redirect hop.
        elapsed: Wall-clock time from send to body completion.
        request: The :class:`PreparedRequest` that produced it.
        encoding: The encoding the caller pinned, or ``None``.
        default_encoding: Fallback for :attr:`text`.
        apparent_encoding: Encoding guessed from a meta tag or by trial.
        charset: curl_cffi alias for the resolved encoding.
        charset_encoding: Encoding taken from ``Content-Type``.
        http_version: ``"HTTP/1.0"``, ``"HTTP/2"`` or ``"HTTP/3"`` when the ABI can
            prove it, else ``None``. ABI v8 carries no negotiated protocol and
            Chromium normalizes HTTP/2 and HTTP/3 responses onto an ``HTTP/1.1``
            status line, so ``HTTP/1.1`` is reported as ``None`` rather than
            guessed -- the same rule ``reason`` follows.
        redirect_count: Number of hops followed.
        redirect_url: The final URL when redirects were followed, else ``None``.
        next: The next-hop :class:`PreparedRequest` for an unfollowed redirect.
        infos: Reserved mapping of extra response metadata (empty).
        content: The body as bytes, read on first access when streaming.
        text: The body decoded with the resolved encoding.
        raw: A file-like view over the body.
        ok: ``True`` for any status below 400, matching requests.
        links: Parsed ``Link`` header entries, keyed by ``rel``.
        is_redirect: ``True`` for a 3xx carrying ``Location``.
        is_permanent_redirect: ``True`` for 301/308 with ``Location``.
    """

    status_code: Optional[int]
    status_line: str
    reason: Optional[str]
    headers: Headers
    cookies: RequestsCookieJar
    url: Optional[str]
    history: List["Response"]
    elapsed: datetime.timedelta
    request: Optional[PreparedRequest]
    encoding: Optional[str]
    default_encoding: Union[str, Callable[[bytes], str]]
    http_version: Optional[str]
    redirect_count: int
    redirect_url: Optional[str]
    next: Optional[PreparedRequest]
    infos: Dict[str, Any]
    #: ``(url, Headers)`` per redirect hop, used to mirror per-hop cookies.
    history_headers: HistoryHeaders

    def __init__(self) -> None:
        """Creates an empty response; sessions fill it in as the request runs."""
        ...

    def __repr__(self) -> str:
        """Returns ``"<Response [200]>"``.

        Returns:
            The repr string."""
        ...
    def __bool__(self) -> bool:
        """Reports success, so ``if response:`` means ``if response.ok:``.

        Returns:
            ``True`` when the status is below 400."""
        ...
    def __iter__(self) -> Iterator[bytes]:
        """Iterates the body in 128-byte chunks, as requests does.

        Yields:
            The body in 128-byte blocks."""
        ...
    def __enter__(self) -> "Response":
        """Returns the response, so it can be used as a context manager.

        Returns:
            This response."""
        ...
    def __exit__(
        self,
        exc_type: Optional[type] = ...,
        exc: Optional[BaseException] = ...,
        tb: Any = ...,
    ) -> None:
        """Closes the response on the way out.

        Args:
            exc_type: Exception type, if any.
            exc: Exception instance, if any.
            tb: Traceback, if any."""
        ...
    def __getstate__(self) -> Dict[str, Any]:
        """Returns the picklable state, reading the body first when needed.

        Returns:
            ``{attribute: value}`` for the documented attributes."""
        ...
    def __setstate__(self, state: Mapping[str, Any]) -> None:
        """Restores state written by :meth:`__getstate__` on a reopened response.

        Args:
            state: The mapping produced by :meth:`__getstate__`."""
        ...

    @property
    def ok(self) -> bool:
        """``True`` when the status is below 400.

        Uses requests' semantics, not curl_cffi's 200--399.

        Returns:
            ``True`` for 1xx--3xx, ``False`` for 4xx and 5xx.

        Example:
            >>> response.ok
            True"""
        ...

    @property
    def raw(self) -> RawStream:
        """File-like view over the body, as ``requests`` exposes it.

        Returns:
            A :class:`RawStream`; ``read()``, ``stream()`` and ``close()`` are
            available, urllib3 internals are not."""
        ...

    @raw.setter
    def raw(self, value: Any) -> None: ...

    @property
    def is_redirect(self) -> bool:
        """Reports whether the response is a followable redirect.

        Returns:
            ``True`` when the status is 301/302/303/307/308 and ``Location`` is
            set."""
        ...

    @property
    def is_permanent_redirect(self) -> bool:
        """Reports whether the redirect is permanent.

        Returns:
            ``True`` for a 301 or 308 carrying ``Location``."""
        ...

    @property
    def apparent_encoding(self) -> Optional[str]:
        """Encoding guessed from the body, matching requests' fallback order.

        Returns:
            A charset declared in a ``<meta>`` tag, else the first of
            utf-8/gb18030/big5/shift_jis/euc-kr that decodes, else
            ``iso-8859-1``; ``None`` when the body has not been read.
        """
        ...

    @property
    def charset(self) -> str:
        """curl_cffi alias for the resolved response encoding.

        Returns:
            ``encoding``, else the ``Content-Type`` charset, else
            ``default_encoding``."""
        ...

    @property
    def charset_encoding(self) -> Optional[str]:
        """Returns the encoding taken from the ``Content-Type`` header.

        Returns:
            The charset name, ``"utf-8"`` for a JSON content type, or ``None``."""
        ...

    @property
    def content(self) -> bytes:
        """Returns the body as bytes.

        Reading it on a streaming response drains the native request, so
        ``iter_content`` cannot be used afterwards. Accessing it twice is free.

        Returns:
            The complete body."""
        ...

    @content.setter
    def content(self, value: Any) -> None: ...

    @property
    def text(self) -> str:
        """Returns the body decoded with the resolved encoding.

        Order: ``encoding``, the ``Content-Type`` charset, ``apparent_encoding``,
        then ``default_encoding``. Decoding errors are replaced, never raised.

        Returns:
            The decoded body; ``""`` when the body is empty.

        Example:
            >>> response.text[:40]
            '{"page": 1, "items": [...]}'"""
        ...

    def json(self, **kwargs: Any) -> Any:
        """Parses the body as JSON.

        Args:
            **kwargs: Forwarded to :func:`json.loads`.

        Returns:
            The decoded JSON value.

        Raises:
            JSONDecodeError: The body is empty or is not valid JSON. The class
                subclasses both :class:`~chrome_client.InvalidJSONError` and
                ``ValueError``.

        Example:
            >>> response.json()["items"]
            [...]
        """
        ...

    @property
    def links(self) -> Dict[str, HeaderLink]:
        """Returns the parsed ``Link`` header entries.

        Returns:
            ``{rel: {"url": ..., ...}}``; empty when the header is absent.

        Example:
            >>> response.links["next"]["url"]
            'https://example.com/page/2'"""
        ...

    def raise_for_status(self) -> "Response":
        """Raises for a 4xx or 5xx status, as requests does.

        Returns:
            The response itself, so the call can be chained.

        Raises:
            HTTPError: The status is 400--599. The message names the status, the
                reason and the URL, and the exception carries ``.response``.

        Example:
            >>> response.raise_for_status()
        """
        ...

    @overload
    def iter_content(self, chunk_size: Optional[int] = ..., decode_unicode: Literal[False] = ...) -> Iterator[bytes]:
        """Iterates the body in chunks, streaming when ``stream=True`` was used.

        Args:
            chunk_size: Bytes per yielded block. ``None`` means
                :data:`CONTENT_CHUNK_SIZE`. The value is not a hard guarantee
                when the underlying reader yields larger blocks.
            decode_unicode: Decode with the response encoding and yield ``str``.
                Multi-byte characters split across chunks are carried over
                correctly.

        Yields:
            ``bytes``, or ``str`` when ``decode_unicode`` is ``True``.

        Raises:
            TypeError: ``chunk_size`` is not an int or ``None``.
            ValueError: ``chunk_size`` is not positive.
            StreamConsumedError: The body was already consumed by another
                reader.

        Example:
            >>> for chunk in response.iter_content(64 * 1024):
            ...     ...
        """
    @overload
    def iter_content(self, chunk_size: Optional[int] = ..., decode_unicode: Literal[True] = ...) -> Iterator[str]: ...

    @overload
    def iter_lines(
        self, chunk_size: int = ..., decode_unicode: Literal[False] = ..., delimiter: Optional[bytes] = ...
    ) -> Iterator[bytes]:
        """Iterates the body line by line.

        Args:
            chunk_size: Bytes read per underlying chunk; it does not bound the
                yielded line length.
            decode_unicode: Decode with the response encoding and yield ``str``.
            delimiter: Explicit separator. Defaults to ``splitlines()``
                semantics, so ``\r\n``, ``\r`` and ``\n`` are all handled.

        Yields:
            One line per iteration, without the terminator. A trailing partial
            line is yielded when the body ends.

        Example:
            >>> for line in response.iter_lines():
            ...     print(line)
        """
    @overload
    def iter_lines(
        self, chunk_size: int = ..., decode_unicode: Literal[True] = ..., delimiter: Optional[str] = ...
    ) -> Iterator[str]: ...

    def close(self) -> None:
        """Releases the native request.

        Cancels an unfinished native request, so closing a partially read
        ``stream=True`` response does not leak a paused body queue.
        """
        ...


class AsyncResponse(Response):
    """Response whose streaming body is consumed with ``async for``.

    Everything :class:`Response` offers is here too; the ``a*`` methods are the
    awaitable counterparts.  Awaiting ``acontent()`` is what drains the stream,
    and ``aiter_content()`` consumes it incrementally.
    """

    async def acontent(self) -> bytes:
        """Drains and returns the body as bytes.

        Returns:
            The full body. Idempotent: after the first call the buffered content
            is returned.

        Example:
            >>> body = await response.acontent()
        """
        ...

    async def atext(self) -> str:
        """Drains the body and decodes it.

        Returns:
            The body decoded with the resolved encoding.

        Example:
            >>> text = await response.atext()
        """
        ...

    async def ajson(self, **kwargs: Any) -> Any:
        """Drains the body and parses it as JSON.

        Args:
            **kwargs: Forwarded to :func:`json.loads`.

        Returns:
            The decoded JSON value.

        Raises:
            JSONDecodeError: The body is empty or not valid JSON.

        Example:
            >>> payload = await response.ajson()
        """
        ...

    @overload
    def aiter_content(self, chunk_size: Optional[int] = ..., decode_unicode: Literal[False] = ...) -> AsyncIterator[bytes]:
        """Iterates the body in chunks without blocking the loop.

        Args:
            chunk_size: Bytes per yielded block; ``None`` means
                :data:`CONTENT_CHUNK_SIZE`.
            decode_unicode: Decode with the response encoding and yield ``str``.

        Yields:
            ``bytes``, or ``str`` when ``decode_unicode`` is ``True``.

        Example:
            >>> async for chunk in response.aiter_content(65536):
            ...     ...
        """
    @overload
    def aiter_content(self, chunk_size: Optional[int] = ..., decode_unicode: Literal[True] = ...) -> AsyncIterator[str]: ...

    #: curl_cffi spells the byte stream ``aiter_content``; earlier releases of
    #: this package spelled it ``aiter_bytes``. Both are kept.
    @overload
    def aiter_bytes(self, chunk_size: Optional[int] = ..., decode_unicode: Literal[False] = ...) -> AsyncIterator[bytes]:
        """Alias of :meth:`aiter_content`, the spelling earlier releases used.

        Args:
            chunk_size: Bytes per yielded block; ``None`` means
                :data:`CONTENT_CHUNK_SIZE`.
            decode_unicode: Decode with the response encoding and yield ``str``.

        Yields:
            ``bytes``, or ``str`` when ``decode_unicode`` is ``True``.
        """
    @overload
    def aiter_bytes(self, chunk_size: Optional[int] = ..., decode_unicode: Literal[True] = ...) -> AsyncIterator[str]: ...

    @overload
    def aiter_lines(
        self, chunk_size: Optional[int] = ..., decode_unicode: Literal[False] = ..., delimiter: Optional[bytes] = ...
    ) -> AsyncIterator[bytes]:
        """Iterates the body line by line.

        Args:
            chunk_size: Bytes read per underlying chunk.
            decode_unicode: Decode with the response encoding and yield ``str``.
            delimiter: Explicit separator; defaults to ``splitlines()``.

        Yields:
            One line per iteration, without the terminator.

        Example:
            >>> async for line in response.aiter_lines():
            ...     print(line)
        """
    @overload
    def aiter_lines(
        self, chunk_size: Optional[int] = ..., decode_unicode: Literal[True] = ..., delimiter: Optional[str] = ...
    ) -> AsyncIterator[str]: ...

    async def aclose(self) -> None:
        """Releases the native request, cancelling it if unread.

        Awaitable counterpart of :meth:`Response.close`.
        """
        ...
