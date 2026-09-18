"""Type surface of :mod:`chrome_client.api`.

``requests`` creates a throwaway ``Session`` for every module-level call.  That is
not viable here: one session owns a Chromium ``URLRequestContext`` with its own
threads, socket pools and caches, so a per-call session would cost megabytes and a
thread spin-up per request.

So ``chrome_client.get(...)`` and friends share one process-wide session.  The
deliberate consequence is that module-level calls also share a connection pool and
a cookie store, unlike requests.  Use an explicit ``Session`` when isolation
matters, and :func:`close_shared_session` to drop the shared one.

Passing a constructor-only option (``base_url``, ``default_headers``,
``trust_env``, ``max_engines``, ``user_agent``, ``accept_language``, ``cache``,
``response_class``, ``proxy_auth``, ``max_clients``) builds a private session for
that one call instead of reconfiguring the shared one.

    import chrome_client as requests
    response = requests.get("https://example.com", impersonate="chrome_153")
"""

from typing import Any, Callable, Mapping, Optional, Union

from ._types import (
    AuthLike,
    Body,
    CacheMode,
    ContentCallback,
    CookiesLike,
    FilesLike,
    HeadersLike,
    Hooks,
    ParamsLike,
    Proxies,
    ResponseClass,
    Retry,
    Timeout,
    Verify,
)
from .impersonate import ExtraFingerprints, HttpVersion, Impersonate
from .models import Response
from .multipart import CurlMime
from .sessions import AsyncSession, Session


def shared_session() -> Session:
    """Returns the process-wide session, creating it on first use.

    The pid check makes a forked child build its own: Chromium's threads do not
    survive ``fork()``.

    Returns:
        The shared :class:`~chrome_client.Session`.

    Example:
        >>> import chrome_client
        >>> chrome_client.shared_session() is chrome_client.shared_session()
        True
    """
    ...


def close_shared_session() -> None:
    """Closes and drops the process-wide session.

    Call this before a fork, or at shutdown, to release the engine's threads,
    sockets and cookies. The next module-level call builds a fresh session.

    Example:
        >>> import chrome_client
        >>> chrome_client.close_shared_session()
    """
    ...


def session(
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
    default_encoding: Union[str, Callable[[bytes], str]] = 'utf-8',
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
) -> Session:
    """Creates a synchronous :class:`~chrome_client.Session`.

    Args:
        impersonate: The pinned Chromium profile that owns the TLS ClientHello, ALPN,
            HTTP/2 SETTINGS and HTTP/3 transport parameters. Accepts
            ``chrome_99``..``chrome_153``, the curl_cffi spelling
            ``chrome99``..``chrome153``, and ``chrome``/``chromium`` for the
            newest pinned version.
        proxy: A single proxy URL for every request, e.g.
            ``"http://user:pass@127.0.0.1:8080"`` or ``"socks5://127.0.0.1:1080"``.
            Overrides ``proxies``.
        proxies: requests-style mapping, matched in the order ``scheme://host``,
            ``scheme``, ``all://host``, ``all``. Mutable at runtime.
        proxy_auth: ``(username, password)`` applied to the proxy.
        verify: ``True`` (default), ``False`` to skip verification, or a CA bundle path
            in PEM form. An engine-level setting.
        timeout: One deadline in seconds, or ``(connect, read)``. ABI v8 carries a
            single deadline, so the larger of the two is used.
        headers: Default headers merged into every request.
        cookies: Initial cookies, as a mapping or a jar.
        params: Default query parameters.
        auth: Default auth: a callable, an :class:`~chrome_client.AuthBase`, or a
            ``(user, password)`` tuple.
        cert: Rejected. Client certificates need a Core setting ABI v8 does not expose.
        stream: Default for the per-request ``stream=`` flag.
        hooks: ``{"response": callable}`` hooks.
        max_redirects: Cap on redirects. Below Chromium's own cap of 20 the hops are
            driven from Python so the caller's limit is honoured.
        trust_env: Read ``HTTP_PROXY``/``HTTPS_PROXY``/``NO_PROXY``,
            ``REQUESTS_CA_BUNDLE``/``CURL_CA_BUNDLE`` and ``.netrc``.
        allow_redirects: Default redirect policy.
        max_response_bytes: Body ceiling; exceeding it cancels the request and raises
            :class:`~chrome_client.ResponseTooLarge`.
        base_url: Prefix for relative URLs.
        http_version: Pin ``"v1"``, ``"v2"`` or ``"v3"`` instead of letting Chromium
            negotiate through ALPN and Alt-Svc.
        ja3: Rejected; the profile owns the ClientHello.
        akamai: Rejected; the profile owns the HTTP/2 fingerprint.
        perk: Rejected; the profile owns the TLS fingerprint.
        extra_fp: :class:`~chrome_client.ExtraFingerprints` overrides. Only
            ``header_order`` and ``form_boundary`` can be honoured.
        default_headers: Kept for source compatibility. The facade's header set is
            always empty; Chromium emits its own defaults.
        default_encoding: Fallback encoding for ``Response.text``, or a callable taking
            the body and returning an encoding name.
        discard_cookies: Neither send nor record cookies.
        raise_for_status: Check every response before returning it.
        retry: Attempt count, or a :class:`RetryStrategy`.
        cache: Enable the Chromium HTTP cache for this session's engines.
        user_agent: Engine-level User-Agent for HTTP *and* WebSocket. The Core rejects a
            per-request UA because its placement is part of the fingerprint.
        accept_language: Engine-level ``Accept-Language``.
        interface: Rejected; ABI v8 has no network-interface binding.
        doh_url: Rejected; DNS-over-HTTPS config is not exposed.
        max_recv_speed: Rejected; Chromium owns transfer pacing.
        curl_options: Rejected; there is no libcurl surface here.
        max_engines: How many distinct engine configurations one session keeps alive for
            per-request overrides.
        response_class: Subclass to instantiate instead of
            :class:`~chrome_client.Response`/``AsyncResponse``.

    Returns:
        The new session. Close it, or use it as a context manager.

    Raises:
        UnsupportedFeature: A rejected option was supplied.
        ImpersonateError: ``impersonate`` is out of range or the wrong family.

    Example:
        >>> with chrome_client.session(impersonate="chrome_153") as s:
        ...     s.get("https://example.com")
    """
    ...


