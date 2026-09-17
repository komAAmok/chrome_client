"""Type surface of :mod:`chrome_client.cookies`.

Two stores exist and they are not the same thing:

* Chromium's ``CookieMonster`` inside the Core, which parses ``Set-Cookie``,
  applies domain/path/``SameSite``/``Secure`` policy and attaches the ``Cookie``
  header itself. It is authoritative and override any ``Cookie`` header the
  caller supplies -- and ABI v8 cannot read, write or clear it.
* This jar, a ``requests``-shaped :class:`RequestsCookieJar`. It mirrors every
  ``Set-Cookie`` the Core reports so ``session.cookies`` is readable, and it
  supplies caller-set cookies on the way out.

Because the Core wins ties, a caller mutation that conflicts with what the Core
already stored can only be honoured by rebuilding the engine; ``Session`` does
that automatically, and the jar's ``revision`` is how it notices.

``RequestsCookieJar`` is declared as a ``MutableMapping[str, str]`` (the same
shape typeshed gives ``requests.cookies.RequestsCookieJar``) so ``jar["k"]``,
``jar.set(...)``, ``jar.get_dict()`` and ``jar.update(...)`` all type. Note the
runtime quirk it inherits from ``cookielib``: ``for cookie in jar`` yields
``Cookie`` objects, not names -- use :meth:`RequestsCookieJar.keys` for names.
"""

import http.cookiejar as cookielib
from http.cookiejar import Cookie as Cookie
from http.cookies import Morsel
from typing import Any, Dict, Iterable, Iterator, List, Mapping, MutableMapping, Optional, Tuple, Union

from typing_extensions import TypeAlias

from ._types import HeadersLike


class MockRequest:
    """Adapts a URL and header mapping to the interface ``cookielib`` expects.

    ``cookielib`` calls into a request object to decide which cookies apply; this
    supplies the handful of methods it needs for a URL whose headers have not
    been built yet.

    Attributes:
        type: The URL scheme, as ``cookielib`` calls it.
    """

    type: str

    def __init__(self, url: str, headers: Optional[HeadersLike] = None) -> None:
        """Wraps a URL for cookie matching.

        Args:
            url: The URL cookies are being matched against.
            headers: Headers already decided for this request; a ``Host`` header
                is used to rebuild the URL when the port is not inline.
        """
        ...

    def get_type(self) -> str:
        """Returns the URL scheme.

        Returns:
            e.g. ``"https"``.
        """
        ...

    def get_host(self) -> str:
        """Returns the URL's netloc.

        Returns:
            Host and port as written, e.g. ``"example.com:8443"``.
        """
        ...

    def get_origin_req_host(self) -> str:
        """Alias of :meth:`get_host`, as ``cookielib`` names it."""
        ...

    def get_full_url(self) -> str:
        """Returns the URL, rebuilt from ``Host`` when the port is not inline.

        Returns:
            The request URL ``cookielib`` should match domain cookies against.
        """
        ...

    def is_unverifiable(self) -> bool:
        """Always ``True``: there is no browsing context to be same-origin with.

        Returns:
            ``True``, so third-party-cookie policy is applied conservatively.
        """
        ...

    def has_header(self, name: str) -> bool:
        """Reports whether a header was seen or added.

        Args:
            name: Header name, matched exactly as ``cookielib`` spells it.

        Returns:
            ``True`` when the header exists in either the incoming or the
            newly-added set.
        """
        ...

    def get_header(self, name: str, default: Optional[str] = None) -> Optional[str]:
        """Returns a header value.

        Args:
            name: Header name.
            default: Returned when the header is absent.

        Returns:
            The value, or ``default``.
        """
        ...

    def add_header(self, key: str, value: str) -> None:
        """Always raises: cookie headers must go through
        :meth:`add_unredirected_header`.

        Args:
            key: Header name, ignored.
            value: Header value, ignored.

        Raises:
            NotImplementedError: Always. ``cookielib`` calls this for redirects,
                and cookies must survive a redirect to be sent with it."""
        ...

    def add_unredirected_header(self, name: str, value: str) -> None:
        """Records a header that must be re-emitted on redirect.

        Args:
            name: Header name.
            value: Header value.
        """
        ...

    def get_new_headers(self) -> Dict[str, str]:
        """Returns the headers added through :meth:`add_unredirected_header`.

        Returns:
            The mapping; ``request.get_new_headers()["Cookie"]`` is how the
            facade reads back the cookie line it built.
        """
        ...

    @property
    def unverifiable(self) -> bool:
        """``cookielib``'s attribute form of :meth:`is_unverifiable`.

        Returns:
            Always ``True``."""
        ...

    @property
    def origin_req_host(self) -> str:
        """``cookielib``'s attribute form of :meth:`get_origin_req_host`.

        Returns:
            The request host."""
        ...

    @property
    def host(self) -> str:
        """``cookielib``'s attribute form of :meth:`get_host`.

        Returns:
            The request host."""
        ...


