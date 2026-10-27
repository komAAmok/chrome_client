"""Sessions.

One ``Session`` owns one Chromium engine per distinct configuration it is asked
for, and keeps them (see ``engine.EngineCache``).  That is what makes a session a
session: the engine holds the connection pool, the TLS session cache, and the
cookie store, so reusing it is what preserves server-side state across requests.

Cookie handling is a two-store problem described in ``cookies.py``.  The short
version, as implemented in ``_outgoing_cookie_header``: the Core's store wins
whenever it holds a cookie for the target URL, so the facade sends a ``Cookie``
header only when the store would send nothing, and rebuilds the engine (dropping
its store) when the caller's jar and the store disagree.
"""

import asyncio
import math
import os
import random
import time
import warnings
from collections import deque
from urllib.parse import unquote, urljoin, urlsplit

try:
    from collections.abc import Mapping
except ImportError:  # Python 3.6
    from collections import Mapping

from . import multipart as _multipart
from .auth import build_auth
from .cookies import RequestsCookieJar, cookiejar_from_dict, merge_cookies
from .engine import DEFAULT_MAX_ENGINES, EngineConfig, EngineCache
from .exceptions import (InvalidProxyURL, InvalidURL, ProxyError,
                         RequestException, ResponseTooLarge, SessionClosed,
                         Timeout, TooManyRedirects, UnsupportedFeature,
                         map_native_error)
from .impersonate import (default_priority, normalize_http_version,
                          normalize_impersonate, reject_fingerprint_overrides,
                          validate_extra_fp)
from .models import (PreparedRequest, Request, Response, AsyncResponse,
                     _negotiated_http_version, parse_raw_headers,
                     reason_from_status_line)
from .structures import CaseInsensitiveDict, Headers
from .utils import (default_headers as _default_headers, get_netrc_auth,
                    requote_uri, resolve_proxies, should_bypass_proxies,
                    to_key_val_list)

#: Events drained from the Core per event-loop wakeup.  Batching is what keeps a
#: large download from costing one loop round trip per Core chunk.
ASYNC_POLL_BATCH = 64

#: Hops Chromium follows inside one ``URLRequest``, from
#: ``net::URLRequest::kMaxRedirects`` (net/url_request/url_request.h:110:
#: "If request's redirect count is twenty, return a network error"). The Core
#: installs no redirect limit of its own -- core/source/request.cc leaves
#: Chromium's ``redirect_limit_`` at its default -- so this is the ceiling of the
#: native follow path, and the facade must not claim more. A ``max_redirects``
#: at or above it raises UnsupportedFeature instead of letting Chromium fail
#: at hop 21 with a limit the caller never set.
#:
#: ``models.DEFAULT_REDIRECT_LIMIT`` (30) is deliberately NOT used here. It is
#: requests' own default and a public name callers import, so it stays in
#: ``models``; but adopting it as this session's default would promise 30 hops
#: from a build that stops at 20. ``max_redirects=None`` means "Chromium's
#: ceiling", which is the only default this build can actually honour.
CHROMIUM_MAX_REDIRECTS = 20

#: Bytes buffered per streaming async response before the reader is throttled.
STREAM_BUFFER_LIMIT = 1024 * 1024


#: Chromium derives the Referer from `URLRequest::SetReferrer`, not from the
#: extra headers. The Core routes a caller-supplied Referer there
#: (core/source/request.cc), so the value reaches the wire through the same
#: path a real Chrome uses.


#: Headers `mn_websocket_create` rejects (`IsForbiddenWebSocketHeader` in
#: core/source/minicronet.cc): Chromium derives each of them itself, and the
#: User-Agent in particular is part of the impersonated fingerprint.
_FORBIDDEN_WS_HEADERS = ("Connection", "Host", "Origin", "User-Agent", "Upgrade",
                         "Sec-WebSocket-*")


def _is_forbidden_ws_header(name):
    lowered = str(name).lower()
    return lowered in ("connection", "host", "origin", "user-agent", "upgrade") \
        or lowered.startswith("sec-websocket-")


def _websocket_http_url(url):
    """Maps a ``ws(s)://`` URL to its HTTP equivalent for cookie matching."""
    if url.startswith("wss://"):
        return "https://" + url[len("wss://"):]
    if url.startswith("ws://"):
        return "http://" + url[len("ws://"):]
    return url


def _default_websocket_origin(url):
    """Derives the handshake ``Origin`` when the caller supplies none.

    The Core rejects an empty or opaque origin, and there is no page here to
    inherit one from, so the closest honest default is the WebSocket URL's own
    origin -- what a same-origin page connection looks like on the wire.
    """
    parts = urlsplit(_websocket_http_url(url))
    if not parts.scheme or not parts.netloc:
        raise InvalidURL("Invalid WebSocket URL %r" % (url,))
    return "%s://%s" % (parts.scheme, parts.netloc)


def _now():
    return time.monotonic()


def _elapsed(started):
    import datetime
    return datetime.timedelta(seconds=max(0.0, _now() - started))


def _check_timeout(value):
    """Validates one deadline in seconds and returns the value to send.

    ``timeout=0`` used to pass straight through. The Core installs its deadline
    timer only for a positive millisecond count (``core/source/request.cc``:
    ``if (timeout_ms_ > 0)``), so zero silently meant "no deadline at all" and
    the call ran until the server answered -- the opposite of what the caller
    asked for. requests raises ``ValueError`` for the same input, so this does
    too. A sub-millisecond deadline is rounded up to 1ms because the ABI carries
    whole milliseconds: truncating 0.0005s to 0 would recreate the same
    no-timeout hole.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("timeout must be a number of seconds, not %r" % (value,))
    if not math.isfinite(number):
        raise ValueError("timeout must be finite, not %r" % (value,))
    if number <= 0:
        raise ValueError(
            "timeout=%r is not a deadline: pass a positive number of seconds, "
            "or None to wait forever" % (value,))
    return number if number >= 0.001 else 0.001


def _split_timeout(timeout):
    """Accepts requests' ``(connect, read)`` tuple.

    ABI v8 carries one deadline, so the larger of the two is used and the
    distinction is lost; that is better than rejecting a valid requests call.
    """
    if timeout is None:
        return None
    if isinstance(timeout, (tuple, list)):
        if len(timeout) != 2:
            raise ValueError("timeout tuple must be (connect, read)")
        values = [value for value in timeout if value is not None]
        return max(_check_timeout(value) for value in values) if values else None
    return _check_timeout(timeout)


def _cancel_quietly(native):
    """Cancels a Core request, ignoring "already finished".

    ``Request::Cancel`` answers ``MN_ERROR_INVALID_STATE`` once the
    request is complete (``core/source/request.cc``:142), which the pyo3 layer
    turns into a ``RuntimeError``. Cancelling during teardown must not
    replace the caller's own exception with that bookkeeping failure.
    """
    try:
        native.cancel()
    except RuntimeError:
        pass

def _validate_max_response_bytes(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("max_response_bytes must be a non-negative integer or None")
    return value


def _validate_max_redirects(value):
    """Rejects a cap the native follow path cannot honour.

    Chromium stops at CHROMIUM_MAX_REDIRECTS hops inside one URLRequest, so a
    larger cap is unreachable rather than merely unsupported: the request dies
    at hop 21 with ERR_TOO_MANY_REDIRECTS no matter what the caller asked for.
    Returning the value anyway -- which is what the facade did -- turned an
    explicit "follow up to 30" into an unexplained failure three hops early.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("max_redirects must be a non-negative integer or None")
    if value >= CHROMIUM_MAX_REDIRECTS:
        raise UnsupportedFeature(
            "max_redirects=%d cannot be honoured: Chromium stops after %d hops "
            "inside one URLRequest (net::URLRequest::kMaxRedirects), and this "
            "build has no way to raise that ceiling. Pass a value below %d."
            % (value, CHROMIUM_MAX_REDIRECTS, CHROMIUM_MAX_REDIRECTS))
    return value


#: Schemes Chromium's rule parser accepts in a proxy URI, from
#: ``GetSchemeFromUriScheme`` in net/base/proxy_string_util.cc.  These are
#: passed through untouched -- the Core parses them itself, and rewriting a form
#: Chromium already understands is how the facade would break it.
_PROXY_SCHEMES = ("http", "https", "socks", "socks4", "socks5")

#: Per-scheme rule keys accepted by ``MapUrlSchemeToProxyListNoFallback`` plus the
#: special ``socks`` key.  ``http=h:p;https=h:p`` is Chromium's own spelling for
#: "a different proxy per destination scheme", and it must not be rewritten:
#: ``ParseFromString`` splits on ``;`` and then on ``=``, so treating the whole
#: string as one URI produced a rule naming a host literally called "http".
_NATIVE_RULE_KEYS = ("http", "https", "ftp", "socks")

#: ``direct://`` is a real Chromium token meaning "no proxy".  It is only valid
#: as the sole entry of a list (``MultiProxyUrisToProxyChain`` rejects it next to
#: other servers), and it is why the facade must not demand a host.
_NATIVE_DIRECT = "direct://"

#: Pool spellings that are pure renames of a native scheme. ``socks5h`` differs
#: from ``socks5`` only in name: Chromium's SOCKS5 client always sends the
#: hostname to the proxy and lets it resolve (socks5_client_socket.cc), so the
#: "h" is already the behaviour. This is an extension, applied only because
#: Chromium rejects the spelling itself with -336.
_PROXY_SCHEME_ALIASES = {"socks5h": "socks5"}

#: Spellings that request behaviour the Core cannot provide. These fail closed
#: rather than being rewritten, because the difference is observable at the
#: proxy: Chromium resolves SOCKS4 hostnames locally (socks_client_socket.cc
#: DoResolveHost) and ABI v8 exposes no remote-resolve mode, so accepting
#: ``socks4a`` would silently send the caller's DNS out through this machine.
_PROXY_SCHEME_UNSUPPORTED = {
    "socks4a": "Chromium resolves SOCKS4 hostnames locally and the Core exposes "
               "no SOCKS4a (remote-resolve) mode; use socks5 or socks5h, which "
               "send the hostname to the proxy",
    "quic": "this build has no QUIC proxy support "
            "(ENABLE_QUIC_PROXY_SUPPORT is off), so Chromium rejects quic:// "
            "with ERR_NO_SUPPORTED_PROXIES",
}


def _native_uri_scheme(uri):
    """Returns the scheme prefix of a proxy URI, or ``None`` when absent.

    Mirrors ``ProxyUriToProxyServer``: a scheme exists only when ``://`` follows
    it, so ``h:p`` has none and Chromium applies the rule's default scheme.
    """
    marker = uri.find("://")
    if marker == -1:
        return None
    return uri[:marker].strip().lower()


