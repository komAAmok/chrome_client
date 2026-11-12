"""Cookie storage.

Two stores exist and they are not the same thing:

* Chromium's ``CookieMonster`` inside the Core.  It parses ``Set-Cookie``,
  applies domain/path/``SameSite``/``Secure`` policy, and attaches the ``Cookie``
  header itself.  It is authoritative and, importantly, *overrides* any ``Cookie``
  header the caller supplies once it holds a cookie for the target URL.  It is
  also unreachable through ABI v8 -- nothing can read, write, or clear it.
* This jar, a ``requests``-shaped ``RequestsCookieJar``.  It mirrors every
  ``Set-Cookie`` the Core reports so ``session.cookies`` is readable, and it
  supplies caller-set cookies on the way out.

Because the Core wins ties, a caller mutation that conflicts with what the Core
already stored can only be honoured by discarding that Core cookie store, which
means rebuilding the Engine.  ``Session`` does that automatically, by comparing
the ``Cookie`` line this jar would send against the one its engine mirror would
send (``BaseSession._resolve_cookies``); no counter is involved.
"""

import calendar
import copy
import threading
import time
from http import cookiejar as cookielib
from http.cookiejar import Cookie
from http.cookies import Morsel
from urllib.parse import urlparse, urlunparse

try:
    from collections.abc import Mapping, MutableMapping
except ImportError:  # Python 3.6
    from collections import Mapping, MutableMapping

from .exceptions import CookieConflictError


class MockRequest(object):
    """Adapts a URL and header mapping to the interface ``cookielib`` expects."""

    def __init__(self, url, headers=None):
        self._url = url
        self._headers = headers if headers is not None else {}
        self._new_headers = {}
        self.type = urlparse(self._url).scheme

    def get_type(self):
        return self.type

    def get_host(self):
        return urlparse(self._url).netloc

    get_origin_req_host = get_host

    def get_full_url(self):
        # cookielib needs a Host to match against even for schemes that carry
        # the port inline, so rebuild the URL from the Host header when needed.
        if "Host" not in self._headers:
            return self._url
        parsed = urlparse(self._url)
        if parsed.port:
            return self._url
        return urlunparse([
            parsed.scheme, self._headers["Host"], parsed.path,
            parsed.params if hasattr(parsed, "params") else "",
            parsed.query, parsed.fragment,
        ])

    def is_unverifiable(self):
        return True

    def has_header(self, name):
        return name in self._headers or name in self._new_headers

    def get_header(self, name, default=None):
        return self._headers.get(name, self._new_headers.get(name, default))

    def add_header(self, key, value):
        raise NotImplementedError("cookie headers should be added with add_unredirected_header")

    def add_unredirected_header(self, name, value):
        self._new_headers[name] = value

    def get_new_headers(self):
        return self._new_headers

    @property
    def unverifiable(self):
        return self.is_unverifiable()

    @property
    def origin_req_host(self):
        return self.get_origin_req_host()

    @property
    def host(self):
        return self.get_host()


class MockResponse(object):
    """Adapts raw ``Set-Cookie`` lines to the interface ``cookielib`` expects."""

    def __init__(self, set_cookie_lines):
        self._lines = list(set_cookie_lines)

    def info(self):
        return self

    def getheaders(self, name):
        return self.get_all(name)

    def get_all(self, name, default=None):
        if name.lower() != "set-cookie":
            return default if default is not None else []
        return list(self._lines)


def create_cookie(name, value, **kwargs):
    """Builds a ``cookielib.Cookie`` with requests' defaults."""
    result = {
        "version": 0, "name": name, "value": value, "port": None, "domain": "",
        "path": "/", "secure": False, "expires": None, "discard": True,
        "comment": None, "comment_url": None, "rest": {"HttpOnly": None},
        "rfc2109": False,
    }
    unsupported = set(kwargs) - set(result)
    if unsupported:
        raise TypeError("create_cookie() got unexpected keyword arguments %s"
                        % sorted(unsupported))
    result.update(kwargs)
    result["port_specified"] = bool(result["port"])
    result["domain_specified"] = bool(result["domain"])
    result["domain_initial_dot"] = result["domain"].startswith(".")
    result["path_specified"] = bool(result["path"])
    return Cookie(**result)