class MockResponse:
    """Adapts raw ``Set-Cookie`` lines to the interface ``cookielib`` expects."""

    def __init__(self, set_cookie_lines: Iterable[str]) -> None:
        """Wraps the ``Set-Cookie`` lines observed on a hop.

        Args:
            set_cookie_lines: One string per ``Set-Cookie`` field, unjoined, so
                an ``Expires`` value containing a comma stays intact.
        """
        ...

    def info(self) -> "MockResponse":
        """Returns itself, as ``cookielib`` expects on message-like objects.

        Returns:
            This object."""
        ...

    def getheaders(self, name: str) -> List[str]:
        """Alias of :meth:`get_all`, as ``cookielib`` spells it.

        Args:
            name: Header name; only ``set-cookie`` is meaningful.

        Returns:
            The stored lines.
        """
        ...

    def get_all(self, name: str, default: Optional[List[str]] = None) -> List[str]:
        """Returns all values of a header.

        Args:
            name: Header name, matched case-insensitively.
            default: Returned when the header is absent and ``None`` was given.

        Returns:
            The ``Set-Cookie`` lines, or ``default``/``[]`` for another name.
        """
        ...


def create_cookie(name: str, value: str, **kwargs: Any) -> cookielib.Cookie:
    """Builds a ``cookielib.Cookie`` with requests' defaults.

    Args:
        name: Cookie name.
        value: Cookie value.
        **kwargs: Any cookie attribute: ``domain``, ``path``, ``secure``,
            ``expires``, ``discard``, ``comment``, ``comment_url``, ``rest``,
            ``rfc2109``, ``version``, ``port``. Unknown names raise.

    Returns:
        The cookie, with ``domain_specified``/``path_specified``/
        ``domain_initial_dot`` derived from the values given.

    Raises:
        TypeError: An unexpected keyword argument was passed.

    Example:
        >>> cookie = create_cookie("sid", "abc", domain="example.com", path="/")
        >>> cookie.name, cookie.path
        ('sid', '/')
    """
    ...


def morsel_to_cookie(morsel: Morsel) -> cookielib.Cookie:
    """Converts a ``http.cookies.Morsel`` to a ``cookielib.Cookie``.

    Args:
        morsel: The parsed cookie. ``max-age`` wins over ``expires``, and both
            are converted to an absolute epoch time.

    Returns:
        The equivalent ``cookielib.Cookie``.

    Raises:
        TypeError: ``max-age`` is not an integer.

    Example:
        >>> from http.cookies import SimpleCookie
        >>> jar.set("sid", morsel_to_cookie(SimpleCookie("sid=abc")["sid"]))
    """
    ...