def _is_native_rule_list(text):
    """True when *text* is a rule Chromium parses as more than one proxy URI.

    Detection follows ``ParseFromString``: it tokenizes on ``;`` and, when a
    token contains ``=``, treats it as ``<scheme>=<uri-list>``. A bare ``;`` or a
    ``,`` therefore signals the native multi-proxy forms, and ``direct://`` is a
    single token whose meaning (no proxy) needs no host.
    """
    if text.lower() == _NATIVE_DIRECT:
        return True
    if ";" in text:
        return True
    if "," in text:
        return True
    # "http=h:p" has an "=" before any "://"; a URI never does.
    equals = text.find("=")
    if equals != -1 and (text.find("://") == -1 or equals < text.find("://")):
        return True
    return False


def _validate_native_rule_list(text):
    """Rejects native rule syntaxes this build would fail on, otherwise no-op.

    Everything Chromium can parse is left byte-identical. Only the two shapes it
    cannot express are rejected, each with a message naming the problem instead
    of letting the Core answer with the opaque -336.
    """
    # A rule list made only of separators parses to an empty rule list, and an
    # empty ProxyRules means "direct" to Chromium. The Core rejects it instead
    # (non-empty input that yields no rule -> MN_ERROR_INVALID_ARGUMENT), so it
    # is reported here as an unusable proxy rule rather than as a URL error.
    if not any(part.strip() for part in text.split(";")):
        raise InvalidProxyURL(
            "proxy rule %r contains no rule; pass a proxy URL or leave the "
            "option unset for a direct connection" % (text,))

    for token in text.split(";"):
        token = token.strip()
        if not token:
            continue
        value = token
        if "=" in token:
            key, value = token.split("=", 1)
            key = key.strip().lower()
            if key not in _NATIVE_RULE_KEYS:
                raise InvalidProxyURL(
                    "proxy rule %r uses %r as a rule key; Chromium accepts %s"
                    % (text, key, ", ".join(_NATIVE_RULE_KEYS)))
        uris = [uri.strip() for uri in value.split(",") if uri.strip()]
        if not uris:
            continue
        if any(uri.lower() == _NATIVE_DIRECT for uri in uris) and len(uris) > 1:
            raise InvalidProxyURL(
                "proxy rule %r mixes direct:// with other servers; Chromium "
                "accepts direct:// only as the sole entry" % (text,))
        for uri in uris:
            if "@" in uri:
                raise InvalidProxyURL(
                    "proxy rule %r carries credentials inside a rule list; "
                    "Chromium rejects userinfo in a proxy rule, so pass the "
                    "credentials with proxy_auth= instead" % (text,))
            scheme = _native_uri_scheme(uri)
            if scheme is None or scheme == "direct":
                continue
            if scheme in _PROXY_SCHEME_UNSUPPORTED:
                raise UnsupportedFeature(_PROXY_SCHEME_UNSUPPORTED[scheme])
            if scheme not in _PROXY_SCHEMES:
                raise InvalidProxyURL(
                    "proxy rule %r uses scheme %r; Chromium accepts %s"
                    % (text, scheme, ", ".join(_PROXY_SCHEMES)))


def _split_authority(authority):
    """Splits ``[userinfo@]host[:port]`` into ``(host, userinfo, port_text)``.

    ``urlsplit`` is not used here because it silently drops a ``:0`` port and
    keeps the userinfo in the netloc, and both of those matter to Chromium: port
    0 is a real port to ``ParsePort``, and userinfo has to be moved into the
    Engine's separate credential fields. ``port_text`` is ``None`` when there is
    no colon at all, and ``""`` when the colon is present but nothing follows.
    """
    userinfo = None
    if "@" in authority:
        userinfo, authority = authority.rsplit("@", 1)
    if authority.startswith("["):
        # An IPv6 literal keeps its brackets and its colons are not a port.
        end = authority.find("]")
        if end == -1:
            return authority, userinfo, None
        host = authority[:end + 1]
        remainder = authority[end + 1:]
        if not remainder:
            return host, userinfo, None
        if remainder.startswith(":"):
            return host, userinfo, remainder[1:]
        # Anything else after "]" is junk; report it as trailing text.
        return host, userinfo, remainder
    if ":" in authority:
        host, port_text = authority.rsplit(":", 1)
        return host, userinfo, port_text
    return authority, userinfo, None


def normalize_proxy_url(proxy):
    """Normalises one proxy URL into a rule Chromium can parse.

    Chromium's rule parser is strict about the shape of a proxy rule and the
    facade used to hand it the caller's string untouched, so a pool string with
    a trailing newline, a comment, a path or a fragment parsed to an empty rule
    list and every request failed with ``ERR_NO_SUPPORTED_PROXIES`` (-336).

    Chromium's rules also cannot carry userinfo: ``ParseFromString`` rejects an
    ``@`` in the rule, so ``http://user:pass@host:port`` used to parse to an
    empty rule list as well.  The credentials therefore travel in the Engine's
    separate proxy_username / proxy_password fields, which is also how requests
    does it.

    Returns ``(rule, username, password)``.  ``None`` credentials mean the
    caller supplied none; the session-level ``proxy_auth`` then decides.
    """
    if not proxy or not isinstance(proxy, str):
        return proxy, None, None

    # Native Chromium rule syntax is passed through untouched. ParseFromString
    # already understands per-scheme lists (http=h:p;https=h:p), comma-separated
    # fallback lists and direct://; rewriting those is how the facade would
    # break a form the Core parses correctly on its own.
    rule = proxy.strip()
    if rule and _is_native_rule_list(rule):
        _validate_native_rule_list(rule)
        return rule, None, None

    # A value with no content at all means "no proxy", as an empty string does
    # in requests, so it is not an error. Leading/trailing whitespace is not
    # whitespace to Chromium's tokenizer: a newline inside the rule list splits
    # it, which is why the strip happens before any parsing.
    if not rule:
        return "", None, None

    scheme = None
    rest = rule
    if "://" in rule:
        scheme, rest = rule.split("://", 1)
        scheme = scheme.strip().lower()
        if scheme in _PROXY_SCHEME_UNSUPPORTED:
            raise UnsupportedFeature(_PROXY_SCHEME_UNSUPPORTED[scheme])
        scheme = _PROXY_SCHEME_ALIASES.get(scheme, scheme)
        if scheme not in _PROXY_SCHEMES:
            raise InvalidProxyURL(
                "unsupported proxy scheme %r; Chromium accepts %s"
                % (scheme, ", ".join(_PROXY_SCHEMES)))

    # Drop everything that is not authority.  A pool entry such as
    # "http://host:port/path#frag" would otherwise parse to no proxy at all.
    # A pool that returns "socks5://host:port" fed into f"http://{raw}" arrives
    # as "http://socks5://host:port". Splitting on the first "://" would leave a
    # host of "socks5", so the doubled scheme is refused instead of being
    # silently turned into a request to a machine named after a protocol.
    if "://" in rest:
        raise InvalidProxyURL(
            "proxy URL %r has two schemes; the pool entry probably already "
            "carries one, so do not prefix it again" % (proxy,))

    rest = rest.split("#", 1)[0].split("/", 1)[0]
    if "?" in rest:
        raise InvalidProxyURL(
            "proxy URL %r has a query string; a proxy rule is host:port only"
            % (proxy,))
    # Trailing text after the authority -- a pool comment, for instance -- would
    # otherwise be handed to urlsplit as part of the port and reported as an
    # "invalid port", which points the caller at the wrong part of the string.
    rest = rest.strip()

    host, userinfo, port_text = _split_authority(rest)
    if not host:
        raise InvalidProxyURL("proxy URL %r has no host" % (proxy,))
    if port_text is None:
        port = None
    elif port_text == "":
        # Chromium rejects a rule whose port is present but empty
        # (ProxySchemeHostAndPortToProxyServer), and silently reading it as the
        # scheme default would turn an input Chromium refuses into a request.
        raise InvalidProxyURL(
            "proxy URL %r ends with an empty port; drop the colon or give a port"
            % (proxy,))
    elif not port_text.isdigit():
        raise InvalidProxyURL(
            "proxy URL %r has trailing text after the authority; a proxy rule is "
            "host:port only" % (proxy,))
    else:
        port = int(port_text)
        if port > 65535:
            raise InvalidProxyURL(
                "proxy URL %r has an invalid port" % (proxy,))
    if ":" in host and not host.startswith("["):
        host = "[%s]" % host
    # Port 0 is a real value to Chromium (ParsePort returns 0, not the default).
    # Falsy-testing it here quietly turned ":0" into the scheme default.
    authority = "%s:%d" % (host, port) if port is not None else host

    username = password = None
    if userinfo is not None:
        name, separator, secret = userinfo.partition(":")
        username = unquote(name) if name else None
        password = unquote(secret) if separator else None
    return "%s://%s" % (scheme or "http", authority), username, password


def split_proxy_credentials(proxy):
    """Returns ``(rule, username, password)`` for *proxy*.

    Kept as the historical name; the parsing lives in
    :func:`normalize_proxy_url`, which also repairs the URL shapes Chromium
    rejects.
    """
    return normalize_proxy_url(proxy)


#: Proxy failures that say nothing about the origin: the proxy itself was
#: unreachable (-130) or refused the tunnel (-111). A rotating pool produces
#: both on healthy traffic, so they are retried once by default even when the
#: caller asked for no retry policy. Reaching the proxy is what failed, so the
#: origin never saw the request and re-sending it cannot duplicate an effect.
#: Excluded: -375 (ERR_TOO_MANY_RETRIES) means the proxy rejected credentials,
#: and retrying spends another 32 Chromium restarts to fail the same way.
RETRIABLE_PROXY_ERRORS = ("ERR_PROXY_CONNECTION_FAILED",
                          "ERR_TUNNEL_CONNECTION_FAILED")

#: Extra attempt granted to the errors above, but only while the effective
#: retry policy is inert (``count == 0``). A caller who configured a policy
#: keeps exactly that many attempts instead of getting one more on top, so
#: ``retry=2`` always means three attempts whichever way a request failed.
#: One attempt, not a loop: a pool whose endpoint is genuinely dead must still
#: fail the caller quickly rather than absorb the request for a long time.
DEFAULT_PROXY_RETRY_COUNT = 1


def proxy_from_proxies(url, proxies):
    """Selects a requests-style proxy mapping entry for *url*."""
    if proxies is None:
        return None
    if not hasattr(proxies, "items"):
        raise TypeError("proxies must be a mapping of schemes to proxy URLs")
    values = {}
    for key, value in proxies.items():
        key = str(key).lower()
        if value is not None and not isinstance(value, str):
            raise TypeError("proxy values must be strings or None")
        values[key] = value
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    proxy_scheme = {"ws": "http", "wss": "https"}.get(scheme, scheme)
    keys = []
    if parts.hostname:
        keys.extend((scheme + "://" + parts.hostname,
                     proxy_scheme + "://" + parts.hostname))
    keys.extend((scheme, proxy_scheme))
    if parts.hostname:
        keys.append("all://" + parts.hostname)
    keys.append("all")
    for key in keys:
        if key in values:
            return values[key]
    return None


# Kept as a private alias: the regression suite imports this name.
_proxy_from_proxies = proxy_from_proxies