def async_session(
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
    default_encoding: Union[str, Callable[[bytes], str]] = 'utf-8',
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
) -> AsyncSession:
    """Creates an :class:`~chrome_client.AsyncSession`.

    Args:
        impersonate: The pinned Chromium profile that owns the TLS ClientHello, ALPN,
            HTTP/2 SETTINGS and HTTP/3 transport parameters. Accepts
            ``chrome_99``..``chrome_153``, the curl_cffi spelling
            ``chrome99``..``chrome153``, and ``chrome``/``chromium`` for the
            newest pinned version.
        proxy: A single proxy URL for every request, e.g.
            ``"http://user:pass@127.0.0.1:8080"`` or ``"socks5://127.0.0.1:1080"``.
            Overrides ``proxies``.
        proxies: requests-style mapping, matched in the order ``scheme://host``,
            ``scheme``, ``all://host``, ``all``. Mutable at runtime.
        proxy_auth: ``(username, password)`` applied to the proxy.
        verify: ``True`` (default), ``False`` to skip verification, or a CA bundle path
            in PEM form. An engine-level setting.
        timeout: One deadline in seconds, or ``(connect, read)``. ABI v8 carries a
            single deadline, so the larger of the two is used.
        headers: Default headers merged into every request.
        cookies: Initial cookies, as a mapping or a jar.
        params: Default query parameters.
        auth: Default auth: a callable, an :class:`~chrome_client.AuthBase`, or a
            ``(user, password)`` tuple.
        cert: Rejected. Client certificates need a Core setting ABI v8 does not expose.
        stream: Default for the per-request ``stream=`` flag.
        hooks: ``{"response": callable}`` hooks.
        max_redirects: Cap on redirects. Below Chromium's own cap of 20 the hops are
            driven from Python so the caller's limit is honoured.
        trust_env: Read ``HTTP_PROXY``/``HTTPS_PROXY``/``NO_PROXY``,
            ``REQUESTS_CA_BUNDLE``/``CURL_CA_BUNDLE`` and ``.netrc``.
        allow_redirects: Default redirect policy.
        max_response_bytes: Body ceiling; exceeding it cancels the request and raises
            :class:`~chrome_client.ResponseTooLarge`.
        base_url: Prefix for relative URLs.
        http_version: Pin ``"v1"``, ``"v2"`` or ``"v3"`` instead of letting Chromium
            negotiate through ALPN and Alt-Svc.
        ja3: Rejected; the profile owns the ClientHello.
        akamai: Rejected; the profile owns the HTTP/2 fingerprint.
        perk: Rejected; the profile owns the TLS fingerprint.
        extra_fp: :class:`~chrome_client.ExtraFingerprints` overrides. Only
            ``header_order`` and ``form_boundary`` can be honoured.
        default_headers: Kept for source compatibility. The facade's header set is
            always empty; Chromium emits its own defaults.
        default_encoding: Fallback encoding for ``Response.text``, or a callable taking
            the body and returning an encoding name.
        discard_cookies: Neither send nor record cookies.
        raise_for_status: Check every response before returning it.
        retry: Attempt count, or a :class:`RetryStrategy`.
        cache: Enable the Chromium HTTP cache for this session's engines.
        user_agent: Engine-level User-Agent for HTTP *and* WebSocket. The Core rejects a
            per-request UA because its placement is part of the fingerprint.
        accept_language: Engine-level ``Accept-Language``.
        interface: Rejected; ABI v8 has no network-interface binding.
        doh_url: Rejected; DNS-over-HTTPS config is not exposed.
        max_recv_speed: Rejected; Chromium owns transfer pacing.
        curl_options: Rejected; there is no libcurl surface here.
        max_engines: How many distinct engine configurations one session keeps alive for
            per-request overrides.
        response_class: Subclass to instantiate instead of
            :class:`~chrome_client.Response`/``AsyncResponse``.

    Returns:
        The new session; ``await`` its ``aclose()`` or use it as an async
        context manager.

    Raises:
        UnsupportedFeature: A rejected option was supplied.
        ImpersonateError: ``impersonate`` is out of range or the wrong family.

    Example:
        >>> session = chrome_client.async_session(impersonate="chrome_153",
        ...                                       max_clients=64)
        >>> async with session:
        ...     response = await session.get("https://example.com")
    """
    ...


