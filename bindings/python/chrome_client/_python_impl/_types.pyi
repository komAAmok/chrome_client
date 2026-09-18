"""Shared type vocabulary for the ``chrome_client`` stubs.

This file is read by type checkers and never executed, so it is free to use
syntax the supported runtimes do not have (``Literal``, ``Protocol``,
``TypedDict``): those come from ``typing_extensions``, which is a
*type-checking* dependency only and adds nothing to the wheel.

Every public signature in the package names one of the aliases below, so an IDE
resolves ``headers=``, ``data=``, ``timeout=`` or ``impersonate=`` to a single
documented definition instead of falling back to ``Any`` -- that is what makes
parameter hints and completion useful.
"""

import os
from typing import (
    Any,
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    IO,
    Iterable,
    Iterator,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Sequence,
    Tuple,
    Type,
    TYPE_CHECKING,
    Union,
)

from typing_extensions import TypeAlias, TypedDict

from .impersonate import ChromeProfileName, ChromeProfileAlias, Impersonate, HttpVersion

if TYPE_CHECKING:  # cyclic imports, visible to type checkers only
    from .auth import AuthBase
    from .cookies import RequestsCookieJar
    from .models import AsyncResponse, PreparedRequest, Response
    from .sessions import RetryStrategy

__all__ = [
    "AsyncAwaitable",
    "AuthBaseLike",
    "AuthLike",
    "AuthTuple",
    "Body",
    "BytesLike",
    "ChromeFamilyAlias",
    "ChromeProfileAlias",
    "ChromeProfileName",
    "CacheMode",
    "ContentCallback",
    "CookieJarLike",
    "Cookies",
    "CookiesLike",
    "FileEntry",
    "FilesLike",
    "HeaderItems",
    "HeaderMapping",
    "HeaderValue",
    "HeadersLike",
    "Hook",
    "Hooks",
    "HttpVersion",
    "Impersonate",
    "ParamValue",
    "ParamsLike",
    "Proxies",
    "RequestOptions",
    "ResponseClass",
    "Retry",
    "SessionOptions",
    "StrOrBytes",
    "StrPath",
    "Timeout",
    "Verify",
    "WsMessage",
]

# ---------------------------------------------------------------------------
# Scalars
# ---------------------------------------------------------------------------

#: A string or the UTF-8 bytes of one -- both are accepted for URLs, header
#: names/values, query keys and form keys.
StrOrBytes: TypeAlias = Union[str, bytes]

#: Anything a header value may be. ``None`` means "drop this header".
HeaderValue: TypeAlias = Union[str, bytes, int, float, None]

#: ``[(name, value), ...]`` -- the duplicate-preserving headers form.
HeaderItems: TypeAlias = Iterable[Tuple[StrOrBytes, HeaderValue]]

#: The mapping form. Keys are ``str`` rather than ``str | bytes`` because
#: ``Mapping`` is invariant in its key type, so a union key would reject a plain
#: ``dict[str, str]``.
HeaderMapping: TypeAlias = Mapping[str, HeaderValue]

#: A raw header block: a mapping, an iterable of pairs, or a ``Headers``.
HeadersLike: TypeAlias = Union[HeaderMapping, HeaderItems, None]

#: A path, as accepted anywhere a file path is taken (CA bundle, netrc, ...).
StrPath: TypeAlias = Union[str, "os.PathLike[str]"]

#: Any buffer protocol object.
BytesLike: TypeAlias = Union[bytes, bytearray, memoryview]

#: One query/form value, possibly repeated.
ParamValue: TypeAlias = Union[StrOrBytes, int, float, None, Sequence[Union[StrOrBytes, int, float, None]]]

#: ``params=``: a query string, a mapping, or a sequence of pairs.
ParamsLike: TypeAlias = Union[StrOrBytes, Mapping[Any, ParamValue], Iterable[Tuple[Any, ParamValue]]]

#: ``cookies=``: a plain mapping, this package's jar, or a ``cookielib`` jar.
CookieJarLike: TypeAlias = Union["RequestsCookieJar", "Any"]
Cookies: TypeAlias = Any
CookiesLike: TypeAlias = Union[Mapping[str, str], Mapping[str, Any], Any]

# ---------------------------------------------------------------------------
# Body
# ---------------------------------------------------------------------------

#: One ``files=`` entry: a handle/buffer, or a 2/3/4-tuple
#: ``(filename, handle, content_type, headers)``.
FileEntry: TypeAlias = Union[
    IO[bytes],
    BytesLike,
    str,
    Tuple[str, Union[IO[bytes], BytesLike, str]],
    Tuple[str, Union[IO[bytes], BytesLike, str], Optional[str]],
    Tuple[str, Union[IO[bytes], BytesLike, str], Optional[str], Any],
]

#: ``files=``: a mapping of field name to one entry, or several.
FilesLike: TypeAlias = Union[
    Mapping[str, Union[FileEntry, Sequence[FileEntry]]],
    Iterable[Tuple[str, Union[FileEntry, Sequence[FileEntry]]]],
    None,
]

#: ``data=`` / ``content=``. A mapping or sequence of pairs is form-encoded;
#: a file object, generator or async iterator is uploaded chunked.
Body: TypeAlias = Union[
    StrOrBytes,
    BytesLike,
    Mapping[Any, Any],
    Iterable[Tuple[Any, Any]],
    IO[bytes],
    Iterator[bytes],
    AsyncIterator[bytes],
    None,
]

# ---------------------------------------------------------------------------
# Transport options
# ---------------------------------------------------------------------------

