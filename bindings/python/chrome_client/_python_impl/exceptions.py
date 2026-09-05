"""Exception hierarchy.

Shapes match ``requests.exceptions`` so ``except requests.exceptions.Timeout``
style code ports unchanged, with the extra leaves ``curl_cffi`` defines added
alongside. ``RequestException`` derives from ``IOError`` exactly as in requests.
"""

import re


class RequestException(IOError):
    """Base class for every failure raised by this package."""

    def __init__(self, *args, **kwargs):
        response = kwargs.pop("response", None)
        self.response = response
        self.request = kwargs.pop("request", None)
        super(RequestException, self).__init__(*args, **kwargs)
        if response is not None and self.request is None:
            self.request = getattr(response, "request", None)


class InvalidJSONError(RequestException):
    pass


class JSONDecodeError(InvalidJSONError, ValueError):
    """Raised by ``Response.json()`` when the body is not valid JSON."""


class HTTPError(RequestException):
    """Raised by ``Response.raise_for_status()`` for 4xx and 5xx responses."""


class ConnectionError(RequestException):  # noqa: A001 - requests-compatible name
    pass


class ProxyError(ConnectionError):
    pass


class SSLError(ConnectionError):
    pass


class CertificateVerifyError(SSLError):
    pass


class DNSError(ConnectionError):
    pass


class Timeout(RequestException):
    pass


class ConnectTimeout(ConnectionError, Timeout):
    pass


class ReadTimeout(Timeout):
    pass


class URLRequired(RequestException):
    pass


class TooManyRedirects(RequestException):
    pass


class MissingSchema(RequestException, ValueError):
    pass


class InvalidSchema(RequestException, ValueError):
    pass


class InvalidURL(RequestException, ValueError):
    pass


class InvalidHeader(RequestException, ValueError):
    pass


class InvalidProxyURL(InvalidURL):
    pass


class ChunkedEncodingError(RequestException):
    pass


class ContentDecodingError(RequestException):
    pass


class StreamConsumedError(RequestException, TypeError):
    pass


class RetryError(RequestException):
    pass


class UnrewindableBodyError(RequestException):
    pass


class IncompleteRead(RequestException):
    pass


class InterfaceError(RequestException):
    pass


class SessionClosed(RequestException):
    pass


class ImpersonateError(RequestException, ValueError):
    pass


class CookieConflictError(RequestException, RuntimeError):
    pass


class ResponseTooLarge(RequestException):
    """Raised when a body exceeds ``max_response_bytes``."""


class WebSocketError(RequestException):
    pass


class WebSocketClosed(WebSocketError):
    pass


class WebSocketTimeout(WebSocketError, Timeout):
    pass


# curl_cffi spells these differently; keep both names bound to one class so
# ``except`` clauses written against either library work.
CookieConflict = CookieConflictError
RequestsError = RequestException


class UnsupportedFeature(RequestException, NotImplementedError):
    """Raised for options the Chromium Core cannot honour.

    Failing closed is deliberate: silently ignoring a fingerprint or TLS option
    would report a fidelity this build does not have.
    """