def request(
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
    base_url: Optional[str] = None,
    default_headers: Optional[bool] = None,
    trust_env: Optional[bool] = None,
    max_engines: Optional[int] = None,
    user_agent: Optional[str] = None,
    accept_language: Optional[str] = None,
    cache: Optional[bool] = None,
    response_class: ResponseClass = None,
    proxy_auth: AuthLike = None,
    max_clients: Optional[int] = None,
) -> Response:
    """Sends one request through the shared session.

    Keyword options match :meth:`chrome_client.Session.request`. Constructor-only
    options (``base_url``, ``default_headers``, ``trust_env``, ``max_engines``,
    ``user_agent``, ``accept_language``, ``cache``, ``response_class``,
    ``proxy_auth``, ``max_clients``) create a private session for this call, which
    is closed before returning.

    Args:
        method: HTTP method, e.g. ``"GET"``.
        url: Target URL.
        params: Query parameters.
        data: Form or raw body; a file/iterator switches to a chunked upload.
        headers: Headers merged with the shared session's.
        cookies: Per-call cookies.
        files: Multipart file parts.
        auth: Auth callable, ``AuthBase`` or ``(user, password)`` tuple.
        timeout: One deadline, or ``(connect, read)``.
        allow_redirects: ``False`` returns the 3xx itself.
        proxies: Proxy mapping for this call.
        hooks: Response hooks for this call.
        stream: ``True`` keeps the body unread for ``iter_content``.
        verify: ``True``/``False``/CA bundle path.
        cert: Rejected; ABI v8 has no client-certificate setting.
        json: JSON body.
        content: Raw body bytes; mutually exclusive with ``data=``.
        multipart: A :class:`~chrome_client.CurlMime` body.
        impersonate: Profile for this call, e.g. ``"chrome_153"``.
        proxy: Proxy URL for this call.
        http_version: Pin ``"v1"``/``"v2"``/``"v3"``.
        max_redirects: Redirect cap.
        max_response_bytes: Body ceiling.
        referer: Routed through Chromium's own referrer path.
        accept_encoding: Overrides ``Accept-Encoding``.
        default_encoding: Fallback encoding for the response body, or a callable
            taking the body bytes and returning an encoding name.
        discard_cookies: Neither send nor record cookies.
        retry: Attempt count or :class:`~chrome_client.RetryStrategy`.
        cache_mode: Chromium load flags.
        priority: Request priority hint.
        ja3: Rejected; the profile owns the ClientHello.
        akamai: Rejected; the profile owns the HTTP/2 fingerprint.
        perk: Rejected; the profile owns the TLS fingerprint.
        extra_fp: Fingerprint overrides; only ``header_order`` and
            ``form_boundary`` are honourable.
        content_callback: Called with the fully buffered body.
        raise_for_status: Check the response before returning it.
        quote: ``False`` skips percent-encoding the URL.
        curl_options: Rejected.
        interface: Rejected.
        doh_url: Rejected.
        max_recv_speed: Rejected.
        thread: Rejected.
        debug: Rejected.
        base_url: Private-session option: prefix for relative URLs.
        default_headers: Private-session option: kept for compatibility.
        trust_env: Private-session option: read environment proxies/CA/netrc.
        max_engines: Private-session option: engines kept alive.
        user_agent: Private-session option: engine-level User-Agent.
        accept_language: Private-session option: engine-level
            ``Accept-Language``.
        cache: Private-session option: enable the HTTP cache.
        response_class: Private-session option: response class to instantiate.
        proxy_auth: Private-session option: ``(username, password)`` for the
            proxy.
        max_clients: Accepted for curl_cffi parity; ignored on the sync path.

    Returns:
        The :class:`~chrome_client.Response`.

    Raises:
        UnsupportedFeature: A rejected option was supplied.
        Timeout: The deadline elapsed.
        ConnectionError: Network, TLS or proxy failure.
        HTTPError: ``raise_for_status`` was on and the status is 4xx/5xx.

    Example:
        >>> import chrome_client as requests
        >>> requests.request("GET", "https://example.com", timeout=15).status_code
        200
    """
    ...


def get(
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
    base_url: Optional[str] = None,
    default_headers: Optional[bool] = None,
    trust_env: Optional[bool] = None,
    max_engines: Optional[int] = None,
    user_agent: Optional[str] = None,
    accept_language: Optional[str] = None,
    cache: Optional[bool] = None,
    response_class: ResponseClass = None,
    proxy_auth: AuthLike = None,
    max_clients: Optional[int] = None,
) -> Response:
    """Sends a ``GET`` through the shared session.

    Args:
        url: Target URL.
        params: Query parameters.
        data: Form or raw body; a file/iterator switches to a chunked upload.
        headers: Headers merged with the shared session's.
        cookies: Per-call cookies.
        files: Multipart file parts.
        auth: Auth callable, ``AuthBase`` or ``(user, password)`` tuple.
        timeout: One deadline, or ``(connect, read)``.
        allow_redirects: ``False`` returns the 3xx itself.
        proxies: Proxy mapping for this call.
        hooks: Response hooks for this call.
        stream: ``True`` keeps the body unread for ``iter_content``.
        verify: ``True``/``False``/CA bundle path.
        cert: Rejected; ABI v8 has no client-certificate setting.
        json: JSON body.
        content: Raw body bytes; mutually exclusive with ``data=``.
        multipart: A :class:`~chrome_client.CurlMime` body.
        impersonate: Profile for this call, e.g. ``"chrome_153"``.
        proxy: Proxy URL for this call.
        http_version: Pin ``"v1"``/``"v2"``/``"v3"``.
        max_redirects: Redirect cap.
        max_response_bytes: Body ceiling.
        referer: Routed through Chromium's own referrer path.
        accept_encoding: Overrides ``Accept-Encoding``.
        default_encoding: Fallback encoding for the response body, or a callable taking
            the body bytes and returning an encoding name.
        discard_cookies: Neither send nor record cookies.
        retry: Attempt count or :class:`~chrome_client.RetryStrategy`.
        cache_mode: Chromium load flags.
        priority: Request priority hint.
        ja3: Rejected; the profile owns the ClientHello.
        akamai: Rejected; the profile owns the HTTP/2 fingerprint.
        perk: Rejected; the profile owns the TLS fingerprint.
        extra_fp: Fingerprint overrides; only ``header_order`` and ``form_boundary`` are
            honourable.
        content_callback: Called with the fully buffered body.
        raise_for_status: Check the response before returning it.
        quote: ``False`` skips percent-encoding the URL.
        curl_options: Rejected.
        interface: Rejected.
        doh_url: Rejected.
        max_recv_speed: Rejected.
        thread: Rejected.
        debug: Rejected.
        base_url: Private-session option: prefix for relative URLs.
        default_headers: Private-session option: kept for compatibility.
        trust_env: Private-session option: read environment proxies/CA/netrc.
        max_engines: Private-session option: engines kept alive.
        user_agent: Private-session option: engine-level User-Agent.
        accept_language: Private-session option: engine-level ``Accept-Language``.
        cache: Private-session option: enable the HTTP cache.
        response_class: Private-session option: response class to instantiate.
        proxy_auth: Private-session option: ``(username, password)`` for the proxy.
        max_clients: Accepted for curl_cffi parity; ignored on the sync path.

    Returns:
        The :class:`~chrome_client.Response`.

    Example:
        >>> response = requests.get(
        ...     "https://example.com/api", params={"page": 1},
        ...     headers={"Accept": "application/json"},
        ...     impersonate="chrome_153", timeout=15)
        >>> print(response.status_code, response.reason, response.json())
    """
    ...