#: The three date formats RFC 6265 section 5.1.1 requires a cookie parser to
#: accept, plus the one ``http.cookies`` itself emits.
#: RFC 850 spells the weekday out in full ("Sunday, 06-Nov-94 08:49:37 GMT")
#: while RFC 1123 abbreviates it. `%a` matches only the abbreviation, so the
#: strict RFC 850 spelling is listed separately with `%A`; without that entry a
#: server using the RFC 850 form lost its expiry. Both spellings occur in the
#: wild, so both are accepted instead of picking one.
_COOKIE_DATE_FORMATS = (
    "%a, %d %b %Y %H:%M:%S GMT",   # RFC 1123, what servers actually send
    "%a, %d-%b-%Y %H:%M:%S GMT",   # RFC 850 with an abbreviated weekday
    "%A, %d-%b-%Y %H:%M:%S GMT",   # RFC 850 as specified, weekday spelled out
    "%a %b %d %H:%M:%S %Y",        # asctime
    "%a, %d-%b-%y %H:%M:%S GMT",   # two-digit RFC 850 year
    "%A, %d-%b-%y %H:%M:%S GMT",   # two-digit year, weekday spelled out
)


def _parse_cookie_date(text):
    """Parses a cookie ``Expires`` value; returns epoch seconds or ``None``.

    An unparseable date means "no expiry" rather than a failed cookie:
    RFC 6265 tells parsers to ignore an attribute they cannot read, and
    raising here lost the whole cookie instead of just its expiry.
    """
    for template in _COOKIE_DATE_FORMATS:
        try:
            return calendar.timegm(time.strptime(text, template))
        except ValueError:
            continue
    return None


def morsel_to_cookie(morsel):
    """Converts a ``http.cookies.Morsel`` to a ``cookielib.Cookie``."""
    expires = None
    if morsel["max-age"]:
        try:
            expires = int(time.time() + int(morsel["max-age"]))
        except ValueError:
            raise TypeError("max-age: %s must be integer" % morsel["max-age"])
    elif morsel["expires"]:
        # ``http.cookies`` accepts three date formats (RFC 1123 "Wed, 21 Oct
        # 2015 ...", RFC 850 "Wednesday, 21-Oct-15 ...", and asctime), and
        # every real server sends the first. Parsing only the RFC 850 shape
        # requests happens to use raised ValueError("time data ... does not
        # match format") on the common form -- measured with
        # ``expires=Wed, 21 Oct 2015 07:28:00 GMT``.
        expires = _parse_cookie_date(morsel["expires"])
    return create_cookie(
        comment=morsel["comment"] or None, comment_url=bool(morsel["comment"]),
        discard=False, domain=morsel["domain"], expires=expires,
        name=morsel.key, path=morsel["path"], port=None,
        rest={"HttpOnly": morsel["httponly"]}, rfc2109=False,
        secure=bool(morsel["secure"]), value=morsel.value, version=morsel["version"] or 0,
    )


def _normalise_domain(domain):
    """Returns *domain* without the leading dot, so the two spellings compare.

    ``cookielib`` keeps `.example.com` and `example.com` as different keys, and a
    host-only cookie is stored with an empty domain. RFC 6265 does not
    distinguish the first two, so every comparison here goes through this.
    """
    return domain[1:] if domain.startswith(".") else domain