def merge_setting(request_setting, session_setting, dict_class=CaseInsensitiveDict):
    """requests' merge rule: request wins, and a ``None`` value deletes a key."""
    if session_setting is None:
        return request_setting
    if request_setting is None:
        return session_setting
    if not (isinstance(session_setting, Mapping) and isinstance(request_setting, Mapping)):
        return request_setting
    merged = dict_class(to_key_val_list(session_setting))
    merged.update(to_key_val_list(request_setting))
    for key, value in request_setting.items():
        if value is None:
            merged.pop(key, None)
    return merged


def merge_hooks(request_hooks, session_hooks, dict_class=dict):
    if session_hooks is None or not session_hooks.get("response"):
        return request_hooks
    if request_hooks is None or not request_hooks.get("response"):
        return session_hooks
    return merge_setting(request_hooks, session_hooks, dict_class)


class RetryStrategy(object):
    """curl_cffi-shaped retry policy."""

    def __init__(self, count, delay=0.0, jitter=0.0, backoff="linear",
                 codes=(429, 500, 502, 503, 504)):
        if count < 0:
            raise ValueError("retry count must be >= 0")
        if delay < 0 or jitter < 0:
            raise ValueError("retry delay and jitter must be >= 0")
        if backoff not in ("linear", "exponential"):
            raise ValueError("backoff must be 'linear' or 'exponential'")
        self.count = int(count)
        self.delay = float(delay)
        self.jitter = float(jitter)
        self.backoff = backoff
        self.codes = frozenset(codes or ())

    @classmethod
    def coerce(cls, value):
        if value is None:
            return cls(0)
        if isinstance(value, RetryStrategy):
            return value
        return cls(int(value))

    def sleep_for(self, attempt):
        if self.backoff == "exponential":
            delay = self.delay * (2 ** max(0, attempt - 1))
        else:
            delay = self.delay * attempt
        if self.jitter:
            delay += random.uniform(0, self.jitter)
        return delay

    def should_retry_status(self, status_code):
        return status_code in self.codes


def _body_can_be_resent(prepared):
    """True when *prepared*'s body can be produced a second time.

    A transport failure never reached the origin, so replaying the request
    cannot duplicate an effect and the method does not matter -- but the body
    still has to exist. A streamed body has already been consumed from the
    caller's one-shot iterator, so a second attempt sends whatever is left of
    it. Measured before this guard: a two-chunk upload retried after a -130
    reached the origin as the single fragment ``PART2``, because the first
    attempt had already taken ``PART1`` from the generator.
    """
    return not prepared.stream_body


def _request_is_replayable(prepared):
    """True when re-sending *prepared* cannot duplicate an effect at the origin.

    Stricter than :func:`_body_can_be_resent` because it is also used for
    status-code retries: a 5xx is a response *from* the origin, so the first
    attempt was already delivered and only an idempotent method may be sent
    again. A streamed body is excluded either way.
    """
    if prepared.method.upper() not in ("GET", "HEAD", "OPTIONS", "TRACE", "PUT",
                                       "DELETE"):
        return False
    return _body_can_be_resent(prepared)


def _is_retriable_proxy_failure(error):
    """True when *error* is a proxy failure that never reached the origin."""
    if not isinstance(error, ProxyError):
        return False
    text = str(error)
    return any(name in text for name in RETRIABLE_PROXY_ERRORS)


class _SyncBodyReader(object):
    """Pull-based body reader with a limit check.

    ``__del__`` is a safety net, not the intended path: an abandoned
    ``stream=True`` response would otherwise leave the Core request alive with a
    paused body queue.
    """

    def __init__(self, request, limit):
        self._request = request
        self._limit = limit
        self._total = 0
        self._closed = False

    def __iter__(self):
        while not self._closed:
            try:
                chunk = self._request.next_body()
            except RuntimeError as error:
                self.close()
                raise map_native_error(error)
            if chunk is None:
                self.close()
                return
            self._total += len(chunk)
            if self._limit is not None and self._total > self._limit:
                self.close(cancel=True)
                raise ResponseTooLarge(
                    "response exceeded max_response_bytes=%d" % self._limit)
            yield bytes(chunk)

    def close(self, cancel=False):
        if self._closed:
            return
        self._closed = True
        if cancel:
            try:
                self._request.cancel()
            except RuntimeError:
                pass
        try:
            self._request.detach_callback()
        except RuntimeError:
            pass

    def __del__(self):
        try:
            self.close(cancel=True)
        except Exception:
            pass


class _AsyncBodyReader(object):
    def __init__(self, state, limit):
        self._state = state
        self._limit = limit
        self._total = 0
        self._closed = False

    @property
    def state(self):
        """The :class:`_AsyncState` this reader drains.

        Public so ``AsyncResponse._release_async_body`` can find the owning
        event loop to cancel from.
        """
        return self._state

    def __aiter__(self):
        return self

    async def __anext__(self):
        state = self._state
        while True:
            if state.chunks:
                chunk = state.chunks.popleft()
                state.buffered -= len(chunk)
                state.resume()
                self._total += len(chunk)
                if self._limit is not None and self._total > self._limit:
                    await self.aclose(cancel=True)
                    raise ResponseTooLarge(
                        "response exceeded max_response_bytes=%d" % self._limit)
                return chunk
            if state.error is not None:
                error = state.error
                await self.aclose()
                raise error
            if state.done or self._closed:
                await self.aclose()
                raise StopAsyncIteration
            try:
                await state.wait()
            except asyncio.CancelledError:
                await self.aclose(cancel=True)
                raise
            except Timeout:
                await self.aclose(cancel=True)
                raise

    async def aclose(self, cancel=False):
        self.release_without_loop(cancel=cancel)

    def release_without_loop(self, cancel=False):
        """Finishes the reader without needing a running event loop.

        ``aclose`` exists so callers can ``await`` the teardown, but every
        step it takes -- ``state.finish`` cancels the Core request, detaches
        the callback and clears the buffer -- is synchronous. A caller that
        must release from *outside* the loop (an abandoned response being
        closed from another thread, or a loop that already stopped) needs
        this form: scheduling a coroutine on a loop that will never run it
        leaves the native request alive and the socket open.
        """
        if self._closed:
            return
        self._closed = True
        self._state.finish(cancel=cancel)


class _AsyncState(object):
    """Per-request asyncio bridge.

    The Core wakes the loop with ``call_soon_threadsafe``; this drains up to
    ``ASYNC_POLL_BATCH`` events per wakeup instead of one, so a multi-megabyte
    body does not pay an event-loop round trip per Core chunk.  A deadline uses a
    single ``call_later`` timer rather than ``wait_for``, which would add a Task
    and a Future per request.
    """

    __slots__ = ("request", "loop", "future", "chunks", "buffered", "done", "error",
                 "status", "raw_headers", "body", "stream", "limit", "total",
                 "wake", "timer", "closed", "throttled", "hops", "follow")

    def __init__(self, request, loop, stream, limit, timeout, follow=True):
        self.request = request
        self.loop = loop
        self.future = loop.create_future()
        self.stream = stream
        self.limit = limit
        self.chunks = deque() if stream else None
        self.buffered = 0
        self.total = 0
        self.done = False
        self.error = None
        self.status = 0
        self.raw_headers = b""
        self.body = None if stream else bytearray()
        self.wake = asyncio.Event() if stream else None
        self.closed = False
        self.throttled = False
        self.hops = []
        self.follow = follow
        self.timer = loop.call_later(timeout, self._expire) if timeout else None

    # -- loop-thread callbacks ---------------------------------------------
    def drain_chunks(self):
        """Removes and returns every buffered chunk, in arrival order."""
        chunks = []
        while self.chunks:
            chunks.append(self.chunks.popleft())
        self.buffered = 0
        self.throttled = False
        return chunks

    def notify(self):
        if self.closed:
            return
        if self.stream and self.buffered >= STREAM_BUFFER_LIMIT:
            self.throttled = True
            return
        try:
            events = self.request.poll_events(ASYNC_POLL_BATCH)
        except RuntimeError as error:
            self._fail(map_native_error(error))
            return
        for kind, code, raw_headers, chunk, message in events:
            if kind == "body":
                self._on_body(chunk)
            elif kind == "response":
                self.status = code
                self.raw_headers = raw_headers
                if self.stream:
                    self._signal()
            elif kind == "redirect":
                self._on_redirect(code, raw_headers, chunk)
            elif kind == "done":
                self._on_done()
            elif kind == "error":
                self._fail(map_native_error(message or "request failed"))
            if self.closed:
                return
        if len(events) == ASYNC_POLL_BATCH:
            # The batch limit, not an empty queue, ended the drain, so nothing
            # else will wake us: re-arm explicitly.
            self.loop.call_soon(self.notify)

    def error_is_lazy(self):
        """True when the pending error belongs to the body consumer.

        Two failures can be recorded while the head is still being awaited,
        and they belong to different owners:

        * A deadline (``Timeout``) is a property of the request, so it must
          surface from ``get()`` -- a streaming caller waiting on headers
          should not be left asleep.
        * ``max_response_bytes`` is a property of the body, so it must surface
          while the caller is consuming, the same place the synchronous path
          reports it. Raising it from ``get()`` would deny the caller the
          streaming response it asked for.
        """
        return self.stream and isinstance(self.error, ResponseTooLarge)

    def _on_body(self, chunk):
        self.total += len(chunk)
        if self.limit is not None and self.total > self.limit:
            self._fail(ResponseTooLarge(
                "response exceeded max_response_bytes=%d" % self.limit), cancel=True)
            return
        if self.stream:
            self.chunks.append(bytes(chunk))
            self.buffered += len(chunk)
            self._signal()
        else:
            self.body.extend(chunk)

    def _on_redirect(self, status_code, raw_headers, new_url):
        if self.follow:
            self.hops.append((status_code, raw_headers, bytes(new_url).decode(
                "utf-8", "replace"), ""))
            return
        # Chromium defers the hop waiting for `follow_redirect`; the caller asked
        # not to follow, so the 3xx is the response.
        self.status = status_code
        self.raw_headers = raw_headers
        self.done = True
        self._cancel_timer()
        try:
            self.request.cancel()
        except RuntimeError:
            pass
        if not self.future.done():
            self.future.set_result(True)
        self._signal()

    def _on_done(self):
        self.done = True
        self._cancel_timer()
        if not self.stream and not self.future.done():
            self.future.set_result(True)
        self._signal()

    def _fail(self, error, cancel=False):
        if self.error is None:
            self.error = error
        self.done = True
        self._cancel_timer()
        if cancel:
            try:
                self.request.cancel()
            except RuntimeError:
                pass
        # A streaming consumer waits on `wake`, never on `future`; resolving the
        # future instead of signalling would leave that consumer asleep forever,
        # and would also leave an exception nobody retrieves.
        if not self.stream and not self.future.done():
            self.future.set_exception(error)
        self._signal()

    def _expire(self):
        self.timer = None
        self._fail(Timeout("request timed out"), cancel=True)

    def _signal(self):
        if self.wake is not None:
            self.wake.set()

    def _cancel_timer(self):
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None

    # -- consumer side ------------------------------------------------------
    def resume(self):
        if self.throttled and self.buffered < STREAM_BUFFER_LIMIT:
            self.throttled = False
            self.loop.call_soon(self.notify)

    async def wait(self):
        self.wake.clear()
        await self.wake.wait()

    def finish(self, cancel=False):
        if self.closed:
            return
        self.closed = True
        self._cancel_timer()
        if cancel:
            try:
                self.request.cancel()
            except RuntimeError:
                pass
        try:
            self.request.detach_callback()
        except RuntimeError:
            pass
        if self.chunks is not None:
            self.chunks.clear()
        self.buffered = 0
        self._signal()
        if not self.future.done():
            self.future.cancel()