class RequestsCookieJar(  # type: ignore[misc]  # cookielib and Mapping disagree on __iter__; requests has the same shape
    cookielib.CookieJar, MutableMapping[str, str]
):
    """``cookielib.CookieJar`` with the dict interface requests exposes.

    ``revision`` increments on every mutation. ``Session`` snapshots it after
    mirroring server cookies, so a later change means the *caller* edited the jar
    and the Core's own cookie store has to be discarded for the edit to take
    effect.

    Attributes:
        revision: Mutation counter, incremented by every caller-visible change.

    Example:
        >>> jar = RequestsCookieJar()
        >>> jar.set("sid", "abc", domain="example.com", path="/")
        >>> jar.get_dict(domain="example.com")
        {'sid': 'abc'}
        >>> del jar["sid"]
    """

    revision: int

    def __init__(self, policy: Optional[Any] = None) -> None:
        """Creates an empty jar.

        Args:
            policy: A ``cookielib`` cookie policy; the default policy is used
                when omitted.
        """
        ...

    # -- mutation bookkeeping ------------------------------------------------
    def set_cookie(self, cookie: cookielib.Cookie, *args: Any, **kwargs: Any) -> None:
        """Adds a cookie and bumps :attr:`revision`.

        Args:
            cookie: The cookie to store. A double-quoted value is unquoted first,
                matching requests.
            *args: Forwarded to ``cookielib.CookieJar.set_cookie``.
            **kwargs: Forwarded to ``cookielib.CookieJar.set_cookie``.

        Example:
            >>> jar.set_cookie(create_cookie("a", "1"))
        """
        ...

    def clear(self, domain: Optional[str] = None, path: Optional[str] = None, name: Optional[str] = None) -> None:
        """Removes cookies and bumps :attr:`revision`.

        Args:
            domain: Restrict removal to this domain.
            path: Restrict removal to this path.
            name: Restrict removal to this name.

        Example:
            >>> jar.clear()                       # everything
            >>> jar.clear(domain="example.com")   # one domain
        """
        ...

    def clear_expired_cookies(self) -> None:
        """Drops expired cookies without bumping :attr:`revision`.

        ``cookielib.add_cookie_header`` calls this on every read, so counting it
        as a caller mutation would make every request look like a jar edit.
        """
        ...

    def clear_session_cookies(self) -> None:
        """Drops session cookies and bumps :attr:`revision`."""
        ...

    # -- requests dict interface --------------------------------------------
    def get(  # type: ignore[override]  # requests adds domain/path filters
        self,
        name: str,
        default: Optional[str] = None,
        domain: Optional[str] = None,
        path: Optional[str] = None,
    ) -> Optional[str]:
        """Returns a cookie value.

        Args:
            name: Cookie name.
            default: Returned when the cookie is absent.
            domain: Restrict the lookup to this domain.
            path: Restrict the lookup to this path.

        Returns:
            The cookie value, or ``default``.

        Raises:
            CookieConflictError: Two cookies share the name and both match.

        Example:
            >>> jar.get("sid")
            'abc'
        """
        ...

    def set(self, name: str, value: Optional[str], **kwargs: Any) -> Optional[cookielib.Cookie]:
        """Sets a cookie, or removes it when ``value`` is ``None``.

        Args:
            name: Cookie name.
            value: Cookie value; ``None`` removes every cookie with that name
                (optionally filtered by ``domain``/``path`` in ``kwargs``).
            **kwargs: Cookie attributes passed to :func:`create_cookie`, e.g.
                ``domain``, ``path``, ``secure``, ``expires``.

        Returns:
            The stored cookie, or ``None`` when the call removed one.

        Example:
            >>> jar.set("consent", "1", domain="example.com", path="/")
            >>> jar.set("consent", None, domain="example.com")   # removes it
        """
        ...

    def iterkeys(self) -> Iterator[str]:
        """Yields cookie names.

        Yields:
            Each cookie's name.
        """
        ...

    def keys(self) -> List[str]:  # type: ignore[override]  # requests returns a list
        """Returns the cookie names.

        Returns:
            The names, in jar order.

        Example:
            >>> jar.keys()
            ['sid', 'consent']
        """
        ...

    def itervalues(self) -> Iterator[str]:
        """Yields cookie values.

        Yields:
            Each cookie's value.
        """
        ...

    def values(self) -> List[str]:  # type: ignore[override]  # requests returns a list
        """Returns the cookie values.

        Returns:
            The values, in jar order.
        """
        ...

    def iteritems(self) -> Iterator[Tuple[str, str]]:
        """Yields ``(name, value)`` pairs.

        Yields:
            One pair per cookie.
        """
        ...

    def items(self) -> List[Tuple[str, str]]:  # type: ignore[override]  # requests returns a list
        """Returns ``(name, value)`` pairs.

        Returns:
            The pairs, in jar order.
        """
        ...

    def list_domains(self) -> List[str]:
        """Returns the distinct cookie domains.

        Returns:
            Each domain once, in first-seen order.

        Example:
            >>> jar.list_domains()
            ['.example.com']
        """
        ...

    def list_paths(self) -> List[str]:
        """Returns the distinct cookie paths.

        Returns:
            Each path once, in first-seen order.
        """
        ...

    def multiple_domains(self) -> bool:
        """Reports whether two cookies share a domain.

        Returns:
            ``True`` when the jar holds cookies for the same domain more than
            once, which makes a plain ``dict`` view lossy.
        """
        ...

    def get_dict(self, domain: Optional[str] = None, path: Optional[str] = None) -> Dict[str, str]:
        """Returns the jar as a plain dict, optionally filtered.

        Args:
            domain: Only include cookies for this domain.
            path: Only include cookies for this path.

        Returns:
            ``{name: value}``; later duplicates win.

        Example:
            >>> jar.get_dict(domain="example.com")
            {'sid': 'abc'}
        """
        ...

    def __contains__(self, name: object) -> bool:
        """Reports whether a cookie with this name exists.

        Args:
            name: Cookie name.

        Returns:
            ``True`` when a matching cookie is present.
        """
        ...

    def __getitem__(self, name: str) -> str:
        """Returns a cookie value by name.

        Args:
            name: Cookie name.

        Returns:
            The value.

        Raises:
            KeyError: No cookie has that name.
            CookieConflictError: Several cookies share the name.
        """
        ...

    def __setitem__(self, name: str, value: str) -> None:
        """Sets a cookie through :meth:`set`.

        Args:
            name: Cookie name.
            value: Cookie value.
        """
        ...

    def __delitem__(self, name: str) -> None:
        """Removes a cookie by name.

        Args:
            name: Cookie name.

        Raises:
            KeyError: No cookie has that name.
        """
        ...

    def delete(self, name: str, domain: Optional[str] = None, path: Optional[str] = None) -> None:
        """curl_cffi spelling of ``__delitem__`` with domain/path filters.

        Args:
            name: Cookie name.
            domain: Only remove cookies for this domain.
            path: Only remove cookies for this path.

        Example:
            >>> jar.delete("sid", domain="example.com")
        """
        ...

    def remove_cookie_by_name(self, name: str, domain: Optional[str] = None, path: Optional[str] = None) -> bool:
        """Removes matching cookies.

        Args:
            name: Cookie name.
            domain: Only remove cookies for this domain.
            path: Only remove cookies for this path.

        Returns:
            ``True`` when at least one cookie was removed.
        """
        ...

    def update(  # type: ignore[override]  # accepts a jar, a mapping or pairs
        self, other: Optional[Any] = None, **kwargs: Any
    ) -> None:
        """Merges cookies from a jar, a mapping or a sequence of pairs.

        Args:
            other: A ``CookieJar`` (copied cookie by cookie), a mapping, or an
                iterable of ``(name, value)`` pairs.
            **kwargs: Extra ``name=value`` cookies to set.

        Example:
            >>> jar.update({"a": "1", "b": "2"})
        """
        ...

    def copy(self) -> "RequestsCookieJar":
        """Returns an independent copy.

        Returns:
            A new jar holding a copy of every cookie.
        """
        ...

    def get_policy(self) -> Any:
        """Returns the cookie policy this jar was built with.

        Returns:
            The policy object, or ``None`` for the default.
        """
        ...

    def __repr__(self) -> str:
        """Returns a ``<RequestsCookieJar[name=value, ...]>`` summary.

        Returns:
            The summary string."""
        ...
    def __getstate__(self) -> Dict[str, Any]:
        """Returns the picklable state, without the lock.

        Returns:
            The instance ``__dict__`` minus ``_cookies_lock``."""
        ...
    def __setstate__(self, state: Mapping[str, Any]) -> None:
        """Restores the state and recreates the lock.

        Args:
            state: A mapping produced by :meth:`__getstate__`."""
        ...

    # -- Core bridge --------------------------------------------------------
    def cookie_header(self, url: str, headers: Optional[HeadersLike] = None) -> Optional[str]:
        """Returns the ``Cookie`` header value this jar would send for ``url``.

        Args:
            url: The URL about to be requested.
            headers: Headers already decided for the request; a ``Host`` header
                participates in matching.

        Returns:
            The ``Cookie`` line, or ``None`` when no cookie applies.

        Example:
            >>> jar.cookie_header("https://example.com/")
            'sid=abc'
        """
        ...

    def absorb_set_cookie(self, url: str, lines: Iterable[str]) -> "RequestsCookieJar":
        """Records ``Set-Cookie`` lines observed for ``url``.

        Args:
            url: The URL the response came from.
            lines: One string per ``Set-Cookie`` field.

        Returns:
            A fresh jar holding only the cookies *this* hop set, which becomes
            ``response.cookies``.

        Example:
            >>> response.cookies = jar.absorb_set_cookie(url, lines)
        """
        ...


