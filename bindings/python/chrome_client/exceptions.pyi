"""requests' exception hierarchy plus curl_cffi's extra leaves.

At runtime this module *is* ``chrome_client._python_impl.exceptions``: the package aliases it
into ``sys.modules`` on import. The names below are re-exported
explicitly, so a type checker follows the same path instead of
seeing ``Any`` (PEP 561).
"""

from ._python_impl.exceptions import (
    NetErrorTable as NetErrorTable,
    RequestException as RequestException,
    InvalidJSONError as InvalidJSONError,
    JSONDecodeError as JSONDecodeError,
    HTTPError as HTTPError,
    ConnectionError as ConnectionError,
    ProxyError as ProxyError,
    SSLError as SSLError,
    CertificateVerifyError as CertificateVerifyError,
    DNSError as DNSError,
    Timeout as Timeout,
    ConnectTimeout as ConnectTimeout,
    ReadTimeout as ReadTimeout,
    URLRequired as URLRequired,
    TooManyRedirects as TooManyRedirects,
    MissingSchema as MissingSchema,
    InvalidSchema as InvalidSchema,
    InvalidURL as InvalidURL,
    InvalidHeader as InvalidHeader,
    InvalidProxyURL as InvalidProxyURL,
    ChunkedEncodingError as ChunkedEncodingError,
    ContentDecodingError as ContentDecodingError,
    StreamConsumedError as StreamConsumedError,
    RetryError as RetryError,
    UnrewindableBodyError as UnrewindableBodyError,
    IncompleteRead as IncompleteRead,
    InterfaceError as InterfaceError,
    SessionClosed as SessionClosed,
    ImpersonateError as ImpersonateError,
    CookieConflictError as CookieConflictError,
    ResponseTooLarge as ResponseTooLarge,
    WebSocketError as WebSocketError,
    WebSocketClosed as WebSocketClosed,
    WebSocketTimeout as WebSocketTimeout,
    UnsupportedFeature as UnsupportedFeature,
    CookieConflict as CookieConflict,
    RequestsError as RequestsError,
    describe_net_error as describe_net_error,
    name_net_error as name_net_error,
    map_native_error as map_native_error,
)