#: One deadline in seconds, or requests' ``(connect, read)`` pair.
Timeout: TypeAlias = Union[int, float, Tuple[Optional[Union[int, float]], Optional[Union[int, float]]], None]

#: ``verify=``: ``True``/``False``, or the path of a CA bundle in PEM form.
Verify: TypeAlias = Union[bool, str]

#: ``proxies=``: ``{"https": "http://host:port", "all": ...}``. Mutating the
#: session's mapping takes effect on the next request.
Proxies: TypeAlias = Union[Mapping[str, Optional[str]], MutableMapping[str, Optional[str]]]

#: ``auth=``: a 2-tuple, an ``AuthBase`` instance, or any callable.
AuthTuple: TypeAlias = Tuple[StrOrBytes, StrOrBytes]
AuthBaseLike: TypeAlias = Union["AuthBase", Callable[["PreparedRequest"], Any]]
AuthLike: TypeAlias = Union[AuthTuple, AuthBaseLike, None]

#: ``retry=``: an attempt count, or a full :class:`RetryStrategy`.
Retry: TypeAlias = Union[int, "RetryStrategy", None]

#: Chromium's ``URLRequest`` load flags, exposed as a string.
CacheMode: TypeAlias = Optional[str]

#: ``hooks=``: event name to one callable or a list of them.
Hook: TypeAlias = Callable[["Response"], Any]
Hooks: TypeAlias = Mapping[str, Union[Hook, Iterable[Hook]]]

#: Called with each fully buffered body, for non-streaming requests.
ContentCallback: TypeAlias = Callable[[bytes], Any]

#: ``response_class=``: a subclass of ``Response``/``AsyncResponse`` that the
#: session instantiates instead of the default one.
ResponseClass: TypeAlias = Union[Type["Response"], Type["AsyncResponse"], None]

#: One WebSocket message: text or binary.
WsMessage: TypeAlias = Union[str, bytes, bytearray, memoryview]

AsyncAwaitable: TypeAlias = Awaitable[Any]

# ---------------------------------------------------------------------------
# Parameter sets, as TypedDicts
# ---------------------------------------------------------------------------
#
# The verb methods (``Session.get``, ``AsyncSession.post``, ...) forward
# ``**kwargs`` to ``request()``.  These two TypedDicts name the accepted keys.
#
# They are **no longer used in any signature**: spelling the options as
# ``**kwargs: Unpack[RequestOptions]`` (PEP 692) leaves an IDE with nothing to
# show but the explicit ``url``/``params`` unless it implements Unpack -- PyCharm
# does not.  Every entry point therefore lists its parameters explicitly, and
# these two classes remain as the canonical description of the two sets (and as
# the source the expansion is checked against).


class RequestOptions(TypedDict, total=False):
    """Every keyword :meth:`Session.request` accepts beyond ``params``/``data``.

    The verb methods take these keys as ordinary named parameters, not as
    ``**kwargs`` -- see the note above -- so this class is the canonical list
    rather than something an IDE reads off a signature.
    """

    headers: Optional[HeadersLike]
    cookies: Optional[CookiesLike]
    files: FilesLike
    auth: AuthLike
    timeout: Timeout
    allow_redirects: bool
    proxies: Optional[Proxies]
    hooks: Optional[Hooks]
    stream: Optional[bool]
    verify: Optional[Verify]
    cert: Any
    content: Optional[Body]
    multipart: Any
    impersonate: Optional[Impersonate]
    proxy: Optional[str]
    http_version: Optional[HttpVersion]
    max_redirects: Optional[int]
    max_response_bytes: Optional[int]
    referer: Optional[str]
    accept_encoding: Optional[str]
    default_encoding: Optional[str]
    discard_cookies: Optional[bool]
    retry: Retry
    cache_mode: CacheMode
    priority: Optional[int]
    ja3: Optional[str]
    akamai: Optional[str]
    perk: Optional[str]
    extra_fp: Optional[Any]
    content_callback: Optional[ContentCallback]
    raise_for_status: Optional[bool]
    quote: Optional[bool]
    curl_options: Optional[Mapping[Any, Any]]
    interface: Optional[str]
    doh_url: Optional[str]
    max_recv_speed: Optional[int]
    thread: Any
    debug: Any


class SessionOptions(TypedDict, total=False):
    """Every keyword the ``Session``/``AsyncSession`` constructors accept.

    ``chrome_client.session(**kwargs)`` and ``async_session(**kwargs)`` forward
    to those constructors, so unpacking this TypedDict gives the same
    completion there.
    """

    impersonate: Optional[Impersonate]
    proxy: Optional[str]
    proxies: Optional[Proxies]
    proxy_auth: AuthLike
    verify: Verify
    timeout: Timeout
    headers: Optional[HeadersLike]
    cookies: Optional[CookiesLike]
    params: Optional[ParamsLike]
    auth: AuthLike
    cert: Any
    stream: bool
    hooks: Optional[Hooks]
    max_redirects: int
    trust_env: bool
    allow_redirects: bool
    max_response_bytes: Optional[int]
    base_url: Optional[str]
    http_version: Optional[HttpVersion]
    ja3: Optional[str]
    akamai: Optional[str]
    perk: Optional[str]
    extra_fp: Optional[Any]
    default_headers: bool
    default_encoding: Union[str, Callable[[bytes], str]]
    discard_cookies: bool
    raise_for_status: bool
    retry: Retry
    cache: bool
    user_agent: Optional[str]
    accept_language: Optional[str]
    interface: Optional[str]
    doh_url: Optional[str]
    max_recv_speed: int
    curl_options: Optional[Mapping[Any, Any]]
    max_engines: int
    response_class: ResponseClass
    max_clients: Optional[int]
