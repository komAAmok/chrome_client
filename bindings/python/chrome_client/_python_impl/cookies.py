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
means rebuilding the Engine.  ``Session`` does that automatically; the jar's job
is just to record that a mutation happened (``revision``).
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


def morsel_to_cookie(morsel):
    """Converts a ``http.cookies.Morsel`` to a ``cookielib.Cookie``."""
    expires = None
    if morsel["max-age"]:
        try:
            expires = int(time.time() + int(morsel["max-age"]))
        except ValueError:
            raise TypeError("max-age: %s must be integer" % morsel["max-age"])
    elif morsel["expires"]:
        expires = calendar.timegm(
            time.strptime(morsel["expires"], "%a, %d-%b-%Y %H:%M:%S GMT")
        )
    return create_cookie(
        comment=morsel["comment"] or None, comment_url=bool(morsel["comment"]),
        discard=False, domain=morsel["domain"], expires=expires,
        name=morsel.key, path=morsel["path"], port=None,
        rest={"HttpOnly": morsel["httponly"]}, rfc2109=False,
        secure=bool(morsel["secure"]), value=morsel.value, version=morsel["version"] or 0,
    )


class RequestsCookieJar(cookielib.CookieJar, MutableMapping):
    """``cookielib.CookieJar`` with the dict interface requests exposes.

    ``revision`` increments on every mutation.  ``Session`` snapshots it after
    mirroring server cookies, so a later change means the *caller* edited the jar
    and the Core's own cookie store has to be discarded for the edit to take
    effect.
    """

    def __init__(self, policy=None):
        cookielib.CookieJar.__init__(self, policy)
        self.revision = 0

    # -- mutation bookkeeping ------------------------------------------------
    def set_cookie(self, cookie, *args, **kwargs):
        if hasattr(cookie.value, "startswith") and cookie.value.startswith('"') \
                and cookie.value.endswith('"'):
            cookie.value = cookie.value.replace('\\"', "").strip('"')
        self.revision += 1
        return cookielib.CookieJar.set_cookie(self, cookie, *args, **kwargs)

    def clear(self, domain=None, path=None, name=None):
        self.revision += 1
        if domain is None and path is None and name is None:
            return cookielib.CookieJar.clear(self)
        return cookielib.CookieJar.clear(self, domain, path, name)

    def clear_expired_cookies(self):
        # Deliberately not a revision bump: `cookielib.add_cookie_header` calls
        # this on every read, and expiry is not a caller mutation.
        return cookielib.CookieJar.clear_expired_cookies(self)

    def clear_session_cookies(self):
        self.revision += 1
        return cookielib.CookieJar.clear_session_cookies(self)

    # -- requests dict interface --------------------------------------------
    def get(self, name, default=None, domain=None, path=None):
        try:
            return self._find_no_duplicates(name, domain, path)
        except KeyError:
            return default

    def set(self, name, value, **kwargs):
        """Sets a cookie; ``value=None`` removes it, matching requests."""
        if value is None:
            self.remove_cookie_by_name(name, domain=kwargs.get("domain"),
                                       path=kwargs.get("path"))
            return None
        if isinstance(value, Morsel):
            cookie = morsel_to_cookie(value)
        else:
            cookie = create_cookie(name, value, **kwargs)
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

    def absorb_set_cookie(self, url, lines):
        """Records ``Set-Cookie`` lines observed for *url*; returns a fresh jar."""
        received = RequestsCookieJar()
        if not lines:
            return received
        request = MockRequest(url)
        response = MockResponse(lines)
        received.extract_cookies(response, request)
        for cookie in received:
            self.set_cookie(copy.copy(cookie))
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