def options(
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
    base_url: Optional[str] = None,
    default_headers: Optional[bool] = None,
    trust_env: Optional[bool] = None,
    max_engines: Optional[int] = None,
    user_agent: Optional[str] = None,
    accept_language: Optional[str] = None,
    cache: Optional[bool] = None,
    response_class: ResponseClass = None,
    proxy_auth: AuthLike = None,
    max_clients: Optional[int] = None,
) -> Response:
    """Sends an ``OPTIONS`` through the shared session.

    Args:
        url: Target URL.
        params: Query parameters.
        data: Form or raw body; a file/iterator switches to a chunked upload.
        headers: Headers merged with the shared session's.
        cookies: Per-call cookies.
        files: Multipart file parts.
        auth: Auth callable, ``AuthBase`` or ``(user, password)`` tuple.
        timeout: One deadline, or ``(connect, read)``.
        allow_redirects: ``False`` returns the 3xx itself.
        proxies: Proxy mapping for this call.
        hooks: Response hooks for this call.
        stream: ``True`` keeps the body unread for ``iter_content``.
        verify: ``True``/``False``/CA bundle path.
        cert: Rejected; ABI v8 has no client-certificate setting.
        json: JSON body.
        content: Raw body bytes; mutually exclusive with ``data=``.
        multipart: A :class:`~chrome_client.CurlMime` body.
        impersonate: Profile for this call, e.g. ``"chrome_153"``.
        proxy: Proxy URL for this call.
        http_version: Pin ``"v1"``/``"v2"``/``"v3"``.
        max_redirects: Redirect cap.
        max_response_bytes: Body ceiling.
        referer: Routed through Chromium's own referrer path.
        accept_encoding: Overrides ``Accept-Encoding``.
        default_encoding: Fallback encoding for the response body, or a callable taking
            the body bytes and returning an encoding name.
        discard_cookies: Neither send nor record cookies.
        retry: Attempt count or :class:`~chrome_client.RetryStrategy`.
        cache_mode: Chromium load flags.
        priority: Request priority hint.
        ja3: Rejected; the profile owns the ClientHello.
        akamai: Rejected; the profile owns the HTTP/2 fingerprint.
        perk: Rejected; the profile owns the TLS fingerprint.
        extra_fp: Fingerprint overrides; only ``header_order`` and ``form_boundary`` are
            honourable.
        content_callback: Called with the fully buffered body.
        raise_for_status: Check the response before returning it.
        quote: ``False`` skips percent-encoding the URL.
        curl_options: Rejected.
        interface: Rejected.
        doh_url: Rejected.
        max_recv_speed: Rejected.
        thread: Rejected.
        debug: Rejected.
        base_url: Private-session option: prefix for relative URLs.
        default_headers: Private-session option: kept for compatibility.
        trust_env: Private-session option: read environment proxies/CA/netrc.
        max_engines: Private-session option: engines kept alive.
        user_agent: Private-session option: engine-level User-Agent.
        accept_language: Private-session option: engine-level ``Accept-Language``.
        cache: Private-session option: enable the HTTP cache.
        response_class: Private-session option: response class to instantiate.
        proxy_auth: Private-session option: ``(username, password)`` for the proxy.
        max_clients: Accepted for curl_cffi parity; ignored on the sync path.

    Returns:
        The :class:`~chrome_client.Response`.
    """
    ...


