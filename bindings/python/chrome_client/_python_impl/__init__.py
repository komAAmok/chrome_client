"""Implementation package for ``chrome_client``.

The public import stays ``chrome_client``; this subpackage holds the pieces:

* ``exceptions``  -- requests' hierarchy plus curl_cffi's extra leaves
* ``structures``  -- ``CaseInsensitiveDict``, ``Headers``, ``LookupDict``
* ``cookies``     -- ``RequestsCookieJar`` and the Core cookie-store bridge
* ``models``      -- ``Request``, ``PreparedRequest``, ``Response``
* ``engine``      -- Chromium engine configuration and per-session caching
* ``sessions``    -- ``Session`` and ``AsyncSession``
* ``websockets``  -- ``WebSocket`` and ``AsyncWebSocket``
* ``api``         -- module-level ``get``/``post``/... over a shared session
"""

from . import adapters, api, auth, cookies, engine, exceptions, impersonate
from . import models, multipart, sessions, status_codes, structures, utils, websockets
from ._native import native as _native
from .adapters import BaseAdapter, HTTPAdapter
from .api import (close_shared_session, delete, get, head, options, patch, post, put,
                  query, request, session, shared_session, trace)
from .auth import AuthBase, HTTPBasicAuth, HTTPDigestAuth, HTTPProxyAuth
from .cookies import (Cookie, CookieJar, Cookies, RequestsCookieJar,
                      add_dict_to_cookiejar, cookiejar_from_dict, create_cookie,
                      dict_from_cookiejar, merge_cookies, morsel_to_cookie)
from .engine import DEFAULT_MAX_ENGINES, EngineConfig
from .exceptions import (CertificateVerifyError, ChunkedEncodingError, ConnectTimeout,
                         ConnectionError, ContentDecodingError, CookieConflict,
                         CookieConflictError, DNSError, HTTPError, ImpersonateError,
                         IncompleteRead, InterfaceError, InvalidHeader, InvalidJSONError,
                         InvalidProxyURL, InvalidSchema, InvalidURL, JSONDecodeError,
                         MissingSchema, ProxyError, ReadTimeout, RequestException,
                         RequestsError, ResponseTooLarge, RetryError, SSLError,
                         SessionClosed, StreamConsumedError, Timeout, TooManyRedirects,
                         URLRequired, UnrewindableBodyError, UnsupportedFeature,
                         WebSocketClosed, WebSocketError, WebSocketTimeout)
from .impersonate import (CurlHttpVersion, ExtraFingerprints, available_profiles,
                          normalize_http_version, normalize_impersonate)
from .models import AsyncResponse, PreparedRequest, Request, Response
from .multipart import CurlMime
from .sessions import (AsyncClient, AsyncSession, Client, RetryStrategy, Session,
                       _proxy_from_proxies, merge_setting)
from .status_codes import codes
from .structures import CaseInsensitiveDict, Headers, LookupDict
from .websockets import AsyncWebSocket, CurlWsFrame, WebSocket, WsCloseCode


def core_version():
    """Version string reported by the loaded Core, or ``None``."""
    return engine.core_version()


def abi_version():
    """Core ABI version this build links against."""
    return engine.abi_version()


__all__ = [
    # sessions and clients
    "Session", "AsyncSession", "Client", "AsyncClient", "RetryStrategy",
    # models
    "Request", "PreparedRequest", "Response", "AsyncResponse",
    # websockets
    "WebSocket", "AsyncWebSocket", "WsCloseCode", "CurlWsFrame",
    # containers
    "CaseInsensitiveDict", "Headers", "LookupDict", "CookieJar", "Cookies",
    "RequestsCookieJar", "Cookie", "CurlMime", "ExtraFingerprints", "CurlHttpVersion",
    "EngineConfig", "DEFAULT_MAX_ENGINES",
    # adapters and auth
    "HTTPAdapter", "BaseAdapter", "AuthBase", "HTTPBasicAuth", "HTTPProxyAuth",
    "HTTPDigestAuth",
    # cookie helpers
    "cookiejar_from_dict", "dict_from_cookiejar", "add_dict_to_cookiejar",
    "merge_cookies", "create_cookie", "morsel_to_cookie",
    # exceptions
    "RequestException", "RequestsError", "HTTPError", "ConnectionError", "ProxyError",
    "SSLError", "CertificateVerifyError", "DNSError", "Timeout", "ConnectTimeout",
    "ReadTimeout", "TooManyRedirects", "URLRequired", "MissingSchema", "InvalidSchema",
    "InvalidURL", "InvalidHeader", "InvalidProxyURL", "ChunkedEncodingError",
    "ContentDecodingError", "StreamConsumedError", "RetryError", "UnrewindableBodyError",
    "InvalidJSONError", "JSONDecodeError", "IncompleteRead", "InterfaceError",
    "SessionClosed", "ImpersonateError", "CookieConflict", "CookieConflictError",
    "ResponseTooLarge", "UnsupportedFeature", "WebSocketError", "WebSocketClosed",
    "WebSocketTimeout",
    # module-level api
    "request", "get", "options", "head", "post", "put", "patch", "delete", "trace",
    "query", "session", "shared_session", "close_shared_session",
    "codes", "available_profiles", "core_version", "abi_version",
    "normalize_impersonate", "normalize_http_version",
    # submodules
    "exceptions", "structures", "cookies", "models", "sessions", "adapters", "auth",
    "utils", "status_codes", "impersonate", "multipart", "websockets", "engine", "api",
]
