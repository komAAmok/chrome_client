"""Chromium Core HTTP/WebSocket client.

Two API shapes over one Chromium network stack:

* ``requests``-shaped:  ``Session``, ``Response``, ``session.cookies``,
  ``session.proxies``, the ``requests.exceptions`` hierarchy and ``codes``.
* ``curl_cffi``-shaped:  ``impersonate``, ``http_version`` for HTTP/1.1, HTTP/2
  and HTTP/3, ``AsyncSession``, ``CurlMime``, ``Headers``, ``Cookies`` and
  ``WebSocket``.

    import chrome_client
    with chrome_client.Session(impersonate="chrome_153") as session:
        session.get("https://example.com")

``chrome_client.requests`` mirrors the ``requests`` module namespace, including
``requests.Session``::

    from chrome_client import requests
    with requests.Session() as session:
        session.get("https://example.com")

The submodules below are the same objects as their ``_python_impl``
counterparts, so ``chrome_client.exceptions.Timeout`` and
``import chrome_client.utils`` resolve exactly as their requests equivalents do.
"""

from typing import List, Optional

from ._python_impl import (
    BaseAdapter as BaseAdapter,
    HTTPAdapter as HTTPAdapter,
    async_session as async_session,
    close_shared_session as close_shared_session,
    delete as delete,
    get as get,
    head as head,
    options as options,
    patch as patch,
    post as post,
    put as put,
    query as query,
    request as request,
    session as session,
    shared_session as shared_session,
    trace as trace,
    AuthBase as AuthBase,
    HTTPBasicAuth as HTTPBasicAuth,
    HTTPDigestAuth as HTTPDigestAuth,
    HTTPProxyAuth as HTTPProxyAuth,
    Cookie as Cookie,
    CookieJar as CookieJar,
    Cookies as Cookies,
    RequestsCookieJar as RequestsCookieJar,
    add_dict_to_cookiejar as add_dict_to_cookiejar,
    cookiejar_from_dict as cookiejar_from_dict,
    create_cookie as create_cookie,
    dict_from_cookiejar as dict_from_cookiejar,
    merge_cookies as merge_cookies,
    morsel_to_cookie as morsel_to_cookie,
    DEFAULT_MAX_ENGINES as DEFAULT_MAX_ENGINES,
    EngineCache as EngineCache,
    EngineConfig as EngineConfig,
    EngineSlot as EngineSlot,
    CertificateVerifyError as CertificateVerifyError,
    ChunkedEncodingError as ChunkedEncodingError,
    ConnectTimeout as ConnectTimeout,
    ConnectionError as ConnectionError,
    ContentDecodingError as ContentDecodingError,
    CookieConflict as CookieConflict,
    CookieConflictError as CookieConflictError,
    DNSError as DNSError,
    HTTPError as HTTPError,
    ImpersonateError as ImpersonateError,
    IncompleteRead as IncompleteRead,
    InterfaceError as InterfaceError,
    InvalidHeader as InvalidHeader,
    InvalidJSONError as InvalidJSONError,
    InvalidProxyURL as InvalidProxyURL,
    InvalidSchema as InvalidSchema,
    InvalidURL as InvalidURL,
    JSONDecodeError as JSONDecodeError,
    MissingSchema as MissingSchema,
    ProxyError as ProxyError,
    ReadTimeout as ReadTimeout,
    RequestException as RequestException,
    RequestsError as RequestsError,
    ResponseTooLarge as ResponseTooLarge,
    RetryError as RetryError,
    SSLError as SSLError,
    SessionClosed as SessionClosed,
    StreamConsumedError as StreamConsumedError,
    Timeout as Timeout,
    TooManyRedirects as TooManyRedirects,
    URLRequired as URLRequired,
    UnrewindableBodyError as UnrewindableBodyError,
    UnsupportedFeature as UnsupportedFeature,
    WebSocketClosed as WebSocketClosed,
    WebSocketError as WebSocketError,
    WebSocketTimeout as WebSocketTimeout,
    describe_net_error as describe_net_error,
    map_native_error as map_native_error,
    name_net_error as name_net_error,
    ALIASES as ALIASES,
    LATEST_CHROME as LATEST_CHROME,
    OLDEST_CHROME as OLDEST_CHROME,
    ChromeFamilyAlias as ChromeFamilyAlias,
    ChromeProfileAlias as ChromeProfileAlias,
    ChromeProfileName as ChromeProfileName,
    CurlHttpVersion as CurlHttpVersion,
    ExtraFingerprints as ExtraFingerprints,
    HttpVersion as HttpVersion,
    Impersonate as Impersonate,
    available_profiles as available_profiles,
    normalize_http_version as normalize_http_version,
    normalize_impersonate as normalize_impersonate,
    reject_fingerprint_overrides as reject_fingerprint_overrides,
    validate_extra_fp as validate_extra_fp,
    AsyncResponse as AsyncResponse,
    PreparedRequest as PreparedRequest,
    RawStream as RawStream,
    Request as Request,
    Response as Response,
    build_url as build_url,
    http_version_from_status_line as http_version_from_status_line,
    parse_raw_headers as parse_raw_headers,
    reason_from_status_line as reason_from_status_line,
    CurlMime as CurlMime,
    Part as Part,
    body_length as body_length,
    encode_multipart as encode_multipart,
    encode_params as encode_params,
    is_stream_body as is_stream_body,
    iter_body as iter_body,
    json_body as json_body,
    ASYNC_POLL_BATCH as ASYNC_POLL_BATCH,
    STREAM_BUFFER_LIMIT as STREAM_BUFFER_LIMIT,
    AsyncClient as AsyncClient,
    AsyncSession as AsyncSession,
    BaseSession as BaseSession,
    Client as Client,
    RetryStrategy as RetryStrategy,
    Session as Session,
    merge_hooks as merge_hooks,
    merge_setting as merge_setting,
    proxy_from_proxies as proxy_from_proxies,
    REASONS as REASONS,
    codes as codes,
    CaseInsensitiveDict as CaseInsensitiveDict,
    Headers as Headers,
    LookupDict as LookupDict,
    MAX_QUEUED_BYTES as MAX_QUEUED_BYTES,
    MAX_QUEUED_EVENTS as MAX_QUEUED_EVENTS,
    OK as OK,
    AsyncWebSocket as AsyncWebSocket,
    CurlWsFrame as CurlWsFrame,
    WebSocket as WebSocket,
    WsCloseCode as WsCloseCode,
)

from . import adapters as adapters, api as api, auth as auth, cookies as cookies
from . import engine as engine, exceptions as exceptions, impersonate as impersonate
from . import models as models, multipart as multipart, requests as requests
from . import sessions as sessions, status_codes as status_codes, structures as structures
from . import utils as utils, websockets as websockets

#: This release's version, taken from the workspace ``Cargo.toml``.
__version__: str


def core_version() -> Optional[str]:
    """Version string reported by the loaded Core, or ``None``.

    Returns:
        The Core's build version, e.g. ``"153.0.8010.37"``. ``None`` when the
        Core reports none.

    Example:
        >>> import chrome_client
        >>> chrome_client.core_version()      # doctest: +SKIP
        '153.0.8010.37'
    """
    ...


def abi_version() -> int:
    """Core ABI version this build links against.

    Returns:
        The ABI number, ``8`` for this release. The wheel refuses to load a Core
        whose ABI differs, so this always matches the Core in use.

    Example:
        >>> import chrome_client
        >>> chrome_client.abi_version()
        8
    """
    ...


#: Every public name, plus ``"requests"`` and ``"__version__"``.
__all__: List[str]
