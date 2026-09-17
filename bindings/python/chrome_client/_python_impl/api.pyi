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

from typing import Any, Mapping, Optional, Union

from typing_extensions import Unpack

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
    RequestOptions,
    ResponseClass,
    Retry,
    SessionOptions,
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


def session(**kwargs: Unpack[SessionOptions]) -> Session:
    """Creates a synchronous :class:`~chrome_client.Session`.

    Args:
        **kwargs: Any ``Session`` constructor option; see
            :class:`~chrome_client.Session` for the full list
            (``impersonate``, ``http_version``, ``proxy``, ``proxies``,
            ``verify``, ``timeout``, ``headers``, ``cookies``, ``params``,
            ``auth``, ``retry``, ``max_engines``, ``base_url``, ...).

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


def async_session(**kwargs: Unpack[SessionOptions]) -> AsyncSession:
    """Creates an :class:`~chrome_client.AsyncSession`.

    Args:
        **kwargs: Any ``AsyncSession`` constructor option, plus ``max_clients``
            to bound how many requests are in flight at once. ``impersonate``
            accepts the same values as the sync path.

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
    default_encoding: Optional[str] = None,
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
        default_encoding: Decoding fallback for this response.
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
    url: str, params: Optional[ParamsLike] = None, **kwargs: Unpack[RequestOptions]
) -> Response:
    """Sends a ``GET`` through the shared session.

    Args:
        url: Target URL.
        params: Query parameters.
        **kwargs: Any :func:`request` option (``headers``, ``cookies``,
            ``timeout``, ``impersonate``, ``http_version``, ``stream``,
            ``verify``, ``proxies``, ``max_response_bytes``, ...).

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


def options(url: str, **kwargs: Unpack[RequestOptions]) -> Response:
    """Sends an ``OPTIONS`` through the shared session.

    Args:
        url: Target URL.
        **kwargs: Any :func:`request` option.

    Returns:
        The :class:`~chrome_client.Response`.
    """
    ...


def head(url: str, **kwargs: Unpack[RequestOptions]) -> Response:
    """Sends a ``HEAD`` through the shared session.

    ``allow_redirects`` defaults to ``False``, matching requests.

    Args:
        url: Target URL.
        **kwargs: Any :func:`request` option.

    Returns:
        The :class:`~chrome_client.Response` with an empty body.
    """
    ...


def post(
    url: str,
    data: Optional[Body] = None,
    json: Any = None,
    **kwargs: Unpack[RequestOptions],
) -> Response:
    """Sends a ``POST`` through the shared session.

    Args:
        url: Target URL.
        data: Form body, raw bytes, or a file/iterator for chunked upload.
        json: JSON body; sets ``Content-Type: application/json``.
        **kwargs: Any :func:`request` option.

    Returns:
        The :class:`~chrome_client.Response`.

    Example:
        >>> requests.post("https://example.com/api", json={"title": "t"})
    """
    ...


def put(
    url: str, data: Optional[Body] = None, **kwargs: Unpack[RequestOptions]
) -> Response:
    """Sends a ``PUT`` through the shared session.

    Args:
        url: Target URL.
        data: Request body.
        **kwargs: Any :func:`request` option.

    Returns:
        The :class:`~chrome_client.Response`.
    """
    ...


def patch(
    url: str, data: Optional[Body] = None, **kwargs: Unpack[RequestOptions]
) -> Response:
    """Sends a ``PATCH`` through the shared session.

    Args:
        url: Target URL.
        data: Request body.
        **kwargs: Any :func:`request` option.

    Returns:
        The :class:`~chrome_client.Response`.
    """
    ...


def delete(url: str, **kwargs: Unpack[RequestOptions]) -> Response:
    """Sends a ``DELETE`` through the shared session.

    Args:
        url: Target URL.
        **kwargs: Any :func:`request` option.

    Returns:
        The :class:`~chrome_client.Response`.
    """
    ...


def trace(url: str, **kwargs: Unpack[RequestOptions]) -> Response:
    """Sends a ``TRACE`` through the shared session.

    Args:
        url: Target URL.
        **kwargs: Any :func:`request` option.

    Returns:
        The :class:`~chrome_client.Response`.
    """
    ...


def query(url: str, **kwargs: Unpack[RequestOptions]) -> Response:
    """Sends a ``QUERY`` through the shared session.

    Args:
        url: Target URL.
        **kwargs: Any :func:`request` option.

    Returns:
        The :class:`~chrome_client.Response`.
    """
    ...