def _domain_covers(outer, inner):
    """True when a cookie scoped to *outer* also applies to the host *inner*.

    An empty domain means "whatever host the request is for", so it is a
    different thing from a domain that happens to match: it covers every host.
    The two directions are written out separately on purpose -- one of them is
    free (``outer`` empty) and the other costs something (``inner`` empty).
    """
    if not outer:
        # The new scope is "any host", which trivially covers the old one.
        return True
    if not inner:
        # The old cookie had no domain, so it is not a narrower scope than the
        # new one; they describe the same name over overlapping hosts.
        return True
    outer_n, inner_n = _normalise_domain(outer), _normalise_domain(inner)
    if inner_n == outer_n:
        return True
    return inner_n.endswith("." + outer_n)


def _same_path(left, right):
    """True when two paths name the same cookie slot.

    A cookie's identity is ``(domain, path, name)`` (RFC 6265 §5.3), so a write
    at ``/`` and a stored cookie at ``/sub`` are *different* cookies and both
    must survive. An earlier version used RFC 6265 §5.1.4's path-match here,
    which answers "would this request carry that cookie" -- a different
    question -- and made ``set("a", "root", path="/")`` silently delete
    ``a`` at ``/sub``, the same class of loss the server-side split exists to
    prevent.
    """
    return (left or "/") == (right or "/")


def _replaces(existing, new):
    """True when *new* supersedes *existing* instead of sitting beside it.

    Used for caller assignments only (``set()`` / ``update()``). Two distinct
    scopes are legitimate: ``example.invalid`` and ``other.invalid`` may each hold
    a cookie named ``a``, and RFC 6265 requires a request to each host to send
    only its own. So does the same host at two paths.
    """
    if not _domain_covers(new.domain, existing.domain):
        return False
    return _same_path(new.path, existing.path)