def head(
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
    base_url: Optional[str] = None,
    default_headers: Optional[bool] = None,
    trust_env: Optional[bool] = None,
    max_engines: Optional[int] = None,
    user_agent: Optional[str] = None,
    accept_language: Optional[str] = None,
    cache: Optional[bool] = None,
    response_class: ResponseClass = None,
    proxy_auth: AuthLike = None,
    max_clients: Optional[int] = None,
) -> Response:
    """Sends a ``HEAD`` through the shared session.

    ``allow_redirects`` defaults to ``False``, matching requests.

    Args:
        url: Target URL.
        params: Query parameters.
        data: Form or raw body; a file/iterator switches to a chunked upload.
        headers: Headers merged with the shared session's.
        cookies: Per-call cookies.
        files: Multipart file parts.
        auth: Auth callable, ``AuthBase`` or ``(user, password)`` tuple.
        timeout: One deadline, or ``(connect, read)``.
        allow_redirects: ``False`` returns the 3xx itself.
        proxies: Proxy mapping for this call.
        hooks: Response hooks for this call.
        stream: ``True`` keeps the body unread for ``iter_content``.
        verify: ``True``/``False``/CA bundle path.
        cert: Rejected; ABI v8 has no client-certificate setting.
        json: JSON body.
        content: Raw body bytes; mutually exclusive with ``data=``.
        multipart: A :class:`~chrome_client.CurlMime` body.
        impersonate: Profile for this call, e.g. ``"chrome_153"``.
        proxy: Proxy URL for this call.
        http_version: Pin ``"v1"``/``"v2"``/``"v3"``.
        max_redirects: Redirect cap.
        max_response_bytes: Body ceiling.
        referer: Routed through Chromium's own referrer path.
        accept_encoding: Overrides ``Accept-Encoding``.
        default_encoding: Fallback encoding for the response body, or a callable taking
            the body bytes and returning an encoding name.
        discard_cookies: Neither send nor record cookies.
        retry: Attempt count or :class:`~chrome_client.RetryStrategy`.
        cache_mode: Chromium load flags.
        priority: Request priority hint.
        ja3: Rejected; the profile owns the ClientHello.
        akamai: Rejected; the profile owns the HTTP/2 fingerprint.
        perk: Rejected; the profile owns the TLS fingerprint.
        extra_fp: Fingerprint overrides; only ``header_order`` and ``form_boundary`` are
            honourable.
        content_callback: Called with the fully buffered body.
        raise_for_status: Check the response before returning it.
        quote: ``False`` skips percent-encoding the URL.
        curl_options: Rejected.
        interface: Rejected.
        doh_url: Rejected.
        max_recv_speed: Rejected.
        thread: Rejected.
        debug: Rejected.
        base_url: Private-session option: prefix for relative URLs.
        default_headers: Private-session option: kept for compatibility.
        trust_env: Private-session option: read environment proxies/CA/netrc.
        max_engines: Private-session option: engines kept alive.
        user_agent: Private-session option: engine-level User-Agent.
        accept_language: Private-session option: engine-level ``Accept-Language``.
        cache: Private-session option: enable the HTTP cache.
        response_class: Private-session option: response class to instantiate.
        proxy_auth: Private-session option: ``(username, password)`` for the proxy.
        max_clients: Accepted for curl_cffi parity; ignored on the sync path.

    Returns:
        The :class:`~chrome_client.Response` with an empty body.
    """
    ...


def post(
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
    base_url: Optional[str] = None,
    default_headers: Optional[bool] = None,
    trust_env: Optional[bool] = None,
    max_engines: Optional[int] = None,
    user_agent: Optional[str] = None,
    accept_language: Optional[str] = None,
    cache: Optional[bool] = None,
    response_class: ResponseClass = None,
    proxy_auth: AuthLike = None,
    max_clients: Optional[int] = None,
) -> Response:
    """Sends a ``POST`` through the shared session.

    Args:
        url: Target URL.
        data: Form or raw body; a file/iterator switches to a chunked upload.
        json: JSON body.
        params: Query parameters.
        headers: Headers merged with the shared session's.
        cookies: Per-call cookies.
        files: Multipart file parts.
        auth: Auth callable, ``AuthBase`` or ``(user, password)`` tuple.
        timeout: One deadline, or ``(connect, read)``.
        allow_redirects: ``False`` returns the 3xx itself.
        proxies: Proxy mapping for this call.
        hooks: Response hooks for this call.
        stream: ``True`` keeps the body unread for ``iter_content``.
        verify: ``True``/``False``/CA bundle path.
        cert: Rejected; ABI v8 has no client-certificate setting.
        content: Raw body bytes; mutually exclusive with ``data=``.
        multipart: A :class:`~chrome_client.CurlMime` body.
        impersonate: Profile for this call, e.g. ``"chrome_153"``.
        proxy: Proxy URL for this call.
        http_version: Pin ``"v1"``/``"v2"``/``"v3"``.
        max_redirects: Redirect cap.
        max_response_bytes: Body ceiling.
        referer: Routed through Chromium's own referrer path.
        accept_encoding: Overrides ``Accept-Encoding``.
        default_encoding: Fallback encoding for the response body, or a callable taking
            the body bytes and returning an encoding name.
        discard_cookies: Neither send nor record cookies.
        retry: Attempt count or :class:`~chrome_client.RetryStrategy`.
        cache_mode: Chromium load flags.
        priority: Request priority hint.
        ja3: Rejected; the profile owns the ClientHello.
        akamai: Rejected; the profile owns the HTTP/2 fingerprint.
        perk: Rejected; the profile owns the TLS fingerprint.
        extra_fp: Fingerprint overrides; only ``header_order`` and ``form_boundary`` are
            honourable.
        content_callback: Called with the fully buffered body.
        raise_for_status: Check the response before returning it.
        quote: ``False`` skips percent-encoding the URL.
        curl_options: Rejected.
        interface: Rejected.
        doh_url: Rejected.
        max_recv_speed: Rejected.
        thread: Rejected.
        debug: Rejected.
        base_url: Private-session option: prefix for relative URLs.
        default_headers: Private-session option: kept for compatibility.
        trust_env: Private-session option: read environment proxies/CA/netrc.
        max_engines: Private-session option: engines kept alive.
        user_agent: Private-session option: engine-level User-Agent.
        accept_language: Private-session option: engine-level ``Accept-Language``.
        cache: Private-session option: enable the HTTP cache.
        response_class: Private-session option: response class to instantiate.
        proxy_auth: Private-session option: ``(username, password)`` for the proxy.
        max_clients: Accepted for curl_cffi parity; ignored on the sync path.

    Returns:
        The :class:`~chrome_client.Response`.

    Example:
        >>> requests.post("https://example.com/api", json={"title": "t"})
    """
    ...


