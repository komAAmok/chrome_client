"""Requests-style cookie storage with RFC 6265 domain, path and expiry rules."""

import calendar
import threading
import time
from collections.abc import Mapping
from datetime import timezone
from email.utils import parsedate_to_datetime
from http.cookies import CookieError, SimpleCookie
from typing import Dict, Iterator, Optional, Tuple, Union
from urllib.parse import urlparse

from ._utils import domain_matches as _domain_matches, normalize_cookie_domain


def _default_path(request_path: str) -> str:
    """RFC 6265 default-path algorithm."""
    if not request_path or not request_path.startswith('/'):
        return '/'
    if request_path.count('/') <= 1:
        return '/'
    return request_path.rsplit('/', 1)[0] or '/'


def _path_matches(cookie_path: str, request_path: str) -> bool:
    request_path = request_path or '/'
    if request_path == cookie_path:
        return True
    return request_path.startswith(cookie_path) and (
        cookie_path.endswith('/') or
        (len(request_path) > len(cookie_path) and request_path[len(cookie_path)] == '/')
    )


def _parse_expires(value: str) -> Optional[float]:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return calendar.timegm(parsed.utctimetuple()) + parsed.microsecond / 1000000.0
    except (TypeError, ValueError, OverflowError):
        return None


class Cookie:
    """One HTTP cookie."""

    def __init__(self, name: str, value: str, domain: str = '', path: str = '/',
                 seq: int = 0, expires: Optional[float] = None,
                 host_only: bool = False, secure: bool = False,
                 creation_seq: Optional[int] = None):
        self.name = name
        self.value = value
        self.domain = normalize_cookie_domain(domain)
        self.path = path if path and path.startswith('/') else '/'
        self.seq = seq
        self.creation_seq = seq if creation_seq is None else creation_seq
        self.expires = expires
        self.host_only = host_only
        self.secure = secure

    def is_expired(self, now: Optional[float] = None) -> bool:
        return self.expires is not None and self.expires <= (
            time.time() if now is None else now
        )

    def matches(self, domain: str, path: str = '/', secure: bool = False) -> bool:
        domain = normalize_cookie_domain(domain)
        domain_ok = not self.domain or (
            domain == self.domain if self.host_only else (
                domain == self.domain or _domain_matches(self.domain, domain)
            )
        )
        return (
            not self.is_expired() and domain_ok and
            _path_matches(self.path, path) and
            (not self.secure or secure)
        )

    def __repr__(self):
        return '<Cookie %s=%s for %s%s>' % (
            self.name, self.value, self.domain, self.path
        )

    def __str__(self):
        return '%s=%s' % (self.name, self.value)


