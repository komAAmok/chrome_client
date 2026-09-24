"""Type surface of :mod:`chrome_client.sessions`.

One ``Session`` owns one Chromium engine per distinct configuration it is asked
for, and keeps them (see :class:`~chrome_client.engine.EngineCache`).  That is
what makes a session a session: the engine holds the connection pool, the TLS
session cache and the cookie store, so reusing it is what preserves server-side
state across requests.

The signatures below spell out every keyword each entry point accepts.  The
options the runtime forwards through ``**kwargs`` are written as ordinary named
parameters rather than hidden behind ``**kwargs``, because a ``Unpack[TypedDict]``
spelling is only understood by some IDEs -- PyCharm, for one, shows nothing but
the explicit ``url``/``params``.  Written out, every editor completes
``session.get(url, imp<tab>`` down to ``impersonate=`` and checks the value
against the :data:`~chrome_client.impersonate.Impersonate` Literal.
"""

from types import TracebackType
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    FrozenSet,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Type,
    TYPE_CHECKING,
    Union,
)

from typing_extensions import Literal, TypeAlias, overload

from ._types import (
    AuthLike,
    Body,
    CacheMode,
    ContentCallback,
    CookiesLike,
    FilesLike,
    HeadersLike,
    Hook,
    Hooks,
    ParamsLike,
    Proxies,
    ResponseClass,
    Retry,
    Timeout,
    Verify,
)
from .adapters import BaseAdapter
from .auth import AuthBase
from .cookies import RequestsCookieJar
from .impersonate import ExtraFingerprints, HttpVersion, Impersonate
from .models import AsyncResponse, PreparedRequest, Request, Response
from .multipart import CurlMime
from .structures import Headers
from .websockets import AsyncWebSocket, WebSocket

#: Events drained from the Core per event-loop wakeup.
ASYNC_POLL_BATCH: int

#: Bytes buffered per streaming async response before the reader is throttled.
STREAM_BUFFER_LIMIT: int

#: Backoff shape accepted by :class:`RetryStrategy`.
Backoff: TypeAlias = Literal["linear", "exponential"]


class RetryStrategy:
    """curl_cffi-shaped retry policy.

    Applies to transport failures (anything that raises
    :class:`~chrome_client.exceptions.RequestException`) and to the status codes
    in :attr:`codes`.  A retry re-sends the same prepared request, so it is only
    correct for idempotent calls unless the body is rewindable.

    Attributes:
        count: Maximum number of retries after the initial attempt.
        delay: Base delay in seconds.
        jitter: Maximum extra delay added at random, in seconds.
        backoff: ``"linear"`` multiplies ``delay`` by the attempt number;
            ``"exponential"`` doubles it per attempt.
        codes: Frozenset of status codes that trigger a retry.

    Example:
        >>> from chrome_client import Session, RetryStrategy
        >>> retry = RetryStrategy(count=3, delay=0.2, backoff="exponential",
        ...                       codes={429, 503})
        >>> session = Session(retry=retry)
    """

    count: int
    delay: float
    jitter: float
    backoff: Backoff
    codes: FrozenSet[int]

    def __init__(
        self,
        count: int,
        delay: float = ...,
        jitter: float = ...,
        backoff: Backoff = ...,
        codes: Iterable[int] = ...,
    ) -> None:
        """Configures the retry policy.

        Args:
            count: Retries after the first attempt. Must be ``>= 0``.
            delay: Base delay in seconds. Must be ``>= 0``.
            jitter: Random extra delay in seconds, added on top of the backoff.
                Must be ``>= 0``.
            backoff: ``"linear"`` or ``"exponential"``.
            codes: Status codes worth retrying. Defaults to
                ``(429, 500, 502, 503, 504)``.

        Raises:
            ValueError: ``count``, ``delay`` or ``jitter`` is negative, or
                ``backoff`` is neither ``"linear"`` nor ``"exponential"``.

        Example:
            >>> RetryStrategy(2, delay=0.5).sleep_for(2)
            1.0
        """
        ...

    @classmethod
    def coerce(cls, value: Optional[Union[int, "RetryStrategy"]]) -> "RetryStrategy":
        """Normalises the ``retry=`` argument.

        Args:
            value: ``None`` (no retries), an attempt count, or an existing
                strategy, which is returned unchanged.

        Returns:
            A :class:`RetryStrategy`; ``RetryStrategy(0)`` when ``value`` is
            ``None``.

        Raises:
            ValueError: The count is negative.

        Example:
            >>> RetryStrategy.coerce(3).count
            3
            >>> RetryStrategy.coerce(None).count
            0
        """
        ...

    def sleep_for(self, attempt: int) -> float:
        """Returns how long to sleep before the given attempt.

        Args:
            attempt: The attempt number that just failed (1 for the first).

        Returns:
            The backoff for that attempt plus any jitter.

        Example:
            >>> RetryStrategy(3, delay=0.5, backoff="exponential").sleep_for(3)
            2.0
        """
        ...

    def should_retry_status(self, status_code: int) -> bool:
        """Reports whether a response status is in :attr:`codes`.

        Args:
            status_code: The status of the response just received.

        Returns:
            ``True`` when the policy wants this status retried.

        Example:
            >>> RetryStrategy(1).should_retry_status(503)
            True
        """
        ...


class _StreamFlag(int):
    """``Session.stream`` as both the requests flag and curl_cffi's helper.

    ``requests`` documents ``Session.stream`` as a boolean; ``curl_cffi``
    documents ``Session.stream(method, url)`` as a streaming context manager.
    This is an ``int`` -- so it behaves as the flag in every conditional and
    comparison -- that is also callable, so both spellings work.  Only
    ``is True`` / ``is False`` identity checks would see the difference.
    """

    def __call__(
        self,
        method: str,
        url: str,
        params: Optional[ParamsLike] = None,
        data: Optional[Body] = None,
        headers: Optional[HeadersLike] = None,
        cookies: Optional[CookiesLike] = None,
        files: FilesLike = None,
        auth: AuthLike = None,
        timeout: Timeout = None,
        allow_redirects: bool = True,
        proxies: Optional[Proxies] = None,
        hooks: Optional[Hooks] = None,
        verify: Optional[Verify] = None,
        cert: Any = None,
        json: Any = None,
        content: Optional[Body] = None,
        multipart: Optional[CurlMime] = None,
        impersonate: Optional[Impersonate] = None,
        proxy: Optional[str] = None,
        http_version: Optional[HttpVersion] = None,
        max_redirects: Optional[int] = None,
        max_response_bytes: Optional[int] = None,
        referer: Optional[str] = None,
        accept_encoding: Optional[str] = None,
        default_encoding: Optional[Union[str, Callable[[bytes], str]]] = None,
        discard_cookies: Optional[bool] = None,
        retry: Retry = None,
        cache_mode: CacheMode = None,
        priority: Optional[int] = None,
        ja3: Optional[str] = None,
        akamai: Optional[str] = None,
        perk: Optional[str] = None,
        extra_fp: Optional[Union[ExtraFingerprints, Mapping[str, Any]]] = None,
        content_callback: Optional[ContentCallback] = None,
        raise_for_status: Optional[bool] = None,
        quote: Optional[bool] = None,
        curl_options: Optional[Mapping[Any, Any]] = None,
        interface: Optional[str] = None,
        doh_url: Optional[str] = None,
        max_recv_speed: Optional[int] = None,
        thread: Any = None,
        debug: Any = None,
    ) -> "_StreamContext":
        """Opens a streamed request, curl_cffi style.

        Args:
            method: HTTP method, e.g. ``"GET"``. Upper-cased before sending.
            url: Absolute URL, or a relative one when ``base_url`` is set.
            params: Query parameters appended to the URL. A mapping, a sequence of
                key/value pairs, or an already-encoded string.
            data: Request body. A mapping or sequence of pairs is form-encoded;
                bytes/str are sent as-is; a file object, generator or async iterator
                switches to chunked upload; a file-like value in a mapping is sent as
                multipart.
            headers: Headers merged with the session's defaults.
            cookies: Cookies merged with the session jar for this request only.
            files: Multipart file parts, alone or alongside ``data``.
            auth: Per-call auth: callable, ``AuthBase``, or a 2-tuple.
            timeout: One deadline or ``(connect, read)``.
            allow_redirects: ``False`` returns the first 3xx instead of following it.
            proxies: Per-call proxy mapping, merged over the session's.
            hooks: Per-call response hooks.
            verify: ``True``/``False``/CA bundle path, for this call only.
            cert: Rejected; ABI v8 has no client-certificate setting.
            json: JSON body. Implies ``Content-Type: application/json`` unless a header
                already sets it.
            content: Raw body bytes; an alternative to ``data=`` for pre-encoded
                payloads. Passing both raises ``ValueError``.
            multipart: A :class:`~chrome_client.CurlMime` instance that becomes the
                whole body.
            impersonate: Profile override for this call, either an exact
                ``chrome_<major>`` name or a curl_cffi ``chrome<major>`` alias.
            proxy: Proxy URL for this call; wins over ``proxies``.
            http_version: Pin ``"v1"``, ``"v2"`` or ``"v3"`` for this call.
            max_redirects: Redirect cap. Below Chromium's 20, hops are driven from
                Python so the caller's limit wins.
            max_response_bytes: Body ceiling for this call.
            referer: Sets ``Referer`` through Chromium's own referrer path, so it
                reaches the wire the way a real Chrome sends it.
            accept_encoding: Overrides ``Accept-Encoding``. Chromium would otherwise
                choose it as part of the profile.
            default_encoding: Fallback encoding for this response only.
            discard_cookies: Neither send nor record cookies for this call.
            retry: Attempt count or strategy for this call.
            cache_mode: Chromium load flags, e.g. ``"bypass"`` to skip the HTTP cache.
            priority: Request priority hint passed to the Core.
            ja3: Rejected; the profile owns the ClientHello.
            akamai: Rejected; the profile owns the HTTP/2 fingerprint.
            perk: Rejected; the profile owns the TLS fingerprint.
            extra_fp: Fingerprint overrides; only ``header_order`` and ``form_boundary``
                are honourable.
            content_callback: Called with the fully buffered body on non-streaming
                requests.
            raise_for_status: Check the response before returning it.
            quote: ``False`` skips the percent-encoding pass over the URL.
            curl_options: Rejected; there is no libcurl surface here.
            interface: Rejected; no interface binding in ABI v8.
            doh_url: Rejected; DoH is not exposed.
            max_recv_speed: Rejected; Chromium owns transfer pacing.
            thread: Rejected; the sync path already runs on the calling thread.
            debug: Rejected; not a supported option.

        Returns:
            A context manager yielding the :class:`~chrome_client.Response`, so
            ``iter_content`` can be consumed before the connection is released.

        Example:
            >>> with session.stream("GET", url) as response:
            ...     for chunk in response.iter_content(65536):
            ...         ...
        """
        ...


class _AsyncStreamFlag(int):
    """Asyncio form of :class:`_StreamFlag`."""

    def __call__(
        self,
        method: str,
        url: str,
        params: Optional[ParamsLike] = None,
        data: Optional[Body] = None,
        headers: Optional[HeadersLike] = None,
        cookies: Optional[CookiesLike] = None,
        files: FilesLike = None,
        auth: AuthLike = None,
        timeout: Timeout = None,
        allow_redirects: bool = True,
        proxies: Optional[Proxies] = None,
        hooks: Optional[Hooks] = None,
        verify: Optional[Verify] = None,
        cert: Any = None,
        json: Any = None,
        content: Optional[Body] = None,
        multipart: Optional[CurlMime] = None,
        impersonate: Optional[Impersonate] = None,
        proxy: Optional[str] = None,
        http_version: Optional[HttpVersion] = None,
        max_redirects: Optional[int] = None,
        max_response_bytes: Optional[int] = None,
        referer: Optional[str] = None,
        accept_encoding: Optional[str] = None,
        default_encoding: Optional[Union[str, Callable[[bytes], str]]] = None,
        discard_cookies: Optional[bool] = None,
        retry: Retry = None,
        cache_mode: CacheMode = None,
        priority: Optional[int] = None,
        ja3: Optional[str] = None,
        akamai: Optional[str] = None,
        perk: Optional[str] = None,
        extra_fp: Optional[Union[ExtraFingerprints, Mapping[str, Any]]] = None,
        content_callback: Optional[ContentCallback] = None,
        raise_for_status: Optional[bool] = None,
        quote: Optional[bool] = None,
        curl_options: Optional[Mapping[Any, Any]] = None,
        interface: Optional[str] = None,
        doh_url: Optional[str] = None,
        max_recv_speed: Optional[int] = None,
        thread: Any = None,
        debug: Any = None,
    ) -> "_AsyncStreamContext":
        """Opens a streamed request on an :class:`AsyncSession`.

        Args:
            method: HTTP method, e.g. ``"GET"``. Upper-cased before sending.
            url: Absolute URL, or a relative one when ``base_url`` is set.
            params: Query parameters appended to the URL. A mapping, a sequence of
                key/value pairs, or an already-encoded string.
            data: Request body. A mapping or sequence of pairs is form-encoded;
                bytes/str are sent as-is; a file object, generator or async iterator
                switches to chunked upload; a file-like value in a mapping is sent as
                multipart.
            headers: Headers merged with the session's defaults.
            cookies: Cookies merged with the session jar for this request only.
            files: Multipart file parts, alone or alongside ``data``.
            auth: Per-call auth: callable, ``AuthBase``, or a 2-tuple.
            timeout: One deadline or ``(connect, read)``.
            allow_redirects: ``False`` returns the first 3xx instead of following it.
            proxies: Per-call proxy mapping, merged over the session's.
            hooks: Per-call response hooks.
            verify: ``True``/``False``/CA bundle path, for this call only.
            cert: Rejected; ABI v8 has no client-certificate setting.
            json: JSON body. Implies ``Content-Type: application/json`` unless a header
                already sets it.
            content: Raw body bytes; an alternative to ``data=`` for pre-encoded
                payloads. Passing both raises ``ValueError``.
            multipart: A :class:`~chrome_client.CurlMime` instance that becomes the
                whole body.
            impersonate: Profile override for this call, either an exact
                ``chrome_<major>`` name or a curl_cffi ``chrome<major>`` alias.
            proxy: Proxy URL for this call; wins over ``proxies``.
            http_version: Pin ``"v1"``, ``"v2"`` or ``"v3"`` for this call.
            max_redirects: Redirect cap. Below Chromium's 20, hops are driven from
                Python so the caller's limit wins.
            max_response_bytes: Body ceiling for this call.
            referer: Sets ``Referer`` through Chromium's own referrer path, so it
                reaches the wire the way a real Chrome sends it.
            accept_encoding: Overrides ``Accept-Encoding``. Chromium would otherwise
                choose it as part of the profile.
            default_encoding: Fallback encoding for this response only.
            discard_cookies: Neither send nor record cookies for this call.
            retry: Attempt count or strategy for this call.
            cache_mode: Chromium load flags, e.g. ``"bypass"`` to skip the HTTP cache.
            priority: Request priority hint passed to the Core.
            ja3: Rejected; the profile owns the ClientHello.
            akamai: Rejected; the profile owns the HTTP/2 fingerprint.
            perk: Rejected; the profile owns the TLS fingerprint.
            extra_fp: Fingerprint overrides; only ``header_order`` and ``form_boundary``
                are honourable.
            content_callback: Called with the fully buffered body on non-streaming
                requests.
            raise_for_status: Check the response before returning it.
            quote: ``False`` skips the percent-encoding pass over the URL.
            curl_options: Rejected; there is no libcurl surface here.
            interface: Rejected; no interface binding in ABI v8.
            doh_url: Rejected; DoH is not exposed.
            max_recv_speed: Rejected; Chromium owns transfer pacing.
            thread: Rejected; the sync path already runs on the calling thread.
            debug: Rejected; not a supported option.

        Returns:
            An async context manager yielding the
            :class:`~chrome_client.AsyncResponse`.

        Example:
            >>> async with session.stream("GET", url) as response:
            ...     async for chunk in response.aiter_content(65536):
            ...         ...
        """
        ...


class _StreamContext:
    """Context manager returned by ``Session.stream(...)``."""

    def __enter__(self) -> Response: ...
    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]] = ...,
        exc: Optional[BaseException] = ...,
        tb: Optional[TracebackType] = ...,
    ) -> None: ...


class _AsyncStreamContext:
    """Async context manager returned by ``AsyncSession.stream(...)``."""

    async def __aenter__(self) -> AsyncResponse: ...
    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]] = ...,
        exc: Optional[BaseException] = ...,
        tb: Optional[TracebackType] = ...,
    ) -> None: ...