def put(
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
    base_url: Optional[str] = None,
    default_headers: Optional[bool] = None,
    trust_env: Optional[bool] = None,
    max_engines: Optional[int] = None,
    user_agent: Optional[str] = None,
    accept_language: Optional[str] = None,
    cache: Optional[bool] = None,
    response_class: ResponseClass = None,
    proxy_auth: AuthLike = None,
    max_clients: Optional[int] = None,
) -> Response:
    """Sends a ``PUT`` through the shared session.

    Args:
        url: Target URL.
        data: Form or raw body; a file/iterator switches to a chunked upload.
        params: Query parameters.
        headers: Headers merged with the shared session's.
        cookies: Per-call cookies.
        files: Multipart file parts.
        auth: Auth callable, ``AuthBase`` or ``(user, password)`` tuple.
        timeout: One deadline, or ``(connect, read)``.
        allow_redirects: ``False`` returns the 3xx itself.
        proxies: Proxy mapping for this call.
        hooks: Response hooks for this call.
        stream: ``True`` keeps the body unread for ``iter_content``.
        verify: ``True``/``False``/CA bundle path.
        cert: Rejected; ABI v8 has no client-certificate setting.
        json: JSON body.
        content: Raw body bytes; mutually exclusive with ``data=``.
        multipart: A :class:`~chrome_client.CurlMime` body.
        impersonate: Profile for this call, e.g. ``"chrome_153"``.
        proxy: Proxy URL for this call.
        http_version: Pin ``"v1"``/``"v2"``/``"v3"``.
        max_redirects: Redirect cap.
        max_response_bytes: Body ceiling.
        referer: Routed through Chromium's own referrer path.
        accept_encoding: Overrides ``Accept-Encoding``.
        default_encoding: Fallback encoding for the response body, or a callable taking
            the body bytes and returning an encoding name.
        discard_cookies: Neither send nor record cookies.
        retry: Attempt count or :class:`~chrome_client.RetryStrategy`.
        cache_mode: Chromium load flags.
        priority: Request priority hint.
        ja3: Rejected; the profile owns the ClientHello.
        akamai: Rejected; the profile owns the HTTP/2 fingerprint.
        perk: Rejected; the profile owns the TLS fingerprint.
        extra_fp: Fingerprint overrides; only ``header_order`` and ``form_boundary`` are
            honourable.
        content_callback: Called with the fully buffered body.
        raise_for_status: Check the response before returning it.
        quote: ``False`` skips percent-encoding the URL.
        curl_options: Rejected.
        interface: Rejected.
        doh_url: Rejected.
        max_recv_speed: Rejected.
        thread: Rejected.
        debug: Rejected.
        base_url: Private-session option: prefix for relative URLs.
        default_headers: Private-session option: kept for compatibility.
        trust_env: Private-session option: read environment proxies/CA/netrc.
        max_engines: Private-session option: engines kept alive.
        user_agent: Private-session option: engine-level User-Agent.
        accept_language: Private-session option: engine-level ``Accept-Language``.
        cache: Private-session option: enable the HTTP cache.
        response_class: Private-session option: response class to instantiate.
        proxy_auth: Private-session option: ``(username, password)`` for the proxy.
        max_clients: Accepted for curl_cffi parity; ignored on the sync path.

    Returns:
        The :class:`~chrome_client.Response`.
    """
    ...


def patch(
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
    base_url: Optional[str] = None,
    default_headers: Optional[bool] = None,
    trust_env: Optional[bool] = None,
    max_engines: Optional[int] = None,
    user_agent: Optional[str] = None,
    accept_language: Optional[str] = None,
    cache: Optional[bool] = None,
    response_class: ResponseClass = None,
    proxy_auth: AuthLike = None,
    max_clients: Optional[int] = None,
) -> Response:
    """Sends a ``PATCH`` through the shared session.

    Args:
        url: Target URL.
        data: Form or raw body; a file/iterator switches to a chunked upload.
        params: Query parameters.
        headers: Headers merged with the shared session's.
        cookies: Per-call cookies.
        files: Multipart file parts.
        auth: Auth callable, ``AuthBase`` or ``(user, password)`` tuple.
        timeout: One deadline, or ``(connect, read)``.
        allow_redirects: ``False`` returns the 3xx itself.
        proxies: Proxy mapping for this call.
        hooks: Response hooks for this call.
        stream: ``True`` keeps the body unread for ``iter_content``.
        verify: ``True``/``False``/CA bundle path.
        cert: Rejected; ABI v8 has no client-certificate setting.
        json: JSON body.
        content: Raw body bytes; mutually exclusive with ``data=``.
        multipart: A :class:`~chrome_client.CurlMime` body.
        impersonate: Profile for this call, e.g. ``"chrome_153"``.
        proxy: Proxy URL for this call.
        http_version: Pin ``"v1"``/``"v2"``/``"v3"``.
        max_redirects: Redirect cap.
        max_response_bytes: Body ceiling.
        referer: Routed through Chromium's own referrer path.
        accept_encoding: Overrides ``Accept-Encoding``.
        default_encoding: Fallback encoding for the response body, or a callable taking
            the body bytes and returning an encoding name.
        discard_cookies: Neither send nor record cookies.
        retry: Attempt count or :class:`~chrome_client.RetryStrategy`.
        cache_mode: Chromium load flags.
        priority: Request priority hint.
        ja3: Rejected; the profile owns the ClientHello.
        akamai: Rejected; the profile owns the HTTP/2 fingerprint.
        perk: Rejected; the profile owns the TLS fingerprint.
        extra_fp: Fingerprint overrides; only ``header_order`` and ``form_boundary`` are
            honourable.
        content_callback: Called with the fully buffered body.
        raise_for_status: Check the response before returning it.
        quote: ``False`` skips percent-encoding the URL.
        curl_options: Rejected.
        interface: Rejected.
        doh_url: Rejected.
        max_recv_speed: Rejected.
        thread: Rejected.
        debug: Rejected.
        base_url: Private-session option: prefix for relative URLs.
        default_headers: Private-session option: kept for compatibility.
        trust_env: Private-session option: read environment proxies/CA/netrc.
        max_engines: Private-session option: engines kept alive.
        user_agent: Private-session option: engine-level User-Agent.
        accept_language: Private-session option: engine-level ``Accept-Language``.
        cache: Private-session option: enable the HTTP cache.
        response_class: Private-session option: response class to instantiate.
        proxy_auth: Private-session option: ``(username, password)`` for the proxy.
        max_clients: Accepted for curl_cffi parity; ignored on the sync path.

    Returns:
        The :class:`~chrome_client.Response`.
    """
    ...


