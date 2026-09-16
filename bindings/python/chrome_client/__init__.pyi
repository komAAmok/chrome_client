from ._python_impl.adapters import BaseAdapter as BaseAdapter, HTTPAdapter as HTTPAdapter
from ._python_impl.api import async_session as async_session, close_shared_session as close_shared_session, delete as delete, get as get, head as head, options as options, patch as patch, post as post, put as put, query as query, request as request, session as session, shared_session as shared_session, trace as trace
from ._python_impl.auth import AuthBase as AuthBase, HTTPBasicAuth as HTTPBasicAuth, HTTPDigestAuth as HTTPDigestAuth, HTTPProxyAuth as HTTPProxyAuth
from ._python_impl.cookies import Cookie as Cookie, CookieJar as CookieJar, Cookies as Cookies, RequestsCookieJar as RequestsCookieJar, add_dict_to_cookiejar as add_dict_to_cookiejar, cookiejar_from_dict as cookiejar_from_dict, create_cookie as create_cookie, dict_from_cookiejar as dict_from_cookiejar, merge_cookies as merge_cookies, morsel_to_cookie as morsel_to_cookie
from ._python_impl.engine import DEFAULT_MAX_ENGINES as DEFAULT_MAX_ENGINES, EngineConfig as EngineConfig
from ._python_impl.exceptions import CertificateVerifyError as CertificateVerifyError, ChunkedEncodingError as ChunkedEncodingError, ConnectTimeout as ConnectTimeout, ConnectionError as ConnectionError, ContentDecodingError as ContentDecodingError, CookieConflict as CookieConflict, CookieConflictError as CookieConflictError, DNSError as DNSError, HTTPError as HTTPError, ImpersonateError as ImpersonateError, IncompleteRead as IncompleteRead, InterfaceError as InterfaceError, InvalidHeader as InvalidHeader, InvalidJSONError as InvalidJSONError, InvalidProxyURL as InvalidProxyURL, InvalidSchema as InvalidSchema, InvalidURL as InvalidURL, JSONDecodeError as JSONDecodeError, MissingSchema as MissingSchema, ProxyError as ProxyError, ReadTimeout as ReadTimeout, RequestException as RequestException, RequestsError as RequestsError, ResponseTooLarge as ResponseTooLarge, RetryError as RetryError, SSLError as SSLError, SessionClosed as SessionClosed, StreamConsumedError as StreamConsumedError, Timeout as Timeout, TooManyRedirects as TooManyRedirects, URLRequired as URLRequired, UnrewindableBodyError as UnrewindableBodyError, UnsupportedFeature as UnsupportedFeature, WebSocketClosed as WebSocketClosed, WebSocketError as WebSocketError, WebSocketTimeout as WebSocketTimeout
from ._python_impl.impersonate import CurlHttpVersion as CurlHttpVersion, ExtraFingerprints as ExtraFingerprints, available_profiles as available_profiles, normalize_http_version as normalize_http_version, normalize_impersonate as normalize_impersonate
from ._python_impl.models import AsyncResponse as AsyncResponse, PreparedRequest as PreparedRequest, Request as Request, Response as Response
from ._python_impl.multipart import CurlMime as CurlMime
from ._python_impl.sessions import AsyncClient as AsyncClient, AsyncSession as AsyncSession, Client as Client, RetryStrategy as RetryStrategy, Session as Session
from ._python_impl.status_codes import codes as codes
from ._python_impl.structures import CaseInsensitiveDict as CaseInsensitiveDict, Headers as Headers, LookupDict as LookupDict
from ._python_impl.websockets import AsyncWebSocket as AsyncWebSocket, CurlWsFrame as CurlWsFrame, WebSocket as WebSocket, WsCloseCode as WsCloseCode
from . import adapters as adapters, api as api, auth as auth, cookies as cookies, engine as engine, exceptions as exceptions, impersonate as impersonate, models as models, multipart as multipart, requests as requests, sessions as sessions, status_codes as status_codes, structures as structures, utils as utils, websockets as websockets

__version__: str

def core_version() -> str: ...
def abi_version() -> int: ...
