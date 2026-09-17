"""Type surface of :mod:`chrome_client.utils`.

Helpers mirroring ``requests.utils``.  Only the functions that make sense on top
of a Chromium Core are real; ones whose behaviour would be a lie (urllib3 pool
internals) are absent rather than stubbed.
"""

from typing import Any, Dict, List, Optional, Tuple, Union

from typing_extensions import TypeAlias

from ._types import HeadersLike, Proxies
from .structures import CaseInsensitiveDict

#: What this facade advertises it accepts, for callers reading the constant.
DEFAULT_ACCEPT_ENCODING: str
#: Characters RFC 3986 leaves unescaped.
UNRESERVED_SET: frozenset
#: Files searched for netrc credentials, in order.
NETRC_FILES: Tuple[str, ...]

#: One parsed ``Link`` header entry.
HeaderLink: TypeAlias = Dict[str, str]


def default_headers() -> CaseInsensitiveDict:
    """Returns the headers the facade adds on its own: none.

    ``requests`` seeds ``User-Agent``, ``Accept``, ``Accept-Encoding`` and
    ``Connection`` here. Doing that would corrupt the very thing this client
    exists for: the impersonation profile and Chromium own the default header
    set, their values and their order, and an injected ``Accept: */*`` is visible
    to any fingerprinter. Chromium still emits its own defaults, so the request
    is complete without them.

    Returns:
        An empty :class:`~chrome_client.CaseInsensitiveDict`.

    Example:
        >>> len(default_headers())
        0
    """
    ...


def to_key_val_list(value: Any) -> Optional[List[Tuple[Any, Any]]]:
    """Normalises a mapping or pair sequence to a list of pairs.

    Args:
        value: ``None``, a mapping, or an iterable of pairs.

    Returns:
        A list of pairs, or ``None`` when ``value`` is ``None``.

    Raises:
        ValueError: ``value`` is a str, bytes, bool or int, which cannot be a
            pair sequence -- requests raises here for the same reason.

    Example:
        >>> to_key_val_list({"a": "1"})
        [('a', '1')]
    """
    ...


def super_len(obj: Any) -> int:
    """Returns the length of a file-like object, as requests computes it.

    Args:
        obj: Anything with ``__len__``, ``len``, ``fileno``, or ``tell``/``seek``.

    Returns:
        The number of bytes remaining from the current position, or ``0`` when
        the object offers none of those.
    """
    ...


def unquote_unreserved(uri: str) -> str:
    """Unescapes percent-encodings that stand for unreserved characters.

    Args:
        uri: The URI to normalise.

    Returns:
        The URI with ``%41``-style escapes for unreserved characters replaced by
        the characters themselves.

    Raises:
        InvalidURL: A percent escape is not two hex digits.
    """
    ...


def requote_uri(uri: str) -> str:
    """Percent-encodes a URI without double-encoding existing escapes.

    Args:
        uri: The URI to normalise; may already contain escapes.

    Returns:
        The requoted URI. When the escapes are malformed, the URI is encoded
        without preserving ``%``, as requests does.

    Example:
        >>> requote_uri("https://example.com/a b")
        'https://example.com/a%20b'
    """
    ...


def get_encoding_from_headers(headers: HeadersLike) -> Optional[str]:
    """Returns the charset a ``Content-Type`` header declares.

    Args:
        headers: Response headers.

    Returns:
        The charset, ``"utf-8"`` for a JSON content type without one, or ``None``.

    Example:
        >>> get_encoding_from_headers({"content-type": "text/html; charset=gbk"})
        'gbk'
    """
    ...


def get_encodings_from_content(content: Union[str, bytes]) -> List[str]:
    """Returns the encodings declared inside an HTML/XML body.

    Args:
        content: The body, decoded permissively when bytes.

    Returns:
        Charset names found in ``<meta>`` tags and an XML declaration, in the
        order found.

    Example:
        >>> get_encodings_from_content('<meta charset="gb18030">')
        ['gb18030']
    """
    ...


def guess_json_utf(data: bytes) -> Optional[str]:
    """Guesses the encoding of a JSON body from its first bytes.

    Args:
        data: The body; only the first four bytes are inspected.

    Returns:
        ``"utf-8"``, ``"utf-8-sig"``, ``"utf-16"``, ``"utf-16-le"``,
        ``"utf-16-be"``, ``"utf-32"``, ``"utf-32-le"``, ``"utf-32-be"``, or
        ``None`` when the bytes do not identify one.

    Example:
        >>> guess_json_utf(b'{"a": 1}')
        'utf-8'
    """
    ...


def parse_header_links(value: str) -> List[HeaderLink]:
    """Parses an RFC 5988 ``Link`` header.

    Args:
        value: The header value, e.g.
            ``'<https://example.com/p2>; rel="next"'``.

    Returns:
        One dict per link, each carrying ``url`` plus its parameters, so
        ``links[0]["rel"]`` works.

    Example:
        >>> parse_header_links('<https://example.com/p2>; rel="next"')
        [{'url': 'https://example.com/p2', 'rel': 'next'}]
    """
    ...


def guess_filename(obj: Any) -> Optional[str]:
    """Returns a filename for a file-like object, as requests guesses it.

    Args:
        obj: A file object whose ``name`` is a real path.

    Returns:
        The basename, or ``None`` for objects named like ``<stdin>``.
    """
    ...