class _StreamFlag(int):
    """``Session.stream`` as both the requests flag and curl_cffi's helper.

    requests documents ``Session.stream`` as a boolean; curl_cffi documents
    ``Session.stream(method, url)`` as a streaming context manager.  This is an
    ``int`` -- so it behaves as the flag in every conditional and comparison --
    that is also callable, so both spellings work.  Only ``is True`` / ``is False``
    identity checks would see the difference.
    """

    def __new__(cls, value, session):
        instance = int.__new__(cls, 1 if value else 0)
        instance._session = session
        return instance

    def __call__(self, method, url, **kwargs):
        return self._session._stream_context(method, url, **kwargs)

    def __repr__(self):
        return repr(bool(self))


class BaseSession(object):
    """Shared configuration, engine selection, and cookie policy."""

    @property
    def stream(self):
        return _StreamFlag(self._stream, self)

    @stream.setter
    def stream(self, value):
        self._stream = bool(value)

    def __init__(self, impersonate=None, proxy=None, proxies=None, proxy_auth=None,
                 verify=True, timeout=None, headers=None, cookies=None, params=None,
                 auth=None, cert=None, stream=False, hooks=None,
                 max_redirects=None, trust_env=True,
                 allow_redirects=True, max_response_bytes=None, base_url=None,
                 http_version=None, ja3=None, akamai=None, perk=None, extra_fp=None,
                 default_headers=True, default_encoding="utf-8",
                 discard_cookies=False, raise_for_status=False, retry=0, cache=True,
                 user_agent=None, accept_language=None, interface=None, doh_url=None,
                 max_recv_speed=0, curl_options=None, max_engines=DEFAULT_MAX_ENGINES,
                 response_class=None):
        """Configures defaults shared by requests made through this session.

        ``impersonate`` selects a pinned Chromium profile, ``http_version`` can
        pin ``v1``, ``v2`` or ``v3``, and ``proxy``/``proxies`` select the route.
        ``verify`` accepts a boolean or CA bundle path; ``timeout`` accepts one
        deadline or a ``(connect, read)`` pair. Request methods may override these
        defaults for one call. Compatibility-only options unsupported by ABI v8
        raise ``UnsupportedFeature`` instead of being silently ignored.
        """
        reject_fingerprint_overrides(ja3, akamai, perk)
        for name, value in (("cert", cert), ("interface", interface),
                            ("doh_url", doh_url), ("curl_options", curl_options)):
            if value:
                raise UnsupportedFeature(
                    "%s= is not supported: ABI v8 exposes no Core setting for it" % name)
        if max_recv_speed:
            raise UnsupportedFeature(
                "max_recv_speed= is not supported: Chromium owns transfer pacing")
        self.header_order, self.form_boundary = validate_extra_fp(extra_fp)

        self.impersonate = normalize_impersonate(impersonate)
        self.http_version = normalize_http_version(http_version)
        self.proxy = proxy
        self.proxies = dict(proxies) if proxies else {}
        self.proxy_auth = proxy_auth
        self.verify = verify
        self.cert = None
        self.timeout = timeout
        self.params = dict(params) if params else {}
        self.auth = build_auth(auth)
        self.stream = stream
        self.hooks = {"response": []}
        #: Cap on redirects, or None for Chromium's own ceiling
        #: (CHROMIUM_MAX_REDIRECTS). requests defaults this to 30, but this
        #: build cannot follow 30: Chromium stops at 20 inside one URLRequest,
        #: so accepting 30 would promise a limit that never holds. None means
        #: "no caller cap"; an explicit value at or above 20 raises from
        #: _validate_max_redirects rather than being clamped to 20.
        self.max_redirects = _validate_max_redirects(max_redirects)
        self.trust_env = trust_env
        self.allow_redirects = allow_redirects
        self.base_url = base_url
        self.default_encoding = default_encoding
        self.discard_cookies = discard_cookies
        self.raise_for_status = raise_for_status
        self.retry = RetryStrategy.coerce(retry)
        self.cache = cache
        self.user_agent = user_agent
        self.accept_language = accept_language
        self.response_class = response_class
        self.max_response_bytes = _validate_max_response_bytes(max_response_bytes)
        self.extra_fp = extra_fp

        self.headers = Headers(_default_headers() if default_headers else None)
        if headers:
            self.headers.update(headers)
        self.cookies = cookiejar_from_dict(cookies) \
            if not isinstance(cookies, RequestsCookieJar) else cookies
        for event, hook in (hooks or {}).items():
            self.hooks.setdefault(event, [])
            if callable(hook):
                self.hooks[event].append(hook)
            elif hook:
                self.hooks[event].extend(item for item in hook if callable(item))

        from .adapters import HTTPAdapter
        self.adapters = {}
        self.mount("https://", HTTPAdapter())
        self.mount("http://", HTTPAdapter())

        self._engines = EngineCache(max_engines)
        self._generation = 0
        #: Counter for the throwaway engines 'discard_cookies' needs. Kept apart
        #: from '_generation' so a discard request never becomes the store every
        #: later request uses.
        self._discard_serial = 0
        self._closed = False

    # -- requests Session surface ------------------------------------------
    def mount(self, prefix, adapter):
        self.adapters[prefix] = adapter
        for key in sorted((key for key in self.adapters if len(key) < len(prefix)),
                          key=len, reverse=True):
            self.adapters[key] = self.adapters.pop(key)

    def get_adapter(self, url):
        for prefix, adapter in sorted(self.adapters.items(), key=lambda item: -len(item[0])):
            if url.lower().startswith(prefix.lower()):
                return adapter
        from .exceptions import InvalidSchema
        raise InvalidSchema("No connection adapters were found for %r" % (url,))

    def merge_environment_settings(self, url, proxies, stream, verify, cert):
        if self.trust_env:
            environment_proxies = resolve_proxies(url, {}, trust_env=True)
            if environment_proxies:
                # ``proxies`` is optional here, and requests' own spelling of
                # this merge is a bare ``proxies.setdefault(...)``, so a caller
                # that passed None got "AttributeError: 'NoneType' object has
                # no attribute 'setdefault'" the moment the environment named
                # a proxy. Copy into a dict instead of mutating the caller's
                # mapping, and only when there is something to merge.
                merged = dict(proxies) if proxies else {}
                for key, value in environment_proxies.items():
                    merged.setdefault(key, value)
                proxies = merged
            if verify is True or verify is None:
                verify = os.environ.get("REQUESTS_CA_BUNDLE") \
                    or os.environ.get("CURL_CA_BUNDLE") or verify
        return {"proxies": merge_setting(proxies, self.proxies, dict_class=dict),
                "stream": merge_setting(stream, self.stream),
                "verify": merge_setting(verify, self.verify),
                "cert": merge_setting(cert, self.cert)}

    def prepare_request(self, request):
        """Merges session defaults into a ``Request`` and returns a ``PreparedRequest``."""
        cookies = request.cookies or {}
        if not isinstance(cookies, RequestsCookieJar):
            cookies = cookiejar_from_dict(cookies)
        merged_cookies = merge_cookies(merge_cookies(RequestsCookieJar(), self.cookies),
                                       cookies)
        auth = request.auth
        if auth is None and self.trust_env:
            auth = get_netrc_auth(request.url)
        prepared = PreparedRequest()
        prepared.header_order = getattr(request, "header_order", None) or self.header_order
        prepared.quote = getattr(request, "quote", None)
        prepared.prepare(
            method=request.method.upper() if request.method else "GET",
            url=self._absolute_url(request.url),
            files=request.files,
            data=request.data or None,
            json=request.json,
            headers=merge_setting(request.headers, self.headers, dict_class=Headers),
            params=merge_setting(request.params, self.params, dict_class=dict),
            auth=build_auth(auth) or self.auth,
            cookies=merged_cookies,
            hooks=merge_hooks(request.hooks, self.hooks),
        )
        return prepared

    def _absolute_url(self, url):
        if self.base_url and not urlsplit(str(url)).scheme:
            return urljoin(self.base_url, str(url))
        return url

    def close(self):
        self._closed = True
        self._engines.close()
        for adapter in self.adapters.values():
            try:
                adapter.close()
            except NotImplementedError:
                pass

    def upkeep(self):
        """curl_cffi parity hook. Chromium keeps idle sockets warm itself."""
        return 0

    def _ensure_open(self):
        if self._closed:
            raise SessionClosed("Session is closed")

    # -- engine selection ---------------------------------------------------
    def _engine_config(self, impersonate, proxy, verify, http_version, url=None,
                        request_proxy=None, request_proxies=None):
        # Credentials embedded in the proxy URL have to be moved into the
        # Engine's separate fields: Chromium's proxy rule parser rejects
        # userinfo, so leaving them inline made the rule list empty and every
        # request failed with ERR_NO_SUPPORTED_PROXIES before opening a socket.
        # The URL's own credentials win over the session-level proxy_auth.
        #
        # proxy_auth is session state, but an Engine is per-proxy, so it may only
        # be attached when this request actually uses the session's own proxy.
        # Attaching it unconditionally sent the session's credentials to whatever
        # proxy a proxied pool happened to select for one call.
        proxy, url_user, url_password = split_proxy_credentials(proxy)
        ca_pem = None
        if isinstance(verify, str):
            with open(verify, "rb") as handle:
                ca_pem = handle.read()
            verify_flag = True
        elif verify is None:
            verify_flag = bool(self.verify) if not isinstance(self.verify, str) else True
            if isinstance(self.verify, str):
                with open(self.verify, "rb") as handle:
                    ca_pem = handle.read()
        else:
            verify_flag = bool(verify)
        username, password = url_user, url_password
        if username is None and self.proxy_auth \
                and self._proxy_auth_applies(proxy, url, request_proxy,
                                            request_proxies):
            username, password = self.proxy_auth
        return EngineConfig(
            # Normalise here, not only in __init__: a per-call ``impersonate=``
            # reaches the Core through this one function, and without it
            # ``"CHROME_155"``/``"chrome-155"`` were accepted at construction but
            # rejected as ``ProfileUnsupported`` when passed per request.
            impersonate=normalize_impersonate(
                impersonate if impersonate is not None else self.impersonate),
            proxy=proxy,
            verify=verify_flag,
            ca_pem=ca_pem,
            user_agent=self.user_agent,
            accept_language=self.accept_language,
            proxy_username=username,
            proxy_password=password,
            http_version=http_version if http_version is not None else self.http_version,
            cache=self.cache,
            profile_namespace=None,
            generation=self._generation,
        )

    def _proxy_auth_applies(self, rule, url=None, request_proxy=None,
                            request_proxies=None):
        """True when *rule* is the proxy this session's ``proxy_auth`` belongs to.

        ``proxy_auth`` is configured once on the session, while an Engine -- and
        therefore a proxy -- is chosen per request. A rotating pool selects a
        different proxy on most calls, so treating ``proxy_auth`` as applicable
        everywhere sent the session's username and password to every proxy in the
        pool.

        The session names its proxy directly (``proxy=``) or through the
        ``proxies`` mapping, and a single call may override either, so all four
        spellings are candidates. When none of them names a proxy there is
        nothing to scope against and the request's own proxy is the only
        candidate, which is what makes the credentials apply there.
        """
        # Only the SESSION's own spellings are candidates. A per-request
        # proxy= / proxies= is what the request will use, not what the session
        # configured, so treating it as an owner of proxy_auth is exactly the
        # leak this method exists to prevent.
        candidates = []
        if self.proxy is not None:
            candidates.append(self.proxy)
        if self.proxies:
            mapping = dict(self.proxies)
            if request_proxies:
                # A per-request mapping overrides individual keys; the
                # session's remaining entries still name its proxy.
                mapping.update({key: value for key, value in request_proxies.items()
                                if key not in self.proxies})
            selected = proxy_from_proxies(url, mapping) if url is not None else None
            if selected is not None:
                candidates.append(selected)
        if not candidates:
            return True
        for candidate in candidates:
            try:
                configured, _, _ = normalize_proxy_url(candidate)
            except (InvalidProxyURL, UnsupportedFeature):
                # An unusable session proxy cannot be the owner of the
                # credentials; Engine construction reports it separately.
                continue
            if configured == rule:
                return True
        return False

    def _effective_proxy(self, url, proxy, proxies):
        """Picks the proxy for one request.

        Precedence, highest first: the per-call ``proxy=``, the per-call
        ``proxies=``, the session's ``proxy=``, the session's ``proxies``, then
        the environment. The session-level ``proxy=`` used to be stored and
        never read, so ``Session(proxy=...)`` -- the documented way to send a
        whole session through one proxy -- silently sent every request direct.
        """
        if proxy is not None:
            return proxy
        mapping = dict(self.proxies)
        if proxies:
            mapping.update(proxies)
        selected = proxy_from_proxies(url, mapping) if mapping else None
        if selected is None and self.proxy is not None:
            selected = self.proxy
        if selected is None and self.trust_env and not should_bypass_proxies(url):
            environment = resolve_proxies(url, {}, trust_env=True)
            selected = proxy_from_proxies(url, environment) if environment else None
        return selected

    def _slot(self, url, impersonate, proxy, proxies, verify, http_version):
        self._ensure_open()
        # The per-request proxy= / proxies= have to reach _engine_config too:
        # proxy_auth may only be attached to the proxy the session itself
        # configured, and a per-request mapping can select a different one.
        config = self._engine_config(
            impersonate, self._effective_proxy(url, proxy, proxies), verify,
            http_version, url, proxy, proxies)
        return self._engines.get(config)

    # -- cookie policy ------------------------------------------------------
    def _resolve_cookies(self, slot, prepared, discard):
        """Decides the outgoing ``Cookie`` header, rebuilding the engine if needed.

        Returns the ``EngineSlot`` to use, which differs from *slot* only when the
        Core's cookie store has to be discarded for a caller edit to be visible.
        """
        if discard:
            # Dropping the header is not enough: the Core's CookieMonster owns
            # the store and re-attaches every cookie it already holds, so the
            # documented "neither send nor record" only holds if the store is
            # discarded too. A fresh generation is a structurally identical
            # engine with an empty jar, and the mirror stays empty so the
            # response cookies are not absorbed into it either.
            prepared.headers.pop("Cookie", None)
            return self._one_shot_slot(slot.config)
        jar = prepared._cookies if prepared._cookies is not None else self.cookies
        desired = jar.cookie_header(prepared.url)
        store = slot.mirror.cookie_header(prepared.url)
        if store is None:
            if desired:
                prepared.headers["Cookie"] = desired
            return slot
        if desired == store:
            # The Core will emit exactly this line from its own store.
            prepared.headers.pop("Cookie", None)
            return slot
        # The store would override whatever is sent, so replace the store: a new
        # generation is a structurally identical engine with an empty jar.
        self._generation += 1
        replacement = self._engines.get(slot.config.replace(generation=self._generation))
        # The superseded slot is unreachable -- its mirror no longer matches the
        # jar, so nothing selects it again -- but it still occupies one of the
        # bounded cache slots. Measured: 8 caller edits filled all 8 slots with
        # dead generations and evicted the reusable one. Same reason as the
        # discard branch above.
        if replacement is not slot:
            self._engines.discard(slot.config)
        if desired:
            prepared.headers["Cookie"] = desired
        return replacement

    def _one_shot_slot(self, config):
        """Returns an empty-store engine that nothing else can ever reach.

        'discard_cookies' needs a Core cookie store with no cookies in it,
        and ABI v8 offers no way to clear one, so the only faithful
        implementation is a brand-new engine. That engine must be *one-shot*:
        it used to be obtained by bumping 'self._generation', which is also the
        key every later ordinary request reads, so the store the discard request
        had just filled with the server's 'Set-Cookie' became the default store
        for the whole session. Measured: get(/set) -> get(/ok, discard_cookies=
        True) -> get(/plain) sent 'Cookie: sid=ABC' on the last call, which is
        the opposite of "neither send nor record".

        The slot is pulled straight back out of the bounded cache: its store must
        start empty on every discard request, so reusing it would leak the
        previous discard response's cookies, and leaving it cached would evict
        the ordinary engine. The engine itself stays alive through the 'EngineSlot'
        this returns and, for a streaming response, through the native request,
        which holds its own reference to the Chromium context.
        """
        self._discard_serial += 1
        # Negative generations cannot collide with 'self._generation', which
        # only ever grows from zero.
        one_shot = config.replace(generation=-self._discard_serial)
        slot = self._engines.get(one_shot)
        self._engines.discard(one_shot)
        return slot

    def _absorb_cookies(self, slot, response, discard=False):
        """Mirrors every ``Set-Cookie`` from the final hop and each redirect."""
        if discard:
            return
        for url, headers in response.history_headers:
            lines = headers.get_list("set-cookie")
            if lines:
                slot.mirror.absorb_set_cookie(url, lines)
                response.cookies.absorb_set_cookie(url, lines)
                self.cookies.absorb_set_cookie(url, lines)
        lines = response.headers.get_list("set-cookie")
        if lines:
            slot.mirror.absorb_set_cookie(response.url, lines)
            response.cookies.absorb_set_cookie(response.url, lines)
            self.cookies.absorb_set_cookie(response.url, lines)

    # -- response assembly --------------------------------------------------
    def _new_response(self, async_mode=False):
        if self.response_class is not None:
            return self.response_class()
        return AsyncResponse() if async_mode else Response()

    def _finalize(self, response, prepared, status_code, raw_headers, hops, started,
                  slot, discard_cookies=False, body=None):
        status_line, headers = parse_raw_headers(raw_headers)
        if body is not None:
            response.content = body
        response.status_code = status_code
        response.headers = headers
        response.status_line = status_line
        response.reason = reason_from_status_line(status_line, status_code)
        # ABI v8 carries no negotiated protocol, and the status line cannot
        # stand in for one: Chromium normalizes an HTTP/2 response onto
        # "HTTP/1.1 200" (net/spdy/spdy_http_utils.cc), so parsing it reported
        # HTTP/1.1 for every h2 and h3 connection. Reporting the truth is the
        # same rule the reason phrase follows -- derive only what can be derived.
        response.http_version = _negotiated_http_version(status_line)
        response.request = prepared
        response.elapsed = _elapsed(started)
        response.default_encoding = self.default_encoding
        response.redirect_count = len(hops)
        response.history_headers = []
        url = prepared.url
        history = []
        for hop_status, hop_raw, hop_url, _hop_method in hops:
            hop_line, hop_headers = parse_raw_headers(hop_raw)
            hop = Response()
            hop.status_code = hop_status
            hop.headers = hop_headers
            hop.status_line = hop_line
            hop.reason = reason_from_status_line(hop_line, hop_status)
            hop.http_version = _negotiated_http_version(hop_line)
            hop.url = url
            hop.request = prepared
            hop.content = b""
            response.history_headers.append((url, hop_headers))
            history.append(hop)
            url = hop_url
        response.history = history
        response.url = url
        response.redirect_url = url if history else None
        self._absorb_cookies(slot, response, discard_cookies)
        if response.is_redirect:
            following = prepared.copy()
            following.url = urljoin(response.url, response.headers["location"])
            response._next = following
        return response

    def _dispatch_hooks(self, response, prepared):
        for hook in prepared.hooks.get("response", ()):
            result = hook(response)
            if result is not None:
                response = result
        return response

    def _native_request(self, slot, prepared, timeout, allow_redirects, cache_mode,
                        priority):
        body = prepared.body if not prepared.stream_body else None
        return slot.engine.request(
            prepared.method, prepared.url, prepared.wire_headers(), body,
            _split_timeout(timeout), bool(allow_redirects), None, cache_mode,
            priority, bool(prepared.stream_body),
        )


    # -- call normalisation -------------------------------------------------
    def _prepare_call(self, values):
        """Splits a ``request()`` call into a ``Request`` plus transport options.

        Takes ``locals()`` from the caller so the sync and async signatures stay
        identical without repeating 40 argument names three times.
        """
        values = dict(values)
        values.pop("self", None)
        method = values.pop("method")
        url = values.pop("url")
        reject_fingerprint_overrides(values.get("ja3"), values.get("akamai"),
                                     values.get("perk"))
        for name in ("cert", "interface", "doh_url", "curl_options", "thread", "debug"):
            if values.get(name):
                raise UnsupportedFeature(
                    "%s= is not supported: ABI v8 exposes no Core setting for it" % name)
        if values.get("max_recv_speed"):
            raise UnsupportedFeature(
                "max_recv_speed= is not supported: Chromium owns transfer pacing")
        header_order, form_boundary = self.header_order, self.form_boundary
        if values.get("extra_fp") is not None:
            header_order, form_boundary = validate_extra_fp(values["extra_fp"])

        headers = values.get("headers")
        headers = Headers(headers) if headers is not None else None
        # Referer is honoured: the Core routes it to URLRequest::SetReferrer
        # (core/source/request.cc), so it goes out like a real Chrome's. Merge
        # the keyword first and let an explicit header win, mirroring how
        # accept_encoding is handled below.
        if values.get("referer") is not None:
            headers = headers if headers is not None else Headers()
            headers.setdefault("Referer", values["referer"])
        if values.get("accept_encoding") is not None:
            headers = headers if headers is not None else Headers()
            headers["Accept-Encoding"] = values["accept_encoding"]

        data = values.get("data")
        files = values.get("files")
        mime = values.get("multipart")
        if mime is not None:
            body, content_type = mime.encode(boundary=form_boundary)
            data, files = body, None
            headers = headers if headers is not None else Headers()
            headers.setdefault("Content-Type", content_type)
        elif values.get("content") is not None:
            if data:
                raise ValueError("pass either data= or content=, not both")
            data = values["content"]
        elif files and form_boundary:
            body, content_type = _multipart.encode_multipart(data, files,
                                                             boundary=form_boundary)
            data, files = body, None
            headers = headers if headers is not None else Headers()
            headers.setdefault("Content-Type", content_type)

        request = Request(
            method=method, url=url, headers=headers, files=files, data=data,
            json=values.get("json"), params=values.get("params"),
            auth=values.get("auth"), cookies=values.get("cookies"),
            hooks=values.get("hooks"),
        )
        request.quote = values.get("quote")
        request.header_order = header_order

        allow_redirects, effective_max, python_redirects = \
            self._resolve_redirect_policy(values.get("allow_redirects"),
                                          values.get("max_redirects"))
        stream = values.get("stream")
        limit = values.get("max_response_bytes")
        options = {
            "impersonate": values.get("impersonate"),
            "proxy": values.get("proxy"),
            "proxies": values.get("proxies"),
            "verify": values.get("verify"),
            "http_version": values.get("http_version"),
            "timeout": self.timeout if values.get("timeout") is None else values["timeout"],
            "stream": self.stream if stream is None else stream,
            "max_response_bytes": self.max_response_bytes if limit is None
            else _validate_max_response_bytes(limit),
            "cache_mode": values.get("cache_mode"),
            # A caller-supplied priority always wins.  When it does not, a
            # Chrome 124+ profile defaults to "highest", because that is what
            # the browser itself sends: Blink starts a navigation at
            # ResourceLoadPriority::kVeryHigh (document_loader.cc:2142), which
            # ConvertToNetPriority maps to net::HIGHEST
            # (web_url_request.cc:61-62), and net turns HIGHEST into urgency 0,
            # i.e. "priority: u=0, i".  The Core's own default is LOWEST, which
            # serialises as "priority: i" -- urgency omitted means "3", a
            # subresource hint on a header set that claims to be a browser
            # navigation.  Profiles before 124 send no priority header at all,
            # so their default is left untouched: there is no measured defect
            # to fix there and changing it would move their H2 fingerprint.
            "priority": values.get("priority")
            if values.get("priority") is not None
            else default_priority(values.get("impersonate")
                                  if values.get("impersonate") is not None
                                  else self.impersonate),
            "native_redirects": bool(allow_redirects) and not python_redirects,
            "python_redirects": python_redirects,
            "max_redirects": effective_max,
            "retry": RetryStrategy.coerce(values["retry"])
            if values.get("retry") is not None else None,
            "default_encoding": values.get("default_encoding"),
            "content_callback": values.get("content_callback"),
            "raise_for_status": values.get("raise_for_status"),
            "discard_cookies": values.get("discard_cookies"),
        }
        return request, options

    def _open_websocket(self, url, origin, headers, timeout, proxy, proxies,
                        impersonate, protocols, verify):
        slot = self._slot(url, impersonate, proxy, proxies, verify, None)
        merged = Headers(self.headers)
        if headers:
            merged.update(headers)
        forbidden = sorted(name for name in merged if _is_forbidden_ws_header(name))
        if forbidden:
            raise UnsupportedFeature(
                "%s cannot be set on a WebSocket handshake: Chromium owns "
                "%s, and the Core rejects them as extra headers. Set the "
                "User-Agent for the whole session with Session(user_agent=...) or "
                "pick an impersonate profile; Host, Origin and the Sec-WebSocket-* "
                "headers come from the URL and the handshake."
                % (", ".join(forbidden), ", ".join(_FORBIDDEN_WS_HEADERS)))
        http_url = _websocket_http_url(url)
        cookie = self.cookies.cookie_header(http_url)
        if cookie:
            merged["Cookie"] = cookie
        return slot.engine.websocket(url, origin or _default_websocket_origin(url),
                                     merged.multi_items(),
                                     _split_timeout(timeout if timeout is not None
                                                    else self.timeout),
                                     list(protocols or ()))

    def _with_session_defaults(self, options):
        """Fills in the session-level defaults a direct ``send()`` bypasses.

        ``request()`` resolves these in :meth:``_prepare_call``, but the
        requests contract also lets a caller build a ``PreparedRequest`` and
        call ``send()`` directly, where ``options`` is whatever they passed.
        Every default the session holds has to be re-applied here or it
        silently does nothing on that path: ``max_response_bytes``, ``timeout``,
        the stream flag, ``discard_cookies``, ``retry``, and the whole redirect
        policy.

        It lives on ``BaseSession`` because :meth:``AsyncSession.send`` never
        called it. ``AsyncSession(timeout=0.05).send(prep)`` waited for the
        server instead of timing out, and
        ``AsyncSession(max_response_bytes=1024).send(prep)`` returned a 5000-byte
        body, while the synchronous twin raised ``Timeout`` and
        ``ResponseTooLarge`` for the same input. One definition, two callers.
        """
        resolved = dict(options)
        limit = resolved.get("max_response_bytes")
        if limit is None:
            resolved["max_response_bytes"] = self.max_response_bytes
        else:
            resolved["max_response_bytes"] = _validate_max_response_bytes(limit)
        if resolved.get("timeout") is None:
            resolved["timeout"] = self.timeout
        if resolved.get("stream") is None:
            resolved["stream"] = self.stream
        if resolved.get("discard_cookies") is None:
            resolved["discard_cookies"] = self.discard_cookies
        if resolved.get("retry") is None:
            resolved["retry"] = self.retry
        if resolved.get("native_redirects") is None \
                and resolved.get("python_redirects") is None:
            # A caller who builds the options by hand uses the requests
            # spelling ``allow_redirects=``; it used to be dropped on the floor
            # here, so the resolved policy always came out of the session.
            allow, cap, python_redirects = self._resolve_redirect_policy(
                resolved.get("allow_redirects"), resolved.get("max_redirects"))
            resolved["python_redirects"] = python_redirects
            resolved["native_redirects"] = bool(allow) and not python_redirects
            resolved["max_redirects"] = cap
        return resolved


    def _should_retry_status(self, retry, status_code, prepared, attempt):
        """True when a status-code retry is both configured and safe here.

        ``RetryStrategy.should_retry_status`` alone was the whole test, so
        ``Session(retry=1).post(url, data=b"payload")`` against a 500 re-sent
        the request: the server saw two POSTs with the same 7-byte body. A
        transport failure never reached the origin, but a 500 is a response
        from it, which means the first attempt was already delivered. Only a
        replayable request -- an idempotent method with no streamed body, see
        :func:``_request_is_replayable`` -- may be sent twice.

        The default code list (429, 500, 502, 503, 504) is kept as configured:
        the guard is on the request, not on the status, so a caller who wants
        429 or 503 retried on a GET still gets it, and a POST still gets none of
        them by default. A caller who explicitly asks for a non-idempotent
        retry can pass ``method``-appropriate data or accept that the
        facade will not replay it -- silently duplicating a POST is the worse
        failure.
        """
        if attempt > retry.count:
            return False
        if not retry.should_retry_status(status_code):
            return False
        return _request_is_replayable(prepared)

    # -- redirect policy ----------------------------------------------------
    def _resolve_redirect_policy(self, allow_redirects, max_redirects):
        """Resolves one call into ``(allow_redirects, max_redirects, python_redirects)``.

        Three silent defects lived in this decision:

        * ``request()`` defaulted ``allow_redirects=True``, so the session's own
          ``allow_redirects`` was overwritten before the ``None`` check could see it and
          ``Session(allow_redirects=False)`` followed redirects anyway.
        * the request's ``max_redirects`` -- ``None`` whenever the caller passed none -- was
          tested instead of the resolved cap, so ``Session(max_redirects=5)`` fell
          through to Chromium and followed twenty hops.
        * the threshold was 30 (requests' own default) rather than Chromium's 20,
          so ``max_redirects=30`` took the native path and died at hop 21 with a
          limit nobody had set. A cap at or above 20 is refused up front by
          ``_validate_max_redirects`` rather than clamped.

        ``python_redirects`` is true only when the caller asked to follow *and* the
        cap is tighter than Chromium's, which is the one case where re-issuing
        each hop from Python is what makes the caller's number true.
        """
        allow = self.allow_redirects if allow_redirects is None else bool(allow_redirects)
        cap = self.max_redirects if max_redirects is None else max_redirects
        cap = _validate_max_redirects(cap)
        if cap is None:
            # No caller cap: Chromium's own limit is the only one, so the native
            # follow path is both correct and higher fidelity -- it preserves the
            # original request's site-for-cookies. Driving the hops from Python
            # here would also lose 'Response.redirect_count', which is counted
            # from the Core's per-hop events.
            return allow, CHROMIUM_MAX_REDIRECTS, False
        return allow, cap, bool(allow) and cap < CHROMIUM_MAX_REDIRECTS

    def rebuild_method(self, prepared, response):
        method = prepared.method
        if response.status_code == 303 and method != "HEAD":
            method = "GET"
        if response.status_code == 302 and method != "HEAD":
            method = "GET"
        if response.status_code == 301 and method == "POST":
            method = "GET"
        prepared.method = method

    def rebuild_auth(self, prepared, response):
        if "Authorization" in prepared.headers and \
                urlsplit(response.url).hostname != urlsplit(prepared.url).hostname:
            del prepared.headers["Authorization"]

    def _next_hop(self, response, prepared):
        following = prepared.copy()
        following.url = requote_uri(urljoin(response.url, response.headers["location"]))
        self.rebuild_method(following, response)
        if following.method in ("GET", "HEAD") or response.status_code == 303:
            following.body = None
            following.stream_body = False
            for name in ("Content-Length", "Content-Type", "Transfer-Encoding"):
                following.headers.pop(name, None)
        self.rebuild_auth(following, response)
        following.headers.pop("Cookie", None)
        return following