def delete(
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
    base_url: Optional[str] = None,
    default_headers: Optional[bool] = None,
    trust_env: Optional[bool] = None,
    max_engines: Optional[int] = None,
    user_agent: Optional[str] = None,
    accept_language: Optional[str] = None,
    cache: Optional[bool] = None,
    response_class: ResponseClass = None,
    proxy_auth: AuthLike = None,
    max_clients: Optional[int] = None,
) -> Response:
    """Sends a ``DELETE`` through the shared session.

    Args:
        url: Target URL.
        params: Query parameters.
        data: Form or raw body; a file/iterator switches to a chunked upload.
        headers: Headers merged with the shared session's.
        cookies: Per-call cookies.
        files: Multipart file parts.
        auth: Auth callable, ``AuthBase`` or ``(user, password)`` tuple.
        timeout: One deadline, or ``(connect, read)``.
        allow_redirects: ``False`` returns the 3xx itself.
        proxies: Proxy mapping for this call.
        hooks: Response hooks for this call.
        stream: ``True`` keeps the body unread for ``iter_content``.
        verify: ``True``/``False``/CA bundle path.
        cert: Rejected; ABI v8 has no client-certificate setting.
        json: JSON body.
        content: Raw body bytes; mutually exclusive with ``data=``.
        multipart: A :class:`~chrome_client.CurlMime` body.
        impersonate: Profile for this call, e.g. ``"chrome_153"``.
        proxy: Proxy URL for this call.
        http_version: Pin ``"v1"``/``"v2"``/``"v3"``.
        max_redirects: Redirect cap.
        max_response_bytes: Body ceiling.
        referer: Routed through Chromium's own referrer path.
        accept_encoding: Overrides ``Accept-Encoding``.
        default_encoding: Fallback encoding for the response body, or a callable taking
            the body bytes and returning an encoding name.
        discard_cookies: Neither send nor record cookies.
        retry: Attempt count or :class:`~chrome_client.RetryStrategy`.
        cache_mode: Chromium load flags.
        priority: Request priority hint.
        ja3: Rejected; the profile owns the ClientHello.
        akamai: Rejected; the profile owns the HTTP/2 fingerprint.
        perk: Rejected; the profile owns the TLS fingerprint.
        extra_fp: Fingerprint overrides; only ``header_order`` and ``form_boundary`` are
            honourable.
        content_callback: Called with the fully buffered body.
        raise_for_status: Check the response before returning it.
        quote: ``False`` skips percent-encoding the URL.
        curl_options: Rejected.
        interface: Rejected.
        doh_url: Rejected.
        max_recv_speed: Rejected.
        thread: Rejected.
        debug: Rejected.
        base_url: Private-session option: prefix for relative URLs.
        default_headers: Private-session option: kept for compatibility.
        trust_env: Private-session option: read environment proxies/CA/netrc.
        max_engines: Private-session option: engines kept alive.
        user_agent: Private-session option: engine-level User-Agent.
        accept_language: Private-session option: engine-level ``Accept-Language``.
        cache: Private-session option: enable the HTTP cache.
        response_class: Private-session option: response class to instantiate.
        proxy_auth: Private-session option: ``(username, password)`` for the proxy.
        max_clients: Accepted for curl_cffi parity; ignored on the sync path.

    Returns:
        The :class:`~chrome_client.Response`.
    """
    ...