class BaseSession:
    """Shared configuration, engine selection and cookie policy.

    Attributes:
        headers: Facade-level default headers. Empty by default, unlike
            ``requests``: the impersonation profile and Chromium own the real
            default set, and an injected ``Accept: */*`` is visible to any
            fingerprinter.
        cookies: A :class:`~chrome_client.RequestsCookieJar`. Reads and writes
            both take effect; a write that conflicts with the Core's own store
            makes the session rebuild the engine with an empty store.
        proxies: Mutable mapping of scheme to proxy URL. Editing it takes effect
            on the next request.
        params: Default query parameters merged into every request.
        auth: Default authentication, as a callable or a 2-tuple.
        hooks: ``{"response": [...]}`` hooks run after every response.
        adapters: Mounted adapters, keyed by URL prefix.
        impersonate: The canonical profile name, or ``None``.
        http_version: The pinned protocol (``"v1"``/``"v2"``/``"v3"``), or
            ``None`` for Chromium's own negotiation.
        proxy: A proxy URL that overrides ``proxies``, or ``None``.
        proxy_auth: ``(username, password)`` for the proxy.
        verify: ``True``, ``False``, or a CA bundle path.
        timeout: Default deadline, or ``(connect, read)``.
        max_redirects: Cap on redirects followed in Python.
        allow_redirects: Default redirect policy for every request.
        max_response_bytes: Default body ceiling.
        base_url: Prefix applied to relative request URLs.
        default_encoding: Response decoding fallback.
        default_headers: Whether :attr:`headers` starts empty (always empty;
            kept so the constructor flag round-trips).
        discard_cookies: When ``True``, neither send nor record cookies.
        raise_for_status: When ``True``, every response is checked before it is
            returned.
        retry: The normalised :class:`RetryStrategy`.
        cache: Whether the Chromium HTTP cache is enabled for the engine.
        user_agent: Engine-level User-Agent override for HTTP and WebSocket.
        accept_language: Engine-level ``Accept-Language`` override.
        response_class: Subclass instantiated instead of
            :class:`~chrome_client.Response`.
        extra_fp: The original ``extra_fp`` argument, after validation.
        header_order: Emission order for caller-supplied headers.
        form_boundary: The multipart boundary to use instead of a generated one.
    """

    headers: Headers
    cookies: RequestsCookieJar
    proxies: Mapping[str, Optional[str]]
    params: Mapping[str, Any]
    auth: Optional[AuthBase]
    hooks: Mapping[str, List[Hook]]
    adapters: Mapping[str, BaseAdapter]
    impersonate: Optional[str]
    http_version: Optional[str]
    proxy: Optional[str]
    proxy_auth: Optional[Any]
    verify: Verify
    cert: None
    timeout: Timeout
    max_redirects: int
    trust_env: bool
    allow_redirects: bool
    max_response_bytes: Optional[int]
    base_url: Optional[str]
    default_encoding: Union[str, Callable[[bytes], str]]
    default_headers: bool
    discard_cookies: bool
    raise_for_status: bool
    retry: RetryStrategy
    cache: bool
    user_agent: Optional[str]
    accept_language: Optional[str]
    response_class: ResponseClass
    extra_fp: Optional[Union[ExtraFingerprints, Mapping[str, Any]]]
    header_order: Optional[List[str]]
    form_boundary: Optional[str]

    def __init__(
        self,
        impersonate: Optional[Impersonate] = None,
        proxy: Optional[str] = None,
        proxies: Optional[Proxies] = None,
        proxy_auth: AuthLike = None,
        verify: Verify = True,
        timeout: Timeout = None,
        headers: Optional[HeadersLike] = None,
        cookies: Optional[CookiesLike] = None,
        params: Optional[ParamsLike] = None,
        auth: AuthLike = None,
        cert: Any = None,
        stream: bool = False,
        hooks: Optional[Hooks] = None,
        max_redirects: int = 30,
        trust_env: bool = True,
        allow_redirects: bool = True,
        max_response_bytes: Optional[int] = None,
        base_url: Optional[str] = None,
        http_version: Optional[HttpVersion] = None,
        ja3: Optional[str] = None,
        akamai: Optional[str] = None,
        perk: Optional[str] = None,
        extra_fp: Optional[Union[ExtraFingerprints, Mapping[str, Any]]] = None,
        default_headers: bool = True,
        default_encoding: Union[str, Callable[[bytes], str]] = "utf-8",
        discard_cookies: bool = False,
        raise_for_status: bool = False,
        retry: Retry = 0,
        cache: bool = True,
        user_agent: Optional[str] = None,
        accept_language: Optional[str] = None,
        interface: Optional[str] = None,
        doh_url: Optional[str] = None,
        max_recv_speed: int = 0,
        curl_options: Optional[Mapping[Any, Any]] = None,
        max_engines: int = 8,
        response_class: ResponseClass = None,
    ) -> None:
        """Configures the defaults shared by every request this session sends.

        Args:
            impersonate: The pinned Chromium profile that owns the TLS
                ClientHello, ALPN, HTTP/2 SETTINGS and HTTP/3 transport
                parameters. Accepts ``chrome_99``..``chrome_153``, the curl_cffi
                spelling ``chrome99``..``chrome153``, and ``chrome``/``chromium``
                for the newest pinned version.
            proxy: A single proxy URL for every request, e.g.
                ``"http://user:pass@127.0.0.1:8080"`` or
                ``"socks5://127.0.0.1:1080"``. Overrides ``proxies``.
            proxies: requests-style mapping, matched in the order
                ``scheme://host``, ``scheme``, ``all://host``, ``all``.
                Mutable at runtime.
            proxy_auth: ``(username, password)`` applied to the proxy.
            verify: ``True`` (default), ``False`` to skip verification, or a
                CA bundle path in PEM form. An engine-level setting.
            timeout: One deadline in seconds, or ``(connect, read)``. ABI v8
                carries a single deadline, so the larger of the two is used.
            headers: Default headers merged into every request.
            cookies: Initial cookies, as a mapping or a jar.
            params: Default query parameters.
            auth: Default auth: a callable, an :class:`~chrome_client.AuthBase`,
                or a ``(user, password)`` tuple.
            cert: Rejected. Client certificates need a Core setting ABI v8 does
                not expose.
            stream: Default for the per-request ``stream=`` flag.
            hooks: ``{"response": callable}`` hooks.
            max_redirects: Cap on redirects. Below Chromium's own cap of 20 the
                hops are driven from Python so the caller's limit is honoured.
            trust_env: Read ``HTTP_PROXY``/``HTTPS_PROXY``/``NO_PROXY``,
                ``REQUESTS_CA_BUNDLE``/``CURL_CA_BUNDLE`` and ``.netrc``.
            allow_redirects: Default redirect policy.
            max_response_bytes: Body ceiling; exceeding it cancels the request
                and raises :class:`~chrome_client.ResponseTooLarge`.
            base_url: Prefix for relative URLs.
            http_version: Pin ``"v1"``, ``"v2"`` or ``"v3"`` instead of letting
                Chromium negotiate through ALPN and Alt-Svc.
            ja3: Rejected; the profile owns the ClientHello.
            akamai: Rejected; the profile owns the HTTP/2 fingerprint.
            perk: Rejected; the profile owns the TLS fingerprint.
            extra_fp: :class:`~chrome_client.ExtraFingerprints` overrides. Only
                ``header_order`` and ``form_boundary`` can be honoured.
            default_headers: Kept for source compatibility. The facade's header
                set is always empty; Chromium emits its own defaults.
            default_encoding: Fallback encoding for ``Response.text``, or a
                callable taking the body and returning an encoding name.
            discard_cookies: Neither send nor record cookies.
            raise_for_status: Check every response before returning it.
            retry: Attempt count, or a :class:`RetryStrategy`.
            cache: Enable the Chromium HTTP cache for this session's engines.
            user_agent: Engine-level User-Agent for HTTP *and* WebSocket. The
                Core rejects a per-request UA because its placement is part of
                the fingerprint.
            accept_language: Engine-level ``Accept-Language``.
            interface: Rejected; ABI v8 has no network-interface binding.
            doh_url: Rejected; DNS-over-HTTPS config is not exposed.
            max_recv_speed: Rejected; Chromium owns transfer pacing.
            curl_options: Rejected; there is no libcurl surface here.
            max_engines: How many distinct engine configurations one session
                keeps alive for per-request overrides.
            response_class: Subclass to instantiate instead of
                :class:`~chrome_client.Response`/``AsyncResponse``.

        Raises:
            UnsupportedFeature: ``cert``, ``interface``, ``doh_url``,
                ``curl_options`` or ``max_recv_speed`` was set, or ``ja3``,
                ``akamai``, ``perk`` or an unhonourable ``extra_fp`` field was
                supplied. Failing closed is deliberate: silently ignoring a
                fingerprint option would report a fidelity this build lacks.
            ImpersonateError: ``impersonate`` names a non-Chromium family or a
                Chrome major outside the pinned range.

        Example:
            >>> with Session(impersonate="chrome_153", http_version="v2") as s:
            ...     response = s.get("https://example.com", timeout=15)
        """
        ...

    # -- requests Session surface ------------------------------------------
    def mount(self, prefix: str, adapter: BaseAdapter) -> None:
        """Registers an adapter for URLs starting with ``prefix``.

        Args:
            prefix: URL prefix, e.g. ``"https://"``.
            adapter: The adapter to use for matching URLs. A custom adapter
                whose ``send`` is not :class:`~chrome_client.HTTPAdapter`'s is
                delegated to, which is what keeps ``requests_mock``-style test
                doubles working.

        Example:
            >>> session.mount("https://api.example.com", MyAdapter())
        """
        ...

    def get_adapter(self, url: str) -> BaseAdapter:
        """Returns the adapter registered for the longest matching prefix.

        Args:
            url: The URL about to be sent.

        Returns:
            The adapter whose prefix is the longest match.

        Raises:
            InvalidSchema: No mounted adapter matches ``url``.

        Example:
            >>> session.get_adapter("https://example.com").__class__.__name__
            'HTTPAdapter'
        """
        ...

    def prepare_request(self, request: Request) -> PreparedRequest:
        """Merges session defaults into a ``Request``.

        Args:
            request: The user-level request. Its ``headers``, ``params``,
                ``cookies``, ``auth`` and ``hooks`` are merged with the session's
                (the request wins), and a relative URL is resolved against
                ``base_url``. ``.netrc`` credentials are applied when
                ``trust_env`` is on and no ``auth`` was given.

        Returns:
            The wire-ready :class:`~chrome_client.PreparedRequest`.

        Example:
            >>> prepared = session.prepare_request(
            ...     Request(method="GET", url="/status"))
            >>> prepared.headers["User-Agent"]  # doctest: +SKIP
        """
        ...

    def close(self) -> None:
        """Closes every engine this session owns and its mounted adapters.

        Idle sockets, the TLS session cache and the cookie store are released.
        Using the session afterwards raises
        :class:`~chrome_client.SessionClosed`.
        """
        ...

    def upkeep(self) -> int:
        """curl_cffi parity hook.

        Returns:
            Always ``0``; Chromium keeps idle sockets warm itself.

        Example:
            >>> session.upkeep()
            0
        """
        ...

    def merge_environment_settings(
        self,
        url: str,
        proxies: Optional[Proxies],
        stream: Optional[bool],
        verify: Optional[Verify],
        cert: Any,
    ) -> Mapping[str, Any]:
        """Applies environment proxy and CA settings, as requests does.

        Args:
            url: The URL about to be requested.
            proxies: Caller-supplied proxies.
            stream: Caller-supplied stream flag.
            verify: Caller-supplied verification setting.
            cert: Caller-supplied client certificate.

        Returns:
            ``{"proxies": ..., "stream": ..., "verify": ..., "cert": ...}`` with
            session defaults merged in. Empty when ``trust_env`` is ``False``.
        """
        ...

    def rebuild_method(self, prepared: PreparedRequest, response: Response) -> None:
        """Rewrites the method for the next redirect hop, as requests does.

        Args:
            prepared: The request that is about to be re-sent; mutated in place.
            response: The redirect response being followed (303 and 302 become
                ``GET``, 301 turns ``POST`` into ``GET``).
        """
        ...

    def rebuild_auth(self, prepared: PreparedRequest, response: Response) -> None:
        """Drops ``Authorization`` when a redirect changes host.

        Args:
            prepared: The next-hop request; mutated in place.
            response: The redirect response being followed.
        """
        ...

    # -- stream helper ------------------------------------------------------
    @property
    def stream(self) -> _StreamFlag:
        """``session.stream``, usable as a flag *and* as a stream helper.

        Reads as a boolean in conditionals (``if session.stream:``), and is
        callable as ``session.stream("GET", url)`` for curl_cffi-style
        streaming.

        Returns:
            A flag that is also callable.

        Example:
            >>> session.stream = True
            >>> bool(session.stream)
            True"""
        ...

    @stream.setter
    def stream(self, value: Any) -> None: ...