class RequestsCookieJar(cookielib.CookieJar, MutableMapping):
    """``cookielib.CookieJar`` with the dict interface requests exposes.

    ``revision`` counts mutations for callers that want to poll the jar.  It is
    not what ``Session`` uses: the engine's cookie store is rebuilt when the
    ``Cookie`` line this jar would send differs from the one the engine's mirror
    would send (``BaseSession._resolve_cookies``).  Reading can move this counter,
    because ``cookielib.add_cookie_header`` calls ``clear_expired_cookies`` and
    that clears each expired cookie through ``clear()``.
    """

    def __init__(self, policy=None, sink=None):
        """Creates the jar.

        Args:
            policy: The ``cookielib`` policy; ``None`` uses the default.
            sink: Optional write-through target. When set, every mutation is
                pushed into it so the jar and the thing requests actually send
                from cannot disagree. The sink must provide
                ``write_cookie(cookie, source_url)``,
                ``remove_cookie(domain, path, name)`` and ``clear_cookies()``.
        """
        cookielib.CookieJar.__init__(self, policy)
        self.revision = 0
        #: Write-through target, or ``None`` when this jar stands alone.
        self._sink = sink
        #: The URL a caller assignment is recorded as coming from. ``set()``
        #: does not carry one, and the store needs a source to apply its own
        #: domain rules, so the assignment falls back to a URL built from the
        #: cookie's own domain. Empty means "no source known".
        self.source_url = ""
        #: The last URL this jar served, used as the source for a caller
        #: assignment that names no domain. Empty until a request has run.
        self.default_source = ""
        #: When true, mutations stay in the jar and are not written through.
        #: ``discard_cookies`` sets this for the duration of its request: the
        #: response's cookies reach the jar (a caller reads them there) but must
        #: not reach the store, which is the "nor record" half of the promise.
        self.writing_suppressed = False

    # -- mutation bookkeeping ------------------------------------------------
    def set_cookie(self, cookie, *args, **kwargs):
        """Stores one cookie, exactly as ``cookielib`` would.

        No retirement happens here on purpose. This method is also the path a
        server's ``Set-Cookie`` takes (``extract_cookies`` -> ``set_cookie``), and
        a cookie store must keep every distinct ``(domain, path, name)`` triple:
        RFC 6265 §5.4 requires a request to ``/sub/x`` to send both
        ``a=sub; Path=/sub`` and ``a=root; Path=/``. Applying the caller-assignment
        rule here collapsed those two into one, and because the facade then
        pushes the jar's ``Cookie`` line as a header whenever it disagrees with
        the Core, the loss reached the wire: measured, a server that set both
        paths got ``Cookie: a=root`` on the next ``/sub/x`` request.

        ``set()`` and ``update()`` are caller assignments and do retire through
        :meth:`_retire_replaced`; that is the difference between "the server told
        me" and "I am setting this".
        """
        if hasattr(cookie.value, "startswith") and cookie.value.startswith('"') \
                and cookie.value.endswith('"'):
            cookie.value = cookie.value.replace('\\"', "").strip('"')
        self.revision += 1
        stored = cookielib.CookieJar.set_cookie(self, cookie, *args, **kwargs)
        try:
            self._write_through(cookie, self._source_for(cookie))
        except Exception:
            # The store rejected it, so the jar must not keep it either: a
            # caller that saw the exception would otherwise find the cookie in
            # ``session.cookies`` and conclude the assignment worked while the
            # wire never carried it. Measured: ``set(..., domain="not a
            # domain")`` raised and left the entry in the jar.
            cookielib.CookieJar.clear(self, cookie.domain, cookie.path,
                                      cookie.name)
            raise
        return stored

    # -- write-through -------------------------------------------------------
    def _source_for(self, cookie):
        """Returns the URL a store write for *cookie* should claim.

        A server ``Set-Cookie`` already knows its URL; a caller assignment via
        ``set()`` does not, so it falls back to the cookie's own domain. That is
        the same source a browser records for a script-set cookie, and it keeps
        the store's domain checks running instead of bypassing them.

        Args:
            cookie: The cookie about to be written.

        Returns:
            The source URL, or an empty string when none can be derived.
        """
        if self.source_url:
            return self.source_url
        domain = getattr(cookie, "domain", "") or ""
        if not domain:
            # A caller assignment with no domain means "for whatever this
            # session is talking to". The store needs a concrete source to run
            # its own domain check, so the last URL served fills in.
            return self.default_source or ""
        return "%s://%s%s" % (
            "https" if getattr(cookie, "secure", False) else "http",
            domain.lstrip("."),
            getattr(cookie, "path", "/") or "/")

    def _write_through(self, cookie, source_url):
        """Pushes one cookie into the sink, when one is attached.

        A rejected write is not silent: the store's own validation decides, and
        its error propagates to the caller (AGENTS §8). What the jar holds stays
        as ``cookielib`` recorded it, so a caller can still inspect the jar; the
        store is the authority for what goes on the wire.

        Args:
            cookie: The cookie that was just stored.
            source_url: The URL it came from.
        """
        if self._sink is None or self.writing_suppressed or not source_url:
            return
        self._sink.write_cookie(cookie, source_url)

    def _delete_through(self, domain, path, name):
        """Removes one cookie from the sink, when one is attached.

        Args:
            domain: Cookie domain.
            path: Cookie path.
            name: Cookie name.
        """
        if self._sink is None or self.writing_suppressed:
            return
        self._sink.remove_cookie(domain, path, name)

    def _clear_through(self):
        """Empties the sink, when one is attached."""
        if self._sink is None or self.writing_suppressed:
            return
        self._sink.clear_cookies()

    def _retire_replaced(self, cookie):
        """Clears the entries a *caller assignment* supersedes.

        Only ``set()`` and ``update()`` reach this. A server ``Set-Cookie`` goes
        through :meth:`set_cookie`` and leaves every other scope intact, because
        a store merges rather than assigns.

        Clearing happens before the insert because ``cookielib`` keys its store
        by ``(domain, path, name)``: clearing after would delete the cookie that
        was just added whenever the two triples match exactly.
        """
        for existing in list(iter(self)):
            if existing.name != cookie.name:
                continue
            if not _replaces(existing, cookie):
                continue
            cookielib.CookieJar.clear(self, existing.domain, existing.path,
                                      existing.name)
            # Retiring is a deletion too: the store must lose the old value
            # before the replacement lands, or it would keep sending both.
            self._delete_through(existing.domain, existing.path,
                                 existing.name)

    def clear(self, domain=None, path=None, name=None):
        """Removes cookies, writing the removal through to the sink.

        ``cookielib.CookieJar.clear`` only drops one exact triple, so a partial
        clear is widened here to "every entry this scope covers":
        ``clear(domain="example.com")`` must also drop ``.example.com`` from the
        store, or the store would keep sending a cookie the jar no longer has.
        """
        self.revision += 1
        if domain is None and path is None and name is None:
            cookielib.CookieJar.clear(self)
            self._clear_through()
            return None
        triples = []
        for cookie in list(iter(self)):
            if domain is not None and cookie.domain != domain:
                continue
            if path is not None and cookie.path != path:
                continue
            if name is not None and cookie.name != name:
                continue
            triples.append((cookie.domain, cookie.path, cookie.name))
        result = cookielib.CookieJar.clear(self, domain, path, name)
        for triple in triples:
            self._delete_through(*triple)
        return result

    def clear_expired_cookies(self):
        # Deliberately not a revision bump: `cookielib.add_cookie_header` calls
        # this on every read, and expiry is not a caller mutation.
        return cookielib.CookieJar.clear_expired_cookies(self)

    def clear_session_cookies(self):
        self.revision += 1
        for cookie in list(iter(self)):
            if cookie.expires is None:
                self._delete_through(cookie.domain, cookie.path, cookie.name)
        return cookielib.CookieJar.clear_session_cookies(self)

    # -- requests dict interface --------------------------------------------
    def get(self, name, default=None, domain=None, path=None):
        try:
            return self._find_no_duplicates(name, domain, path)
        except KeyError:
            return default

    def set(self, name, value, **kwargs):
        """Sets a cookie; ``value=None`` removes it, matching requests.

        A plain set replaces every cookie of this name whose scope it covers.
        ``cookielib.CookieJar.set_cookie`` only overwrites an entry when the
        ``(domain, path, name)`` triple matches exactly, so a jar holding ``a``
        for ``.example.com`` that receives a caller-written ``a`` for the default
        empty domain keeps both: the request then sends
        ``Cookie: a=server; a=1`` and ``jar["a"]`` raises ``CookieConflictError``.
        Upstream requests behaves the same way (measured against 2.34.2), but for
        a setter that is the wrong answer: the caller wrote "a is 1" and neither
        the read nor the outgoing header agrees.

        The replacement is scope-aware, and the two directions are deliberately
        not symmetric:

        * a write that names a domain replaces only the entries that domain
          covers -- ``set("a", "1", domain="example.invalid")`` leaves
          ``other.invalid`` alone, and ``set("a", ..., domain=".example.com")``
          absorbs a ``sub.example.com`` entry, because the parent scope is what
          the caller asked for;
        * a write that names no domain replaces every entry of that name, because
          an unspecified domain means "this value, wherever this cookie
          applies". Leaving the old ones in place is exactly the defect the
          replacement exists to fix: two entries, a conflict on read, and a
          doubled ``Cookie`` line on the wire.

        The rule is the same in both directions: an unspecified domain on either
        side means the two scopes cannot be told apart, so they are the same
        cookie. Writing ``set("a", "1", domain="example.invalid")`` therefore
        also retires a domain-less ``a`` -- which is what one value per name
        costs when a name is written without saying where it applies.

        ``set_cookie`` and ``update`` from a *jar* keep the upstream semantics:
        they carry the domain and path from the response that produced them, so
        that is a merge of two stores rather than a caller assignment.
        """
        if value is None:
            self.remove_cookie_by_name(name, domain=kwargs.get("domain"),
                                       path=kwargs.get("path"))
            return None
        if isinstance(value, Morsel):
            cookie = morsel_to_cookie(value)
        else:
            cookie = create_cookie(name, value, **kwargs)
        # A caller assignment retires the scopes it covers; a server Set-Cookie
        # (which reaches set_cookie through extract_cookies) does not, because a
        # store merges rather than assigns. See set_cookie's docstring.
        self._retire_replaced(cookie)
        self.set_cookie(cookie)
        return cookie
    def iterkeys(self):
        for cookie in iter(self):
            yield cookie.name

    def keys(self):
        return list(self.iterkeys())

    def itervalues(self):
        for cookie in iter(self):
            yield cookie.value

    def values(self):
        return list(self.itervalues())

    def iteritems(self):
        for cookie in iter(self):
            yield cookie.name, cookie.value

    def items(self):
        return list(self.iteritems())

    def list_domains(self):
        domains = []
        for cookie in iter(self):
            if cookie.domain not in domains:
                domains.append(cookie.domain)
        return domains

    def list_paths(self):
        paths = []
        for cookie in iter(self):
            if cookie.path not in paths:
                paths.append(cookie.path)
        return paths

    def multiple_domains(self):
        domains = []
        for cookie in iter(self):
            if cookie.domain is not None and cookie.domain in domains:
                return True
            domains.append(cookie.domain)
        return False

    def get_dict(self, domain=None, path=None):
        result = {}
        for cookie in iter(self):
            if (domain is None or cookie.domain == domain) \
                    and (path is None or cookie.path == path):
                result[cookie.name] = cookie.value
        return result

    def __contains__(self, name):
        try:
            self.__getitem__(name)
            return True
        except KeyError:
            return False

    def __getitem__(self, name):
        return self._find_no_duplicates(name)

    def __setitem__(self, name, value):
        self.set(name, value)

    def __delitem__(self, name):
        if not self.remove_cookie_by_name(name):
            raise KeyError(name)

    def delete(self, name, domain=None, path=None):
        """curl_cffi spelling of ``__delitem__`` with domain/path filters."""
        self.remove_cookie_by_name(name, domain=domain, path=path)

    def remove_cookie_by_name(self, name, domain=None, path=None):
        removed = False
        for cookie in list(iter(self)):
            if cookie.name != name:
                continue
            if domain is not None and cookie.domain != domain:
                continue
            if path is not None and cookie.path != path:
                continue
            cookielib.CookieJar.clear(self, cookie.domain, cookie.path, cookie.name)
            self._delete_through(cookie.domain, cookie.path, cookie.name)
            removed = True
        if removed:
            self.revision += 1
        return removed

    def update(self, other=None, **kwargs):
        if isinstance(other, cookielib.CookieJar):
            for cookie in other:
                self.set_cookie(copy.copy(cookie))
        elif isinstance(other, Mapping):
            for name, value in other.items():
                self.set(name, value)
        elif other is not None:
            for name, value in other:
                self.set(name, value)
        for name, value in kwargs.items():
            self.set(name, value)

    def copy(self):
        clone = RequestsCookieJar(self._policy)
        clone.update(self)
        return clone

    def get_policy(self):
        return self._policy

    def __repr__(self):
        return "<%s[%s]>" % (type(self).__name__,
                             ", ".join("%s=%s" % item for item in self.items()))

    # -- lookup helpers -----------------------------------------------------
    def _find(self, name, domain=None, path=None):
        for cookie in iter(self):
            if cookie.name == name:
                if domain is None or cookie.domain == domain:
                    if path is None or cookie.path == path:
                        return cookie.value
        raise KeyError("name=%r, domain=%r, path=%r" % (name, domain, path))

    def _find_no_duplicates(self, name, domain=None, path=None):
        found = None
        for cookie in iter(self):
            if cookie.name != name:
                continue
            if domain is not None and cookie.domain != domain:
                continue
            if path is not None and cookie.path != path:
                continue
            if found is not None:
                raise CookieConflictError(
                    "There are multiple cookies with name %r" % (name,))
            found = cookie.value
        if found is None:
            raise KeyError("name=%r, domain=%r, path=%r" % (name, domain, path))
        return found

    def __getstate__(self):
        state = self.__dict__.copy()
        state.pop("_cookies_lock", None)
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        if "_cookies_lock" not in self.__dict__:
            self._cookies_lock = threading.RLock()

    # -- Core bridge --------------------------------------------------------
    def cookie_header(self, url, headers=None):
        """Returns the ``Cookie`` header value this jar would send for *url*."""
        request = MockRequest(url, headers)
        self.add_cookie_header(request)
        return request.get_new_headers().get("Cookie")

    def absorb_set_cookie(self, url, lines, mirror=True):
        """Records ``Set-Cookie`` lines observed for *url*; returns a fresh jar.

        Returns the cookies this response *set*, which is what
        ``Response.cookies`` exposes. Deletion is recorded on the jar itself
        rather than on that throwaway jar: ``cookielib`` implements
        ``Max-Age=0`` and a past ``Expires`` by calling ``clear(domain, path,
        name)`` on the jar it is extracting into, so extracting into a temporary
        jar deleted the cookie from a jar nobody kept and left the live entry in
        place. Measured: a server that set ``sid=ABC`` and then deleted it with
        ``Set-Cookie: sid=; Max-Age=0`` left ``{'sid': 'ABC'}`` in the session.
        """
        received = RequestsCookieJar()
        if not lines:
            return received
        request = MockRequest(url)
        response = MockResponse(lines)
        # Extract into ``self`` so an expiring cookie really deletes the stored
        # one. ``extract_cookies`` only ever sets or clears entries; it never
        # touches the rest of the jar.
        self.extract_cookies(response, request)
        if not mirror:
            # The caller is the session jar and wants only the extraction side
            # effect. Building the throwaway mirror costs a second full
            # ``extract_cookies`` -- measured 47 us on a plain jar and 270-500 us
            # when the jar has a write-through sink, which made a Set-Cookie
            # response three times slower than one without. Nothing reads that
            # second jar on this path: ``Response.cookies`` is filled by the
            # response jar own call.
            return received
        # The same lines go into a second, throwaway jar for
        # ``Response.cookies``. A deletion correctly leaves that jar empty, so
        # ``response.cookies`` reports what this response set, not what the
        # session holds.
        received.extract_cookies(response, request)
        return received