def _warn_if_blocking_the_loop():
    """Warns when a blocking call is made from inside a running event loop.

    ``Session`` is the synchronous API: it blocks the calling thread until the
    response completes, which is what callers of a sync API expect. Inside a
    coroutine that same blocking call stalls every other task on the loop, and
    nothing about the call site makes that visible.

    Measured: three sequential ``Session.get()`` calls inside a coroutine took
    0.69 s and the loop ticked zero times during them, so a concurrent
    ``AsyncSession`` workload sharing that loop made no progress at all.

    This cannot be prevented -- the sync API must block -- so it is surfaced
    once per call rather than silently degrading the whole loop.
    """
    # `asyncio.get_running_loop` is 3.7+; the package still installs on 3.6,
    # where the attribute does not exist at all.
    try:
        running = getattr(asyncio, "get_running_loop", None)
        if running is None:
            return
        running()
    except RuntimeError:
        return
    warnings.warn(
        "a synchronous Session call is running inside an event loop and will "
        "block it; use AsyncSession for concurrent work",
        RuntimeWarning,
        stacklevel=3,
    )

class Session(BaseSession):
    """Synchronous session.

    Safe to share across threads: every blocking Core call releases the GIL, and
    the engine cache is locked.  One session per thread still performs better,
    because Chromium serialises requests that share an HTTP cache key inside one
    engine.
    """

    def request(self, method, url, params=None, data=None, headers=None, cookies=None,
                files=None, auth=None, timeout=None, allow_redirects=None, proxies=None,
                hooks=None, stream=None, verify=None, cert=None, json=None,
                content=None, multipart=None, impersonate=None, proxy=None,
                http_version=None, max_redirects=None, max_response_bytes=None,
                referer=None, accept_encoding=None, default_encoding=None,
                discard_cookies=None, retry=None, cache_mode=None, priority=None,
                ja3=None, akamai=None, perk=None, extra_fp=None, content_callback=None,
                raise_for_status=None, quote=None, curl_options=None, interface=None,
                doh_url=None, max_recv_speed=None, thread=None, debug=None):
        """Sends one request and returns a :class:`Response`.

        Session defaults are used when an option is ``None``. ``params`` appends
        query fields; ``data``/``json``/``content``/``multipart`` select the body;
        ``headers`` and ``cookies`` merge with session state. Transport overrides
        include ``timeout``, redirects, proxy, certificate verification, profile,
        HTTP version, response-size limit, cache mode and request priority.
        """
        request, options = self._prepare_call(locals())
        return self.send(self.prepare_request(request), **options)

    def send(self, request, **options):
        """Sends a ``PreparedRequest``, as ``requests.Session.send`` does."""
        _warn_if_blocking_the_loop()
        self._ensure_open()
        if not isinstance(request, PreparedRequest):
            raise ValueError("You can only send PreparedRequests")
        options = self._with_session_defaults(options)
        adapter = self.get_adapter(request.url)
        from .adapters import HTTPAdapter
        if not isinstance(adapter, HTTPAdapter):
            return adapter.send(
                request, stream=bool(options.get("stream")),
                timeout=options.get("timeout"), verify=options.get("verify", True),
                cert=options.get("cert"), proxies=options.get("proxies"))
        retry = options.get("retry") or self.retry
        attempt = 0
        proxy_attempt = 0
        while True:
            attempt += 1
            try:
                response = self._send_once(request, options)
            except ResponseTooLarge:
                raise
            except RequestException as error:
                # A configured retry policy used to re-send anything that
                # failed, including a body that had already been consumed from
                # the caller's one-shot iterator. Measured: a two-chunk upload
                # retried after a -130 reached the origin as the single
                # fragment ``PART2``. A body that cannot be produced again is
                # not replayable, whatever the method or the policy.
                if attempt <= retry.count and _body_can_be_resent(request):
                    time.sleep(retry.sleep_for(attempt))
                    continue
                # A pool endpoint that is briefly unreachable is the common case
                # behind ERR_PROXY_CONNECTION_FAILED / ERR_TUNNEL_CONNECTION_FAILED,
                # and those never reached the origin, so one retry costs nothing
                # but a second connection attempt.
                if (retry.count == 0
                        and proxy_attempt < DEFAULT_PROXY_RETRY_COUNT
                        and _is_retriable_proxy_failure(error)
                        and _request_is_replayable(request)):
                    proxy_attempt += 1
                    continue
                raise
            if self._should_retry_status(retry, response.status_code, request,
                                         attempt):
                response.close()
                time.sleep(retry.sleep_for(attempt))
                continue
            break
        if options.get("python_redirects"):
            response = self._follow_in_python(response, request, options)
        response = self._dispatch_hooks(response, request)
        if options.get("raise_for_status") if options.get("raise_for_status") is not None \
                else self.raise_for_status:
            response.raise_for_status()
        return response

    def _follow_in_python(self, response, request, options):
        limit = options["max_redirects"]
        history = []
        hop_request = request
        while response.is_redirect:
            if len(history) >= limit:
                raise TooManyRedirects("Exceeded %d redirects." % limit,
                                       response=response)
            response.content
            history.append(response)
            hop_request = self._next_hop(response, hop_request)
            response = self._send_once(hop_request, options)
        if history:
            response.history = history + list(response.history)
        return response

    def _send_once(self, prepared, options):
        slot = self._slot(prepared.url, options.get("impersonate"),
                          options.get("proxy"), options.get("proxies"),
                          options.get("verify"), options.get("http_version"))
        discard = options.get("discard_cookies")
        discard = self.discard_cookies if discard is None else discard
        slot = self._resolve_cookies(slot, prepared, discard)
        limit = options.get("max_response_bytes")
        stream = bool(options.get("stream"))
        started = _now()
        follow = options.get("native_redirects", True)
        native = None
        try:
            native = self._native_request(
                slot, prepared, options.get("timeout"), follow,
                options.get("cache_mode"), options.get("priority"))
            if prepared.stream_body:
                native.start()
                for chunk in _multipart.iter_body(prepared.body):
                    native.upload_write(chunk, False)
                native.upload_finish()
                head = native.await_response() if follow else None
            elif follow:
                head = native.start_stream()
            else:
                native.start()
                head = None
            if follow:
                status_code, raw_headers = head.status_code, head.headers
            else:
                hop, head = native.wait_manual(_split_timeout(options.get("timeout")))
                if hop is None and head is None:
                    _cancel_quietly(native)
                    raise Timeout("request timed out")
                if hop is not None:
                    # Chromium holds the hop open waiting for `follow_redirect`;
                    # the caller asked not to follow, so the 3xx is the response.
                    _cancel_quietly(native)
                    return self._finalize(self._new_response(), prepared, hop[0], hop[1],
                                          [], started, slot, discard, body=b"")
                status_code, raw_headers = head
        except BaseException as error:
            # A failure here leaves a live Core request behind. Only
            # ``RuntimeError`` was caught, so an ``OSError`` from the caller's
            # body iterator -- or anything else raised mid-upload -- escaped
            # with the URLRequest still registered. Measured over ten failed
            # chunked uploads: client descriptors grew 5 -> 23 and the server
            # held six connections that never closed. Cancel first, then
            # re-raise unchanged; the original exception is the one that
            # explains what went wrong.
            if native is not None:
                _cancel_quietly(native)
            if isinstance(error, RuntimeError):
                raise map_native_error(error, request=prepared)
            raise
        hops = native.take_redirects()
        response = self._new_response()
        try:
            if stream:
                response._body_reader = _SyncBodyReader(native, limit)
            elif limit is None:
                response.content = native.read_body()
                native.detach_callback()
            else:
                response.content = b"".join(_SyncBodyReader(native, limit))
        except RuntimeError as error:
            native.detach_callback()
            raise map_native_error(error, request=prepared)
        if options.get("default_encoding") is not None:
            response.default_encoding = options["default_encoding"]
        callback = options.get("content_callback")
        if callback is not None and not stream:
            callback(response.content)
        return self._finalize(response, prepared, status_code, raw_headers,
                              hops, started, slot, discard)

    # -- verbs --------------------------------------------------------------
    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def options(self, url, **kwargs):
        return self.request("OPTIONS", url, **kwargs)

    def head(self, url, **kwargs):
        kwargs.setdefault("allow_redirects", False)
        return self.request("HEAD", url, **kwargs)

    def post(self, url, data=None, json=None, **kwargs):
        return self.request("POST", url, data=data, json=json, **kwargs)

    def put(self, url, data=None, **kwargs):
        return self.request("PUT", url, data=data, **kwargs)

    def patch(self, url, data=None, **kwargs):
        return self.request("PATCH", url, data=data, **kwargs)

    def delete(self, url, **kwargs):
        return self.request("DELETE", url, **kwargs)

    def trace(self, url, **kwargs):
        return self.request("TRACE", url, **kwargs)

    def query(self, url, **kwargs):
        return self.request("QUERY", url, **kwargs)

    def resolve_redirects(self, response, request, stream=False, timeout=None,
                          verify=True, cert=None, proxies=None, yield_requests=False,
                          **kwargs):
        """Yields each response in a redirect chain, as requests does."""
        hop_request = request
        count = 0
        # 'max_redirects' is None when the caller set no cap, meaning Chromium's
        # own ceiling; this generator still needs a number to stop at.
        limit = (CHROMIUM_MAX_REDIRECTS if self.max_redirects is None
                 else self.max_redirects)
        while response.is_redirect:
            if count >= limit:
                raise TooManyRedirects("Exceeded %d redirects." % limit,
                                       response=response)
            response.content
            hop_request = self._next_hop(response, hop_request)
            if yield_requests:
                yield hop_request
            else:
                response = self._send_once(hop_request, {
                    "stream": stream, "timeout": timeout, "verify": verify,
                    "proxies": proxies, "native_redirects": False,
                })
                yield response
            count += 1

    def _stream_context(self, method, url, **kwargs):
        """Backs ``session.stream("GET", url)``, curl_cffi's streaming helper."""
        kwargs["stream"] = True
        return _StreamContext(self.request(method, url, **kwargs))

    def websocket(self, url, origin="", headers=None, timeout=None, proxy=None,
                  proxies=None, impersonate=None, protocols=None, verify=None):
        """Opens a WebSocket and waits for the handshake to complete.

        Waiting matters: the Core rejects `close()` and `send()` before the
        socket is open, so returning early would hand back an object that cannot
        be used yet.
        """
        from .websockets import WebSocket
        try:
            socket = self._open_websocket(url, origin, headers, timeout, proxy,
                                          proxies, impersonate, protocols, verify)
            socket.connect()
        except RequestException:
            raise
        except RuntimeError as error:
            raise map_native_error(error)
        return WebSocket(socket, await_open=True,
                         timeout=timeout if timeout is not None else self.timeout)

    ws_connect = websocket

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class _StreamContext(object):
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self._response

    def __exit__(self, *_exc):
        self._response.close()


