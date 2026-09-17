"""Type surface of the :mod:`chrome_client` implementation package.

The public import stays ``chrome_client``; this subpackage holds the pieces:

* ``exceptions``  -- requests' hierarchy plus curl_cffi's extra leaves
* ``structures``  -- ``CaseInsensitiveDict``, ``Headers``, ``LookupDict``
* ``cookies``     -- ``RequestsCookieJar`` and the Core cookie-store bridge
* ``models``      -- ``Request``, ``PreparedRequest``, ``Response``
* ``engine``      -- Chromium engine configuration and per-session caching
* ``sessions``    -- ``Session`` and ``AsyncSession``
* ``websockets``  -- ``WebSocket`` and ``AsyncWebSocket``
* ``api``         -- module-level ``get``/``post``/... over a shared session

Every name the package exports is re-exported below with an explicit same-name
binding, so a type checker sees the real definitions rather than ``Any``::

    from chrome_client import Session           # fully typed
    from chrome_client.exceptions import Timeout # fully typed
"""

from typing import List, Optional

from . import adapters, api, auth, cookies, engine, exceptions, impersonate
from . import models, multipart, sessions, status_codes, structures, utils, websockets
from .adapters import BaseAdapter as BaseAdapter, HTTPAdapter as HTTPAdapter
from .api import (
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
)
from .auth import (
    AuthBase as AuthBase,
    HTTPBasicAuth as HTTPBasicAuth,
    HTTPDigestAuth as HTTPDigestAuth,
    HTTPProxyAuth as HTTPProxyAuth,
)
from .cookies import (
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
)
from .engine import (
    DEFAULT_MAX_ENGINES as DEFAULT_MAX_ENGINES,
    EngineCache as EngineCache,
    EngineConfig as EngineConfig,
    EngineSlot as EngineSlot,
)
from .exceptions import (
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
)
from .impersonate import (
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
)
from .models import (
    AsyncResponse as AsyncResponse,
    PreparedRequest as PreparedRequest,
    RawStream as RawStream,
    Request as Request,
    Response as Response,
    build_url as build_url,
    http_version_from_status_line as http_version_from_status_line,
    parse_raw_headers as parse_raw_headers,
    reason_from_status_line as reason_from_status_line,
)
from .multipart import (
    CurlMime as CurlMime,
    Part as Part,
    body_length as body_length,
    encode_multipart as encode_multipart,
    encode_params as encode_params,
    is_stream_body as is_stream_body,
    iter_body as iter_body,
    json_body as json_body,
)
from .sessions import (
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
)
from .status_codes import REASONS as REASONS, codes as codes
from .structures import (
    CaseInsensitiveDict as CaseInsensitiveDict,
    Headers as Headers,
    LookupDict as LookupDict,
)
from .websockets import (
    MAX_QUEUED_BYTES as MAX_QUEUED_BYTES,
    MAX_QUEUED_EVENTS as MAX_QUEUED_EVENTS,
    OK as OK,
    AsyncWebSocket as AsyncWebSocket,
    CurlWsFrame as CurlWsFrame,
    WebSocket as WebSocket,
    WsCloseCode as WsCloseCode,
)


def core_version() -> Optional[str]:
    """Version string reported by the loaded Core, or ``None``.

    Returns:
        The Core's build version, e.g. ``"153.0.8010.37"``.

    Example:
        >>> chrome_client.core_version()      # doctest: +SKIP
        '153.0.8010.37'
    """
    ...


def abi_version() -> int:
    """Core ABI version this build links against.

    Returns:
        The ABI number, ``8`` for this release. The wheel refuses to load a Core
        whose ABI differs.

    Example:
        >>> chrome_client.abi_version()
        8
    """
    ...


__all__: List[str]