class Session(BaseSession):
    """Synchronous session.

    Safe to share across threads: every blocking Core call releases the GIL and
    the engine cache is locked.  One session per thread still performs better,
    because Chromium serialises requests that share an HTTP cache key inside one
    engine.  Chromium allows at most six HTTP/1.1 connections per host group, so
    raise the ceiling with HTTP/2 or HTTP/3 endpoints rather than more threads.

    Example:
        >>> with Session(impersonate="chrome_153") as session:
        ...     response = session.get("https://example.com/api", params={"page": 1})
        ...     data = response.json()
    """

    def request(
        self,
        method: str,
        url: str,
        params: Optional[ParamsLike] = None,
        data: Optional[Body] = None,
        headers: Optional[HeadersLike] = None,
        cookies: Optional[CookiesLike] = None,
        files: FilesLike = None,
        auth: AuthLike = None,
        timeout: Timeout = None,
        allow_redirects: bool = True,
        proxies: Optional[Proxies] = None,
        hooks: Optional[Hooks] = None,
        stream: Optional[bool] = None,
        verify: Optional[Verify] = None,
        cert: Any = None,
        json: Any = None,
        content: Optional[Body] = None,
        multipart: Optional[CurlMime] = None,
        impersonate: Optional[Impersonate] = None,
        proxy: Optional[str] = None,
        http_version: Optional[HttpVersion] = None,
        max_redirects: Optional[int] = None,
        max_response_bytes: Optional[int] = None,
        referer: Optional[str] = None,
        accept_encoding: Optional[str] = None,
        default_encoding: Optional[Union[str, Callable[[bytes], str]]] = None,
        discard_cookies: Optional[bool] = None,
        retry: Retry = None,
        cache_mode: CacheMode = None,
        priority: Optional[int] = None,
        ja3: Optional[str] = None,
        akamai: Optional[str] = None,
        perk: Optional[str] = None,
        extra_fp: Optional[Union[ExtraFingerprints, Mapping[str, Any]]] = None,
        content_callback: Optional[ContentCallback] = None,
        raise_for_status: Optional[bool] = None,
        quote: Optional[bool] = None,
        curl_options: Optional[Mapping[Any, Any]] = None,
        interface: Optional[str] = None,
        doh_url: Optional[str] = None,
        max_recv_speed: Optional[int] = None,
        thread: Any = None,
        debug: Any = None,
    ) -> Response:
        """Sends one request and returns a :class:`~chrome_client.Response`.

        Session defaults are used wherever an option is ``None``.

        Args:
            method: HTTP method, e.g. ``"GET"``. Upper-cased before sending.
            url: Absolute URL, or a relative one when ``base_url`` is set.
            params: Query parameters appended to the URL. A mapping, a sequence
                of key/value pairs, or an already-encoded string.
            data: Request body. A mapping or sequence of pairs is
                form-encoded; bytes/str are sent as-is; a file object, generator
                or async iterator switches to chunked upload; a file-like value
                in a mapping is sent as multipart.
            headers: Headers merged with the session's defaults.
            cookies: Cookies merged with the session jar for this request only.
            files: Multipart file parts, alone or alongside ``data``.
            auth: Per-call auth: callable, ``AuthBase``, or a 2-tuple.
            timeout: One deadline or ``(connect, read)``.
            allow_redirects: ``False`` returns the first 3xx instead of
                following it.
            proxies: Per-call proxy mapping, merged over the session's.
            hooks: Per-call response hooks.
            stream: ``True`` leaves the body unread so ``iter_content`` can
                consume it incrementally.
            verify: ``True``/``False``/CA bundle path, for this call only.
            cert: Rejected; ABI v8 has no client-certificate setting.
            json: JSON body. Implies ``Content-Type: application/json`` unless
                a header already sets it.
            content: Raw body bytes; an alternative to ``data=`` for
                pre-encoded payloads. Passing both raises ``ValueError``.
            multipart: A :class:`~chrome_client.CurlMime` instance that becomes
                the whole body.
            impersonate: Profile override for this call, either an exact
                ``chrome_<major>`` name or a curl_cffi ``chrome<major>`` alias.
            proxy: Proxy URL for this call; wins over ``proxies``.
            http_version: Pin ``"v1"``, ``"v2"`` or ``"v3"`` for this call.
            max_redirects: Redirect cap. Below Chromium's 20, hops are driven
                from Python so the caller's limit wins.
            max_response_bytes: Body ceiling for this call.
            referer: Sets ``Referer`` through Chromium's own referrer path, so
                it reaches the wire the way a real Chrome sends it.
            accept_encoding: Overrides ``Accept-Encoding``. Chromium would
                otherwise choose it as part of the profile.
            default_encoding: Fallback encoding for this response only.
            discard_cookies: Neither send nor record cookies for this call.
            retry: Attempt count or strategy for this call.
            cache_mode: Chromium load flags, e.g. ``"bypass"`` to skip the HTTP
                cache.
            priority: Request priority hint passed to the Core.
            ja3: Rejected; the profile owns the ClientHello.
            akamai: Rejected; the profile owns the HTTP/2 fingerprint.
            perk: Rejected; the profile owns the TLS fingerprint.
            extra_fp: Fingerprint overrides; only ``header_order`` and
                ``form_boundary`` are honourable.
            content_callback: Called with the fully buffered body on
                non-streaming requests.
            raise_for_status: Check the response before returning it.
            quote: ``False`` skips the percent-encoding pass over the URL.
            curl_options: Rejected; there is no libcurl surface here.
            interface: Rejected; no interface binding in ABI v8.
            doh_url: Rejected; DoH is not exposed.
            max_recv_speed: Rejected; Chromium owns transfer pacing.
            thread: Rejected; the sync path already runs on the calling thread.
            debug: Rejected; not a supported option.

        Returns:
            The final :class:`~chrome_client.Response`, with ``history`` holding
            one entry per redirect hop and ``.cookies`` carrying the cookies that
            hop set.

        Raises:
            UnsupportedFeature: A rejected option was supplied (see above).
            Timeout: The deadline elapsed.
            ConnectionError: DNS, TCP, TLS or proxy failure.
            CertificateVerifyError: Certificate expiry, name mismatch or an
                untrusted CA.
            TooManyRedirects: ``max_redirects`` was exceeded.
            ResponseTooLarge: The body passed ``max_response_bytes``.
            HTTPError: ``raise_for_status`` was on and the status is 4xx/5xx.

        Example:
            >>> response = session.request(
            ...     "POST", "https://example.com/api",
            ...     json={"page": 1}, impersonate="chrome152",
            ...     timeout=(5, 15))
            >>> response.status_code
            200
        """
        ...

    def send(
        self,
        request: PreparedRequest,
        timeout: Timeout = None,
        proxies: Optional[Proxies] = None,
        stream: Optional[bool] = None,
        verify: Optional[Verify] = None,
        cert: Any = None,
        impersonate: Optional[Impersonate] = None,
        proxy: Optional[str] = None,
        http_version: Optional[HttpVersion] = None,
        max_redirects: Optional[int] = None,
        max_response_bytes: Optional[int] = None,
        default_encoding: Optional[Union[str, Callable[[bytes], str]]] = None,
        discard_cookies: Optional[bool] = None,
        retry: Retry = None,
        cache_mode: CacheMode = None,
        priority: Optional[int] = None,
        content_callback: Optional[ContentCallback] = None,
        raise_for_status: Optional[bool] = None,
        native_redirects: Optional[bool] = None,
        python_redirects: Optional[bool] = None,
    ) -> Response:
        """Sends a prepared request, as ``requests.Session.send`` does.

        Args:
            request: The :class:`~chrome_client.PreparedRequest` to send. A request
                whose URL matches a non-default adapter is delegated to that
                adapter.
            timeout: One deadline or ``(connect, read)``.
            proxies: Per-call proxy mapping, merged over the session's.
            stream: ``True`` leaves the body unread so ``iter_content`` can consume it
                incrementally.
            verify: ``True``/``False``/CA bundle path, for this call only.
            cert: Rejected; ABI v8 has no client-certificate setting.
            impersonate: Profile override for this call, either an exact
                ``chrome_<major>`` name or a curl_cffi ``chrome<major>`` alias.
            proxy: Proxy URL for this call; wins over ``proxies``.
            http_version: Pin ``"v1"``, ``"v2"`` or ``"v3"`` for this call.
            max_redirects: Redirect cap. Below Chromium's 20, hops are driven from
                Python so the caller's limit wins.
            max_response_bytes: Body ceiling for this call.
            default_encoding: Fallback encoding for this response only.
            discard_cookies: Neither send nor record cookies for this call.
            retry: Attempt count or strategy for this call.
            cache_mode: Chromium load flags, e.g. ``"bypass"`` to skip the HTTP cache.
            priority: Request priority hint passed to the Core.
            content_callback: Called with the fully buffered body on non-streaming
                requests.
            raise_for_status: Check the response before returning it.
            native_redirects: ``True`` lets Chromium follow redirects internally (the
                default).
            python_redirects: ``True`` follows redirects from Python instead, honouring
                ``max_redirects``.

        Returns:
            The :class:`~chrome_client.Response`.

        Raises:
            ValueError: ``request`` is not a
                :class:`~chrome_client.PreparedRequest`.
            SessionClosed: The session was already closed.

        Example:
            >>> prepared = session.prepare_request(
            ...     Request(method="GET", url="https://example.com"))
            >>> session.send(prepared).status_code
            200
        """
        ...

    # -- verbs --------------------------------------------------------------
    def get(
        self,
        url: str,
        params: Optional[ParamsLike] = None,
        data: Optional[Body] = None,
        headers: Optional[HeadersLike] = None,
        cookies: Optional[CookiesLike] = None,
        files: FilesLike = None,
        auth: AuthLike = None,
        timeout: Timeout = None,
        allow_redirects: bool = True,
        proxies: Optional[Proxies] = None,
        hooks: Optional[Hooks] = None,
        stream: Optional[bool] = None,
        verify: Optional[Verify] = None,
        cert: Any = None,
        json: Any = None,
        content: Optional[Body] = None,
        multipart: Optional[CurlMime] = None,
        impersonate: Optional[Impersonate] = None,
        proxy: Optional[str] = None,
        http_version: Optional[HttpVersion] = None,
        max_redirects: Optional[int] = None,
        max_response_bytes: Optional[int] = None,
        referer: Optional[str] = None,
        accept_encoding: Optional[str] = None,
        default_encoding: Optional[Union[str, Callable[[bytes], str]]] = None,
        discard_cookies: Optional[bool] = None,
        retry: Retry = None,
        cache_mode: CacheMode = None,
        priority: Optional[int] = None,
        ja3: Optional[str] = None,
        akamai: Optional[str] = None,
        perk: Optional[str] = None,
        extra_fp: Optional[Union[ExtraFingerprints, Mapping[str, Any]]] = None,
        content_callback: Optional[ContentCallback] = None,
        raise_for_status: Optional[bool] = None,
        quote: Optional[bool] = None,
        curl_options: Optional[Mapping[Any, Any]] = None,
        interface: Optional[str] = None,
        doh_url: Optional[str] = None,
        max_recv_speed: Optional[int] = None,
        thread: Any = None,
        debug: Any = None,
    ) -> Response:
        """Sends a ``GET``.

        Args:
            url: Absolute URL, or a relative one when ``base_url`` is set.
            params: Query parameters appended to the URL. A mapping, a sequence of
                key/value pairs, or an already-encoded string.
            data: Request body. A mapping or sequence of pairs is form-encoded;
                bytes/str are sent as-is; a file object, generator or async iterator
                switches to chunked upload; a file-like value in a mapping is sent as
                multipart.
            headers: Headers merged with the session's defaults.
            cookies: Cookies merged with the session jar for this request only.
            files: Multipart file parts, alone or alongside ``data``.
            auth: Per-call auth: callable, ``AuthBase``, or a 2-tuple.
            timeout: One deadline or ``(connect, read)``.
            allow_redirects: ``False`` returns the first 3xx instead of following it.
            proxies: Per-call proxy mapping, merged over the session's.
            hooks: Per-call response hooks.
            stream: ``True`` leaves the body unread so ``iter_content`` can consume it
                incrementally.
            verify: ``True``/``False``/CA bundle path, for this call only.
            cert: Rejected; ABI v8 has no client-certificate setting.
            json: JSON body. Implies ``Content-Type: application/json`` unless a header
                already sets it.
            content: Raw body bytes; an alternative to ``data=`` for pre-encoded
                payloads. Passing both raises ``ValueError``.
            multipart: A :class:`~chrome_client.CurlMime` instance that becomes the
                whole body.
            impersonate: Profile override for this call, either an exact
                ``chrome_<major>`` name or a curl_cffi ``chrome<major>`` alias.
            proxy: Proxy URL for this call; wins over ``proxies``.
            http_version: Pin ``"v1"``, ``"v2"`` or ``"v3"`` for this call.
            max_redirects: Redirect cap. Below Chromium's 20, hops are driven from
                Python so the caller's limit wins.
            max_response_bytes: Body ceiling for this call.
            referer: Sets ``Referer`` through Chromium's own referrer path, so it
                reaches the wire the way a real Chrome sends it.
            accept_encoding: Overrides ``Accept-Encoding``. Chromium would otherwise
                choose it as part of the profile.
            default_encoding: Fallback encoding for this response only.
            discard_cookies: Neither send nor record cookies for this call.
            retry: Attempt count or strategy for this call.
            cache_mode: Chromium load flags, e.g. ``"bypass"`` to skip the HTTP cache.
            priority: Request priority hint passed to the Core.
            ja3: Rejected; the profile owns the ClientHello.
            akamai: Rejected; the profile owns the HTTP/2 fingerprint.
            perk: Rejected; the profile owns the TLS fingerprint.
            extra_fp: Fingerprint overrides; only ``header_order`` and ``form_boundary``
                are honourable.
            content_callback: Called with the fully buffered body on non-streaming
                requests.
            raise_for_status: Check the response before returning it.
            quote: ``False`` skips the percent-encoding pass over the URL.
            curl_options: Rejected; there is no libcurl surface here.
            interface: Rejected; no interface binding in ABI v8.
            doh_url: Rejected; DoH is not exposed.
            max_recv_speed: Rejected; Chromium owns transfer pacing.
            thread: Rejected; the sync path already runs on the calling thread.
            debug: Rejected; not a supported option.

        Returns:
            The :class:`~chrome_client.Response`.

        Example:
            >>> session.get("https://example.com", params={"q": "x"},
            ...             impersonate="chrome_153").json()
        """
        ...

    def options(
        self,
        url: str,
        params: Optional[ParamsLike] = None,
        data: Optional[Body] = None,
        headers: Optional[HeadersLike] = None,
        cookies: Optional[CookiesLike] = None,
        files: FilesLike = None,
        auth: AuthLike = None,
        timeout: Timeout = None,
        allow_redirects: bool = True,
        proxies: Optional[Proxies] = None,
        hooks: Optional[Hooks] = None,
        stream: Optional[bool] = None,
        verify: Optional[Verify] = None,
        cert: Any = None,
        json: Any = None,
        content: Optional[Body] = None,
        multipart: Optional[CurlMime] = None,
        impersonate: Optional[Impersonate] = None,
        proxy: Optional[str] = None,
        http_version: Optional[HttpVersion] = None,
        max_redirects: Optional[int] = None,
        max_response_bytes: Optional[int] = None,
        referer: Optional[str] = None,
        accept_encoding: Optional[str] = None,
        default_encoding: Optional[Union[str, Callable[[bytes], str]]] = None,
        discard_cookies: Optional[bool] = None,
        retry: Retry = None,
        cache_mode: CacheMode = None,
        priority: Optional[int] = None,
        ja3: Optional[str] = None,
        akamai: Optional[str] = None,
        perk: Optional[str] = None,
        extra_fp: Optional[Union[ExtraFingerprints, Mapping[str, Any]]] = None,
        content_callback: Optional[ContentCallback] = None,
        raise_for_status: Optional[bool] = None,
        quote: Optional[bool] = None,
        curl_options: Optional[Mapping[Any, Any]] = None,
        interface: Optional[str] = None,
        doh_url: Optional[str] = None,
        max_recv_speed: Optional[int] = None,
        thread: Any = None,
        debug: Any = None,
    ) -> Response:
        """Sends an ``OPTIONS``.

        Args:
            url: Absolute URL, or a relative one when ``base_url`` is set.
            params: Query parameters appended to the URL. A mapping, a sequence of
                key/value pairs, or an already-encoded string.
            data: Request body. A mapping or sequence of pairs is form-encoded;
                bytes/str are sent as-is; a file object, generator or async iterator
                switches to chunked upload; a file-like value in a mapping is sent as
                multipart.
            headers: Headers merged with the session's defaults.
            cookies: Cookies merged with the session jar for this request only.
            files: Multipart file parts, alone or alongside ``data``.
            auth: Per-call auth: callable, ``AuthBase``, or a 2-tuple.
            timeout: One deadline or ``(connect, read)``.
            allow_redirects: ``False`` returns the first 3xx instead of following it.
            proxies: Per-call proxy mapping, merged over the session's.
            hooks: Per-call response hooks.
            stream: ``True`` leaves the body unread so ``iter_content`` can consume it
                incrementally.
            verify: ``True``/``False``/CA bundle path, for this call only.
            cert: Rejected; ABI v8 has no client-certificate setting.
            json: JSON body. Implies ``Content-Type: application/json`` unless a header
                already sets it.
            content: Raw body bytes; an alternative to ``data=`` for pre-encoded
                payloads. Passing both raises ``ValueError``.
            multipart: A :class:`~chrome_client.CurlMime` instance that becomes the
                whole body.
            impersonate: Profile override for this call, either an exact
                ``chrome_<major>`` name or a curl_cffi ``chrome<major>`` alias.
            proxy: Proxy URL for this call; wins over ``proxies``.
            http_version: Pin ``"v1"``, ``"v2"`` or ``"v3"`` for this call.
            max_redirects: Redirect cap. Below Chromium's 20, hops are driven from
                Python so the caller's limit wins.
            max_response_bytes: Body ceiling for this call.
            referer: Sets ``Referer`` through Chromium's own referrer path, so it
                reaches the wire the way a real Chrome sends it.
            accept_encoding: Overrides ``Accept-Encoding``. Chromium would otherwise
                choose it as part of the profile.
            default_encoding: Fallback encoding for this response only.
            discard_cookies: Neither send nor record cookies for this call.
            retry: Attempt count or strategy for this call.
            cache_mode: Chromium load flags, e.g. ``"bypass"`` to skip the HTTP cache.
            priority: Request priority hint passed to the Core.
            ja3: Rejected; the profile owns the ClientHello.
            akamai: Rejected; the profile owns the HTTP/2 fingerprint.
            perk: Rejected; the profile owns the TLS fingerprint.
            extra_fp: Fingerprint overrides; only ``header_order`` and ``form_boundary``
                are honourable.
            content_callback: Called with the fully buffered body on non-streaming
                requests.
            raise_for_status: Check the response before returning it.
            quote: ``False`` skips the percent-encoding pass over the URL.
            curl_options: Rejected; there is no libcurl surface here.
            interface: Rejected; no interface binding in ABI v8.
            doh_url: Rejected; DoH is not exposed.
            max_recv_speed: Rejected; Chromium owns transfer pacing.
            thread: Rejected; the sync path already runs on the calling thread.
            debug: Rejected; not a supported option.

        Returns:
            The :class:`~chrome_client.Response`, carrying the ``Allow`` header
            the server returned.
        """
        ...

    def head(
        self,
        url: str,
        params: Optional[ParamsLike] = None,
        data: Optional[Body] = None,
        headers: Optional[HeadersLike] = None,
        cookies: Optional[CookiesLike] = None,
        files: FilesLike = None,
        auth: AuthLike = None,
        timeout: Timeout = None,
        allow_redirects: bool = False,
        proxies: Optional[Proxies] = None,
        hooks: Optional[Hooks] = None,
        stream: Optional[bool] = None,
        verify: Optional[Verify] = None,
        cert: Any = None,
        json: Any = None,
        content: Optional[Body] = None,
        multipart: Optional[CurlMime] = None,
        impersonate: Optional[Impersonate] = None,
        proxy: Optional[str] = None,
        http_version: Optional[HttpVersion] = None,
        max_redirects: Optional[int] = None,
        max_response_bytes: Optional[int] = None,
        referer: Optional[str] = None,
        accept_encoding: Optional[str] = None,
        default_encoding: Optional[Union[str, Callable[[bytes], str]]] = None,
        discard_cookies: Optional[bool] = None,
        retry: Retry = None,
        cache_mode: CacheMode = None,
        priority: Optional[int] = None,
        ja3: Optional[str] = None,
        akamai: Optional[str] = None,
        perk: Optional[str] = None,
        extra_fp: Optional[Union[ExtraFingerprints, Mapping[str, Any]]] = None,
        content_callback: Optional[ContentCallback] = None,
        raise_for_status: Optional[bool] = None,
        quote: Optional[bool] = None,
        curl_options: Optional[Mapping[Any, Any]] = None,
        interface: Optional[str] = None,
        doh_url: Optional[str] = None,
        max_recv_speed: Optional[int] = None,
        thread: Any = None,
        debug: Any = None,
    ) -> Response:
        """Sends a ``HEAD``.

        ``allow_redirects`` defaults to ``False`` here, matching requests.

        Args:
            url: Absolute URL, or a relative one when ``base_url`` is set.
            params: Query parameters appended to the URL. A mapping, a sequence of
                key/value pairs, or an already-encoded string.
            data: Request body. A mapping or sequence of pairs is form-encoded;
                bytes/str are sent as-is; a file object, generator or async iterator
                switches to chunked upload; a file-like value in a mapping is sent as
                multipart.
            headers: Headers merged with the session's defaults.
            cookies: Cookies merged with the session jar for this request only.
            files: Multipart file parts, alone or alongside ``data``.
            auth: Per-call auth: callable, ``AuthBase``, or a 2-tuple.
            timeout: One deadline or ``(connect, read)``.
            allow_redirects: ``False`` returns the first 3xx instead of following it.
            proxies: Per-call proxy mapping, merged over the session's.
            hooks: Per-call response hooks.
            stream: ``True`` leaves the body unread so ``iter_content`` can consume it
                incrementally.
            verify: ``True``/``False``/CA bundle path, for this call only.
            cert: Rejected; ABI v8 has no client-certificate setting.
            json: JSON body. Implies ``Content-Type: application/json`` unless a header
                already sets it.
            content: Raw body bytes; an alternative to ``data=`` for pre-encoded
                payloads. Passing both raises ``ValueError``.
            multipart: A :class:`~chrome_client.CurlMime` instance that becomes the
                whole body.
            impersonate: Profile override for this call, either an exact
                ``chrome_<major>`` name or a curl_cffi ``chrome<major>`` alias.
            proxy: Proxy URL for this call; wins over ``proxies``.
            http_version: Pin ``"v1"``, ``"v2"`` or ``"v3"`` for this call.
            max_redirects: Redirect cap. Below Chromium's 20, hops are driven from
                Python so the caller's limit wins.
            max_response_bytes: Body ceiling for this call.
            referer: Sets ``Referer`` through Chromium's own referrer path, so it
                reaches the wire the way a real Chrome sends it.
            accept_encoding: Overrides ``Accept-Encoding``. Chromium would otherwise
                choose it as part of the profile.
            default_encoding: Fallback encoding for this response only.
            discard_cookies: Neither send nor record cookies for this call.
            retry: Attempt count or strategy for this call.
            cache_mode: Chromium load flags, e.g. ``"bypass"`` to skip the HTTP cache.
            priority: Request priority hint passed to the Core.
            ja3: Rejected; the profile owns the ClientHello.
            akamai: Rejected; the profile owns the HTTP/2 fingerprint.
            perk: Rejected; the profile owns the TLS fingerprint.
            extra_fp: Fingerprint overrides; only ``header_order`` and ``form_boundary``
                are honourable.
            content_callback: Called with the fully buffered body on non-streaming
                requests.
            raise_for_status: Check the response before returning it.
            quote: ``False`` skips the percent-encoding pass over the URL.
            curl_options: Rejected; there is no libcurl surface here.
            interface: Rejected; no interface binding in ABI v8.
            doh_url: Rejected; DoH is not exposed.
            max_recv_speed: Rejected; Chromium owns transfer pacing.
            thread: Rejected; the sync path already runs on the calling thread.
            debug: Rejected; not a supported option.

        Returns:
            The :class:`~chrome_client.Response` with an empty body; read
            ``headers`` and ``status_code``.
        """
        ...

    def post(
        self,
        url: str,
        data: Optional[Body] = None,
        json: Any = None,
        params: Optional[ParamsLike] = None,
        headers: Optional[HeadersLike] = None,
        cookies: Optional[CookiesLike] = None,
        files: FilesLike = None,
        auth: AuthLike = None,
        timeout: Timeout = None,
        allow_redirects: bool = True,
        proxies: Optional[Proxies] = None,
        hooks: Optional[Hooks] = None,
        stream: Optional[bool] = None,
        verify: Optional[Verify] = None,
        cert: Any = None,
        content: Optional[Body] = None,
        multipart: Optional[CurlMime] = None,
        impersonate: Optional[Impersonate] = None,
        proxy: Optional[str] = None,
        http_version: Optional[HttpVersion] = None,
        max_redirects: Optional[int] = None,
        max_response_bytes: Optional[int] = None,
        referer: Optional[str] = None,
        accept_encoding: Optional[str] = None,
        default_encoding: Optional[Union[str, Callable[[bytes], str]]] = None,
        discard_cookies: Optional[bool] = None,
        retry: Retry = None,
        cache_mode: CacheMode = None,
        priority: Optional[int] = None,
        ja3: Optional[str] = None,
        akamai: Optional[str] = None,
        perk: Optional[str] = None,
        extra_fp: Optional[Union[ExtraFingerprints, Mapping[str, Any]]] = None,
        content_callback: Optional[ContentCallback] = None,
        raise_for_status: Optional[bool] = None,
        quote: Optional[bool] = None,
        curl_options: Optional[Mapping[Any, Any]] = None,
        interface: Optional[str] = None,
        doh_url: Optional[str] = None,
        max_recv_speed: Optional[int] = None,
        thread: Any = None,
        debug: Any = None,
    ) -> Response:
        """Sends a ``POST``.

        Args:
            url: Absolute URL, or a relative one when ``base_url`` is set.
            data: Request body. A mapping or sequence of pairs is form-encoded;
                bytes/str are sent as-is; a file object, generator or async iterator
                switches to chunked upload; a file-like value in a mapping is sent as
                multipart.
            json: JSON body. Implies ``Content-Type: application/json`` unless a header
                already sets it.
            params: Query parameters appended to the URL. A mapping, a sequence of
                key/value pairs, or an already-encoded string.
            headers: Headers merged with the session's defaults.
            cookies: Cookies merged with the session jar for this request only.
            files: Multipart file parts, alone or alongside ``data``.
            auth: Per-call auth: callable, ``AuthBase``, or a 2-tuple.
            timeout: One deadline or ``(connect, read)``.
            allow_redirects: ``False`` returns the first 3xx instead of following it.
            proxies: Per-call proxy mapping, merged over the session's.
            hooks: Per-call response hooks.
            stream: ``True`` leaves the body unread so ``iter_content`` can consume it
                incrementally.
            verify: ``True``/``False``/CA bundle path, for this call only.
            cert: Rejected; ABI v8 has no client-certificate setting.
            content: Raw body bytes; an alternative to ``data=`` for pre-encoded
                payloads. Passing both raises ``ValueError``.
            multipart: A :class:`~chrome_client.CurlMime` instance that becomes the
                whole body.
            impersonate: Profile override for this call, either an exact
                ``chrome_<major>`` name or a curl_cffi ``chrome<major>`` alias.
            proxy: Proxy URL for this call; wins over ``proxies``.
            http_version: Pin ``"v1"``, ``"v2"`` or ``"v3"`` for this call.
            max_redirects: Redirect cap. Below Chromium's 20, hops are driven from
                Python so the caller's limit wins.
            max_response_bytes: Body ceiling for this call.
            referer: Sets ``Referer`` through Chromium's own referrer path, so it
                reaches the wire the way a real Chrome sends it.
            accept_encoding: Overrides ``Accept-Encoding``. Chromium would otherwise
                choose it as part of the profile.
            default_encoding: Fallback encoding for this response only.
            discard_cookies: Neither send nor record cookies for this call.
            retry: Attempt count or strategy for this call.
            cache_mode: Chromium load flags, e.g. ``"bypass"`` to skip the HTTP cache.
            priority: Request priority hint passed to the Core.
            ja3: Rejected; the profile owns the ClientHello.
            akamai: Rejected; the profile owns the HTTP/2 fingerprint.
            perk: Rejected; the profile owns the TLS fingerprint.
            extra_fp: Fingerprint overrides; only ``header_order`` and ``form_boundary``
                are honourable.
            content_callback: Called with the fully buffered body on non-streaming
                requests.
            raise_for_status: Check the response before returning it.
            quote: ``False`` skips the percent-encoding pass over the URL.
            curl_options: Rejected; there is no libcurl surface here.
            interface: Rejected; no interface binding in ABI v8.
            doh_url: Rejected; DoH is not exposed.
            max_recv_speed: Rejected; Chromium owns transfer pacing.
            thread: Rejected; the sync path already runs on the calling thread.
            debug: Rejected; not a supported option.

        Returns:
            The :class:`~chrome_client.Response`.

        Example:
            >>> session.post("https://example.com/api", json={"title": "t"})
            >>> session.post("https://example.com/upload",
            ...              files={"f": ("a.txt", b"...", "text/plain")})
        """
        ...

    def put(
        self,
        url: str,
        data: Optional[Body] = None,
        params: Optional[ParamsLike] = None,
        headers: Optional[HeadersLike] = None,
        cookies: Optional[CookiesLike] = None,
        files: FilesLike = None,
        auth: AuthLike = None,
        timeout: Timeout = None,
        allow_redirects: bool = True,
        proxies: Optional[Proxies] = None,
        hooks: Optional[Hooks] = None,
        stream: Optional[bool] = None,
        verify: Optional[Verify] = None,
        cert: Any = None,
        json: Any = None,
        content: Optional[Body] = None,
        multipart: Optional[CurlMime] = None,
        impersonate: Optional[Impersonate] = None,
        proxy: Optional[str] = None,
        http_version: Optional[HttpVersion] = None,
        max_redirects: Optional[int] = None,
        max_response_bytes: Optional[int] = None,
        referer: Optional[str] = None,
        accept_encoding: Optional[str] = None,
        default_encoding: Optional[Union[str, Callable[[bytes], str]]] = None,
        discard_cookies: Optional[bool] = None,
        retry: Retry = None,
        cache_mode: CacheMode = None,
        priority: Optional[int] = None,
        ja3: Optional[str] = None,
        akamai: Optional[str] = None,
        perk: Optional[str] = None,
        extra_fp: Optional[Union[ExtraFingerprints, Mapping[str, Any]]] = None,
        content_callback: Optional[ContentCallback] = None,
        raise_for_status: Optional[bool] = None,
        quote: Optional[bool] = None,
        curl_options: Optional[Mapping[Any, Any]] = None,
        interface: Optional[str] = None,
        doh_url: Optional[str] = None,
        max_recv_speed: Optional[int] = None,
        thread: Any = None,
        debug: Any = None,
    ) -> Response:
        """Sends a ``PUT``.

        Args:
            url: Absolute URL, or a relative one when ``base_url`` is set.
            data: Request body. A mapping or sequence of pairs is form-encoded;
                bytes/str are sent as-is; a file object, generator or async iterator
                switches to chunked upload; a file-like value in a mapping is sent as
                multipart.
            params: Query parameters appended to the URL. A mapping, a sequence of
                key/value pairs, or an already-encoded string.
            headers: Headers merged with the session's defaults.
            cookies: Cookies merged with the session jar for this request only.
            files: Multipart file parts, alone or alongside ``data``.
            auth: Per-call auth: callable, ``AuthBase``, or a 2-tuple.
            timeout: One deadline or ``(connect, read)``.
            allow_redirects: ``False`` returns the first 3xx instead of following it.
            proxies: Per-call proxy mapping, merged over the session's.
            hooks: Per-call response hooks.
            stream: ``True`` leaves the body unread so ``iter_content`` can consume it
                incrementally.
            verify: ``True``/``False``/CA bundle path, for this call only.
            cert: Rejected; ABI v8 has no client-certificate setting.
            json: JSON body. Implies ``Content-Type: application/json`` unless a header
                already sets it.
            content: Raw body bytes; an alternative to ``data=`` for pre-encoded
                payloads. Passing both raises ``ValueError``.
            multipart: A :class:`~chrome_client.CurlMime` instance that becomes the
                whole body.
            impersonate: Profile override for this call, either an exact
                ``chrome_<major>`` name or a curl_cffi ``chrome<major>`` alias.
            proxy: Proxy URL for this call; wins over ``proxies``.
            http_version: Pin ``"v1"``, ``"v2"`` or ``"v3"`` for this call.
            max_redirects: Redirect cap. Below Chromium's 20, hops are driven from
                Python so the caller's limit wins.
            max_response_bytes: Body ceiling for this call.
            referer: Sets ``Referer`` through Chromium's own referrer path, so it
                reaches the wire the way a real Chrome sends it.
            accept_encoding: Overrides ``Accept-Encoding``. Chromium would otherwise
                choose it as part of the profile.
            default_encoding: Fallback encoding for this response only.
            discard_cookies: Neither send nor record cookies for this call.
            retry: Attempt count or strategy for this call.
            cache_mode: Chromium load flags, e.g. ``"bypass"`` to skip the HTTP cache.
            priority: Request priority hint passed to the Core.
            ja3: Rejected; the profile owns the ClientHello.
            akamai: Rejected; the profile owns the HTTP/2 fingerprint.
            perk: Rejected; the profile owns the TLS fingerprint.
            extra_fp: Fingerprint overrides; only ``header_order`` and ``form_boundary``
                are honourable.
            content_callback: Called with the fully buffered body on non-streaming
                requests.
            raise_for_status: Check the response before returning it.
            quote: ``False`` skips the percent-encoding pass over the URL.
            curl_options: Rejected; there is no libcurl surface here.
            interface: Rejected; no interface binding in ABI v8.
            doh_url: Rejected; DoH is not exposed.
            max_recv_speed: Rejected; Chromium owns transfer pacing.
            thread: Rejected; the sync path already runs on the calling thread.
            debug: Rejected; not a supported option.

        Returns:
            The :class:`~chrome_client.Response`.
        """
        ...

    def patch(
        self,
        url: str,
        data: Optional[Body] = None,
        params: Optional[ParamsLike] = None,
        headers: Optional[HeadersLike] = None,
        cookies: Optional[CookiesLike] = None,
        files: FilesLike = None,
        auth: AuthLike = None,
        timeout: Timeout = None,
        allow_redirects: bool = True,
        proxies: Optional[Proxies] = None,
        hooks: Optional[Hooks] = None,
        stream: Optional[bool] = None,
        verify: Optional[Verify] = None,
        cert: Any = None,
        json: Any = None,
        content: Optional[Body] = None,
        multipart: Optional[CurlMime] = None,
        impersonate: Optional[Impersonate] = None,
        proxy: Optional[str] = None,
        http_version: Optional[HttpVersion] = None,
        max_redirects: Optional[int] = None,
        max_response_bytes: Optional[int] = None,
        referer: Optional[str] = None,
        accept_encoding: Optional[str] = None,
        default_encoding: Optional[Union[str, Callable[[bytes], str]]] = None,
        discard_cookies: Optional[bool] = None,
        retry: Retry = None,
        cache_mode: CacheMode = None,
        priority: Optional[int] = None,
        ja3: Optional[str] = None,
        akamai: Optional[str] = None,
        perk: Optional[str] = None,
        extra_fp: Optional[Union[ExtraFingerprints, Mapping[str, Any]]] = None,
        content_callback: Optional[ContentCallback] = None,
        raise_for_status: Optional[bool] = None,
        quote: Optional[bool] = None,
        curl_options: Optional[Mapping[Any, Any]] = None,
        interface: Optional[str] = None,
        doh_url: Optional[str] = None,
        max_recv_speed: Optional[int] = None,
        thread: Any = None,
        debug: Any = None,
    ) -> Response:
        """Sends a ``PATCH``.

        Args:
            url: Absolute URL, or a relative one when ``base_url`` is set.
            data: Request body. A mapping or sequence of pairs is form-encoded;
                bytes/str are sent as-is; a file object, generator or async iterator
                switches to chunked upload; a file-like value in a mapping is sent as
                multipart.
            params: Query parameters appended to the URL. A mapping, a sequence of
                key/value pairs, or an already-encoded string.
            headers: Headers merged with the session's defaults.
            cookies: Cookies merged with the session jar for this request only.
            files: Multipart file parts, alone or alongside ``data``.
            auth: Per-call auth: callable, ``AuthBase``, or a 2-tuple.
            timeout: One deadline or ``(connect, read)``.
            allow_redirects: ``False`` returns the first 3xx instead of following it.
            proxies: Per-call proxy mapping, merged over the session's.
            hooks: Per-call response hooks.
            stream: ``True`` leaves the body unread so ``iter_content`` can consume it
                incrementally.
            verify: ``True``/``False``/CA bundle path, for this call only.
            cert: Rejected; ABI v8 has no client-certificate setting.
            json: JSON body. Implies ``Content-Type: application/json`` unless a header
                already sets it.
            content: Raw body bytes; an alternative to ``data=`` for pre-encoded
                payloads. Passing both raises ``ValueError``.
            multipart: A :class:`~chrome_client.CurlMime` instance that becomes the
                whole body.
            impersonate: Profile override for this call, either an exact
                ``chrome_<major>`` name or a curl_cffi ``chrome<major>`` alias.
            proxy: Proxy URL for this call; wins over ``proxies``.
            http_version: Pin ``"v1"``, ``"v2"`` or ``"v3"`` for this call.
            max_redirects: Redirect cap. Below Chromium's 20, hops are driven from
                Python so the caller's limit wins.
            max_response_bytes: Body ceiling for this call.
            referer: Sets ``Referer`` through Chromium's own referrer path, so it
                reaches the wire the way a real Chrome sends it.
            accept_encoding: Overrides ``Accept-Encoding``. Chromium would otherwise
                choose it as part of the profile.
            default_encoding: Fallback encoding for this response only.
            discard_cookies: Neither send nor record cookies for this call.
            retry: Attempt count or strategy for this call.
            cache_mode: Chromium load flags, e.g. ``"bypass"`` to skip the HTTP cache.
            priority: Request priority hint passed to the Core.
            ja3: Rejected; the profile owns the ClientHello.
            akamai: Rejected; the profile owns the HTTP/2 fingerprint.
            perk: Rejected; the profile owns the TLS fingerprint.
            extra_fp: Fingerprint overrides; only ``header_order`` and ``form_boundary``
                are honourable.
            content_callback: Called with the fully buffered body on non-streaming
                requests.
            raise_for_status: Check the response before returning it.
            quote: ``False`` skips the percent-encoding pass over the URL.
            curl_options: Rejected; there is no libcurl surface here.
            interface: Rejected; no interface binding in ABI v8.
            doh_url: Rejected; DoH is not exposed.
            max_recv_speed: Rejected; Chromium owns transfer pacing.
            thread: Rejected; the sync path already runs on the calling thread.
            debug: Rejected; not a supported option.

        Returns:
            The :class:`~chrome_client.Response`.
        """
        ...

    def delete(
        self,
        url: str,
        params: Optional[ParamsLike] = None,
        data: Optional[Body] = None,
        headers: Optional[HeadersLike] = None,
        cookies: Optional[CookiesLike] = None,
        files: FilesLike = None,
        auth: AuthLike = None,
        timeout: Timeout = None,
        allow_redirects: bool = True,
        proxies: Optional[Proxies] = None,
        hooks: Optional[Hooks] = None,
        stream: Optional[bool] = None,
        verify: Optional[Verify] = None,
        cert: Any = None,
        json: Any = None,
        content: Optional[Body] = None,
        multipart: Optional[CurlMime] = None,
        impersonate: Optional[Impersonate] = None,
        proxy: Optional[str] = None,
        http_version: Optional[HttpVersion] = None,
        max_redirects: Optional[int] = None,
        max_response_bytes: Optional[int] = None,
        referer: Optional[str] = None,
        accept_encoding: Optional[str] = None,
        default_encoding: Optional[Union[str, Callable[[bytes], str]]] = None,
        discard_cookies: Optional[bool] = None,
        retry: Retry = None,
        cache_mode: CacheMode = None,
        priority: Optional[int] = None,
        ja3: Optional[str] = None,
        akamai: Optional[str] = None,
        perk: Optional[str] = None,
        extra_fp: Optional[Union[ExtraFingerprints, Mapping[str, Any]]] = None,
        content_callback: Optional[ContentCallback] = None,
        raise_for_status: Optional[bool] = None,
        quote: Optional[bool] = None,
        curl_options: Optional[Mapping[Any, Any]] = None,
        interface: Optional[str] = None,
        doh_url: Optional[str] = None,
        max_recv_speed: Optional[int] = None,
        thread: Any = None,
        debug: Any = None,
    ) -> Response:
        """Sends a ``DELETE``.

        Args:
            url: Absolute URL, or a relative one when ``base_url`` is set.
            params: Query parameters appended to the URL. A mapping, a sequence of
                key/value pairs, or an already-encoded string.
            data: Request body. A mapping or sequence of pairs is form-encoded;
                bytes/str are sent as-is; a file object, generator or async iterator
                switches to chunked upload; a file-like value in a mapping is sent as
                multipart.
            headers: Headers merged with the session's defaults.
            cookies: Cookies merged with the session jar for this request only.
            files: Multipart file parts, alone or alongside ``data``.
            auth: Per-call auth: callable, ``AuthBase``, or a 2-tuple.
            timeout: One deadline or ``(connect, read)``.
            allow_redirects: ``False`` returns the first 3xx instead of following it.
            proxies: Per-call proxy mapping, merged over the session's.
            hooks: Per-call response hooks.
            stream: ``True`` leaves the body unread so ``iter_content`` can consume it
                incrementally.
            verify: ``True``/``False``/CA bundle path, for this call only.
            cert: Rejected; ABI v8 has no client-certificate setting.
            json: JSON body. Implies ``Content-Type: application/json`` unless a header
                already sets it.
            content: Raw body bytes; an alternative to ``data=`` for pre-encoded
                payloads. Passing both raises ``ValueError``.
            multipart: A :class:`~chrome_client.CurlMime` instance that becomes the
                whole body.
            impersonate: Profile override for this call, either an exact
                ``chrome_<major>`` name or a curl_cffi ``chrome<major>`` alias.
            proxy: Proxy URL for this call; wins over ``proxies``.
            http_version: Pin ``"v1"``, ``"v2"`` or ``"v3"`` for this call.
            max_redirects: Redirect cap. Below Chromium's 20, hops are driven from
                Python so the caller's limit wins.
            max_response_bytes: Body ceiling for this call.
            referer: Sets ``Referer`` through Chromium's own referrer path, so it
                reaches the wire the way a real Chrome sends it.
            accept_encoding: Overrides ``Accept-Encoding``. Chromium would otherwise
                choose it as part of the profile.
            default_encoding: Fallback encoding for this response only.
            discard_cookies: Neither send nor record cookies for this call.
            retry: Attempt count or strategy for this call.
            cache_mode: Chromium load flags, e.g. ``"bypass"`` to skip the HTTP cache.
            priority: Request priority hint passed to the Core.
            ja3: Rejected; the profile owns the ClientHello.
            akamai: Rejected; the profile owns the HTTP/2 fingerprint.
            perk: Rejected; the profile owns the TLS fingerprint.
            extra_fp: Fingerprint overrides; only ``header_order`` and ``form_boundary``
                are honourable.
            content_callback: Called with the fully buffered body on non-streaming
                requests.
            raise_for_status: Check the response before returning it.
            quote: ``False`` skips the percent-encoding pass over the URL.
            curl_options: Rejected; there is no libcurl surface here.
            interface: Rejected; no interface binding in ABI v8.
            doh_url: Rejected; DoH is not exposed.
            max_recv_speed: Rejected; Chromium owns transfer pacing.
            thread: Rejected; the sync path already runs on the calling thread.
            debug: Rejected; not a supported option.

        Returns:
            The :class:`~chrome_client.Response`.
        """
        ...

    def trace(
        self,
        url: str,
        params: Optional[ParamsLike] = None,
        data: Optional[Body] = None,
        headers: Optional[HeadersLike] = None,
        cookies: Optional[CookiesLike] = None,
        files: FilesLike = None,
        auth: AuthLike = None,
        timeout: Timeout = None,
        allow_redirects: bool = True,
        proxies: Optional[Proxies] = None,
        hooks: Optional[Hooks] = None,
        stream: Optional[bool] = None,
        verify: Optional[Verify] = None,
        cert: Any = None,
        json: Any = None,
        content: Optional[Body] = None,
        multipart: Optional[CurlMime] = None,
        impersonate: Optional[Impersonate] = None,
        proxy: Optional[str] = None,
        http_version: Optional[HttpVersion] = None,
        max_redirects: Optional[int] = None,
        max_response_bytes: Optional[int] = None,
        referer: Optional[str] = None,
        accept_encoding: Optional[str] = None,
        default_encoding: Optional[Union[str, Callable[[bytes], str]]] = None,
        discard_cookies: Optional[bool] = None,
        retry: Retry = None,
        cache_mode: CacheMode = None,
        priority: Optional[int] = None,
        ja3: Optional[str] = None,
        akamai: Optional[str] = None,
        perk: Optional[str] = None,
        extra_fp: Optional[Union[ExtraFingerprints, Mapping[str, Any]]] = None,
        content_callback: Optional[ContentCallback] = None,
        raise_for_status: Optional[bool] = None,
        quote: Optional[bool] = None,
        curl_options: Optional[Mapping[Any, Any]] = None,
        interface: Optional[str] = None,
        doh_url: Optional[str] = None,
        max_recv_speed: Optional[int] = None,
        thread: Any = None,
        debug: Any = None,
    ) -> Response:
        """Sends a ``TRACE``.

        Args:
            url: Absolute URL, or a relative one when ``base_url`` is set.
            params: Query parameters appended to the URL. A mapping, a sequence of
                key/value pairs, or an already-encoded string.
            data: Request body. A mapping or sequence of pairs is form-encoded;
                bytes/str are sent as-is; a file object, generator or async iterator
                switches to chunked upload; a file-like value in a mapping is sent as
                multipart.
            headers: Headers merged with the session's defaults.
            cookies: Cookies merged with the session jar for this request only.
            files: Multipart file parts, alone or alongside ``data``.
            auth: Per-call auth: callable, ``AuthBase``, or a 2-tuple.
            timeout: One deadline or ``(connect, read)``.
            allow_redirects: ``False`` returns the first 3xx instead of following it.
            proxies: Per-call proxy mapping, merged over the session's.
            hooks: Per-call response hooks.
            stream: ``True`` leaves the body unread so ``iter_content`` can consume it
                incrementally.
            verify: ``True``/``False``/CA bundle path, for this call only.
            cert: Rejected; ABI v8 has no client-certificate setting.
            json: JSON body. Implies ``Content-Type: application/json`` unless a header
                already sets it.
            content: Raw body bytes; an alternative to ``data=`` for pre-encoded
                payloads. Passing both raises ``ValueError``.
            multipart: A :class:`~chrome_client.CurlMime` instance that becomes the
                whole body.
            impersonate: Profile override for this call, either an exact
                ``chrome_<major>`` name or a curl_cffi ``chrome<major>`` alias.
            proxy: Proxy URL for this call; wins over ``proxies``.
            http_version: Pin ``"v1"``, ``"v2"`` or ``"v3"`` for this call.
            max_redirects: Redirect cap. Below Chromium's 20, hops are driven from
                Python so the caller's limit wins.
            max_response_bytes: Body ceiling for this call.
            referer: Sets ``Referer`` through Chromium's own referrer path, so it
                reaches the wire the way a real Chrome sends it.
            accept_encoding: Overrides ``Accept-Encoding``. Chromium would otherwise
                choose it as part of the profile.
            default_encoding: Fallback encoding for this response only.
            discard_cookies: Neither send nor record cookies for this call.
            retry: Attempt count or strategy for this call.
            cache_mode: Chromium load flags, e.g. ``"bypass"`` to skip the HTTP cache.
            priority: Request priority hint passed to the Core.
            ja3: Rejected; the profile owns the ClientHello.
            akamai: Rejected; the profile owns the HTTP/2 fingerprint.
            perk: Rejected; the profile owns the TLS fingerprint.
            extra_fp: Fingerprint overrides; only ``header_order`` and ``form_boundary``
                are honourable.
            content_callback: Called with the fully buffered body on non-streaming
                requests.
            raise_for_status: Check the response before returning it.
            quote: ``False`` skips the percent-encoding pass over the URL.
            curl_options: Rejected; there is no libcurl surface here.
            interface: Rejected; no interface binding in ABI v8.
            doh_url: Rejected; DoH is not exposed.
            max_recv_speed: Rejected; Chromium owns transfer pacing.
            thread: Rejected; the sync path already runs on the calling thread.
            debug: Rejected; not a supported option.

        Returns:
            The :class:`~chrome_client.Response`.
        """
        ...

    def query(
        self,
        url: str,
        params: Optional[ParamsLike] = None,
        data: Optional[Body] = None,
        headers: Optional[HeadersLike] = None,
        cookies: Optional[CookiesLike] = None,
        files: FilesLike = None,
        auth: AuthLike = None,
        timeout: Timeout = None,
        allow_redirects: bool = True,
        proxies: Optional[Proxies] = None,
        hooks: Optional[Hooks] = None,
        stream: Optional[bool] = None,
        verify: Optional[Verify] = None,
        cert: Any = None,
        json: Any = None,
        content: Optional[Body] = None,
        multipart: Optional[CurlMime] = None,
        impersonate: Optional[Impersonate] = None,
        proxy: Optional[str] = None,
        http_version: Optional[HttpVersion] = None,
        max_redirects: Optional[int] = None,
        max_response_bytes: Optional[int] = None,
        referer: Optional[str] = None,
        accept_encoding: Optional[str] = None,
        default_encoding: Optional[Union[str, Callable[[bytes], str]]] = None,
        discard_cookies: Optional[bool] = None,
        retry: Retry = None,
        cache_mode: CacheMode = None,
        priority: Optional[int] = None,
        ja3: Optional[str] = None,
        akamai: Optional[str] = None,
        perk: Optional[str] = None,
        extra_fp: Optional[Union[ExtraFingerprints, Mapping[str, Any]]] = None,
        content_callback: Optional[ContentCallback] = None,
        raise_for_status: Optional[bool] = None,
        quote: Optional[bool] = None,
        curl_options: Optional[Mapping[Any, Any]] = None,
        interface: Optional[str] = None,
        doh_url: Optional[str] = None,
        max_recv_speed: Optional[int] = None,
        thread: Any = None,
        debug: Any = None,
    ) -> Response:
        """Sends a ``QUERY`` (draft-ietf-httpbis-safe-method-w-body).

        Args:
            url: Absolute URL, or a relative one when ``base_url`` is set.
            params: Query parameters appended to the URL. A mapping, a sequence of
                key/value pairs, or an already-encoded string.
            data: Request body. A mapping or sequence of pairs is form-encoded;
                bytes/str are sent as-is; a file object, generator or async iterator
                switches to chunked upload; a file-like value in a mapping is sent as
                multipart.
            headers: Headers merged with the session's defaults.
            cookies: Cookies merged with the session jar for this request only.
            files: Multipart file parts, alone or alongside ``data``.
            auth: Per-call auth: callable, ``AuthBase``, or a 2-tuple.
            timeout: One deadline or ``(connect, read)``.
            allow_redirects: ``False`` returns the first 3xx instead of following it.
            proxies: Per-call proxy mapping, merged over the session's.
            hooks: Per-call response hooks.
            stream: ``True`` leaves the body unread so ``iter_content`` can consume it
                incrementally.
            verify: ``True``/``False``/CA bundle path, for this call only.
            cert: Rejected; ABI v8 has no client-certificate setting.
            json: JSON body. Implies ``Content-Type: application/json`` unless a header
                already sets it.
            content: Raw body bytes; an alternative to ``data=`` for pre-encoded
                payloads. Passing both raises ``ValueError``.
            multipart: A :class:`~chrome_client.CurlMime` instance that becomes the
                whole body.
            impersonate: Profile override for this call, either an exact
                ``chrome_<major>`` name or a curl_cffi ``chrome<major>`` alias.
            proxy: Proxy URL for this call; wins over ``proxies``.
            http_version: Pin ``"v1"``, ``"v2"`` or ``"v3"`` for this call.
            max_redirects: Redirect cap. Below Chromium's 20, hops are driven from
                Python so the caller's limit wins.
            max_response_bytes: Body ceiling for this call.
            referer: Sets ``Referer`` through Chromium's own referrer path, so it
                reaches the wire the way a real Chrome sends it.
            accept_encoding: Overrides ``Accept-Encoding``. Chromium would otherwise
                choose it as part of the profile.
            default_encoding: Fallback encoding for this response only.
            discard_cookies: Neither send nor record cookies for this call.
            retry: Attempt count or strategy for this call.
            cache_mode: Chromium load flags, e.g. ``"bypass"`` to skip the HTTP cache.
            priority: Request priority hint passed to the Core.
            ja3: Rejected; the profile owns the ClientHello.
            akamai: Rejected; the profile owns the HTTP/2 fingerprint.
            perk: Rejected; the profile owns the TLS fingerprint.
            extra_fp: Fingerprint overrides; only ``header_order`` and ``form_boundary``
                are honourable.
            content_callback: Called with the fully buffered body on non-streaming
                requests.
            raise_for_status: Check the response before returning it.
            quote: ``False`` skips the percent-encoding pass over the URL.
            curl_options: Rejected; there is no libcurl surface here.
            interface: Rejected; no interface binding in ABI v8.
            doh_url: Rejected; DoH is not exposed.
            max_recv_speed: Rejected; Chromium owns transfer pacing.
            thread: Rejected; the sync path already runs on the calling thread.
            debug: Rejected; not a supported option.

        Returns:
            The :class:`~chrome_client.Response`.
        """
        ...

    @overload
    def resolve_redirects(
        self,
        response: Response,
        request: PreparedRequest,
        stream: bool = ...,
        timeout: Timeout = ...,
        verify: Verify = ...,
        cert: Any = ...,
        proxies: Optional[Proxies] = ...,
        yield_requests: Literal[False] = ...,
        **kwargs: Any,
    ) -> Iterator[Response]:
        """Yields each response in a redirect chain, as requests does.

        Args:
            response: The first (redirecting) response.
            request: The request that produced it.
            stream: Forwarded to each hop's send.
            timeout: Deadline for each hop.
            verify: Verification setting for each hop.
            cert: Unused; accepted for signature compatibility.
            proxies: Proxy mapping for each hop.
            yield_requests: When ``True``, yields the next-hop
                :class:`~chrome_client.PreparedRequest` instead of sending it,
                which is how requests builds a redirect chain lazily.
            **kwargs: Forwarded to :meth:`send`.

        Yields:
            Each :class:`~chrome_client.Response` in the chain, or each
            :class:`~chrome_client.PreparedRequest` when ``yield_requests`` is
            ``True``.

        Raises:
            TooManyRedirects: The session's ``max_redirects`` was exceeded.

        Example:
            >>> for hop in session.resolve_redirects(response, request):
            ...     print(hop.status_code, hop.url)
        """
    @overload
    def resolve_redirects(
        self,
        response: Response,
        request: PreparedRequest,
        stream: bool = ...,
        timeout: Timeout = ...,
        verify: Verify = ...,
        cert: Any = ...,
        proxies: Optional[Proxies] = ...,
        yield_requests: Literal[True] = ...,
        **kwargs: Any,
    ) -> Iterator[PreparedRequest]: ...

    def websocket(
        self,
        url: str,
        origin: str = "",
        headers: Optional[HeadersLike] = None,
        timeout: Timeout = None,
        proxy: Optional[str] = None,
        proxies: Optional[Proxies] = None,
        impersonate: Optional[Impersonate] = None,
        protocols: Optional[Sequence[str]] = None,
        verify: Optional[Verify] = None,
    ) -> WebSocket:
        """Opens a WebSocket and waits for the handshake to complete.

        Waiting matters: the Core rejects ``close()`` and ``send()`` before the
        socket is open, so returning early would hand back an object that cannot
        be used yet.

        Args:
            url: ``ws://`` or ``wss://`` URL.
            origin: Handshake ``Origin``. Defaults to the URL's own origin --
                what a same-origin page looks like on the wire -- because the
                Core rejects an empty origin and there is no page to inherit one
                from here.
            headers: Extra handshake headers. ``Host``, ``Origin``,
                ``Connection``, ``Upgrade``, ``User-Agent`` and any
                ``Sec-WebSocket-*`` are rejected: Chromium derives them itself,
                and the UA's placement is part of the fingerprint. Set the UA
                for the whole session with ``Session(user_agent=...)`` instead.
            timeout: Handshake deadline.
            proxy: Proxy URL for the handshake.
            proxies: Proxy mapping for the handshake.
            impersonate: Profile override for the handshake.
            protocols: Subprotocols to offer in ``Sec-WebSocket-Protocol``.
            verify: Verification setting for the ``wss://`` handshake.

        Returns:
            A connected :class:`~chrome_client.WebSocket`.

        Raises:
            UnsupportedFeature: A forbidden handshake header was supplied.
            WebSocketTimeout: The handshake did not complete in time.
            WebSocketError: The handshake failed at the network or TLS layer.

        Example:
            >>> with session.websocket("wss://echo.example.com") as socket:
            ...     socket.send_str("ping")
            ...     print(socket.recv_str())
        """
        ...

    def ws_connect(
        self,
        url: str,
        origin: str = "",
        headers: Optional[HeadersLike] = None,
        timeout: Timeout = None,
        proxy: Optional[str] = None,
        proxies: Optional[Proxies] = None,
        impersonate: Optional[Impersonate] = None,
        protocols: Optional[Sequence[str]] = None,
        verify: Optional[Verify] = None,
    ) -> WebSocket:
        """Alias of :meth:`websocket`, matching curl_cffi's spelling.

        Args:
            url: ``ws://`` or ``wss://`` URL.
            origin: Handshake ``Origin``; see :meth:`websocket`.
            headers: Extra handshake headers.
            timeout: Handshake deadline.
            proxy: Proxy URL.
            proxies: Proxy mapping.
            impersonate: Profile override.
            protocols: Subprotocols to offer.
            verify: Verification setting.

        Returns:
            A connected :class:`~chrome_client.WebSocket`.
        """
        ...

    def __enter__(self) -> "Session":
        """Returns the session, so ``with Session() as s:`` works.

        Returns:
            This session."""
        ...
    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]] = ...,
        exc: Optional[BaseException] = ...,
        tb: Optional[TracebackType] = ...,
    ) -> None:
        """Closes the session and its engines on the way out.

        Args:
            exc_type: Exception type, if any.
            exc: Exception instance, if any.
            tb: Traceback, if any."""
        ...