class AsyncSession(BaseSession):
    """Asyncio session.

    No worker thread or thread pool is involved: the Core wakes the running loop
    directly and each wakeup drains a batch of events, so thousands of in-flight
    requests cost one Future and one small state object each.

    ``max_clients`` bounds how many requests are in flight at once.  Leaving it
    unset is fine for a few thousand; setting it is how a caller keeps a burst
    from opening more sockets than the far end tolerates.
    """

    def __init__(self, impersonate=None, proxy=None, proxies=None, proxy_auth=None,
                 verify=True, timeout=None, headers=None, cookies=None, params=None,
                 auth=None, cert=None, stream=False, hooks=None,
                 max_redirects=None, trust_env=True,
                 allow_redirects=True, max_response_bytes=None, base_url=None,
                 http_version=None, ja3=None, akamai=None, perk=None, extra_fp=None,
                 default_headers=True, default_encoding="utf-8",
                 discard_cookies=False, raise_for_status=False, retry=0, cache=True,
                 user_agent=None, accept_language=None, interface=None, doh_url=None,
                 max_recv_speed=0, curl_options=None, max_engines=DEFAULT_MAX_ENGINES,
                 response_class=None, max_clients=None):
        BaseSession.__init__(
            self, impersonate=impersonate, proxy=proxy, proxies=proxies,
            proxy_auth=proxy_auth, verify=verify, timeout=timeout, headers=headers,
            cookies=cookies, params=params, auth=auth, cert=cert, stream=stream,
            hooks=hooks, max_redirects=max_redirects, trust_env=trust_env,
            allow_redirects=allow_redirects, max_response_bytes=max_response_bytes,
            base_url=base_url, http_version=http_version, ja3=ja3,
            akamai=akamai, perk=perk, extra_fp=extra_fp,
            default_headers=default_headers, default_encoding=default_encoding,
            discard_cookies=discard_cookies, raise_for_status=raise_for_status,
            retry=retry, cache=cache, user_agent=user_agent,
            accept_language=accept_language, interface=interface, doh_url=doh_url,
            max_recv_speed=max_recv_speed, curl_options=curl_options,
            max_engines=max_engines, response_class=response_class)
        self.max_clients = max_clients
        self._gate = None
        self._gate_loop = None

    def _semaphore(self, loop):
        if not self.max_clients:
            return None
        if self._gate is None or self._gate_loop is not loop:
            self._gate = asyncio.Semaphore(self.max_clients)
            self._gate_loop = loop
        return self._gate

    async def request(self, method, url, params=None, data=None, headers=None,
                      cookies=None, files=None, auth=None, timeout=None,
                      allow_redirects=None, proxies=None, hooks=None, stream=None,
                      verify=None, cert=None, json=None, content=None, multipart=None,
                      impersonate=None, proxy=None, http_version=None,
                      max_redirects=None, max_response_bytes=None, referer=None,
                      accept_encoding=None, default_encoding=None,
                      discard_cookies=None, retry=None, cache_mode=None, priority=None,
                      ja3=None, akamai=None, perk=None, extra_fp=None,
                      content_callback=None, raise_for_status=None, quote=None,
                      curl_options=None, interface=None, doh_url=None,
                      max_recv_speed=None, thread=None, debug=None):
        request, options = self._prepare_call(locals())
        return await self.send(self.prepare_request(request), **options)

    async def send(self, request, **options):
        """Sends a ``PreparedRequest``, as ``requests.Session.send`` does.

        ``_with_session_defaults`` is applied first, exactly as the
        synchronous path does. Without it every session default was dead on
        this entry point: ``AsyncSession(timeout=0.05).send(prep)`` returned after
        1.01s with no timeout, and
        ``AsyncSession(max_response_bytes=1024).send(prep)`` returned a 5000-byte
        body. Both raise on the synchronous twin.
        """
        self._ensure_open()
        if not isinstance(request, PreparedRequest):
            raise ValueError("You can only send PreparedRequests")
        options = self._with_session_defaults(options)
        retry = options.get("retry") or self.retry
        attempt = 0
        proxy_attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._send_once(request, options)
            except ResponseTooLarge:
                raise
            except RequestException as error:
                # Same guard as the synchronous path: a consumed one-shot body
                # cannot be produced again, so re-sending sends a fragment.
                if attempt <= retry.count and _body_can_be_resent(request):
                    await asyncio.sleep(retry.sleep_for(attempt))
                    continue
                if (retry.count == 0
                        and proxy_attempt < DEFAULT_PROXY_RETRY_COUNT
                        and _is_retriable_proxy_failure(error)
                        and _request_is_replayable(request)):
                    proxy_attempt += 1
                    continue
                raise
            if self._should_retry_status(retry, response.status_code, request, attempt):
                await response.aclose()
                await asyncio.sleep(retry.sleep_for(attempt))
                continue
            break
        if options.get("python_redirects"):
            response = await self._follow_in_python(response, request, options)
        response = self._dispatch_hooks(response, request)
        raise_flag = options.get("raise_for_status")
        if self.raise_for_status if raise_flag is None else raise_flag:
            response.raise_for_status()
        return response

    async def _follow_in_python(self, response, request, options):
        limit = options["max_redirects"]
        history = []
        hop_request = request
        while response.is_redirect:
            if len(history) >= limit:
                raise TooManyRedirects("Exceeded %d redirects." % limit,
                                       response=response)
            await response.acontent()
            history.append(response)
            hop_request = self._next_hop(response, hop_request)
            response = await self._send_once(hop_request, options)
        if history:
            response.history = history + list(response.history)
        return response

    async def _send_once(self, prepared, options):
        loop = _running_loop()
        gate = self._semaphore(loop)
        if gate is not None:
            await gate.acquire()
        try:
            return await self._perform(prepared, options, loop)
        finally:
            if gate is not None:
                gate.release()

    async def _perform(self, prepared, options, loop):
        slot = self._slot(prepared.url, options.get("impersonate"),
                          options.get("proxy"), options.get("proxies"),
                          options.get("verify"), options.get("http_version"))
        discard = options.get("discard_cookies")
        discard = self.discard_cookies if discard is None else discard
        slot = self._resolve_cookies(slot, prepared, discard)
        stream = bool(options.get("stream"))
        limit = options.get("max_response_bytes")
        timeout = _split_timeout(options.get("timeout"))
        started = _now()
        try:
            native = self._native_request(
                slot, prepared, options.get("timeout"),
                options.get("native_redirects", True), options.get("cache_mode"),
                options.get("priority"))
        except RuntimeError as error:
            raise map_native_error(error, request=prepared)
        follow = options.get("native_redirects", True)
        state = _AsyncState(native, loop, stream, limit, timeout, follow)
        try:
            if prepared.stream_body:
                native.attach_async(loop, state.notify)
                native.start()
                await self._feed_upload(native, prepared.body)
            else:
                native.start_async(loop, state.notify)
        except BaseException as error:
            # Only ``RuntimeError`` was caught here, so an ``OSError`` from the
            # caller's async body iterator -- and ``CancelledError``, which is
            # a BaseException -- escaped with the Core request still running.
            # ``state.finish(cancel=True)`` cancels it and detaches the
            # callback; the exception is then re-raised unchanged.
            state.finish(cancel=True)
            if isinstance(error, RuntimeError):
                raise map_native_error(error, request=prepared)
            raise
        try:
            if stream:
                await self._await_headers(state)
            else:
                await state.future
        except asyncio.CancelledError:
            state.finish(cancel=True)
            raise
        except BaseException:
            state.finish(cancel=True)
            raise
        response = self._new_response(async_mode=True)
        if options.get("default_encoding") is not None:
            response.default_encoding = options["default_encoding"]
        if stream:
            # Always hand a streaming response its reader, exactly as the
            # synchronous path does. The condition used to be `stream and
            # follow`, so with allow_redirects=False (or a max_redirects
            # tighter than Chromium's own limit) the body was read eagerly
            # from ``state.body`` -- which is None in streaming mode -- and the
            # response silently came back empty. The reader waits on the same
            # state, so it is correct for both redirect modes.
            response._async_body_reader = _AsyncBodyReader(state, limit)
        else:
            response.content = bytes(state.body) if state.body is not None else b""
            state.finish()
            callback = options.get("content_callback")
            if callback is not None:
                callback(response.content)
        return self._finalize(response, prepared, state.status, state.raw_headers,
                              state.hops, started, slot, discard)

    @staticmethod
    async def _await_headers(state):
        """Waits for the response head, for a streaming request.

        A body-size violation must not be raised here. The caller asked for a
        stream, so the contract is that ``max_response_bytes`` fails while the
        body is being consumed -- the same place the synchronous path fails.
        The limit is detected in :meth:`_AsyncState._on_body`, which runs on
        the event loop between wakeups, so a small response can trip it before
        the response event is even seen. Re-raising it here turned a lazy
        stream into an eager failure at ``get()``, which is what made
        ``test_streaming_and_response_limit`` fail: the test then never got a
        response to iterate.
        """
        while not state.status:
            if state.error is not None and not state.error_is_lazy():
                raise state.error
            if state.done:
                break
            await state.wait()
        if state.error is not None and not state.error_is_lazy():
            raise state.error

    @staticmethod
    async def _feed_upload(native, body):
        if hasattr(body, "__aiter__"):
            async for chunk in body:
                native.upload_write(_multipart._bytes(chunk), False)
        else:
            for chunk in _multipart.iter_body(body):
                native.upload_write(chunk, False)
                await asyncio.sleep(0)
        native.upload_finish()

    # -- verbs --------------------------------------------------------------
    async def get(self, url, **kwargs):
        return await self.request("GET", url, **kwargs)

    async def options(self, url, **kwargs):
        return await self.request("OPTIONS", url, **kwargs)

    async def head(self, url, **kwargs):
        kwargs.setdefault("allow_redirects", False)
        return await self.request("HEAD", url, **kwargs)

    async def post(self, url, data=None, json=None, **kwargs):
        return await self.request("POST", url, data=data, json=json, **kwargs)

    async def put(self, url, data=None, **kwargs):
        return await self.request("PUT", url, data=data, **kwargs)

    async def patch(self, url, data=None, **kwargs):
        return await self.request("PATCH", url, data=data, **kwargs)

    async def delete(self, url, **kwargs):
        return await self.request("DELETE", url, **kwargs)

    async def trace(self, url, **kwargs):
        return await self.request("TRACE", url, **kwargs)

    async def query(self, url, **kwargs):
        return await self.request("QUERY", url, **kwargs)

    def _stream_context(self, method, url, **kwargs):
        kwargs["stream"] = True
        return _AsyncStreamContext(self.request(method, url, **kwargs))

    async def websocket(self, url, origin="", headers=None, timeout=None, proxy=None,
                        proxies=None, impersonate=None, protocols=None, verify=None):
        from .websockets import AsyncWebSocket
        try:
            socket = self._open_websocket(url, origin, headers, timeout, proxy,
                                          proxies, impersonate, protocols, verify)
            return await AsyncWebSocket.open(
                socket, timeout=timeout if timeout is not None else self.timeout)
        except RequestException:
            raise
        except RuntimeError as error:
            raise map_native_error(error)

    ws_connect = websocket

    async def upkeep(self):
        return 0

    async def aclose(self):
        self.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        await self.aclose()


class _AsyncStreamContext(object):
    def __init__(self, awaitable):
        self._awaitable = awaitable
        self._response = None

    async def __aenter__(self):
        self._response = await self._awaitable
        return self._response

    async def __aexit__(self, *_exc):
        if self._response is not None:
            await self._response.aclose()


def _running_loop():
    if hasattr(asyncio, "get_running_loop"):
        return asyncio.get_running_loop()
    return asyncio.get_event_loop()


# Historical names kept as aliases so existing imports keep working.
Client = Session
AsyncClient = AsyncSession