def trace(
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
    base_url: Optional[str] = None,
    default_headers: Optional[bool] = None,
    trust_env: Optional[bool] = None,
    max_engines: Optional[int] = None,
    user_agent: Optional[str] = None,
    accept_language: Optional[str] = None,
    cache: Optional[bool] = None,
    response_class: ResponseClass = None,
    proxy_auth: AuthLike = None,
    max_clients: Optional[int] = None,
) -> Response:
    """Sends a ``TRACE`` through the shared session.

    Args:
        url: Target URL.
        params: Query parameters.
        data: Form or raw body; a file/iterator switches to a chunked upload.
        headers: Headers merged with the shared session's.
        cookies: Per-call cookies.
        files: Multipart file parts.
        auth: Auth callable, ``AuthBase`` or ``(user, password)`` tuple.
        timeout: One deadline, or ``(connect, read)``.
        allow_redirects: ``False`` returns the 3xx itself.
        proxies: Proxy mapping for this call.
        hooks: Response hooks for this call.
        stream: ``True`` keeps the body unread for ``iter_content``.
        verify: ``True``/``False``/CA bundle path.
        cert: Rejected; ABI v8 has no client-certificate setting.
        json: JSON body.
        content: Raw body bytes; mutually exclusive with ``data=``.
        multipart: A :class:`~chrome_client.CurlMime` body.
        impersonate: Profile for this call, e.g. ``"chrome_153"``.
        proxy: Proxy URL for this call.
        http_version: Pin ``"v1"``/``"v2"``/``"v3"``.
        max_redirects: Redirect cap.
        max_response_bytes: Body ceiling.
        referer: Routed through Chromium's own referrer path.
        accept_encoding: Overrides ``Accept-Encoding``.
        default_encoding: Fallback encoding for the response body, or a callable taking
            the body bytes and returning an encoding name.
        discard_cookies: Neither send nor record cookies.
        retry: Attempt count or :class:`~chrome_client.RetryStrategy`.
        cache_mode: Chromium load flags.
        priority: Request priority hint.
        ja3: Rejected; the profile owns the ClientHello.
        akamai: Rejected; the profile owns the HTTP/2 fingerprint.
        perk: Rejected; the profile owns the TLS fingerprint.
        extra_fp: Fingerprint overrides; only ``header_order`` and ``form_boundary`` are
            honourable.
        content_callback: Called with the fully buffered body.
        raise_for_status: Check the response before returning it.
        quote: ``False`` skips percent-encoding the URL.
        curl_options: Rejected.
        interface: Rejected.
        doh_url: Rejected.
        max_recv_speed: Rejected.
        thread: Rejected.
        debug: Rejected.
        base_url: Private-session option: prefix for relative URLs.
        default_headers: Private-session option: kept for compatibility.
        trust_env: Private-session option: read environment proxies/CA/netrc.
        max_engines: Private-session option: engines kept alive.
        user_agent: Private-session option: engine-level User-Agent.
        accept_language: Private-session option: engine-level ``Accept-Language``.
        cache: Private-session option: enable the HTTP cache.
        response_class: Private-session option: response class to instantiate.
        proxy_auth: Private-session option: ``(username, password)`` for the proxy.
        max_clients: Accepted for curl_cffi parity; ignored on the sync path.

    Returns:
        The :class:`~chrome_client.Response`.
    """
    ...


def query(
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
    base_url: Optional[str] = None,
    default_headers: Optional[bool] = None,
    trust_env: Optional[bool] = None,
    max_engines: Optional[int] = None,
    user_agent: Optional[str] = None,
    accept_language: Optional[str] = None,
    cache: Optional[bool] = None,
    response_class: ResponseClass = None,
    proxy_auth: AuthLike = None,
    max_clients: Optional[int] = None,
) -> Response:
    """Sends a ``QUERY`` through the shared session.

    Args:
        url: Target URL.
        params: Query parameters.
        data: Form or raw body; a file/iterator switches to a chunked upload.
        headers: Headers merged with the shared session's.
        cookies: Per-call cookies.
        files: Multipart file parts.
        auth: Auth callable, ``AuthBase`` or ``(user, password)`` tuple.
        timeout: One deadline, or ``(connect, read)``.
        allow_redirects: ``False`` returns the 3xx itself.
        proxies: Proxy mapping for this call.
        hooks: Response hooks for this call.
        stream: ``True`` keeps the body unread for ``iter_content``.
        verify: ``True``/``False``/CA bundle path.
        cert: Rejected; ABI v8 has no client-certificate setting.
        json: JSON body.
        content: Raw body bytes; mutually exclusive with ``data=``.
        multipart: A :class:`~chrome_client.CurlMime` body.
        impersonate: Profile for this call, e.g. ``"chrome_153"``.
        proxy: Proxy URL for this call.
        http_version: Pin ``"v1"``/``"v2"``/``"v3"``.
        max_redirects: Redirect cap.
        max_response_bytes: Body ceiling.
        referer: Routed through Chromium's own referrer path.
        accept_encoding: Overrides ``Accept-Encoding``.
        default_encoding: Fallback encoding for the response body, or a callable taking
            the body bytes and returning an encoding name.
        discard_cookies: Neither send nor record cookies.
        retry: Attempt count or :class:`~chrome_client.RetryStrategy`.
        cache_mode: Chromium load flags.
        priority: Request priority hint.
        ja3: Rejected; the profile owns the ClientHello.
        akamai: Rejected; the profile owns the HTTP/2 fingerprint.
        perk: Rejected; the profile owns the TLS fingerprint.
        extra_fp: Fingerprint overrides; only ``header_order`` and ``form_boundary`` are
            honourable.
        content_callback: Called with the fully buffered body.
        raise_for_status: Check the response before returning it.
        quote: ``False`` skips percent-encoding the URL.
        curl_options: Rejected.
        interface: Rejected.
        doh_url: Rejected.
        max_recv_speed: Rejected.
        thread: Rejected.
        debug: Rejected.
        base_url: Private-session option: prefix for relative URLs.
        default_headers: Private-session option: kept for compatibility.
        trust_env: Private-session option: read environment proxies/CA/netrc.
        max_engines: Private-session option: engines kept alive.
        user_agent: Private-session option: engine-level User-Agent.
        accept_language: Private-session option: engine-level ``Accept-Language``.
        cache: Private-session option: enable the HTTP cache.
        response_class: Private-session option: response class to instantiate.
        proxy_auth: Private-session option: ``(username, password)`` for the proxy.
        max_clients: Accepted for curl_cffi parity; ignored on the sync path.

    Returns:
        The :class:`~chrome_client.Response`.
    """
    ...