class AsyncSession(BaseSession):
    """Asyncio session.

    No worker thread or thread pool is involved: the Core wakes the running loop
    directly and each wakeup drains a batch of events, so thousands of in-flight
    requests cost one future and one small state object each.

    ``max_clients`` bounds how many requests are in flight at once.  Leaving it
    unset is fine for a few thousand; setting it is how a caller keeps a burst
    from opening more sockets than the far end tolerates.

    Attributes:
        max_clients: In-flight request ceiling, or ``None`` for unbounded.

    Example:
        >>> async def main():
        ...     async with AsyncSession(impersonate="chrome_153",
        ...                             max_clients=64) as session:
        ...         responses = await asyncio.gather(
        ...             *[session.get(url) for url in urls])
        ...     return responses
    """

    max_clients: Optional[int]

    @property  # type: ignore[override]  # the same int flag at runtime; here it is narrowed so `async with` types
    def stream(self) -> _AsyncStreamFlag:
        """``session.stream`` for the asyncio session.

        Reads as a boolean in conditionals, and is awaitable-style callable as
        ``session.stream("GET", url)``, which yields the
        :class:`~chrome_client.AsyncResponse` from an ``async with`` block.

        Returns:
            A flag that is also callable, returning an async context manager.

        Example:
            >>> async with session.stream("GET", url) as response:
            ...     async for chunk in response.aiter_content(65536):
            ...         ...
        """
        ...

    @stream.setter
    def stream(self, value: Any) -> None: ...

    def __init__(
        self,
        impersonate: Optional[Impersonate] = None,
        proxy: Optional[str] = None,
        proxies: Optional[Proxies] = None,
        proxy_auth: AuthLike = None,
        verify: Verify = True,
        timeout: Timeout = None,
        headers: Optional[HeadersLike] = None,
        cookies: Optional[CookiesLike] = None,
        params: Optional[ParamsLike] = None,
        auth: AuthLike = None,
        cert: Any = None,
        stream: bool = False,
        hooks: Optional[Hooks] = None,
        max_redirects: int = 30,
        trust_env: bool = True,
        allow_redirects: bool = True,
        max_response_bytes: Optional[int] = None,
        base_url: Optional[str] = None,
        http_version: Optional[HttpVersion] = None,
        ja3: Optional[str] = None,
        akamai: Optional[str] = None,
        perk: Optional[str] = None,
        extra_fp: Optional[Union[ExtraFingerprints, Mapping[str, Any]]] = None,
        default_headers: bool = True,
        default_encoding: Union[str, Callable[[bytes], str]] = "utf-8",
        discard_cookies: bool = False,
        raise_for_status: bool = False,
        retry: Retry = 0,
        cache: bool = True,
        user_agent: Optional[str] = None,
        accept_language: Optional[str] = None,
        interface: Optional[str] = None,
        doh_url: Optional[str] = None,
        max_recv_speed: int = 0,
        curl_options: Optional[Mapping[Any, Any]] = None,
        max_engines: int = 8,
        response_class: ResponseClass = None,
        max_clients: Optional[int] = None,
    ) -> None:
        """Configures the asyncio session.

        Accepts every :class:`BaseSession` option plus ``max_clients``.

        Args:
            impersonate: Pinned profile; see :class:`BaseSession`.
            proxy: Proxy URL overriding ``proxies``.
            proxies: requests-style proxy mapping.
            proxy_auth: ``(username, password)`` for the proxy.
            verify: ``True``/``False``/CA bundle path.
            timeout: Default deadline, or ``(connect, read)``.
            headers: Default headers.
            cookies: Initial cookies.
            params: Default query parameters.
            auth: Default auth.
            cert: Rejected; see :class:`BaseSession`.
            stream: Default ``stream=`` flag.
            hooks: Response hooks.
            max_redirects: Redirect cap.
            trust_env: Read environment proxy/CA/``.netrc`` settings.
            allow_redirects: Default redirect policy.
            max_response_bytes: Default body ceiling.
            base_url: Prefix for relative URLs.
            http_version: Pin ``"v1"``/``"v2"``/``"v3"``.
            ja3: Rejected.
            akamai: Rejected.
            perk: Rejected.
            extra_fp: Fingerprint overrides.
            default_headers: Kept for source compatibility.
            default_encoding: Decoding fallback for ``Response.text``.
            discard_cookies: Neither send nor record cookies.
            raise_for_status: Check every response.
            retry: Attempt count or :class:`RetryStrategy`.
            cache: Enable the Chromium HTTP cache.
            user_agent: Engine-level User-Agent for HTTP and WebSocket.
            accept_language: Engine-level ``Accept-Language``.
            interface: Rejected.
            doh_url: Rejected.
            max_recv_speed: Rejected.
            curl_options: Rejected.
            max_engines: Engines kept alive per session.
            response_class: Response subclass to instantiate.
            max_clients: Maximum concurrently in-flight requests; ``None``
                leaves the gate open.

        Raises:
            UnsupportedFeature: Any rejected option was supplied.
            ImpersonateError: ``impersonate`` is out of range or the wrong
                family.

        Example:
            >>> session = AsyncSession(impersonate="chrome_153", max_clients=32)
        """
        ...

    async def request(
        self,
        method: str,
        url: str,
        params: Optional[ParamsLike] = None,
        data: Optional[Body] = None,
        headers: Optional[HeadersLike] = None,
        cookies: Optional[CookiesLike] = None,
        files: FilesLike = None,
        auth: AuthLike = None,
        timeout: Timeout = None,
        allow_redirects: bool = True,
        proxies: Optional[Proxies] = None,
        hooks: Optional[Hooks] = None,
        stream: Optional[bool] = None,
        verify: Optional[Verify] = None,
        cert: Any = None,
        json: Any = None,
        content: Optional[Body] = None,
        multipart: Optional[CurlMime] = None,
        impersonate: Optional[Impersonate] = None,
        proxy: Optional[str] = None,
        http_version: Optional[HttpVersion] = None,
        max_redirects: Optional[int] = None,
        max_response_bytes: Optional[int] = None,
        referer: Optional[str] = None,
        accept_encoding: Optional[str] = None,
        default_encoding: Optional[Union[str, Callable[[bytes], str]]] = None,
        discard_cookies: Optional[bool] = None,
        retry: Retry = None,
        cache_mode: CacheMode = None,
        priority: Optional[int] = None,
        ja3: Optional[str] = None,
        akamai: Optional[str] = None,
        perk: Optional[str] = None,
        extra_fp: Optional[Union[ExtraFingerprints, Mapping[str, Any]]] = None,
        content_callback: Optional[ContentCallback] = None,
        raise_for_status: Optional[bool] = None,
        quote: Optional[bool] = None,
        curl_options: Optional[Mapping[Any, Any]] = None,
        interface: Optional[str] = None,
        doh_url: Optional[str] = None,
        max_recv_speed: Optional[int] = None,
        thread: Any = None,
        debug: Any = None,
    ) -> AsyncResponse:
        """Sends one request and returns an :class:`~chrome_client.AsyncResponse`.

        Takes exactly the same options as :meth:`Session.request`; the
        difference is that the Core wakes the running event loop instead of a
        thread blocking on the socket, and ``max_clients`` gates concurrency.

        Args:
            method: HTTP method, e.g. ``"GET"``.
            url: Absolute URL, or relative with ``base_url`` set.
            params: Query parameters.
            data: Form body, raw bytes, or an (async) iterator for chunked
                upload.
            headers: Headers merged with the session's.
            cookies: Per-call cookies.
            files: Multipart file parts.
            auth: Per-call auth.
            timeout: One deadline or ``(connect, read)``.
            allow_redirects: ``False`` returns the 3xx itself.
            proxies: Per-call proxy mapping.
            hooks: Per-call response hooks; they run on the loop thread and must
                not block it.
            stream: ``True`` keeps the body unread for ``aiter_content``.
            verify: Verification setting for this call.
            cert: Rejected.
            json: JSON body.
            content: Raw body bytes; mutually exclusive with ``data=``.
            multipart: A :class:`~chrome_client.CurlMime` body.
            impersonate: Profile override for this call.
            proxy: Proxy URL for this call.
            http_version: Protocol pin for this call.
            max_redirects: Redirect cap for this call.
            max_response_bytes: Body ceiling for this call.
            referer: Routed through Chromium's referrer path.
            accept_encoding: Overrides ``Accept-Encoding``.
            default_encoding: Decoding fallback for this response.
            discard_cookies: Neither send nor record cookies.
            retry: Attempt count or strategy.
            cache_mode: Chromium load flags.
            priority: Request priority hint.
            ja3: Rejected.
            akamai: Rejected.
            perk: Rejected.
            extra_fp: Fingerprint overrides.
            content_callback: Called with the fully buffered body.
            raise_for_status: Check the response before returning it.
            quote: ``False`` skips percent-encoding the URL.
            curl_options: Rejected.
            interface: Rejected.
            doh_url: Rejected.
            max_recv_speed: Rejected.
            thread: Rejected.
            debug: Rejected.

        Returns:
            The :class:`~chrome_client.AsyncResponse`. Await ``acontent()`` or
            ``ajson()``, or use ``async for chunk in response.aiter_content()``.

        Raises:
            UnsupportedFeature: A rejected option was supplied.
            asyncio.CancelledError: The awaiting task was cancelled; the Core
                request is cancelled with it.
            Timeout: The deadline elapsed.
            ConnectionError: Network, TLS or proxy failure.
            TooManyRedirects: The redirect cap was exceeded.
            ResponseTooLarge: The body ceiling was exceeded.

        Example:
            >>> response = await session.request(
            ...     "GET", "https://example.com", impersonate="chrome152")
            >>> data = await response.json()  # doctest: +SKIP
        """
        ...

    async def send(
        self,
        request: PreparedRequest,
        timeout: Timeout = None,
        proxies: Optional[Proxies] = None,
        stream: Optional[bool] = None,
        verify: Optional[Verify] = None,
        cert: Any = None,
        impersonate: Optional[Impersonate] = None,
        proxy: Optional[str] = None,
        http_version: Optional[HttpVersion] = None,
        max_redirects: Optional[int] = None,
        max_response_bytes: Optional[int] = None,
        default_encoding: Optional[Union[str, Callable[[bytes], str]]] = None,
        discard_cookies: Optional[bool] = None,
        retry: Retry = None,
        cache_mode: CacheMode = None,
        priority: Optional[int] = None,
        content_callback: Optional[ContentCallback] = None,
        raise_for_status: Optional[bool] = None,
        native_redirects: Optional[bool] = None,
        python_redirects: Optional[bool] = None,
    ) -> AsyncResponse:
        """Sends a prepared request.

        Args:
            request: The :class:`~chrome_client.PreparedRequest` to send. A request
                whose URL matches a non-default adapter is delegated to that
                adapter.
            timeout: One deadline or ``(connect, read)``.
            proxies: Per-call proxy mapping, merged over the session's.
            stream: ``True`` leaves the body unread so ``iter_content`` can consume it
                incrementally.
            verify: ``True``/``False``/CA bundle path, for this call only.
            cert: Rejected; ABI v8 has no client-certificate setting.
            impersonate: Profile override for this call, either an exact
                ``chrome_<major>`` name or a curl_cffi ``chrome<major>`` alias.
            proxy: Proxy URL for this call; wins over ``proxies``.
            http_version: Pin ``"v1"``, ``"v2"`` or ``"v3"`` for this call.
            max_redirects: Redirect cap. Below Chromium's 20, hops are driven from
                Python so the caller's limit wins.
            max_response_bytes: Body ceiling for this call.
            default_encoding: Fallback encoding for this response only.
            discard_cookies: Neither send nor record cookies for this call.
            retry: Attempt count or strategy for this call.
            cache_mode: Chromium load flags, e.g. ``"bypass"`` to skip the HTTP cache.
            priority: Request priority hint passed to the Core.
            content_callback: Called with the fully buffered body on non-streaming
                requests.
            raise_for_status: Check the response before returning it.
            native_redirects: ``True`` lets Chromium follow redirects internally (the
                default).
            python_redirects: ``True`` follows redirects from Python instead, honouring
                ``max_redirects``.

        Returns:
            The :class:`~chrome_client.AsyncResponse`.

        Raises:
            ValueError: ``request`` is not a ``PreparedRequest``.
            SessionClosed: The session was already closed.
        """
        ...

    async def get(
        self,
        url: str,
        params: Optional[ParamsLike] = None,
        data: Optional[Body] = None,
        headers: Optional[HeadersLike] = None,
        cookies: Optional[CookiesLike] = None,
        files: FilesLike = None,
        auth: AuthLike = None,
        timeout: Timeout = None,
        allow_redirects: bool = True,
        proxies: Optional[Proxies] = None,
        hooks: Optional[Hooks] = None,
        stream: Optional[bool] = None,
        verify: Optional[Verify] = None,
        cert: Any = None,
        json: Any = None,
        content: Optional[Body] = None,
        multipart: Optional[CurlMime] = None,
        impersonate: Optional[Impersonate] = None,
        proxy: Optional[str] = None,
        http_version: Optional[HttpVersion] = None,
        max_redirects: Optional[int] = None,
        max_response_bytes: Optional[int] = None,
        referer: Optional[str] = None,
        accept_encoding: Optional[str] = None,
        default_encoding: Optional[Union[str, Callable[[bytes], str]]] = None,
        discard_cookies: Optional[bool] = None,
        retry: Retry = None,
        cache_mode: CacheMode = None,
        priority: Optional[int] = None,
        ja3: Optional[str] = None,
        akamai: Optional[str] = None,
        perk: Optional[str] = None,
        extra_fp: Optional[Union[ExtraFingerprints, Mapping[str, Any]]] = None,
        content_callback: Optional[ContentCallback] = None,
        raise_for_status: Optional[bool] = None,
        quote: Optional[bool] = None,
        curl_options: Optional[Mapping[Any, Any]] = None,
        interface: Optional[str] = None,
        doh_url: Optional[str] = None,
        max_recv_speed: Optional[int] = None,
        thread: Any = None,
        debug: Any = None,
    ) -> AsyncResponse:
        """Sends a ``GET``.

        Args:
            url: Absolute URL, or a relative one when ``base_url`` is set.
            params: Query parameters appended to the URL. A mapping, a sequence of
                key/value pairs, or an already-encoded string.
            data: Request body. A mapping or sequence of pairs is form-encoded;
                bytes/str are sent as-is; a file object, generator or async iterator
                switches to chunked upload; a file-like value in a mapping is sent as
                multipart.
            headers: Headers merged with the session's defaults.
            cookies: Cookies merged with the session jar for this request only.
            files: Multipart file parts, alone or alongside ``data``.
            auth: Per-call auth: callable, ``AuthBase``, or a 2-tuple.
            timeout: One deadline or ``(connect, read)``.
            allow_redirects: ``False`` returns the first 3xx instead of following it.
            proxies: Per-call proxy mapping, merged over the session's.
            hooks: Per-call response hooks.
            stream: ``True`` leaves the body unread so ``iter_content`` can consume it
                incrementally.
            verify: ``True``/``False``/CA bundle path, for this call only.
            cert: Rejected; ABI v8 has no client-certificate setting.
            json: JSON body. Implies ``Content-Type: application/json`` unless a header
                already sets it.
            content: Raw body bytes; an alternative to ``data=`` for pre-encoded
                payloads. Passing both raises ``ValueError``.
            multipart: A :class:`~chrome_client.CurlMime` instance that becomes the
                whole body.
            impersonate: Profile override for this call, either an exact
                ``chrome_<major>`` name or a curl_cffi ``chrome<major>`` alias.
            proxy: Proxy URL for this call; wins over ``proxies``.
            http_version: Pin ``"v1"``, ``"v2"`` or ``"v3"`` for this call.
            max_redirects: Redirect cap. Below Chromium's 20, hops are driven from
                Python so the caller's limit wins.
            max_response_bytes: Body ceiling for this call.
            referer: Sets ``Referer`` through Chromium's own referrer path, so it
                reaches the wire the way a real Chrome sends it.
            accept_encoding: Overrides ``Accept-Encoding``. Chromium would otherwise
                choose it as part of the profile.
            default_encoding: Fallback encoding for this response only.
            discard_cookies: Neither send nor record cookies for this call.
            retry: Attempt count or strategy for this call.
            cache_mode: Chromium load flags, e.g. ``"bypass"`` to skip the HTTP cache.
            priority: Request priority hint passed to the Core.
            ja3: Rejected; the profile owns the ClientHello.
            akamai: Rejected; the profile owns the HTTP/2 fingerprint.
            perk: Rejected; the profile owns the TLS fingerprint.
            extra_fp: Fingerprint overrides; only ``header_order`` and ``form_boundary``
                are honourable.
            content_callback: Called with the fully buffered body on non-streaming
                requests.
            raise_for_status: Check the response before returning it.
            quote: ``False`` skips the percent-encoding pass over the URL.
            curl_options: Rejected; there is no libcurl surface here.
            interface: Rejected; no interface binding in ABI v8.
            doh_url: Rejected; DoH is not exposed.
            max_recv_speed: Rejected; Chromium owns transfer pacing.
            thread: Rejected; the sync path already runs on the calling thread.
            debug: Rejected; not a supported option.

        Returns:
            The :class:`~chrome_client.AsyncResponse`.

        Example:
            >>> responses = await asyncio.gather(
            ...     *[session.get(url) for url in urls])
        """
        ...

    async def options(
        self,
        url: str,
        params: Optional[ParamsLike] = None,
        data: Optional[Body] = None,
        headers: Optional[HeadersLike] = None,
        cookies: Optional[CookiesLike] = None,
        files: FilesLike = None,
        auth: AuthLike = None,
        timeout: Timeout = None,
        allow_redirects: bool = True,
        proxies: Optional[Proxies] = None,
        hooks: Optional[Hooks] = None,
        stream: Optional[bool] = None,
        verify: Optional[Verify] = None,
        cert: Any = None,
        json: Any = None,
        content: Optional[Body] = None,
        multipart: Optional[CurlMime] = None,
        impersonate: Optional[Impersonate] = None,
        proxy: Optional[str] = None,
        http_version: Optional[HttpVersion] = None,
        max_redirects: Optional[int] = None,
        max_response_bytes: Optional[int] = None,
        referer: Optional[str] = None,
        accept_encoding: Optional[str] = None,
        default_encoding: Optional[Union[str, Callable[[bytes], str]]] = None,
        discard_cookies: Optional[bool] = None,
        retry: Retry = None,
        cache_mode: CacheMode = None,
        priority: Optional[int] = None,
        ja3: Optional[str] = None,
        akamai: Optional[str] = None,
        perk: Optional[str] = None,
        extra_fp: Optional[Union[ExtraFingerprints, Mapping[str, Any]]] = None,
        content_callback: Optional[ContentCallback] = None,
        raise_for_status: Optional[bool] = None,
        quote: Optional[bool] = None,
        curl_options: Optional[Mapping[Any, Any]] = None,
        interface: Optional[str] = None,
        doh_url: Optional[str] = None,
        max_recv_speed: Optional[int] = None,
        thread: Any = None,
        debug: Any = None,
    ) -> AsyncResponse:
        """Sends an ``OPTIONS``.

        Args:
            url: Absolute URL, or a relative one when ``base_url`` is set.
            params: Query parameters appended to the URL. A mapping, a sequence of
                key/value pairs, or an already-encoded string.
            data: Request body. A mapping or sequence of pairs is form-encoded;
                bytes/str are sent as-is; a file object, generator or async iterator
                switches to chunked upload; a file-like value in a mapping is sent as
                multipart.
            headers: Headers merged with the session's defaults.
            cookies: Cookies merged with the session jar for this request only.
            files: Multipart file parts, alone or alongside ``data``.
            auth: Per-call auth: callable, ``AuthBase``, or a 2-tuple.
            timeout: One deadline or ``(connect, read)``.
            allow_redirects: ``False`` returns the first 3xx instead of following it.
            proxies: Per-call proxy mapping, merged over the session's.
            hooks: Per-call response hooks.
            stream: ``True`` leaves the body unread so ``iter_content`` can consume it
                incrementally.
            verify: ``True``/``False``/CA bundle path, for this call only.
            cert: Rejected; ABI v8 has no client-certificate setting.
            json: JSON body. Implies ``Content-Type: application/json`` unless a header
                already sets it.
            content: Raw body bytes; an alternative to ``data=`` for pre-encoded
                payloads. Passing both raises ``ValueError``.
            multipart: A :class:`~chrome_client.CurlMime` instance that becomes the
                whole body.
            impersonate: Profile override for this call, either an exact
                ``chrome_<major>`` name or a curl_cffi ``chrome<major>`` alias.
            proxy: Proxy URL for this call; wins over ``proxies``.
            http_version: Pin ``"v1"``, ``"v2"`` or ``"v3"`` for this call.
            max_redirects: Redirect cap. Below Chromium's 20, hops are driven from
                Python so the caller's limit wins.
            max_response_bytes: Body ceiling for this call.
            referer: Sets ``Referer`` through Chromium's own referrer path, so it
                reaches the wire the way a real Chrome sends it.
            accept_encoding: Overrides ``Accept-Encoding``. Chromium would otherwise
                choose it as part of the profile.
            default_encoding: Fallback encoding for this response only.
            discard_cookies: Neither send nor record cookies for this call.
            retry: Attempt count or strategy for this call.
            cache_mode: Chromium load flags, e.g. ``"bypass"`` to skip the HTTP cache.
            priority: Request priority hint passed to the Core.
            ja3: Rejected; the profile owns the ClientHello.
            akamai: Rejected; the profile owns the HTTP/2 fingerprint.
            perk: Rejected; the profile owns the TLS fingerprint.
            extra_fp: Fingerprint overrides; only ``header_order`` and ``form_boundary``
                are honourable.
            content_callback: Called with the fully buffered body on non-streaming
                requests.
            raise_for_status: Check the response before returning it.
            quote: ``False`` skips the percent-encoding pass over the URL.
            curl_options: Rejected; there is no libcurl surface here.
            interface: Rejected; no interface binding in ABI v8.
            doh_url: Rejected; DoH is not exposed.
            max_recv_speed: Rejected; Chromium owns transfer pacing.
            thread: Rejected; the sync path already runs on the calling thread.
            debug: Rejected; not a supported option.

        Returns:
            The :class:`~chrome_client.AsyncResponse`.
        """
        ...

    async def head(
        self,
        url: str,
        params: Optional[ParamsLike] = None,
        data: Optional[Body] = None,
        headers: Optional[HeadersLike] = None,
        cookies: Optional[CookiesLike] = None,
        files: FilesLike = None,
        auth: AuthLike = None,
        timeout: Timeout = None,
        allow_redirects: bool = False,
        proxies: Optional[Proxies] = None,
        hooks: Optional[Hooks] = None,
        stream: Optional[bool] = None,
        verify: Optional[Verify] = None,
        cert: Any = None,
        json: Any = None,
        content: Optional[Body] = None,
        multipart: Optional[CurlMime] = None,
        impersonate: Optional[Impersonate] = None,
        proxy: Optional[str] = None,
        http_version: Optional[HttpVersion] = None,
        max_redirects: Optional[int] = None,
        max_response_bytes: Optional[int] = None,
        referer: Optional[str] = None,
        accept_encoding: Optional[str] = None,
        default_encoding: Optional[Union[str, Callable[[bytes], str]]] = None,
        discard_cookies: Optional[bool] = None,
        retry: Retry = None,
        cache_mode: CacheMode = None,
        priority: Optional[int] = None,
        ja3: Optional[str] = None,
        akamai: Optional[str] = None,
        perk: Optional[str] = None,
        extra_fp: Optional[Union[ExtraFingerprints, Mapping[str, Any]]] = None,
        content_callback: Optional[ContentCallback] = None,
        raise_for_status: Optional[bool] = None,
        quote: Optional[bool] = None,
        curl_options: Optional[Mapping[Any, Any]] = None,
        interface: Optional[str] = None,
        doh_url: Optional[str] = None,
        max_recv_speed: Optional[int] = None,
        thread: Any = None,
        debug: Any = None,
    ) -> AsyncResponse:
        """Sends a ``HEAD`` (``allow_redirects`` defaults to ``False``).

        Args:
            url: Absolute URL, or a relative one when ``base_url`` is set.
            params: Query parameters appended to the URL. A mapping, a sequence of
                key/value pairs, or an already-encoded string.
            data: Request body. A mapping or sequence of pairs is form-encoded;
                bytes/str are sent as-is; a file object, generator or async iterator
                switches to chunked upload; a file-like value in a mapping is sent as
                multipart.
            headers: Headers merged with the session's defaults.
            cookies: Cookies merged with the session jar for this request only.
            files: Multipart file parts, alone or alongside ``data``.
            auth: Per-call auth: callable, ``AuthBase``, or a 2-tuple.
            timeout: One deadline or ``(connect, read)``.
            allow_redirects: ``False`` returns the first 3xx instead of following it.
            proxies: Per-call proxy mapping, merged over the session's.
            hooks: Per-call response hooks.
            stream: ``True`` leaves the body unread so ``iter_content`` can consume it
                incrementally.
            verify: ``True``/``False``/CA bundle path, for this call only.
            cert: Rejected; ABI v8 has no client-certificate setting.
            json: JSON body. Implies ``Content-Type: application/json`` unless a header
                already sets it.
            content: Raw body bytes; an alternative to ``data=`` for pre-encoded
                payloads. Passing both raises ``ValueError``.
            multipart: A :class:`~chrome_client.CurlMime` instance that becomes the
                whole body.
            impersonate: Profile override for this call, either an exact
                ``chrome_<major>`` name or a curl_cffi ``chrome<major>`` alias.
            proxy: Proxy URL for this call; wins over ``proxies``.
            http_version: Pin ``"v1"``, ``"v2"`` or ``"v3"`` for this call.
            max_redirects: Redirect cap. Below Chromium's 20, hops are driven from
                Python so the caller's limit wins.
            max_response_bytes: Body ceiling for this call.
            referer: Sets ``Referer`` through Chromium's own referrer path, so it
                reaches the wire the way a real Chrome sends it.
            accept_encoding: Overrides ``Accept-Encoding``. Chromium would otherwise
                choose it as part of the profile.
            default_encoding: Fallback encoding for this response only.
            discard_cookies: Neither send nor record cookies for this call.
            retry: Attempt count or strategy for this call.
            cache_mode: Chromium load flags, e.g. ``"bypass"`` to skip the HTTP cache.
            priority: Request priority hint passed to the Core.
            ja3: Rejected; the profile owns the ClientHello.
            akamai: Rejected; the profile owns the HTTP/2 fingerprint.
            perk: Rejected; the profile owns the TLS fingerprint.
            extra_fp: Fingerprint overrides; only ``header_order`` and ``form_boundary``
                are honourable.
            content_callback: Called with the fully buffered body on non-streaming
                requests.
            raise_for_status: Check the response before returning it.
            quote: ``False`` skips the percent-encoding pass over the URL.
            curl_options: Rejected; there is no libcurl surface here.
            interface: Rejected; no interface binding in ABI v8.
            doh_url: Rejected; DoH is not exposed.
            max_recv_speed: Rejected; Chromium owns transfer pacing.
            thread: Rejected; the sync path already runs on the calling thread.
            debug: Rejected; not a supported option.

        Returns:
            The :class:`~chrome_client.AsyncResponse` with an empty body.
        """
        ...

    async def post(
        self,
        url: str,
        data: Optional[Body] = None,
        json: Any = None,
        params: Optional[ParamsLike] = None,
        headers: Optional[HeadersLike] = None,
        cookies: Optional[CookiesLike] = None,
        files: FilesLike = None,
        auth: AuthLike = None,
        timeout: Timeout = None,
        allow_redirects: bool = True,
        proxies: Optional[Proxies] = None,
        hooks: Optional[Hooks] = None,
        stream: Optional[bool] = None,
        verify: Optional[Verify] = None,
        cert: Any = None,
        content: Optional[Body] = None,
        multipart: Optional[CurlMime] = None,
        impersonate: Optional[Impersonate] = None,
        proxy: Optional[str] = None,
        http_version: Optional[HttpVersion] = None,
        max_redirects: Optional[int] = None,
        max_response_bytes: Optional[int] = None,
        referer: Optional[str] = None,
        accept_encoding: Optional[str] = None,
        default_encoding: Optional[Union[str, Callable[[bytes], str]]] = None,
        discard_cookies: Optional[bool] = None,
        retry: Retry = None,
        cache_mode: CacheMode = None,
        priority: Optional[int] = None,
        ja3: Optional[str] = None,
        akamai: Optional[str] = None,
        perk: Optional[str] = None,
        extra_fp: Optional[Union[ExtraFingerprints, Mapping[str, Any]]] = None,
        content_callback: Optional[ContentCallback] = None,
        raise_for_status: Optional[bool] = None,
        quote: Optional[bool] = None,
        curl_options: Optional[Mapping[Any, Any]] = None,
        interface: Optional[str] = None,
        doh_url: Optional[str] = None,
        max_recv_speed: Optional[int] = None,
        thread: Any = None,
        debug: Any = None,
    ) -> AsyncResponse:
        """Sends a ``POST``.

        Args:
            url: Absolute URL, or a relative one when ``base_url`` is set.
            data: Request body. A mapping or sequence of pairs is form-encoded;
                bytes/str are sent as-is; a file object, generator or async iterator
                switches to chunked upload; a file-like value in a mapping is sent as
                multipart.
            json: JSON body. Implies ``Content-Type: application/json`` unless a header
                already sets it.
            params: Query parameters appended to the URL. A mapping, a sequence of
                key/value pairs, or an already-encoded string.
            headers: Headers merged with the session's defaults.
            cookies: Cookies merged with the session jar for this request only.
            files: Multipart file parts, alone or alongside ``data``.
            auth: Per-call auth: callable, ``AuthBase``, or a 2-tuple.
            timeout: One deadline or ``(connect, read)``.
            allow_redirects: ``False`` returns the first 3xx instead of following it.
            proxies: Per-call proxy mapping, merged over the session's.
            hooks: Per-call response hooks.
            stream: ``True`` leaves the body unread so ``iter_content`` can consume it
                incrementally.
            verify: ``True``/``False``/CA bundle path, for this call only.
            cert: Rejected; ABI v8 has no client-certificate setting.
            content: Raw body bytes; an alternative to ``data=`` for pre-encoded
                payloads. Passing both raises ``ValueError``.
            multipart: A :class:`~chrome_client.CurlMime` instance that becomes the
                whole body.
            impersonate: Profile override for this call, either an exact
                ``chrome_<major>`` name or a curl_cffi ``chrome<major>`` alias.
            proxy: Proxy URL for this call; wins over ``proxies``.
            http_version: Pin ``"v1"``, ``"v2"`` or ``"v3"`` for this call.
            max_redirects: Redirect cap. Below Chromium's 20, hops are driven from
                Python so the caller's limit wins.
            max_response_bytes: Body ceiling for this call.
            referer: Sets ``Referer`` through Chromium's own referrer path, so it
                reaches the wire the way a real Chrome sends it.
            accept_encoding: Overrides ``Accept-Encoding``. Chromium would otherwise
                choose it as part of the profile.
            default_encoding: Fallback encoding for this response only.
            discard_cookies: Neither send nor record cookies for this call.
            retry: Attempt count or strategy for this call.
            cache_mode: Chromium load flags, e.g. ``"bypass"`` to skip the HTTP cache.
            priority: Request priority hint passed to the Core.
            ja3: Rejected; the profile owns the ClientHello.
            akamai: Rejected; the profile owns the HTTP/2 fingerprint.
            perk: Rejected; the profile owns the TLS fingerprint.
            extra_fp: Fingerprint overrides; only ``header_order`` and ``form_boundary``
                are honourable.
            content_callback: Called with the fully buffered body on non-streaming
                requests.
            raise_for_status: Check the response before returning it.
            quote: ``False`` skips the percent-encoding pass over the URL.
            curl_options: Rejected; there is no libcurl surface here.
            interface: Rejected; no interface binding in ABI v8.
            doh_url: Rejected; DoH is not exposed.
            max_recv_speed: Rejected; Chromium owns transfer pacing.
            thread: Rejected; the sync path already runs on the calling thread.
            debug: Rejected; not a supported option.

        Returns:
            The :class:`~chrome_client.AsyncResponse`.
        """
        ...

    async def put(
        self,
        url: str,
        data: Optional[Body] = None,
        params: Optional[ParamsLike] = None,
        headers: Optional[HeadersLike] = None,
        cookies: Optional[CookiesLike] = None,
        files: FilesLike = None,
        auth: AuthLike = None,
        timeout: Timeout = None,
        allow_redirects: bool = True,
        proxies: Optional[Proxies] = None,
        hooks: Optional[Hooks] = None,
        stream: Optional[bool] = None,
        verify: Optional[Verify] = None,
        cert: Any = None,
        json: Any = None,
        content: Optional[Body] = None,
        multipart: Optional[CurlMime] = None,
        impersonate: Optional[Impersonate] = None,
        proxy: Optional[str] = None,
        http_version: Optional[HttpVersion] = None,
        max_redirects: Optional[int] = None,
        max_response_bytes: Optional[int] = None,
        referer: Optional[str] = None,
        accept_encoding: Optional[str] = None,
        default_encoding: Optional[Union[str, Callable[[bytes], str]]] = None,
        discard_cookies: Optional[bool] = None,
        retry: Retry = None,
        cache_mode: CacheMode = None,
        priority: Optional[int] = None,
        ja3: Optional[str] = None,
        akamai: Optional[str] = None,
        perk: Optional[str] = None,
        extra_fp: Optional[Union[ExtraFingerprints, Mapping[str, Any]]] = None,
        content_callback: Optional[ContentCallback] = None,
        raise_for_status: Optional[bool] = None,
        quote: Optional[bool] = None,
        curl_options: Optional[Mapping[Any, Any]] = None,
        interface: Optional[str] = None,
        doh_url: Optional[str] = None,
        max_recv_speed: Optional[int] = None,
        thread: Any = None,
        debug: Any = None,
    ) -> AsyncResponse:
        """Sends a ``PUT``.

        Args:
            url: Absolute URL, or a relative one when ``base_url`` is set.
            data: Request body. A mapping or sequence of pairs is form-encoded;
                bytes/str are sent as-is; a file object, generator or async iterator
                switches to chunked upload; a file-like value in a mapping is sent as
                multipart.
            params: Query parameters appended to the URL. A mapping, a sequence of
                key/value pairs, or an already-encoded string.
            headers: Headers merged with the session's defaults.
            cookies: Cookies merged with the session jar for this request only.
            files: Multipart file parts, alone or alongside ``data``.
            auth: Per-call auth: callable, ``AuthBase``, or a 2-tuple.
            timeout: One deadline or ``(connect, read)``.
            allow_redirects: ``False`` returns the first 3xx instead of following it.
            proxies: Per-call proxy mapping, merged over the session's.
            hooks: Per-call response hooks.
            stream: ``True`` leaves the body unread so ``iter_content`` can consume it
                incrementally.
            verify: ``True``/``False``/CA bundle path, for this call only.
            cert: Rejected; ABI v8 has no client-certificate setting.
            json: JSON body. Implies ``Content-Type: application/json`` unless a header
                already sets it.
            content: Raw body bytes; an alternative to ``data=`` for pre-encoded
                payloads. Passing both raises ``ValueError``.
            multipart: A :class:`~chrome_client.CurlMime` instance that becomes the
                whole body.
            impersonate: Profile override for this call, either an exact
                ``chrome_<major>`` name or a curl_cffi ``chrome<major>`` alias.
            proxy: Proxy URL for this call; wins over ``proxies``.
            http_version: Pin ``"v1"``, ``"v2"`` or ``"v3"`` for this call.
            max_redirects: Redirect cap. Below Chromium's 20, hops are driven from
                Python so the caller's limit wins.
            max_response_bytes: Body ceiling for this call.
            referer: Sets ``Referer`` through Chromium's own referrer path, so it
                reaches the wire the way a real Chrome sends it.
            accept_encoding: Overrides ``Accept-Encoding``. Chromium would otherwise
                choose it as part of the profile.
            default_encoding: Fallback encoding for this response only.
            discard_cookies: Neither send nor record cookies for this call.
            retry: Attempt count or strategy for this call.
            cache_mode: Chromium load flags, e.g. ``"bypass"`` to skip the HTTP cache.
            priority: Request priority hint passed to the Core.
            ja3: Rejected; the profile owns the ClientHello.
            akamai: Rejected; the profile owns the HTTP/2 fingerprint.
            perk: Rejected; the profile owns the TLS fingerprint.
            extra_fp: Fingerprint overrides; only ``header_order`` and ``form_boundary``
                are honourable.
            content_callback: Called with the fully buffered body on non-streaming
                requests.
            raise_for_status: Check the response before returning it.
            quote: ``False`` skips the percent-encoding pass over the URL.
            curl_options: Rejected; there is no libcurl surface here.
            interface: Rejected; no interface binding in ABI v8.
            doh_url: Rejected; DoH is not exposed.
            max_recv_speed: Rejected; Chromium owns transfer pacing.
            thread: Rejected; the sync path already runs on the calling thread.
            debug: Rejected; not a supported option.

        Returns:
            The :class:`~chrome_client.AsyncResponse`.
        """
        ...

    async def patch(
        self,
        url: str,
        data: Optional[Body] = None,
        params: Optional[ParamsLike] = None,
        headers: Optional[HeadersLike] = None,
        cookies: Optional[CookiesLike] = None,
        files: FilesLike = None,
        auth: AuthLike = None,
        timeout: Timeout = None,
        allow_redirects: bool = True,
        proxies: Optional[Proxies] = None,
        hooks: Optional[Hooks] = None,
        stream: Optional[bool] = None,
        verify: Optional[Verify] = None,
        cert: Any = None,
        json: Any = None,
        content: Optional[Body] = None,
        multipart: Optional[CurlMime] = None,
        impersonate: Optional[Impersonate] = None,
        proxy: Optional[str] = None,
        http_version: Optional[HttpVersion] = None,
        max_redirects: Optional[int] = None,
        max_response_bytes: Optional[int] = None,
        referer: Optional[str] = None,
        accept_encoding: Optional[str] = None,
        default_encoding: Optional[Union[str, Callable[[bytes], str]]] = None,
        discard_cookies: Optional[bool] = None,
        retry: Retry = None,
        cache_mode: CacheMode = None,
        priority: Optional[int] = None,
        ja3: Optional[str] = None,
        akamai: Optional[str] = None,
        perk: Optional[str] = None,
        extra_fp: Optional[Union[ExtraFingerprints, Mapping[str, Any]]] = None,
        content_callback: Optional[ContentCallback] = None,
        raise_for_status: Optional[bool] = None,
        quote: Optional[bool] = None,
        curl_options: Optional[Mapping[Any, Any]] = None,
        interface: Optional[str] = None,
        doh_url: Optional[str] = None,
        max_recv_speed: Optional[int] = None,
        thread: Any = None,
        debug: Any = None,
    ) -> AsyncResponse:
        """Sends a ``PATCH``.

        Args:
            url: Absolute URL, or a relative one when ``base_url`` is set.
            data: Request body. A mapping or sequence of pairs is form-encoded;
                bytes/str are sent as-is; a file object, generator or async iterator
                switches to chunked upload; a file-like value in a mapping is sent as
                multipart.
            params: Query parameters appended to the URL. A mapping, a sequence of
                key/value pairs, or an already-encoded string.
            headers: Headers merged with the session's defaults.
            cookies: Cookies merged with the session jar for this request only.
            files: Multipart file parts, alone or alongside ``data``.
            auth: Per-call auth: callable, ``AuthBase``, or a 2-tuple.
            timeout: One deadline or ``(connect, read)``.
            allow_redirects: ``False`` returns the first 3xx instead of following it.
            proxies: Per-call proxy mapping, merged over the session's.
            hooks: Per-call response hooks.
            stream: ``True`` leaves the body unread so ``iter_content`` can consume it
                incrementally.
            verify: ``True``/``False``/CA bundle path, for this call only.
            cert: Rejected; ABI v8 has no client-certificate setting.
            json: JSON body. Implies ``Content-Type: application/json`` unless a header
                already sets it.
            content: Raw body bytes; an alternative to ``data=`` for pre-encoded
                payloads. Passing both raises ``ValueError``.
            multipart: A :class:`~chrome_client.CurlMime` instance that becomes the
                whole body.
            impersonate: Profile override for this call, either an exact
                ``chrome_<major>`` name or a curl_cffi ``chrome<major>`` alias.
            proxy: Proxy URL for this call; wins over ``proxies``.
            http_version: Pin ``"v1"``, ``"v2"`` or ``"v3"`` for this call.
            max_redirects: Redirect cap. Below Chromium's 20, hops are driven from
                Python so the caller's limit wins.
            max_response_bytes: Body ceiling for this call.
            referer: Sets ``Referer`` through Chromium's own referrer path, so it
                reaches the wire the way a real Chrome sends it.
            accept_encoding: Overrides ``Accept-Encoding``. Chromium would otherwise
                choose it as part of the profile.
            default_encoding: Fallback encoding for this response only.
            discard_cookies: Neither send nor record cookies for this call.
            retry: Attempt count or strategy for this call.
            cache_mode: Chromium load flags, e.g. ``"bypass"`` to skip the HTTP cache.
            priority: Request priority hint passed to the Core.
            ja3: Rejected; the profile owns the ClientHello.
            akamai: Rejected; the profile owns the HTTP/2 fingerprint.
            perk: Rejected; the profile owns the TLS fingerprint.
            extra_fp: Fingerprint overrides; only ``header_order`` and ``form_boundary``
                are honourable.
            content_callback: Called with the fully buffered body on non-streaming
                requests.
            raise_for_status: Check the response before returning it.
            quote: ``False`` skips the percent-encoding pass over the URL.
            curl_options: Rejected; there is no libcurl surface here.
            interface: Rejected; no interface binding in ABI v8.
            doh_url: Rejected; DoH is not exposed.
            max_recv_speed: Rejected; Chromium owns transfer pacing.
            thread: Rejected; the sync path already runs on the calling thread.
            debug: Rejected; not a supported option.

        Returns:
            The :class:`~chrome_client.AsyncResponse`.
        """
        ...

    async def delete(
        self,
        url: str,
        params: Optional[ParamsLike] = None,
        data: Optional[Body] = None,
        headers: Optional[HeadersLike] = None,
        cookies: Optional[CookiesLike] = None,
        files: FilesLike = None,
        auth: AuthLike = None,
        timeout: Timeout = None,
        allow_redirects: bool = True,
        proxies: Optional[Proxies] = None,
        hooks: Optional[Hooks] = None,
        stream: Optional[bool] = None,
        verify: Optional[Verify] = None,
        cert: Any = None,
        json: Any = None,
        content: Optional[Body] = None,
        multipart: Optional[CurlMime] = None,
        impersonate: Optional[Impersonate] = None,
        proxy: Optional[str] = None,
        http_version: Optional[HttpVersion] = None,
        max_redirects: Optional[int] = None,
        max_response_bytes: Optional[int] = None,
        referer: Optional[str] = None,
        accept_encoding: Optional[str] = None,
        default_encoding: Optional[Union[str, Callable[[bytes], str]]] = None,
        discard_cookies: Optional[bool] = None,
        retry: Retry = None,
        cache_mode: CacheMode = None,
        priority: Optional[int] = None,
        ja3: Optional[str] = None,
        akamai: Optional[str] = None,
        perk: Optional[str] = None,
        extra_fp: Optional[Union[ExtraFingerprints, Mapping[str, Any]]] = None,
        content_callback: Optional[ContentCallback] = None,
        raise_for_status: Optional[bool] = None,
        quote: Optional[bool] = None,
        curl_options: Optional[Mapping[Any, Any]] = None,
        interface: Optional[str] = None,
        doh_url: Optional[str] = None,
        max_recv_speed: Optional[int] = None,
        thread: Any = None,
        debug: Any = None,
    ) -> AsyncResponse:
        """Sends a ``DELETE``.

        Args:
            url: Absolute URL, or a relative one when ``base_url`` is set.
            params: Query parameters appended to the URL. A mapping, a sequence of
                key/value pairs, or an already-encoded string.
            data: Request body. A mapping or sequence of pairs is form-encoded;
                bytes/str are sent as-is; a file object, generator or async iterator
                switches to chunked upload; a file-like value in a mapping is sent as
                multipart.
            headers: Headers merged with the session's defaults.
            cookies: Cookies merged with the session jar for this request only.
            files: Multipart file parts, alone or alongside ``data``.
            auth: Per-call auth: callable, ``AuthBase``, or a 2-tuple.
            timeout: One deadline or ``(connect, read)``.
            allow_redirects: ``False`` returns the first 3xx instead of following it.
            proxies: Per-call proxy mapping, merged over the session's.
            hooks: Per-call response hooks.
            stream: ``True`` leaves the body unread so ``iter_content`` can consume it
                incrementally.
            verify: ``True``/``False``/CA bundle path, for this call only.
            cert: Rejected; ABI v8 has no client-certificate setting.
            json: JSON body. Implies ``Content-Type: application/json`` unless a header
                already sets it.
            content: Raw body bytes; an alternative to ``data=`` for pre-encoded
                payloads. Passing both raises ``ValueError``.
            multipart: A :class:`~chrome_client.CurlMime` instance that becomes the
                whole body.
            impersonate: Profile override for this call, either an exact
                ``chrome_<major>`` name or a curl_cffi ``chrome<major>`` alias.
            proxy: Proxy URL for this call; wins over ``proxies``.
            http_version: Pin ``"v1"``, ``"v2"`` or ``"v3"`` for this call.
            max_redirects: Redirect cap. Below Chromium's 20, hops are driven from
                Python so the caller's limit wins.
            max_response_bytes: Body ceiling for this call.
            referer: Sets ``Referer`` through Chromium's own referrer path, so it
                reaches the wire the way a real Chrome sends it.
            accept_encoding: Overrides ``Accept-Encoding``. Chromium would otherwise
                choose it as part of the profile.
            default_encoding: Fallback encoding for this response only.
            discard_cookies: Neither send nor record cookies for this call.
            retry: Attempt count or strategy for this call.
            cache_mode: Chromium load flags, e.g. ``"bypass"`` to skip the HTTP cache.
            priority: Request priority hint passed to the Core.
            ja3: Rejected; the profile owns the ClientHello.
            akamai: Rejected; the profile owns the HTTP/2 fingerprint.
            perk: Rejected; the profile owns the TLS fingerprint.
            extra_fp: Fingerprint overrides; only ``header_order`` and ``form_boundary``
                are honourable.
            content_callback: Called with the fully buffered body on non-streaming
                requests.
            raise_for_status: Check the response before returning it.
            quote: ``False`` skips the percent-encoding pass over the URL.
            curl_options: Rejected; there is no libcurl surface here.
            interface: Rejected; no interface binding in ABI v8.
            doh_url: Rejected; DoH is not exposed.
            max_recv_speed: Rejected; Chromium owns transfer pacing.
            thread: Rejected; the sync path already runs on the calling thread.
            debug: Rejected; not a supported option.

        Returns:
            The :class:`~chrome_client.AsyncResponse`.
        """
        ...

    async def trace(
        self,
        url: str,
        params: Optional[ParamsLike] = None,
        data: Optional[Body] = None,
        headers: Optional[HeadersLike] = None,
        cookies: Optional[CookiesLike] = None,
        files: FilesLike = None,
        auth: AuthLike = None,
        timeout: Timeout = None,
        allow_redirects: bool = True,
        proxies: Optional[Proxies] = None,
        hooks: Optional[Hooks] = None,
        stream: Optional[bool] = None,
        verify: Optional[Verify] = None,
        cert: Any = None,
        json: Any = None,
        content: Optional[Body] = None,
        multipart: Optional[CurlMime] = None,
        impersonate: Optional[Impersonate] = None,
        proxy: Optional[str] = None,
        http_version: Optional[HttpVersion] = None,
        max_redirects: Optional[int] = None,
        max_response_bytes: Optional[int] = None,
        referer: Optional[str] = None,
        accept_encoding: Optional[str] = None,
        default_encoding: Optional[Union[str, Callable[[bytes], str]]] = None,
        discard_cookies: Optional[bool] = None,
        retry: Retry = None,
        cache_mode: CacheMode = None,
        priority: Optional[int] = None,
        ja3: Optional[str] = None,
        akamai: Optional[str] = None,
        perk: Optional[str] = None,
        extra_fp: Optional[Union[ExtraFingerprints, Mapping[str, Any]]] = None,
        content_callback: Optional[ContentCallback] = None,
        raise_for_status: Optional[bool] = None,
        quote: Optional[bool] = None,
        curl_options: Optional[Mapping[Any, Any]] = None,
        interface: Optional[str] = None,
        doh_url: Optional[str] = None,
        max_recv_speed: Optional[int] = None,
        thread: Any = None,
        debug: Any = None,
    ) -> AsyncResponse:
        """Sends a ``TRACE``.

        Args:
            url: Absolute URL, or a relative one when ``base_url`` is set.
            params: Query parameters appended to the URL. A mapping, a sequence of
                key/value pairs, or an already-encoded string.
            data: Request body. A mapping or sequence of pairs is form-encoded;
                bytes/str are sent as-is; a file object, generator or async iterator
                switches to chunked upload; a file-like value in a mapping is sent as
                multipart.
            headers: Headers merged with the session's defaults.
            cookies: Cookies merged with the session jar for this request only.
            files: Multipart file parts, alone or alongside ``data``.
            auth: Per-call auth: callable, ``AuthBase``, or a 2-tuple.
            timeout: One deadline or ``(connect, read)``.
            allow_redirects: ``False`` returns the first 3xx instead of following it.
            proxies: Per-call proxy mapping, merged over the session's.
            hooks: Per-call response hooks.
            stream: ``True`` leaves the body unread so ``iter_content`` can consume it
                incrementally.
            verify: ``True``/``False``/CA bundle path, for this call only.
            cert: Rejected; ABI v8 has no client-certificate setting.
            json: JSON body. Implies ``Content-Type: application/json`` unless a header
                already sets it.
            content: Raw body bytes; an alternative to ``data=`` for pre-encoded
                payloads. Passing both raises ``ValueError``.
            multipart: A :class:`~chrome_client.CurlMime` instance that becomes the
                whole body.
            impersonate: Profile override for this call, either an exact
                ``chrome_<major>`` name or a curl_cffi ``chrome<major>`` alias.
            proxy: Proxy URL for this call; wins over ``proxies``.
            http_version: Pin ``"v1"``, ``"v2"`` or ``"v3"`` for this call.
            max_redirects: Redirect cap. Below Chromium's 20, hops are driven from
                Python so the caller's limit wins.
            max_response_bytes: Body ceiling for this call.
            referer: Sets ``Referer`` through Chromium's own referrer path, so it
                reaches the wire the way a real Chrome sends it.
            accept_encoding: Overrides ``Accept-Encoding``. Chromium would otherwise
                choose it as part of the profile.
            default_encoding: Fallback encoding for this response only.
            discard_cookies: Neither send nor record cookies for this call.
            retry: Attempt count or strategy for this call.
            cache_mode: Chromium load flags, e.g. ``"bypass"`` to skip the HTTP cache.
            priority: Request priority hint passed to the Core.
            ja3: Rejected; the profile owns the ClientHello.
            akamai: Rejected; the profile owns the HTTP/2 fingerprint.
            perk: Rejected; the profile owns the TLS fingerprint.
            extra_fp: Fingerprint overrides; only ``header_order`` and ``form_boundary``
                are honourable.
            content_callback: Called with the fully buffered body on non-streaming
                requests.
            raise_for_status: Check the response before returning it.
            quote: ``False`` skips the percent-encoding pass over the URL.
            curl_options: Rejected; there is no libcurl surface here.
            interface: Rejected; no interface binding in ABI v8.
            doh_url: Rejected; DoH is not exposed.
            max_recv_speed: Rejected; Chromium owns transfer pacing.
            thread: Rejected; the sync path already runs on the calling thread.
            debug: Rejected; not a supported option.

        Returns:
            The :class:`~chrome_client.AsyncResponse`.
        """
        ...

    async def query(
        self,
        url: str,
        params: Optional[ParamsLike] = None,
        data: Optional[Body] = None,
        headers: Optional[HeadersLike] = None,
        cookies: Optional[CookiesLike] = None,
        files: FilesLike = None,
        auth: AuthLike = None,
        timeout: Timeout = None,
        allow_redirects: bool = True,
        proxies: Optional[Proxies] = None,
        hooks: Optional[Hooks] = None,
        stream: Optional[bool] = None,
        verify: Optional[Verify] = None,
        cert: Any = None,
        json: Any = None,
        content: Optional[Body] = None,
        multipart: Optional[CurlMime] = None,
        impersonate: Optional[Impersonate] = None,
        proxy: Optional[str] = None,
        http_version: Optional[HttpVersion] = None,
        max_redirects: Optional[int] = None,
        max_response_bytes: Optional[int] = None,
        referer: Optional[str] = None,
        accept_encoding: Optional[str] = None,
        default_encoding: Optional[Union[str, Callable[[bytes], str]]] = None,
        discard_cookies: Optional[bool] = None,
        retry: Retry = None,
        cache_mode: CacheMode = None,
        priority: Optional[int] = None,
        ja3: Optional[str] = None,
        akamai: Optional[str] = None,
        perk: Optional[str] = None,
        extra_fp: Optional[Union[ExtraFingerprints, Mapping[str, Any]]] = None,
        content_callback: Optional[ContentCallback] = None,
        raise_for_status: Optional[bool] = None,
        quote: Optional[bool] = None,
        curl_options: Optional[Mapping[Any, Any]] = None,
        interface: Optional[str] = None,
        doh_url: Optional[str] = None,
        max_recv_speed: Optional[int] = None,
        thread: Any = None,
        debug: Any = None,
    ) -> AsyncResponse:
        """Sends a ``QUERY``.

        Args:
            url: Absolute URL, or a relative one when ``base_url`` is set.
            params: Query parameters appended to the URL. A mapping, a sequence of
                key/value pairs, or an already-encoded string.
            data: Request body. A mapping or sequence of pairs is form-encoded;
                bytes/str are sent as-is; a file object, generator or async iterator
                switches to chunked upload; a file-like value in a mapping is sent as
                multipart.
            headers: Headers merged with the session's defaults.
            cookies: Cookies merged with the session jar for this request only.
            files: Multipart file parts, alone or alongside ``data``.
            auth: Per-call auth: callable, ``AuthBase``, or a 2-tuple.
            timeout: One deadline or ``(connect, read)``.
            allow_redirects: ``False`` returns the first 3xx instead of following it.
            proxies: Per-call proxy mapping, merged over the session's.
            hooks: Per-call response hooks.
            stream: ``True`` leaves the body unread so ``iter_content`` can consume it
                incrementally.
            verify: ``True``/``False``/CA bundle path, for this call only.
            cert: Rejected; ABI v8 has no client-certificate setting.
            json: JSON body. Implies ``Content-Type: application/json`` unless a header
                already sets it.
            content: Raw body bytes; an alternative to ``data=`` for pre-encoded
                payloads. Passing both raises ``ValueError``.
            multipart: A :class:`~chrome_client.CurlMime` instance that becomes the
                whole body.
            impersonate: Profile override for this call, either an exact
                ``chrome_<major>`` name or a curl_cffi ``chrome<major>`` alias.
            proxy: Proxy URL for this call; wins over ``proxies``.
            http_version: Pin ``"v1"``, ``"v2"`` or ``"v3"`` for this call.
            max_redirects: Redirect cap. Below Chromium's 20, hops are driven from
                Python so the caller's limit wins.
            max_response_bytes: Body ceiling for this call.
            referer: Sets ``Referer`` through Chromium's own referrer path, so it
                reaches the wire the way a real Chrome sends it.
            accept_encoding: Overrides ``Accept-Encoding``. Chromium would otherwise
                choose it as part of the profile.
            default_encoding: Fallback encoding for this response only.
            discard_cookies: Neither send nor record cookies for this call.
            retry: Attempt count or strategy for this call.
            cache_mode: Chromium load flags, e.g. ``"bypass"`` to skip the HTTP cache.
            priority: Request priority hint passed to the Core.
            ja3: Rejected; the profile owns the ClientHello.
            akamai: Rejected; the profile owns the HTTP/2 fingerprint.
            perk: Rejected; the profile owns the TLS fingerprint.
            extra_fp: Fingerprint overrides; only ``header_order`` and ``form_boundary``
                are honourable.
            content_callback: Called with the fully buffered body on non-streaming
                requests.
            raise_for_status: Check the response before returning it.
            quote: ``False`` skips the percent-encoding pass over the URL.
            curl_options: Rejected; there is no libcurl surface here.
            interface: Rejected; no interface binding in ABI v8.
            doh_url: Rejected; DoH is not exposed.
            max_recv_speed: Rejected; Chromium owns transfer pacing.
            thread: Rejected; the sync path already runs on the calling thread.
            debug: Rejected; not a supported option.

        Returns:
            The :class:`~chrome_client.AsyncResponse`.
        """
        ...

    async def websocket(
        self,
        url: str,
        origin: str = "",
        headers: Optional[HeadersLike] = None,
        timeout: Timeout = None,
        proxy: Optional[str] = None,
        proxies: Optional[Proxies] = None,
        impersonate: Optional[Impersonate] = None,
        protocols: Optional[Sequence[str]] = None,
        verify: Optional[Verify] = None,
    ) -> AsyncWebSocket:
        """Opens a WebSocket and awaits the handshake.

        Args:
            url: ``ws://`` or ``wss://`` URL.
            origin: Handshake ``Origin``; defaults to the URL's own origin.
            headers: Extra handshake headers. The Core's forbidden set
                (``Host``, ``Origin``, ``Connection``, ``Upgrade``,
                ``User-Agent``, ``Sec-WebSocket-*``) is rejected.
            timeout: Handshake deadline.
            proxy: Proxy URL for the handshake.
            proxies: Proxy mapping for the handshake.
            impersonate: Profile override for the handshake.
            protocols: Subprotocols to offer.
            verify: Verification setting for ``wss://``.

        Returns:
            A connected :class:`~chrome_client.AsyncWebSocket`.

        Raises:
            UnsupportedFeature: A forbidden handshake header was supplied.
            WebSocketTimeout: The handshake did not complete in time.
            WebSocketError: The handshake failed.

        Example:
            >>> async with AsyncSession() as session:
            ...     socket = await session.websocket("wss://echo.example.com")
            ...     async with socket:
            ...         await socket.send_json({"op": "ping"})
            ...         print(await socket.recv_json())
        """
        ...

    async def ws_connect(
        self,
        url: str,
        origin: str = "",
        headers: Optional[HeadersLike] = None,
        timeout: Timeout = None,
        proxy: Optional[str] = None,
        proxies: Optional[Proxies] = None,
        impersonate: Optional[Impersonate] = None,
        protocols: Optional[Sequence[str]] = None,
        verify: Optional[Verify] = None,
    ) -> AsyncWebSocket:
        """Alias of :meth:`websocket`, matching curl_cffi's spelling.

        Args:
            url: ``ws://`` or ``wss://`` URL.
            origin: Handshake ``Origin``.
            headers: Extra handshake headers.
            timeout: Handshake deadline.
            proxy: Proxy URL.
            proxies: Proxy mapping.
            impersonate: Profile override.
            protocols: Subprotocols to offer.
            verify: Verification setting.

        Returns:
            A connected :class:`~chrome_client.AsyncWebSocket`.
        """
        ...

    async def upkeep(self) -> int:  # type: ignore[override]  # curl_cffi spells it async
        """curl_cffi parity hook.

        Returns:
            Always ``0``; Chromium keeps idle sockets warm itself.
        """
        ...

    async def aclose(self) -> None:
        """Closes the session and every engine it owns.

        Awaitable counterpart of :meth:`BaseSession.close`, so it can be called
        from an ``async with`` block or a shutdown handler.
        """
        ...

    async def __aenter__(self) -> "AsyncSession":
        """Returns the session, so ``async with AsyncSession() as s:`` works.

        Returns:
            This session."""
        ...
    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]] = ...,
        exc: Optional[BaseException] = ...,
        tb: Optional[TracebackType] = ...,
    ) -> None:
        """Awaits :meth:`AsyncSession.aclose` on the way out.

        Args:
            exc_type: Exception type, if any.
            exc: Exception instance, if any.
            tb: Traceback, if any."""
        ...