def cookiejar_from_dict(
    cookie_dict: Optional[Mapping[str, str]],
    cookiejar: Optional[RequestsCookieJar] = None,
    overwrite: bool = True,
) -> RequestsCookieJar:
    """Builds a jar from a mapping.

    Args:
        cookie_dict: ``{name: value}``. ``None`` yields an empty jar.
        cookiejar: Jar to fill; a new one is created when omitted.
        overwrite: When ``False``, names already in the jar are left alone.

    Returns:
        The jar.

    Example:
        >>> cookiejar_from_dict({"a": "1"}).get_dict()
        {'a': '1'}
    """
    ...


def dict_from_cookiejar(cookiejar: cookielib.CookieJar) -> Dict[str, str]:
    """Returns a jar as a plain dict.

    Args:
        cookiejar: Any ``cookielib`` jar.

    Returns:
        ``{name: value}``.

    Example:
        >>> dict_from_cookiejar(session.cookies)
        {'sid': 'abc'}
    """
    ...


def add_dict_to_cookiejar(cookiejar: RequestsCookieJar, cookie_dict: Mapping[str, str]) -> RequestsCookieJar:
    """Adds every entry of a mapping to an existing jar.

    Args:
        cookiejar: The jar to fill.
        cookie_dict: ``{name: value}``.

    Returns:
        The same jar.

    Example:
        >>> add_dict_to_cookiejar(session.cookies, {"a": "1"})
    """
    ...


def merge_cookies(cookiejar: cookielib.CookieJar, cookies: Union[Mapping[str, str], cookielib.CookieJar, None]) -> cookielib.CookieJar:
    """Merges ``cookies`` into ``cookiejar``, as requests does.

    Args:
        cookiejar: The destination jar.
        cookies: A mapping (merged without overwriting existing names) or
            another jar (copied cookie by cookie).

    Returns:
        ``cookiejar``, mutated in place.

    Raises:
        ValueError: ``cookiejar`` is not a ``cookielib.CookieJar``.

    Example:
        >>> merged = merge_cookies(RequestsCookieJar(), {"a": "1"})
    """
    ...


#: Historical name for this package's jar.
CookieJar: TypeAlias = RequestsCookieJar
#: curl_cffi's name for the same jar.
Cookies: TypeAlias = RequestsCookieJar