#: Chromium net error codes worth naming.  The Core reports the numeric code and
#: a coarse category; the code is what actually tells a caller whether to retry,
#: fix a URL, or trust a certificate, so it drives both the exception class and
#: the message.
_NET_ERRORS = {
    -2: ("ERR_FAILED", None),
    # ERR_ABORTED means the caller cancelled. It used to be what a rejected
    # certificate reported too, because the Core did not override
    # OnSSLCertificateError; ABI v8 with the current Core reports the ERR_CERT_*
    # code instead.
    -3: ("ERR_ABORTED", None),
    -7: ("ERR_TIMED_OUT", Timeout),
    -15: ("ERR_SOCKET_NOT_CONNECTED", None),
    -21: ("ERR_NETWORK_CHANGED", None),
    -100: ("ERR_CONNECTION_CLOSED", None),
    -101: ("ERR_CONNECTION_RESET", None),
    -102: ("ERR_CONNECTION_REFUSED", None),
    -103: ("ERR_CONNECTION_ABORTED", None),
    -104: ("ERR_CONNECTION_FAILED", None),
    -105: ("ERR_NAME_NOT_RESOLVED", DNSError),
    -106: ("ERR_INTERNET_DISCONNECTED", None),
    -107: ("ERR_SSL_PROTOCOL_ERROR", SSLError),
    -108: ("ERR_ADDRESS_INVALID", InvalidURL),
    -109: ("ERR_ADDRESS_UNREACHABLE", None),
    -110: ("ERR_SSL_CLIENT_AUTH_CERT_NEEDED", SSLError),
    -113: ("ERR_SSL_VERSION_OR_CIPHER_MISMATCH", SSLError),
    -118: ("ERR_CONNECTION_TIMED_OUT", ConnectTimeout),
    -121: ("ERR_SOCKS_CONNECTION_FAILED", ProxyError),
    -130: ("ERR_PROXY_CONNECTION_FAILED", ProxyError),
    -137: ("ERR_NAME_RESOLUTION_FAILED", DNSError),
    -200: ("ERR_CERT_COMMON_NAME_INVALID", CertificateVerifyError),
    -201: ("ERR_CERT_DATE_INVALID", CertificateVerifyError),
    -202: ("ERR_CERT_AUTHORITY_INVALID", CertificateVerifyError),
    -203: ("ERR_CERT_CONTAINS_ERRORS", CertificateVerifyError),
    -204: ("ERR_CERT_NO_REVOCATION_MECHANISM", CertificateVerifyError),
    -206: ("ERR_CERT_REVOKED", CertificateVerifyError),
    -207: ("ERR_CERT_INVALID", CertificateVerifyError),
    -208: ("ERR_CERT_WEAK_SIGNATURE_ALGORITHM", CertificateVerifyError),
    -210: ("ERR_CERT_NON_UNIQUE_NAME", CertificateVerifyError),
    -211: ("ERR_CERT_WEAK_KEY", CertificateVerifyError),
    -212: ("ERR_CERT_NAME_CONSTRAINT_VIOLATION", CertificateVerifyError),
    -213: ("ERR_CERT_VALIDITY_TOO_LONG", CertificateVerifyError),
    -214: ("ERR_CERTIFICATE_TRANSPARENCY_REQUIRED", CertificateVerifyError),
    -215: ("ERR_CERT_SYMANTEC_LEGACY", CertificateVerifyError),
    -217: ("ERR_CERT_KNOWN_INTERCEPTION_BLOCKED", CertificateVerifyError),
    -300: ("ERR_INVALID_URL", InvalidURL),
    -301: ("ERR_DISALLOWED_URL_SCHEME", InvalidSchema),
    -302: ("ERR_UNKNOWN_URL_SCHEME", InvalidSchema),
    -310: ("ERR_TOO_MANY_REDIRECTS", TooManyRedirects),
    -311: ("ERR_UNSAFE_REDIRECT", TooManyRedirects),
    -312: ("ERR_UNSAFE_PORT", InvalidURL),
    # A malformed or absent response is requests' ConnectionError (urllib3
    # ProtocolError); only genuinely chunked framing faults are
    # ChunkedEncodingError there.
    -320: ("ERR_INVALID_RESPONSE", ConnectionError),
    -321: ("ERR_INVALID_CHUNKED_ENCODING", ChunkedEncodingError),
    -324: ("ERR_EMPTY_RESPONSE", ConnectionError),
    -325: ("ERR_RESPONSE_HEADERS_TOO_BIG", InvalidHeader),
    -336: ("ERR_TUNNEL_CONNECTION_FAILED", ProxyError),
    -337: ("ERR_SSL_HANDSHAKE_NOT_COMPLETED", SSLError),
    -348: ("ERR_PROXY_AUTH_REQUESTED", ProxyError),
    -350: ("ERR_CONTENT_DECODING_FAILED", ContentDecodingError),
    -354: ("ERR_INCOMPLETE_CHUNKED_ENCODING", IncompleteRead),
    -358: ("ERR_QUIC_PROTOCOL_ERROR", ChunkedEncodingError),
    -371: ("ERR_QUIC_HANDSHAKE_FAILED", SSLError),
    -400: ("ERR_CACHE_MISS", None),
}

_NET_ERROR_PATTERN = re.compile(r"net error (-?\d+)")


def describe_net_error(code):
    """Returns ``ERR_NAME`` for a Chromium net error code, or ``None``."""
    entry = _NET_ERRORS.get(code)
    return entry[0] if entry else None


def name_net_error(message):
    """Rewrites a Core failure string to name its net error code.

    Used where the exception class is fixed by the API shape -- a WebSocket
    failure is a ``WebSocketError`` whatever the cause -- but the message should
    still say ``ERR_UNSAFE_PORT`` rather than ``Network``.
    """
    text = str(message)
    match = _NET_ERROR_PATTERN.search(text)
    if match is None:
        return text
    code = int(match.group(1))
    named = _NET_ERRORS.get(code)
    return "%s (net error %d)" % (named[0], code) if named else text


def map_native_error(error, request=None):
    """Maps a native error string onto the public hierarchy.

    The Core reports ``minicronet::Error`` variant names, so the mapping is a
    lookup rather than a heuristic; only the redirect and timeout wordings need
    a substring test.
    """
    message = str(error)
    match = _NET_ERROR_PATTERN.search(message)
    code = int(match.group(1)) if match else None
    named = _NET_ERRORS.get(code) if code is not None else None
    if named is not None:
        # Replace the coarse category with the net error name: "ERR_CERT_DATE_
        # INVALID" tells the caller what to do, "Tls" does not.
        message = "%s (net error %d)" % (named[0], code)
    for needle, kind in _NATIVE_ERRORS:
        if needle in str(error):
            if named is not None and named[1] is not None:
                kind = named[1]
            return kind(message, request=request)
    if named is not None and named[1] is not None:
        return named[1](message, request=request)
    lowered = message.lower()
    if "timeout" in lowered or "timed out" in lowered:
        return Timeout(message, request=request)
    return RequestException(message, request=request)


_NATIVE_ERRORS = (
    ("ProfileUnsupported", ImpersonateError),
    ("ProfileConflict", ImpersonateError),
    ("InvalidArgument", InvalidURL),
    ("UnsupportedAbi", RequestException),
    ("OutOfMemory", RequestException),
    ("InitializationFailed", RequestException),
    ("InvalidState", RequestException),
    ("Timeout", Timeout),
    ("Canceled", RequestException),
    ("Tls", SSLError),
    ("Proxy", ProxyError),
    ("Protocol", ChunkedEncodingError),
    ("Redirect", TooManyRedirects),
    ("CacheMiss", RequestException),
    ("CallbackPanic", RequestException),
    ("BufferLimit", RequestException),
    ("Network", ConnectionError),
)
