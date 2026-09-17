"""Type surface of :mod:`chrome_client.exceptions`.

Shapes match ``requests.exceptions`` so ``except requests.exceptions.Timeout``
style code ports unchanged, with the extra leaves ``curl_cffi`` defines added
alongside. ``RequestException`` derives from ``IOError`` exactly as in requests,
so ``except IOError`` still catches everything this package raises.

Every exception carries the request/response it belongs to, which is what makes
``except RequestException as error: error.response`` usable for retry logic::

    try:
        session.get("https://expired.example.com")
    except CertificateVerifyError as error:
        print(error)          # ERR_CERT_DATE_INVALID (net error -201)
    except ConnectionError:
        ...
"""

from typing import Any, Optional

from typing_extensions import TypeAlias

#: Chromium net error code to ``(ERR_NAME, exception class or None)``.
NetErrorTable: TypeAlias = Any


class RequestException(IOError):
    """Base class for every failure raised by this package.

    Attributes:
        response: The response that was being processed, when one exists.
        request: The prepared request that failed.
    """

    response: Optional[Any]
    request: Optional[Any]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Builds the exception.

        Args:
            *args: Message parts, passed to :class:`IOError`.
            **kwargs: ``response=`` and ``request=`` are stored as attributes;
                when a response is given and no request is, the response's own
                ``.request`` is adopted.
        """
        ...


class InvalidJSONError(RequestException):
    """Raised when a body was expected to be JSON and is not."""


class JSONDecodeError(InvalidJSONError, ValueError):
    """Raised by ``Response.json()`` when the body is not valid JSON.

    Subclasses ``ValueError`` too, so existing ``except ValueError`` handlers
    keep working.
    """


class HTTPError(RequestException):
    """Raised by ``Response.raise_for_status()`` for 4xx and 5xx responses."""


class ConnectionError(RequestException):  # noqa: A001 - requests-compatible name
    """Network-level failure: DNS, TCP, TLS transport or a malformed response."""


class ProxyError(ConnectionError):
    """The proxy could not be reached, or refused the CONNECT."""


class SSLError(ConnectionError):
    """TLS failure other than certificate verification."""


class CertificateVerifyError(SSLError):
    """Certificate verification failed.

    The message names the specific check: expiry
    (``ERR_CERT_DATE_INVALID``, ``-201``), name mismatch
    (``ERR_CERT_COMMON_NAME_INVALID``, ``-200``) or an untrusted CA
    (``ERR_CERT_AUTHORITY_INVALID``, ``-202``).
    """


class DNSError(ConnectionError):
    """The host name could not be resolved."""


class Timeout(RequestException):
    """The request exceeded its deadline."""


class ConnectTimeout(ConnectionError, Timeout):
    """The connection attempt timed out."""


class ReadTimeout(Timeout):
    """The response was not completed before the deadline."""


class URLRequired(RequestException):
    """No URL was supplied."""


class TooManyRedirects(RequestException):
    """The redirect cap was exceeded.

    Carries ``.response``, the last redirecting response.
    """


class MissingSchema(RequestException, ValueError):
    """The URL has no scheme, e.g. ``"example.com"``."""


class InvalidSchema(RequestException, ValueError):
    """The URL scheme is not http/https/ws/wss."""


class InvalidURL(RequestException, ValueError):
    """The URL is malformed."""


class InvalidHeader(RequestException, ValueError):
    """A header name or value contains characters that would split the header."""


class InvalidProxyURL(InvalidURL):
    """A proxy URL is malformed."""


class ChunkedEncodingError(RequestException):
    """The response body was not framed as its headers promised."""


class ContentDecodingError(RequestException):
    """The body could not be decoded as its ``Content-Encoding`` promised."""


class StreamConsumedError(RequestException, TypeError):
    """The body was already consumed by another reader."""


class RetryError(RequestException):
    """The retry policy exhausted its attempts."""


class UnrewindableBodyError(RequestException):
    """A body could not be replayed for a redirect or retry."""


class IncompleteRead(RequestException):
    """The response ended before the promised body length was reached."""


class InterfaceError(RequestException):
    """A network-interface binding failed, or was requested and refused."""


class SessionClosed(RequestException):
    """The session was used after :meth:`Session.close`."""


class ImpersonateError(RequestException, ValueError):
    """The ``impersonate`` value names no profile this build ships.

    Raised for non-Chromium families (Edge, Safari, Firefox, Tor, Chrome
    Android, Safari iOS, OkHttp) and for Chrome majors outside the pinned range,
    rather than silently downgrading to Chrome.
    """


class CookieConflictError(RequestException, RuntimeError):
    """Several cookies share a name and both match the lookup."""


class ResponseTooLarge(RequestException):
    """Raised when a body exceeds ``max_response_bytes``."""


class WebSocketError(RequestException):
    """A WebSocket failed: handshake, framing or transport."""


class WebSocketClosed(WebSocketError):
    """The WebSocket closed, either locally or by the peer."""


class WebSocketTimeout(WebSocketError, Timeout):
    """The WebSocket handshake or read exceeded its deadline."""


class UnsupportedFeature(RequestException, NotImplementedError):
    """Raised for options the Chromium Core cannot honour.

    Failing closed is deliberate: silently ignoring a fingerprint or TLS option
    would report a fidelity this build does not have. Raised for ``ja3``,
    ``akamai``, ``perk``, ``cert``, ``interface``, ``doh_url``,
    ``curl_options``, ``max_recv_speed``, ``referer`` (via headers) and the
    unhonourable ``extra_fp`` fields.
    """


#: curl_cffi's name for :class:`CookieConflictError`.
CookieConflict: TypeAlias = CookieConflictError
#: curl_cffi's name for :class:`RequestException`.
RequestsError: TypeAlias = RequestException


def describe_net_error(code: int) -> Optional[str]:
    """Returns the Chromium net error name for a code.

    Args:
        code: The numeric net error, e.g. ``-201``.

    Returns:
        The ``ERR_*`` name, or ``None`` when the code is not in the table.

    Example:
        >>> describe_net_error(-201)
        'ERR_CERT_DATE_INVALID'
    """
    ...


def name_net_error(message: Any) -> str:
    """Rewrites a Core failure string to name its net error code.

    Used where the exception class is fixed by the API shape -- a WebSocket
    failure is a :class:`WebSocketError` whatever the cause -- but the message
    should still say ``ERR_UNSAFE_PORT`` rather than ``Network``.

    Args:
        message: The Core's failure string, e.g. ``"Network (net error -312)"``.

    Returns:
        The message with the code replaced by its name, or the input unchanged
        when it carries no known code.

    Example:
        >>> name_net_error("Network (net error -312)")
        'ERR_UNSAFE_PORT (net error -312)'
    """
    ...


def map_native_error(error: BaseException, request: Optional[Any] = None) -> RequestException:
    """Maps a native error onto the public hierarchy.

    The Core reports ``minicronet::Error`` variant names, so this is a lookup
    rather than a heuristic; only the redirect and timeout wordings need a
    substring test.

    Args:
        error: The exception or message string the native layer raised.
        request: The prepared request to attach, so ``error.request`` is usable.

    Returns:
        The most specific matching exception. A known net error code drives the
        class -- e.g. ``-105`` becomes :class:`DNSError`, ``-118`` becomes
        :class:`ConnectTimeout` -- and the message is rewritten to name it.

    Example:
        >>> try:
        ...     session.get("https://nonexistent.invalid")
        ... except DNSError as error:
        ...     print(error)          # ERR_NAME_NOT_RESOLVED (net error -105)
    """
    ...
