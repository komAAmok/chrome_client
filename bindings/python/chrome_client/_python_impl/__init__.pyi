from typing import List
from . import adapters, api, auth, cookies, engine, exceptions, impersonate, models, multipart, sessions, status_codes, structures, utils, websockets
from .adapters import BaseAdapter, HTTPAdapter
from .api import async_session, close_shared_session, delete, get, head, options, patch, post, put, query, request, session, shared_session, trace
from .auth import AuthBase, HTTPBasicAuth, HTTPDigestAuth, HTTPProxyAuth
from .cookies import Cookie, CookieJar, Cookies, RequestsCookieJar, add_dict_to_cookiejar, cookiejar_from_dict, create_cookie, dict_from_cookiejar, merge_cookies, morsel_to_cookie
from .engine import DEFAULT_MAX_ENGINES, EngineConfig
from .exceptions import CertificateVerifyError, ChunkedEncodingError, ConnectTimeout, ConnectionError, ContentDecodingError, CookieConflict, CookieConflictError, DNSError, HTTPError, ImpersonateError, IncompleteRead, InterfaceError, InvalidHeader, InvalidJSONError, InvalidProxyURL, InvalidSchema, InvalidURL, JSONDecodeError, MissingSchema, ProxyError, ReadTimeout, RequestException, RequestsError, ResponseTooLarge, RetryError, SSLError, SessionClosed, StreamConsumedError, Timeout, TooManyRedirects, URLRequired, UnrewindableBodyError, UnsupportedFeature, WebSocketClosed, WebSocketError, WebSocketTimeout
from .impersonate import CurlHttpVersion, ExtraFingerprints, available_profiles, normalize_http_version, normalize_impersonate
from .models import AsyncResponse, PreparedRequest, Request, Response
from .multipart import CurlMime
from .sessions import AsyncClient, AsyncSession, Client, RetryStrategy, Session, merge_setting, proxy_from_proxies
from .status_codes import codes
from .structures import CaseInsensitiveDict, Headers, LookupDict
from .websockets import AsyncWebSocket, CurlWsFrame, WebSocket, WsCloseCode

def core_version() -> str: ...
def abi_version() -> int: ...

__all__: List[str]