def cookiejar_from_dict(cookie_dict, cookiejar=None, overwrite=True):
    if cookiejar is None:
        cookiejar = RequestsCookieJar()
    if cookie_dict is not None:
        names = cookiejar.keys() if not overwrite else ()
        for name, value in cookie_dict.items():
            if name not in names:
                cookiejar.set_cookie(create_cookie(name, value))
    return cookiejar


def dict_from_cookiejar(cookiejar):
    return {cookie.name: cookie.value for cookie in cookiejar}


def add_dict_to_cookiejar(cookiejar, cookie_dict):
    return cookiejar_from_dict(cookie_dict, cookiejar=cookiejar)


def merge_cookies(cookiejar, cookies):
    """Merges *cookies* into *cookiejar* and returns it, as requests does."""
    if not isinstance(cookiejar, cookielib.CookieJar):
        raise ValueError("You can only merge into CookieJar")
    if isinstance(cookies, dict):
        cookiejar = cookiejar_from_dict(cookies, cookiejar=cookiejar, overwrite=False)
    elif isinstance(cookies, cookielib.CookieJar):
        for cookie in cookies:
            try:
                cookiejar.set_cookie(copy.copy(cookie))
            except AttributeError:
                pass
    return cookiejar


# Public aliases: `CookieJar` is this package's historical name and `Cookies` is
# the curl_cffi one.
CookieJar = RequestsCookieJar
Cookies = RequestsCookieJar