#: ``Client`` is this package's historical name for :class:`Session`; curl_cffi
#: spells it the same way. It is the same class object at runtime.
Client: TypeAlias = Session
#: ``AsyncClient`` is the historical name for :class:`AsyncSession`.
AsyncClient: TypeAlias = AsyncSession


def split_proxy_credentials(
    proxy: Optional[str],
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Splits ``user:pass@host:port`` out of a proxy URL.

    Chromium's proxy rules cannot carry userinfo: ``ParseFromString`` does not
    accept an ``@`` in the rule, so ``proxy="http://user:pass@host:port"`` parses
    to an empty rule list and every request fails with
    ``ERR_NO_SUPPORTED_PROXIES`` (-336) before a socket is opened. The
    credentials have to travel in the Engine's separate ``proxy_username`` /
    ``proxy_password`` fields.

    Args:
        proxy: The proxy URL as the caller wrote it, with or without userinfo.

    Returns:
        ``(proxy_without_userinfo, username, password)``. The username and
        password are ``None`` when the URL carried no userinfo, which leaves the
        session-level ``proxy_auth`` in charge.

    Example:
        >>> split_proxy_credentials("http://user:pa%40ss@127.0.0.1:8080")
        ('http://127.0.0.1:8080', 'user', 'pa@ss')
    """
    ...


def proxy_from_proxies(url: str, proxies: Optional[Proxies]) -> Optional[str]:
    """Selects a requests-style proxy mapping entry for ``url``.

    Args:
        url: The URL about to be requested. ``ws://``/``wss://`` are matched
            with their ``http``/``https`` equivalents.
        proxies: The mapping to search. Keys are tried in the order
            ``scheme://host``, ``scheme``, ``all://host``, ``all``, so a
            host-specific entry beats a scheme-wide one.

    Returns:
        The matching proxy URL, or ``None`` when no key matches.

    Raises:
        TypeError: ``proxies`` is not a mapping, or an entry is neither a string
            nor ``None``.

    Example:
        >>> proxy_from_proxies("https://example.com",
        ...                    {"https://example.com": "http://127.0.0.1:8080"})
        'http://127.0.0.1:8080'
    """
    ...


def merge_setting(
    request_setting: Any, session_setting: Any, dict_class: Type[Any] = ...
) -> Any:
    """requests' merge rule: the request wins, and ``None`` deletes a key.

    Args:
        request_setting: The per-call value; wins when both are set.
        session_setting: The session default.
        dict_class: Mapping type used for the merge result, ``CaseInsensitiveDict``
            by default.

    Returns:
        The merged setting. Non-mapping values are replaced wholesale.

    Example:
        >>> merge_setting({"a": "1"}, {"b": "2"})
        CaseInsensitiveDict({'b': '2', 'a': '1'})
    """
    ...


def merge_hooks(
    request_hooks: Optional[Hooks], session_hooks: Optional[Hooks], dict_class: Type[Any] = ...
) -> Optional[Hooks]:
    """Merges per-request response hooks with the session's.

    Args:
        request_hooks: Hooks supplied on the call.
        session_hooks: Hooks configured on the session.
        dict_class: Mapping type used for the merge result.

    Returns:
        The merged hooks, or whichever side was non-empty.
    """
    ...