class CookieJar:
    """Cookie jar keyed by ``(domain, path, name)``."""

    def __init__(self, default_domain: Optional[str] = None):
        self._cookies: Dict[Tuple[str, str, str], Cookie] = {}
        self._default_domain = normalize_cookie_domain(default_domain or '')
        self._seq_counter = 0
        # Cookie updates happen from response callbacks while callers may
        # update a shared Session from another worker.  Keep each operation
        # atomic; RLock also covers the existing method-to-method calls.
        self._lock = threading.RLock()

    def _next_seq(self) -> int:
        self._seq_counter += 1
        return self._seq_counter

    def _purge_expired(self, now: Optional[float] = None) -> None:
        with self._lock:
            now = time.time() if now is None else now
            for key, cookie in list(self._cookies.items()):
                if cookie.is_expired(now):
                    del self._cookies[key]

    @property
    def default_domain(self) -> str:
        with self._lock:
            return self._default_domain

    def set_default_domain(self, domain: Optional[str]) -> None:
        with self._lock:
            self._default_domain = normalize_cookie_domain(domain or '')

    def _set_default_domain_if_empty(self, domain: Optional[str]) -> None:
        """Set the auto-detected domain once, atomically."""
        with self._lock:
            if not self._default_domain:
                self._default_domain = normalize_cookie_domain(domain or '')

    def set(self, name: str, value: str, domain: Optional[str] = None,
            path: str = '/', expires: Optional[float] = None,
            max_age: Optional[int] = None, host_only: Optional[bool] = None,
            secure: bool = False) -> None:
        with self._lock:
            if not isinstance(name, str) or not isinstance(value, str):
                raise TypeError('cookie name and value must be strings')
            effective_domain = normalize_cookie_domain(domain or self._default_domain)
            effective_path = path if path and path.startswith('/') else '/'
            if max_age is not None:
                try:
                    max_age = int(max_age)
                except (TypeError, ValueError):
                    raise ValueError('max_age must be an integer')
                expires = time.time() + max_age
            key = (effective_domain, effective_path, name)
            if expires is not None and expires <= time.time():
                self._cookies.pop(key, None)
                return
            if host_only is None:
                host_only = bool(effective_domain) and not bool(domain)
            seq = self._next_seq()
            previous = self._cookies.get(key)
            self._cookies[key] = Cookie(
                name, value, effective_domain, effective_path,
                seq=seq, expires=expires,
                host_only=host_only, secure=secure,
                creation_seq=previous.creation_seq if previous else seq,
            )

    set_cookie = set

    def update_from_set_cookie(self, values, request_url: str) -> None:
        """Apply Set-Cookie values using RFC domain/path/expiry rules."""
        with self._lock:
            parsed_url = urlparse(request_url)
            request_domain = normalize_cookie_domain(parsed_url.hostname or '')
            request_path = parsed_url.path or '/'
            now = time.time()

            for header in values:
                parts = header.split(';')
                name, separator, value = parts[0].strip().partition('=')
                if not separator or not name:
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] == '"':
                    quoted = SimpleCookie()
                    try:
                        quoted.load('__cookie=' + value)
                        value = quoted['__cookie'].value
                    except (CookieError, KeyError):
                        value = value[1:-1]
                attributes = {}
                for attribute in parts[1:]:
                    key, separator, attribute_value = attribute.strip().partition('=')
                    if key:
                        attributes[key.lower()] = (
                            attribute_value.strip().strip('"') if separator else True
                        )
                domain_value = attributes.get('domain', '')
                domain_attr = normalize_cookie_domain(
                    domain_value if isinstance(domain_value, str) else ''
                )
                if domain_attr and not (
                    request_domain == domain_attr or
                    _domain_matches(domain_attr, request_domain)
                ):
                    continue
                domain = domain_attr or request_domain
                host_only = not bool(domain_attr)
                path_value = attributes.get('path', '')
                path_attr = path_value if isinstance(path_value, str) else ''
                path = path_attr if path_attr.startswith('/') else _default_path(request_path)

                expires = None
                max_age_value = attributes.get('max-age', '')
                max_age = max_age_value if isinstance(max_age_value, str) else ''
                expires_value = attributes.get('expires', '')
                expires_header = expires_value if isinstance(expires_value, str) else ''
                if max_age:
                    try:
                        expires = now + int(max_age)
                    except (TypeError, ValueError):
                        expires = _parse_expires(expires_header)
                else:
                    expires = _parse_expires(expires_header)

                self.set(
                    name, value, domain=domain, path=path,
                    expires=expires, host_only=host_only,
                    secure=bool(attributes.get('secure', False)),
                )

    def cookies_for_request(self, url: str) -> list:
        with self._lock:
            self._purge_expired()
            parsed = urlparse(url)
            domain = normalize_cookie_domain(parsed.hostname or '')
            path = parsed.path or '/'
            secure = parsed.scheme.lower() in ('https', 'wss')
            matches = [
                cookie for cookie in self._cookies.values()
                if cookie.matches(domain, path, secure)
            ]
            # RFC 6265: longer paths first, then older creation time.
            matches.sort(key=lambda cookie: (-len(cookie.path), cookie.creation_seq))
            return matches

    def get(self, name: str, default: Optional[str] = None,
            domain: Optional[str] = None, path: Optional[str] = None) -> Optional[str]:
        with self._lock:
            self._purge_expired()
            candidates = [cookie for cookie in self._cookies.values() if cookie.name == name]
            if domain is not None:
                domain = normalize_cookie_domain(domain)
                candidates = [
                    cookie for cookie in candidates
                    if not cookie.domain or domain == cookie.domain or (
                        not cookie.host_only and _domain_matches(cookie.domain, domain)
                    )
                ]
            if path is not None:
                candidates = [cookie for cookie in candidates if _path_matches(cookie.path, path)]
            if not candidates:
                return default
            candidates.sort(key=lambda cookie: (len(cookie.path), cookie.seq))
            return candidates[-1].value

    def get_dict(self, domain: Optional[str] = None,
                 path: Optional[str] = None) -> Dict[str, str]:
        with self._lock:
            self._purge_expired()
            cookies = list(self._cookies.values())
            if domain is not None:
                domain = normalize_cookie_domain(domain)
                cookies = [
                    cookie for cookie in cookies
                    if not cookie.domain or domain == cookie.domain or (
                        not cookie.host_only and _domain_matches(cookie.domain, domain)
                    )
                ]
            if path is not None:
                cookies = [cookie for cookie in cookies if _path_matches(cookie.path, path)]
            cookies.sort(key=lambda cookie: (len(cookie.path), cookie.seq))
            return {cookie.name: cookie.value for cookie in cookies}

    def update(self, cookies: Union[Mapping, 'CookieJar'],
               domain: Optional[str] = None) -> None:
        # Snapshot the source before taking the destination lock.  This keeps
        # concurrent ``a.update(b)``/``b.update(a)`` calls from lock inversion.
        source = tuple(cookies.iter_cookies()) if isinstance(cookies, CookieJar) and cookies is not self else None
        with self._lock:
            if isinstance(cookies, CookieJar):
                if cookies is self:
                    return
                for cookie in source:
                    self.set(
                        cookie.name, cookie.value, cookie.domain, cookie.path,
                        expires=cookie.expires, host_only=cookie.host_only,
                        secure=cookie.secure,
                    )
            elif isinstance(cookies, Mapping):
                for name, value in cookies.items():
                    self.set(name, value, domain)
            else:
                raise TypeError('cookies must be a mapping or CookieJar')

    def clear(self, domain: Optional[str] = None,
              path: Optional[str] = None) -> None:
        with self._lock:
            if domain is None and path is None:
                self._cookies.clear()
                return
            domain = normalize_cookie_domain(domain or '')
            for key, cookie in list(self._cookies.items()):
                if (domain and cookie.domain != domain) or (path is not None and cookie.path != path):
                    continue
                del self._cookies[key]

    def clear_expired_cookies(self) -> None:
        self._purge_expired()

    def clear_session_cookies(self) -> None:
        with self._lock:
            for key, cookie in list(self._cookies.items()):
                if cookie.expires is None:
                    del self._cookies[key]

    def delete(self, name: Optional[str] = None, domain: Optional[str] = None,
               path: Optional[str] = None) -> None:
        with self._lock:
            if name is None and domain is None and path is None:
                raise ValueError("at least one of 'name', 'domain' or 'path' is required")
            domain = normalize_cookie_domain(domain) if domain is not None else None
            for key, cookie in list(self._cookies.items()):
                if name is not None and cookie.name != name:
                    continue
                if domain is not None and cookie.domain != domain:
                    continue
                if path is not None and cookie.path != path:
                    continue
                del self._cookies[key]

    def remove(self, name: str, domain: Optional[str] = None,
               path: Optional[str] = None) -> None:
        self.delete(name=name, domain=domain, path=path)

    def copy(self) -> 'CookieJar':
        with self._lock:
            jar = CookieJar(self._default_domain or None)
            jar.update(self)
            return jar

    def _deduped(self) -> Dict[str, Cookie]:
        with self._lock:
            self._purge_expired()
            latest = {}
            for cookie in self._cookies.values():
                previous = latest.get(cookie.name)
                if previous is None or cookie.seq > previous.seq:
                    latest[cookie.name] = cookie
            return latest

    def items(self) -> Iterator[Tuple[str, str]]:
        for name, cookie in self._deduped().items():
            yield name, cookie.value

    def keys(self) -> Iterator[str]:
        return iter(self._deduped().keys())

    def values(self) -> Iterator[str]:
        for cookie in self._deduped().values():
            yield cookie.value

    def list_domains(self) -> list:
        with self._lock:
            self._purge_expired()
            return list(dict.fromkeys(cookie.domain for cookie in self._cookies.values()))

    def items_for_domain(self, domain: str, path: str = '/') -> Iterator[Tuple[str, str]]:
        # Compatibility helper; use a synthetic HTTPS URL so Secure cookies remain visible.
        host = '[%s]' % domain if ':' in domain and not domain.startswith('[') else domain
        for cookie in self.cookies_for_request('https://%s%s' % (host, path)):
            yield cookie.name, cookie.value

    def iter_cookies(self) -> Iterator[Cookie]:
        with self._lock:
            self._purge_expired()
            # Return a snapshot; yielding a live dict view would release the
            # lock between iterations and race a concurrent Set-Cookie update.
            return iter(tuple(self._cookies.values()))

    def __setitem__(self, name: str, value: str) -> None:
        self.set(name, value)

    def __getitem__(self, name: str) -> str:
        value = self.get(name)
        if value is None:
            raise KeyError(name)
        return value

    def __contains__(self, name: str) -> bool:
        return self.get(name) is not None

    def __bool__(self) -> bool:
        return len(self) > 0

    def __iter__(self) -> Iterator[Cookie]:
        return self.iter_cookies()

    def __len__(self) -> int:
        with self._lock:
            self._purge_expired()
            return len(self._cookies)

    def __repr__(self):
        cookies = list(self.iter_cookies())
        return '<CookieJar[%s]>' % ', '.join(repr(cookie) for cookie in cookies)

    __str__ = __repr__