def get_auth_from_url(url: str) -> Tuple[str, str]:
    """Extracts credentials embedded in a URL.

    Args:
        url: A URL that may carry ``user:pass@``.

    Returns:
        ``(username, password)``, both unquoted and both ``""`` when absent.

    Example:
        >>> get_auth_from_url("https://user:pass@example.com/")
        ('user', 'pass')
    """
    ...


def urldefragauth(url: str) -> str:
    """Strips credentials and the fragment from a URL.

    Args:
        url: The URL to clean.

    Returns:
        The URL without userinfo or fragment -- the form safe to log, which is
        what requests uses it for.

    Example:
        >>> urldefragauth("https://user:pass@example.com/p#frag")
        'https://example.com/p'
    """
    ...


def prepend_scheme_if_needed(url: str, new_scheme: str) -> str:
    """Adds a scheme to a scheme-less URL.

    Args:
        url: The URL, possibly without a scheme.
        new_scheme: Scheme to assume.

    Returns:
        The URL with a scheme and a netloc.

    Example:
        >>> prepend_scheme_if_needed("example.com/p", "https")
        'https://example.com/p'
    """
    ...


def is_ipv4_address(value: str) -> bool:
    """Reports whether a string is a dotted-quad IPv4 address.

    Args:
        value: The candidate.

    Returns:
        ``True`` for four dot-separated octets.

    Example:
        >>> is_ipv4_address("127.0.0.1"), is_ipv4_address("example.com")
        (True, False)
    """
    ...


def address_in_network(address: str, network: str) -> bool:
    """Reports whether an IPv4 address falls inside a CIDR block.

    Args:
        address: The IPv4 address to test.
        network: A CIDR string, e.g. ``"10.0.0.0/8"``.

    Returns:
        ``True`` when the address is inside the block; ``False`` when either
        argument is malformed.

    Example:
        >>> address_in_network("10.1.2.3", "10.0.0.0/8")
        True
    """
    ...


def should_bypass_proxies(url: str, no_proxy: Optional[str] = None) -> bool:
    """Applies ``NO_PROXY`` semantics for a URL.

    Args:
        url: The URL about to be requested.
        no_proxy: The ``NO_PROXY`` value; read from the environment when omitted.
            Entries may be bare hosts, ``host:port`` or CIDR blocks.

    Returns:
        ``True`` when the proxy should be skipped. A URL with no host bypasses
        the proxy, matching requests.

    Example:
        >>> should_bypass_proxies("http://127.0.0.1/", "127.0.0.1")
        True
    """
    ...


def proxy_bypass(host: str) -> bool:
    """Platform proxy-bypass hook.

    Args:
        host: The host name being requested.

    Returns:
        Always ``False``: the Core does not read platform proxy settings, so
        claiming otherwise would route traffic somewhere the caller did not ask
        for.
    """
    ...


def get_environ_proxies(url: str, no_proxy: Optional[str] = None) -> Dict[str, str]:
    """Returns the proxy environment variables that apply to a URL.

    Args:
        url: The URL about to be requested.
        no_proxy: The ``NO_PROXY`` value; read from the environment when omitted.

    Returns:
        ``{scheme: proxy_url}`` for every ``*_proxy`` variable set, or ``{}``
        when the URL bypasses the proxy.

    Example:
        >>> get_environ_proxies("https://example.com")   # doctest: +SKIP
        {'https': 'http://127.0.0.1:8080'}
    """
    ...


def resolve_proxies(request: Any, proxies: Optional[Proxies], trust_env: bool = True) -> Dict[str, Optional[str]]:
    """Merges caller proxies with the environment, honouring ``NO_PROXY``.

    Args:
        request: A request object carrying ``.url``, or the URL itself.
        proxies: Caller-supplied mapping.
        trust_env: Whether to consult the environment.

    Returns:
        The merged mapping; the caller's own entries always win.

    Example:
        >>> resolve_proxies("https://example.com", {"https": "http://p:8080"})
        {'https': 'http://p:8080'}
    """
    ...


def get_netrc_auth(url: str, raise_errors: bool = False) -> Optional[Tuple[str, str]]:
    """Returns ``.netrc`` credentials for a URL.

    Args:
        url: The URL whose hostname is looked up.
        raise_errors: When ``True``, a malformed netrc file raises instead of
            being treated as absent.

    Returns:
        ``(login, password)``, or ``None`` when there is no netrc file, no host
        entry, or no credentials for it.

    Example:
        >>> get_netrc_auth("https://example.com")   # doctest: +SKIP
        ('user', 'secret')
    """
    ...


def check_header_validity(header: Tuple[Any, Any]) -> None:
    """Rejects header values that would let a caller inject a second header.

    Args:
        header: A ``(name, value)`` pair. Bytes values are allowed.

    Raises:
        InvalidHeader: A part is ``None``, or contains ``\\n``, ``\\r`` or a NUL.

    Example:
        >>> check_header_validity(("Accept", "application/json"))
        >>> check_header_validity(("X-Bad", "a\\r\\nX-Injected: 1"))  # doctest: +SKIP
        Traceback (most recent call last):
        InvalidHeader: Invalid leading whitespace, ...
    """
    ...


def default_user_agent(name: str = ...) -> str:
    """Returns a placeholder User-Agent.

    Kept for source compatibility only: the impersonation profile owns the real
    User-Agent, so nothing in this package calls it to build a request.

    Args:
        name: The label to return.

    Returns:
        ``name`` unchanged.
    """
    ...
