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
import queue
import threading
import time
import warnings
import weakref
from collections import deque
from urllib.parse import unquote, urljoin, urlsplit

try:
    from collections.abc import Mapping
except ImportError:  # Python 3.6
    from collections import Mapping

from . import multipart as _multipart
from .browser_headers import browser_headers, header_order as browser_header_order
from .auth import build_auth
from .cookies import RequestsCookieJar, cookiejar_from_dict, merge_cookies
from .engine import (BINDING_FIELDS, DEFAULT_MAX_ENGINES, EngineCache,
                     EngineConfig, EngineSlot, SessionBinding)
from .exceptions import (InvalidProxyURL, InvalidURL, ProxyError,
                         RequestException, ResponseTooLarge, SessionClosed,
                         SSLError, Timeout, TooManyRedirects,
                         UnsupportedFeature, map_native_error)
from .impersonate import (default_priority, normalize_http_version,
                          normalize_impersonate, reject_fingerprint_overrides,
                          validate_extra_fp)
from .models import (PreparedRequest, Request, Response, AsyncResponse,
                     _headers_from_fields, _negotiated_http_version, compose_status_line,
                     ech_of, parse_raw_headers,
                     reason_from_status_line)
from .structures import CaseInsensitiveDict, Headers
from .utils import (default_headers as _default_headers, get_netrc_auth,
                    resolve_proxies, should_bypass_proxies,
                    to_key_val_list)

#: Ceiling on the events drained from the Core per event-loop wakeup.
#:
#: A CEILING, not a target.  The Core posts one event per read, so draining a
#: single response gets one event per call: measured on a 4 MiB body, 156
#: ``poll_events`` calls carrying 159 events, batch sizes {1: 154, 5: 1}.  The
#: batching that does happen comes from OTHER requests that are ready at the same
#: instant -- 64 concurrent 1 KiB responses gave {1: 101, 2: 26, 3: 13}.
#:
#: So the per-chunk loop round trip is still paid.  Removing it needs the Core to
#: coalesce its own events, not a larger number here: raising this changes
#: nothing, because the queue is empty (not the ceiling) when a single-response
#: drain ends.  See docs/NEXT_STEPS.md, "提案 2：批量唤醒投递".
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
    """Reduces a requests deadline to the one number ABI v8 can carry.

    ABI v8 has a single deadline per request, and Chromium owns both the
    connect and the read side of it. A ``(connect, read)`` pair therefore has no
    faithful representation: the two halves cannot be told apart.

    This used to take ``max(connect, read)``, which silently deleted the only
    thing the pair is for. Measured against a server that accepts and never
    answers, ``timeout=(0.2, 10)`` waited 10.0s where requests waits 0.2s,
    and the error was ``Timeout`` rather than ``ConnectTimeout``. Silently
    honouring the larger half is exactly the degradation this project refuses
    elsewhere (``max_redirects``, ``extra_fp``), so a pair with two *different*
    values now fails closed. A pair whose halves agree is one deadline by
    definition and is accepted.
    """
    if timeout is None:
        return None
    if isinstance(timeout, (tuple, list)):
        if len(timeout) != 2:
            raise ValueError("timeout tuple must be (connect, read)")
        values = [_check_timeout(value) for value in timeout if value is not None]
        if not values:
            return None
        if len(set(values)) > 1:
            raise UnsupportedFeature(
                "timeout=(connect, read) cannot be honoured: ABI v8 carries one "
                "deadline per request and Chromium owns both halves, so %r would "
                "silently wait %r for a connection the caller capped at %r. Pass a "
                "single number instead." % (tuple(timeout), max(values), min(values)))
        return values[0]
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



#: Headers Chromium or the profile owns: they are produced by the Core, so a
#: caller cannot remove them through extra headers -- ``SetExtraRequestHeaders``
#: only ever adds. Deleting one silently changed nothing, which is the
#: degradation this project refuses: measured, ``headers={"User-Agent": None}``
#: and ``headers={"Accept-Encoding": None}`` both sent the original value.
_CORE_OWNED_HEADERS = frozenset((
    "user-agent",
    "accept-encoding",
    "accept-language",
    "host",
    "connection",
    "content-length",
    "transfer-encoding",
))


#: RFC 9110 §7.6.1 hop-by-hop fields, minus the ones this facade does honour.
#: Chromium owns connection lifetime, and ``SetExtraRequestHeaders`` never reaches
#: the connection pool, so setting any of these changes nothing on HTTP/1.1 --
#: measured: ``Connection: close`` was sent but three requests still shared one
#: socket. On HTTP/2 and HTTP/3 they are discarded outright
#: (``net/spdy/spdy_http_utils.cc:214-221`` skips ``connection``, ``proxy-connection``
#: and ``transfer-encoding``; RFC 9113 §8.2.2 and RFC 9114 §4.2 forbid all of
#: them), so accepting a value would make behaviour depend on the negotiated
#: protocol. ``transfer-encoding`` and ``content-length`` are excluded here
#: because they are body framing, which the caller does control.
_HOP_BY_HOP_HEADERS = frozenset((
    "connection",
    "proxy-connection",
    "keep-alive",
    "te",
    "trailer",
    "upgrade",
))


def _reject_hop_by_hop_headers(headers):
    """Fails closed for fields only the connection layer can honour.

    Args:
        headers: The prepared request headers, or ``None``.

    Raises:
        UnsupportedFeature: A hop-by-hop field was set or removed. The message
            names every offending field and says why it cannot work.
    """
    if headers is None:
        return
    present = {name.lower() for name, _ in headers.multi_items()}
    offending = sorted(present.union(headers.deleted_names) & _HOP_BY_HOP_HEADERS)
    if not offending:
        return
    raise UnsupportedFeature(
        "%s cannot be set: Chromium owns connection lifetime, and extra headers "
        "do not reach the connection pool. On HTTP/2 and HTTP/3 these fields are "
        "discarded outright (RFC 9113 section 8.2.2, RFC 9114 section 4.2), so "
        "accepting a value would make behaviour depend on the negotiated "
        "protocol." % (", ".join(offending),))

def _reject_core_owned_removals(headers, method):
    """Fails closed when a caller asks to remove a header the Core owns.

    Args:
        headers: The prepared request headers, or ``None``.
        method: The request method, so the body-framing headers can be judged.

    Raises:
        UnsupportedFeature: A removal was requested for a header the binding
            cannot delete. The message names the headers and what to do instead.
    """
    if headers is None:
        return
    removed = sorted(name for name in headers.deleted_names
                     if name in _CORE_OWNED_HEADERS)
    if not removed:
        return
    raise UnsupportedFeature(
        "%s cannot be removed: Chromium owns %s for every request it sends, and "
        "the ABI only lets the binding add extra headers, never delete one. Set "
        "the value you want instead, or choose a profile whose captured header "
        "list already omits it."
        % (", ".join(removed), "it" if len(removed) == 1 else "them"))

def _validate_max_redirects(value):
    """Rejects every caller-supplied cap; Chromium owns the redirect limit.

    ``net::URLRequest::kMaxRedirects`` is 20 and the Core installs no limit of
    its own, so it is the only ceiling this build can honour. Lowering it is
    not something ABI v8 can ask for, and the facade used to fake it by
    re-issuing each hop from Python -- which reimplemented redirect semantics
    in the binding layer, where they drifted from Chromium four ways:
    ``Referrer-Policy`` was not applied, the previous hop Set-Cookie was not
    sent, Response.redirect_count came back 0, and a 302 turned a PUT into a
    GET although Chromium only rewrites POST.

    The only accepted value is None, meaning Chromium own limit. Anything
    else fails closed instead of following a different hop count than asked.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("max_redirects must be a non-negative integer or None")
    raise UnsupportedFeature(
        "max_redirects=%d cannot be honoured: ABI v8 cannot lower Chromium's "
        "%d-hop ceiling (net::URLRequest::kMaxRedirects), and re-issuing the "
        "hops from Python would reimplement redirect semantics the Core owns. "
        "Use allow_redirects=False for a single hop, or omit max_redirects to "
        "accept Chromium's own limit." % (value, CHROMIUM_MAX_REDIRECTS))


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



def _read_pem(path, what):
    """Reads one PEM file the caller named for a client certificate.

    Paths only, never inline PEM text: ``requests`` accepts either, but a
    private key passed as a string literal has already been copied into the
    caller's source, shell history and process arguments by the time this
    runs, and reading the file keeps that decision in the caller's hands
    rather than making it silent.

    Args:
        path: Filesystem path to read.
        what: Which half of the pair this is, for the error message.

    Returns:
        The file contents as bytes.

    Raises:
        UnsupportedFeature: The file could not be read.
    """
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as error:
        raise UnsupportedFeature(
            "cert= could not read the %s file %r: %s" % (what, path, error))


def normalize_host_mapping(rules):
    """Validates the shape of a MappedHostResolver rule list.

    The syntax belongs to Chromium: ``HostMappingRules`` parses it, and a second
    parser here would drift from it the first time either changed. So this only
    rejects what cannot be a rule string at all -- a non-string, an empty
    entry -- and leaves the grammar to the Core, which reports what it could not
    parse.

    Args:
        rules: A string of rules separated by newlines (Chromium also accepts
            commas), a list of such strings, or ``None``.

    Returns:
        A newline-joined string, or ``None`` when nothing was given.

    Raises:
        UnsupportedFeature: The value is not a rule string or a list of them,
            or one entry is empty.

    Example:
        >>> normalize_host_mapping("MAP example.test 93.184.216.34")
        'MAP example.test 93.184.216.34'
        >>> normalize_host_mapping(["MAP a.test 10.0.0.1", "MAP b.test 10.0.0.2"])
        'MAP a.test 10.0.0.1,MAP b.test 10.0.0.2'
        >>> normalize_host_mapping(None) is None
        True
    """
    if rules is None:
        return None
    if isinstance(rules, str):
        entries = [line.strip() for line in rules.replace(",", "\n").splitlines()]
    elif isinstance(rules, (list, tuple)):
        entries = []
        for item in rules:
            if not isinstance(item, str):
                raise UnsupportedFeature(
                    "host_mapping entries must be strings, not %r" % (item,))
            entries.append(item.strip())
    else:
        raise UnsupportedFeature(
            "host_mapping accepts a rule string or a list of them, not %r" % (rules,))
    kept = [entry for entry in entries if entry]
    if not kept:
        raise UnsupportedFeature(
            "host_mapping was given but contains no rule; drop the argument to "
            "use the system resolver")
    for entry in kept:
        _check_host_mapping_rule(entry)
    # Chromium splits this string on commas
    # (net/base/host_mapping_rules.cc, SetRulesFromString -> SplitStringPiece(",")),
    # not on newlines, so a newline-joined list reaches it as one unparsable
    # rule. It then logs and skips it -- SetRulesFromString has no failure
    # return -- and the request silently resolves through the real DNS instead.
    # Joining with a comma is what makes a multi-rule list actually work.
    return ",".join(kept)


#: The two rule keywords Chromium accepts
#: (net/base/host_mapping_rules.cc, AddRuleFromString). Anything else is
#: rejected there by logging and returning false, and SetRulesFromString has no
#: way to report that -- so the check has to be here or a typo resolves through
#: the real DNS as if no rule had been given.
_HOST_MAPPING_KEYWORDS = ("map", "exclude")


def _check_host_mapping_rule(rule):
    """Rejects a rule Chromium would parse and then silently drop.

    Chromium takes exactly two shapes: ``MAP <pattern> <host>[:<port>]`` and
    ``EXCLUDE <pattern>``, split on single spaces. Checking the shape here is
    not a second implementation of the grammar -- it is the only way to report
    a mistake, because ``HostMappingRules::SetRulesFromString`` cannot fail:
    it logs ``Failed parsing rule`` and moves on, so a misspelled rule means the
    request resolves normally and the mapping looks like it worked.

    Args:
        rule: One rule, already stripped.

    Raises:
        UnsupportedFeature: The rule is not one of the two accepted shapes.
    """
    parts = rule.split()
    keyword = parts[0].lower() if parts else ""
    if keyword == "map":
        if len(parts) != 3:
            raise UnsupportedFeature(
                "host_mapping rule %r needs exactly MAP <pattern> <host>[:port]"
                % (rule,))
        return
    if keyword == "exclude":
        if len(parts) != 2:
            raise UnsupportedFeature(
                "host_mapping rule %r needs exactly EXCLUDE <pattern>" % (rule,))
        return
    raise UnsupportedFeature(
        "host_mapping rule %r must start with MAP or EXCLUDE; Chromium logs and "
        "skips anything else, so it would be accepted and never applied" % (rule,))

def normalize_dns_nameservers(value):
    """Validates the shape of a nameserver list.

    Chromium takes ``host[:port]`` entries separated by commas and parses each
    one itself; this only rejects values that cannot be an entry list at all.

    Args:
        value: A comma-separated string, a list of strings, or ``None``.

    Returns:
        A comma-joined string, or ``None``.

    Raises:
        UnsupportedFeature: The value is not a string or string list.
    """
    return _join_entries(value, "dns_nameservers")


def normalize_doh(doh_template, secure_mode):
    """Validates a DoH template together with its secure-DNS mode.

    A template with mode 0 is a contradiction -- one says DoH may be used, the
    other forbids it -- and the Core rejects it. Reporting it here names both
    arguments instead of surfacing a bare ``InvalidArgument`` from engine
    construction.

    Args:
        doh_template: An ``https://`` DoH template, or ``None``.
        secure_mode: ``None``/0 off, 1 opportunistic, 2 secure.

    Returns:
        ``(doh_template, secure_mode)``.

    Raises:
        UnsupportedFeature: The mode is out of range, or a template has no mode.
    """
    mode = 0 if secure_mode is None else int(secure_mode)
    if mode < 0 or mode > 2:
        raise UnsupportedFeature(
            "dns_secure_mode must be 0 (off), 1 (opportunistic) or 2 (secure), "
            "not %r" % (secure_mode,))
    if doh_template is None:
        return None, mode
    if not isinstance(doh_template, str) or not doh_template.strip():
        raise UnsupportedFeature(
            "doh_template must be a non-empty https:// template, not %r"
            % (doh_template,))
    if mode == 0:
        raise UnsupportedFeature(
            "doh_template= needs dns_secure_mode=1 or 2: a template with secure "
            "DNS off says both \"DoH may be used\" and \"DoH must not be used\"")
    return doh_template.strip(), mode


def normalize_hsts_bypass(value):
    """Validates an HSTS-preload bypass list.

    Chromium's TransportSecurityState matches bypass entries by single label
    and silently ignores anything else in a release build, so a dotted entry
    would be accepted and never applied. The Core rejects those; this reports
    which entry and why.

    Args:
        value: A comma-separated string, a list of strings, or ``None``.

    Returns:
        A comma-joined string, or ``None``.

    Raises:
        UnsupportedFeature: An entry is empty or contains a dot.
    """
    joined = _join_entries(value, "hsts_bypass")
    if joined is None:
        return None
    for entry in joined.split(","):
        if "." in entry:
            raise UnsupportedFeature(
                "hsts_bypass entry %r must be a single label: Chromium matches "
                "these by label and silently ignores a dotted one" % (entry,))
    return joined


def normalize_proxy_chain(value):
    """Validates a multi-hop proxy chain.

    ``proxy=`` and ``proxy_chain=`` describe the same connection path twice,
    and the Core refuses both at once rather than silently picking one.

    Args:
        value: Comma-separated ``scheme://host:port`` entries, or ``None``.

    Returns:
        The stripped string, or ``None``.

    Raises:
        UnsupportedFeature: The value is not a non-empty string.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise UnsupportedFeature(
            "proxy_chain must be a comma-separated list of scheme://host:port "
            "entries, not %r" % (value,))
    # The Core parses each entry itself and, on anything it cannot turn into a
    # ProxyServer, gives up and leaves the context unbuilt -- the caller then
    # sees InitializationFailed with no mention of the chain. Rejecting the
    # shape here names the option instead.
    for entry in (part.strip() for part in value.split(",")):
        scheme, separator, rest = entry.partition("://")
        if not separator or not rest.rsplit(":", 1)[-1].isdigit() \
                or ":" not in rest:
            raise UnsupportedFeature(
                "proxy_chain entry %r is not scheme://host:port" % (entry,))
        if scheme.lower() not in _PROXY_SCHEMES:
            raise UnsupportedFeature(
                "proxy_chain entry %r uses scheme %r; Chromium accepts %s"
                % (entry, scheme, ", ".join(_PROXY_SCHEMES)))
    return value.strip()


def normalize_platform(value):
    """Validates the platform identity the engine presents.

    The User-Agent group, ``sec-ch-ua-platform`` and ``sec-ch-ua-mobile`` are
    three statements of one fact, so all three derive from this value. AUTO
    follows the host the Core runs on.

    Args:
        value: ``"auto"``, ``"windows"``, ``"macos"`` or ``"linux"``.

    Returns:
        The lower-cased name, or ``"auto"`` when unset.

    Raises:
        UnsupportedFeature: The name is not one of the four.
    """
    name = "auto" if value is None else str(value).strip().lower()
    if name not in ("auto", "windows", "macos", "linux"):
        raise UnsupportedFeature(
            "platform must be auto, windows, macos or linux, not %r" % (value,))
    return name


def normalize_max_sockets(value):
    """Validates the per-origin connection ceiling.

    0 keeps Chromium's own limit (6 per origin for normal traffic). Raising it
    is an observable deviation from the browser being imitated -- real Chrome
    never opens a seventh connection to one origin -- so it stays opt-in.

    Args:
        value: ``0`` for Chromium's default, otherwise 1..256.

    Returns:
        The value as an int.

    Raises:
        UnsupportedFeature: The value is negative or above the pool ceiling.
    """
    limit = int(value or 0)
    if limit < 0 or limit > 256:
        raise UnsupportedFeature(
            "max_sockets_per_group must be 0 (Chromium default) or 1..256, "
            "not %r" % (value,))
    return limit


#: Cache modes defined in terms of what this session's HTTP cache already
#: holds.  A ``discard_cookies`` request runs on its own engine (see
#: :meth:`BaseSession._throwaway_slot`), and an engine owns its ``HttpCache``
#: partition (core/source/engine.cc:863-869, EnableHttpCache on the per-engine
#: URLRequestContext), so that cache is always empty. Asking for one of these
#: on such a request would silently answer a different question:
#: ``only_if_cached`` turns a local read into a network round trip and then
#: reports ERR_CACHE_MISS, and ``force`` re-fetches what it promised to take
#: from the cache. AGENTS section 8: refuse what cannot be honoured.
_DISCARD_INCOMPATIBLE_CACHE_MODES = ("only_if_cached", "force")


#: One shared worker for dropping throwaway engines off the caller's thread.
#: ``mn_engine_release`` posts a shutdown task to the Core's network thread and
#: waits on a WaitableEvent (core/source/engine.cc:493-521), so the drop of the
#: last reference blocks for a full round trip. On the asyncio loop that is a
#: loop stall proportional to the number of discard requests in flight: measured
#: 83 ms at 16 concurrent discards, against 2.5 ms for the same load without
#: discard_cookies. One daemon thread, not a pool: the work is a single wait and
#: a pool would just add queueing.
_RETIRED = queue.Queue()


def _retire_worker():
    """Drops retired engines until the process ends."""
    while True:
        slot = _RETIRED.get()
        try:
            if slot is not None:
                # release_engine() drops the reference and is idempotent, so a
                # slot that reached the queue twice cannot raise here.
                slot.release_engine()
        except Exception:
            # Nothing here may kill the worker: it serves every session.
            pass
        finally:
            _RETIRED.task_done()


def _start_retire_worker():
    """Starts the retirement worker once per process."""
    thread = threading.Thread(target=_retire_worker, name="chrome-client-retire",
                              daemon=True)
    thread.start()


_RETIRE_START_LOCK = threading.Lock()
_retire_started = False


def retire_engine_slot(slot):
    """Drops a throwaway engine without blocking the calling thread.

    The Core releases a URLRequestContext on its own network thread and this call
    waits for that, so doing it inline parks whichever thread called it -- for an
    async request that is the event loop. The slot is handed to a daemon worker
    instead; the engine is unreachable from anywhere else once the request that
    built it returns, which is what makes releasing it later safe.

    Thread-safe and idempotent: the first caller starts the worker under a lock,
    so two threads cannot start two workers and leave one of them parked on a
    queue nothing will ever fill.

    Args:
        slot: An :class:`EngineSlot` nobody else holds, or ``None``.

    Returns:
        ``None``.
    """
    if slot is None:
        return
    global _retire_started
    if not _retire_started:
        with _RETIRE_START_LOCK:
            if not _retire_started:
                _start_retire_worker()
                _retire_started = True
    _RETIRED.put(slot)


def check_discard_cache_mode(discard, cache_mode):
    """Refuses cache modes a discard request cannot honour.

    Args:
        discard: Whether this request asked for "neither send nor record".
        cache_mode: The ``cache_mode=`` value, or ``None``.

    Returns:
        ``None``.

    Raises:
        UnsupportedFeature: The mode reads a cache this engine cannot have.

    Example:
        >>> check_discard_cache_mode(True, "no_store") is None
        True
    """
    if not discard or cache_mode is None:
        return None
    if cache_mode in _DISCARD_INCOMPATIBLE_CACHE_MODES:
        raise UnsupportedFeature(
            "cache_mode=%r with discard_cookies=True: a discard request runs on "
            "its own engine, whose HTTP cache is empty by construction, so %r "
            "would silently do something else. Use cache_mode='default', "
            "'no_store' or 'bypass' for a discard request, or drop "
            "discard_cookies=True and read the session's cache."
            % (cache_mode, cache_mode))
    return None


def _join_entries(value, name):
    """Normalises a comma-separated entry list shared by two options.

    Args:
        value: A string, a list of strings, or ``None``.
        name: The argument name, for the error message.

    Returns:
        A comma-joined string, or ``None``.

    Raises:
        UnsupportedFeature: The value is not a string or a list of them.
    """
    if value is None:
        return None
    if isinstance(value, str):
        entries = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        entries = []
        for item in value:
            if not isinstance(item, str):
                raise UnsupportedFeature(
                    "%s entries must be strings, not %r" % (name, item))
            entries.append(item.strip())
    else:
        raise UnsupportedFeature(
            "%s accepts a comma-separated string or a list of them, not %r"
            % (name, value))
    kept = [entry for entry in entries if entry]
    if not kept:
        raise UnsupportedFeature(
            "%s was given but contains no entry; drop the argument to keep the "
            "system setting" % name)
    return ",".join(kept)

def _read_ca_bundle(path):
    """Reads a CA bundle the caller named with ``verify=``.

    ``verify="/missing.pem"`` used to escape as a bare ``FileNotFoundError`` from
    inside ``open()``: not a ``RequestException``, so ``except RequestException``
    around a request missed it, and the message named no option. ``cert=`` has
    always reported the same situation as a ``RequestException`` naming its
    option, so this path does too.

    Args:
        path: Filesystem path to read.

    Returns:
        The file contents as bytes.

    Raises:
        SSLError: The bundle could not be read.
    """
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as error:
        raise SSLError(
            "verify= could not read the CA bundle %r: %s" % (path, error))


def normalize_client_cert(cert):
    """Turns ``cert=`` into ``(cert_pem, key_pem)`` bytes, or ``None``.

    Accepts the two spellings ``requests`` and ``curl_cffi`` both use:

    * ``"combined.pem"`` -- one file holding the certificate chain and its key.
      The Core separates the blocks itself (``PemBlocksToDer``), so the file
      is passed through whole rather than split here.
    * ``("cert.pem", "key.pem")`` -- the two halves in separate files.

    Returns:
        ``None`` when unset, otherwise ``(cert_pem, key_pem)``.

    Raises:
        UnsupportedFeature: The value is not one of those two shapes, or a
            file could not be read.
    """
    if cert is None:
        return None
    if isinstance(cert, (str, bytes, os.PathLike)) and not isinstance(cert, tuple):
        combined = _read_pem(cert, "combined certificate")
        # The Core needs a key in the same buffer; passing the combined file as
        # both halves is how a single-file cert= reaches it. PemBlocksToDer
        # picks the CERTIFICATE blocks out of one and the PrivateKey out of the
        # other, so the duplication costs one memcpy and keeps this side free
        # of PEM splitting rules.
        return combined, combined
    if isinstance(cert, tuple) and len(cert) == 2:
        return _read_pem(cert[0], "certificate"), _read_pem(cert[1], "private key")
    raise UnsupportedFeature(
        "cert= accepts a combined PEM path or a (cert, key) pair of paths, "
        "not %r" % (cert,))

# Kept as a private alias: the regression suite imports this name.
_proxy_from_proxies = proxy_from_proxies


def merge_setting(request_setting, session_setting, dict_class=CaseInsensitiveDict):
    """requests' merge rule: request wins, and a ``None`` value deletes a key.

    When ``dict_class`` is :class:`Headers`, a deletion recorded by the request
    side is carried onto the result.  ``requests`` gets this for free because a
    deletion is just a ``None`` value in the merge; ``Headers`` applies the
    deletion immediately, so the record has to be forwarded explicitly or the
    session-level default of that name comes back.

    Args:
        request_setting: The per-request mapping, or ``None``.
        session_setting: The session-level mapping, or ``None``.
        dict_class: The mapping type to build.

    Returns:
    The merged mapping.
    """
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
    recorded = getattr(request_setting, "deleted_names", None)
    if recorded:
        # Headers applies a deletion immediately, so the merge above never sees
        # it as a None value.  Without this the session-level default of that
        # name survived: measured, Session(headers={"X-Gone": "yes"}).get(url,
        # headers={"X-Gone": None}) still sent X-Gone: yes.
        for name in recorded:
            merged.pop(name, None)
        if hasattr(merged, "deleted_names"):
            for name in recorded:
                merged._remove(name)
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
                 "wake", "timer", "closed", "throttled", "hops", "follow",
                 "http_version", "header_fields", "reason_phrase", "ech_status")

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
        #: The protocol the Core reported, or ``None`` when it did not know.
        self.http_version = None
        #: Parsed header fields from the Core, or ``None`` before a response.
        self.header_fields = None
        #: Reason phrase from the Core, or ``None`` before a response.
        self.reason_phrase = None
        #: ECH status the Core reported, or ``None`` before a response.
        self.ech_status = None
        self.body = None if stream else []
        self.wake = asyncio.Event() if stream else None
        self.closed = False
        self.throttled = False
        self.hops = []
        self.follow = follow
        self.timer = loop.call_later(timeout, self._expire) if timeout else None

    # -- loop-thread callbacks ---------------------------------------------
    # There is deliberately no bulk drain-everything helper here. One used to
    # exist and nothing called it; worse, it cleared the buffered count and the
    # throttled flag without re-arming the reader. Once a stream is throttled the
    # Core has already stopped reading and will not post another event, so
    # clearing that flag is the one operation that cannot be undone by hand: the
    # request would stay asleep forever. resume() below is the only place that
    # clears it, and it re-arms with call_soon.

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
        for kind, code, raw_headers, chunk, message, fields in events:
            if kind == "body":
                self._on_body(chunk)
            elif kind == "response":
                self.status = code
                self.raw_headers = raw_headers
                # The protocol is a getter on the request, not part of the
                # event tuple: only the head needs it, and widening the tuple
                # would change a shape several places unpack positionally.
                self.http_version = self.request.http_version()
                self.header_fields = self.request.header_fields()
                self.reason_phrase = self.request.reason_phrase()
                self.ech_status = self.request.ech_status()
                if self.stream:
                    self._signal()
            elif kind == "redirect":
                self._on_redirect(code, raw_headers, chunk, fields)
            elif kind == "done":
                self._on_done()
            elif kind == "error":
                self._fail(map_native_error(message or "request failed"))
            if self.closed:
                return
        if len(events) == ASYNC_POLL_BATCH:
            # The ceiling, not an empty queue, ended the drain, so nothing else
            # will wake us: re-arm explicitly.  Measured on a 4 MiB streamed
            # body this branch ran once in 156 drains; it exists for the case
            # where several requests' events are queued at once, which is the
            # only way this request can still have work left after a full batch.
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
            # A list of the Core's own buffers, joined once when the response is
            # assembled.  Extending a bytearray copied every chunk on arrival and
            # then copied the whole body again on ``bytes(...)``, so a large
            # download paid two full copies; joining once pays one.
            self.body.append(chunk)

    def _on_redirect(self, status_code, raw_headers, new_url, fields=None):
        if self.follow:
            self.hops.append((status_code, raw_headers, bytes(new_url).decode(
                "utf-8", "replace"), "", fields))
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


class _SessionJarSink(object):
    """Forwards a jar mutation to the session's engine store.

    A jar can be created before any engine exists (``Session(cookies=...)``)
    and can outlive one (the cache evicts), so the sink resolves the session per
    call rather than capturing a slot. The three methods are exactly what
    :class:`~.cookies.RequestsCookieJar` calls.

    The session is held weakly, and that is what makes the whole object graph
    collectable by reference counting instead of waiting for a cyclic
    collection. A strong reference here closes a cycle --
    session -> jar -> sink -> session -- and an object in a cycle never reaches
    a refcount of zero even after every external reference is gone. The session
    holds its native resources as attributes (``_engines`` -> ``EngineSlot`` ->
    the Chromium context and its socket pool), so nothing releases them until the
    cyclic collector visits that generation. Measured with the collector
    disabled: 200 create/request/close cycles left 200 sessions and 403 open
    descriptors, all reclaimed by a single ``gc.collect()``. With the weak
    reference the same loop leaves 0.
    """

    __slots__ = ("_session",)

    def __init__(self, session):
        """Binds the sink to the session whose store it mirrors.

        Args:
            session: The owning :class:`BaseSession`.
        """
        self._session = weakref.ref(session)

    def _owner(self):
        """Resolves the session, or refuses once it is gone.

        Returns:
            The live session.

        Raises:
            SessionClosed: The session was collected; only the jar is left.
        """
        session = self._session()
        if session is None:
            raise SessionClosed("Session was closed")
        return session

    def write_cookie(self, cookie, source_url):
        """Writes one cookie through to the store.

        Under the session's cookie lock, because the jar retires the scopes a
        caller assignment covers and then writes the replacement: two threads
        interleaving those steps can retire each other's work, so the pair has
        to be one critical section rather than two.

        Args:
            cookie: A ``cookielib.Cookie``.
            source_url: The URL the cookie came from.
        """
        session = self._owner()
        with session._cookie_lock:
            session._store_slot().write_cookie(cookie, source_url)

    def remove_cookie(self, domain, path, name):
        """Deletes one cookie from the store.

        Args:
            domain: Cookie domain.
            path: Cookie path.
            name: Cookie name.
        """
        session = self._owner()
        with session._cookie_lock:
            session._store_slot().remove_cookie(domain, path, name)

    def clear_cookies(self):
        """Empties the store."""
        session = self._owner()
        with session._cookie_lock:
            session._store_slot().clear_cookies()


class BaseSession(object):
    """Shared configuration, engine selection, and cookie policy."""

    @property
    def max_sockets_per_group(self):
        """Per-origin TCP connection ceiling for engines this session builds.

        Returns:
            ``0`` for Chromium's own limit, otherwise 1..256.
        """
        return self._max_sockets_per_group

    @max_sockets_per_group.setter
    def max_sockets_per_group(self, value):
        """Validates the ceiling on every assignment, not only in the constructor.

        ``normalize_max_sockets`` used to run once, against the constructor
        argument. Assigning the attribute afterwards put the raw value straight
        into the engine cache key, and the Core rejected anything above its
        per-pool soft cap with ``MN_ERROR_INVALID_ARGUMENT`` -- which
        ``map_native_error`` reports as ``InvalidURL``, telling the caller its URL
        was malformed when the URL had nothing to do with it. Measured:
        ``session.max_sockets_per_group = 9999`` then a request raised
        ``InvalidURL``.

        Args:
            value: ``0`` for Chromium's default, otherwise 1..256.

        Raises:
            UnsupportedFeature: The value is negative or above the pool ceiling.
        """
        self._max_sockets_per_group = normalize_max_sockets(value)

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
                 allow_redirects=True, max_response_bytes=None,
                 strip_authorization=False, take_body_buffers=False,
                 base_url=None,
                 http_version=None, ja3=None, akamai=None, perk=None, extra_fp=None,
                 default_headers=True, default_encoding="utf-8",
                 discard_cookies=False, raise_for_status=False, retry=0, cache=True,
                 user_agent=None, accept_language=None, interface=None, doh_url=None,
                 max_recv_speed=0, curl_options=None, max_engines=DEFAULT_MAX_ENGINES,
                 response_class=None, host_mapping=None, dns_nameservers=None,
                 doh_template=None, dns_secure_mode=None, dns_over_tls=False,
                 dns_rotate=False, dns_padding=False, hsts_bypass=None,
                 proxy_chain=None, platform="auto", max_sockets_per_group=0):
        """Configures defaults shared by requests made through this session.

        ``impersonate`` selects a pinned Chromium profile, ``http_version`` can
        pin ``v1``, ``v2`` or ``v3``, and ``proxy``/``proxies`` select the route.
        ``verify`` accepts a boolean or CA bundle path; ``timeout`` accepts one
        deadline or a ``(connect, read)`` pair. Request methods may override these
        defaults for one call. Compatibility-only options unsupported by ABI v8
        raise ``UnsupportedFeature`` instead of being silently ignored.

        ``max_redirects`` must stay ``None``: Chromium's 20-hop ceiling is the only
        limit this build can honour, and ABI v8 exposes no way to lower it. A
        numeric cap raises ``UnsupportedFeature`` rather than re-issuing the hops
        from Python, which would put redirect semantics in the binding layer where
        they would drift from Chromium's.
        """
        reject_fingerprint_overrides(ja3, akamai, perk)
        for name, value in (("interface", interface), ("doh_url", doh_url),
                            ("curl_options", curl_options)):
            if value:
                raise UnsupportedFeature(
                    "%s= is not supported: ABI v11 exposes no Core setting for it" % name)
        if max_recv_speed:
            raise UnsupportedFeature(
                "max_recv_speed= is not supported: Chromium owns transfer pacing")
        self.header_order, self.form_boundary = validate_extra_fp(extra_fp)

        self.impersonate = normalize_impersonate(impersonate)
        self.http_version = normalize_http_version(http_version)
        self.proxy = proxy
        self.proxies = dict(proxies) if proxies else {}
        self.proxy_auth = proxy_auth
        # ``None`` is not a documented value here, and bool(None) is False:
        # passing one silently turned certificate verification OFF at the
        # engine, which is the one setting where a silent downgrade is
        # unacceptable. The constructor default is True, so an explicit None
        # is read as "leave it at the default" rather than "disable it".
        self.verify = True if verify is None else verify
        #: Chromium MappedHostResolver rules, e.g. "MAP example.test 93.184.216.34".
        #: A rule rewrites only the resolution result: SNI, the Host header and
        #: certificate verification keep using the URL host, which is what makes a
        #: CDN IP reachable without turning its certificate into an IP-SAN one.
        self.host_mapping = normalize_host_mapping(host_mapping)
        #: Resolver configuration. Each of these makes the Core build a
        #: standalone HostResolver instead of using the system one, which is why
        #: they are engine-level and part of the cache key.
        self.dns_nameservers = normalize_dns_nameservers(dns_nameservers)
        self.doh_template, self.dns_secure_mode = normalize_doh(
            doh_template, dns_secure_mode)
        self.dns_over_tls = bool(dns_over_tls)
        self.dns_rotate = bool(dns_rotate)
        self.dns_padding = bool(dns_padding)
        self.hsts_bypass = normalize_hsts_bypass(hsts_bypass)
        # The ABI rejects both at once (core/abi/minicronet.h: proxy_chain and
        # proxy_rules "describe the same connection path twice"), but the
        # rejection used to arrive with the FIRST REQUEST as InvalidURL /
        # InvalidArgument -- a URL error for a configuration conflict. The
        # combination is decidable here, so it is refused here.
        if proxy is not None and proxy_chain is not None:
            raise InvalidProxyURL(
                "proxy= and proxy_chain= both describe the connection path; "
                "set one of them")
        self.proxy_chain = normalize_proxy_chain(proxy_chain)
        self.platform = normalize_platform(platform)
        self.max_sockets_per_group = max_sockets_per_group
        #: Client certificate for mTLS, parsed to (cert_pem, key_pem) bytes.
        #: The certificate is registered on the engine per (host, port) just
        #: before a request needs it -- see _register_client_cert -- because the
        #: Core keys it by server and an engine may be shared by many hosts.
        self.cert = normalize_client_cert(cert)
        #: Per-(host, port) client certificates, registered at runtime.
        self._client_certs = {}
        #: Constructor ``cert=`` applies to every server this session reaches.
        #: The host is only known when a request is made, so it is kept here
        #: and resolved per request rather than keyed by host at construction.
        self._client_cert_default = self.cert
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
        # Each of these is documented as a flag with a True/False default, never
        # None. bool(None) is False, so an explicit None silently SELECTED the
        # off state -- measured: cache=None disabled the HTTP cache, and
        # default_headers=None dropped every captured browser header. None is
        # read here as "leave the default", the same rule verify= follows.
        self.trust_env = True if trust_env is None else trust_env
        self.allow_redirects = True if allow_redirects is None else allow_redirects
        #: Drop a caller-supplied ``Authorization`` when a redirect changes the
        #: origin. Off by default: this was silently broken before ABI 12 (the
        #: header reached the new host), so turning it on is a behaviour change
        #: callers opt into. See docs/COMPATIBILITY_BOUNDARY.md.
        self.strip_authorization = bool(strip_authorization)
        #: Wrap the Core body buffer instead of copying it, handing the memory
        #: back through the ABI when the chunk is dropped. Off by default: the
        #: copy is about 1.6% of a 64 MiB local stream, so this exists for the
        #: allocation behaviour, not for throughput.
        self.take_body_buffers = bool(take_body_buffers)
        self.base_url = base_url
        self.default_encoding = default_encoding
        self.discard_cookies = discard_cookies
        self.raise_for_status = raise_for_status
        self.retry = RetryStrategy.coerce(retry)
        self.cache = True if cache is None else cache
        self.user_agent = user_agent
        self.accept_language = accept_language
        self.response_class = response_class
        self.max_response_bytes = _validate_max_response_bytes(max_response_bytes)
        self.extra_fp = extra_fp

        #: Whether the profile's captured browser headers are injected.  Stored,
        #: not just consumed at construction: :meth:`_browser_headers` reads it on
        #: every request, and a session that says "no default headers" must keep
        #: saying so for its whole life.
        self.default_headers = True if default_headers is None else bool(default_headers)
        self.headers = Headers(_default_headers() if self.default_headers else None)
        if headers:
            self.headers.update(headers)
        self.cookies = cookiejar_from_dict(cookies) \
            if not isinstance(cookies, RequestsCookieJar) else cookies
        # The jar writes through to the engine's store (ABI v9), so a caller
        # edit reaches the wire without a reconciliation step. The sink is
        # resolved lazily because the engine does not exist yet at construction
        # -- see _SessionJarSink.
        self.cookies._sink = _SessionJarSink(self)
        #: Cookies that arrived before the sink existed (Session(cookies=...))
        #: and have not been written to a store yet. Flushed once, before the
        #: first request that has a URL to give them as their source.
        self._pending_store_cookies = True
        #: Slots whose store already mirrors the jar.
        #:
        #: Keyed by the slot itself, not by ``id(slot)``. CPython reuses an id
        #: once the object is freed, so an id-keyed set can mistake a brand-new
        #: slot -- empty store, same recycled id -- for one already seeded, and
        #: skip it. Measured: after the engine cache cycled with max_engines=1,
        #: the session's cookie stopped being sent. A strong reference also
        #: keeps the entry alive, which is correct: the slot is alive for as long
        #: as it is cached, and that is exactly when the entry is meaningful.
        #:
        #: A dropped slot keeps itself out of this set: the cache holds the only
        #: other reference, so eviction frees the object and CPython removes the
        #: entry. Measured over 60 engine cycles with max_engines=1 (59
        #: evictions): the set held 1 entry, so no pruning pass is needed.
        self._seeded_slots = set()
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
        #: Serialises operations that read the store and then act on what they
        #: read -- accept a Set-Cookie line, honour a jar edit, or clear the
        #: store for 'discard_cookies'.  Each is several store calls that only
        #: make sense together, and two threads interleaving them would see a
        #: half-applied state.
        self._cookie_lock = threading.RLock()
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
        """Merges session defaults into a ``Request`` and returns a ``PreparedRequest``.

        When the session names a profile, the header set that profile's own Chrome
        sent is merged in underneath the caller's headers -- see
        :meth:`_browser_headers`.  This is the single place both sessions pass
        through, so the synchronous and asynchronous paths cannot disagree about
        what the wire looks like.

        Args:
            request: The caller's :class:`Request`.

        Returns:
            A :class:`PreparedRequest` with session defaults applied.
        """
        cookies = request.cookies or {}
        if not isinstance(cookies, RequestsCookieJar):
            cookies = cookiejar_from_dict(cookies)
        # merged_cookies exists only for requests-compatibility: it is what a
        # caller inspecting prepared._cookies expects to see. It is NOT what goes
        # on the wire -- the Core CookieMonster owns the Cookie header -- and
        # building it is not free: it copies the whole session jar, and copying
        # through set_cookie writes every entry through to the store again.
        # Measured with 200 session cookies: 200 redundant store writes and 200
        # redundant lock acquisitions per request, p50 468 ms against 12 ms with an
        # empty jar, 17 req/s against 614, and 17 req/s under 8 threads. So the
        # copy is built only when the caller actually asked for per-request
        # cookies; the common case passes None through.
        if cookies:
            merged_cookies = merge_cookies(
                merge_cookies(RequestsCookieJar(), self.cookies), cookies)
        else:
            merged_cookies = RequestsCookieJar()
        auth = request.auth
        if auth is None and self.trust_env:
            auth = get_netrc_auth(request.url)
        prepared = PreparedRequest()
        prepared.header_order = (getattr(request, "header_order", None)
                                 or self.header_order)
        prepared.quote = getattr(request, "quote", None)
        headers = self._browser_headers(request, prepared)
        prepared.prepare(
            method=request.method.upper() if request.method else "GET",
            url=self._absolute_url(request.url),
            files=request.files,
            data=request.data or None,
            json=request.json,
            headers=headers,
            params=merge_setting(request.params, self.params, dict_class=dict),
            auth=build_auth(auth) or self.auth,
            cookies=merged_cookies,
            hooks=merge_hooks(request.hooks, self.hooks),
        )
        # The per-request additions are exactly the caller-supplied cookies: the
        # session jar is already in the store (the jar is a view of it since ABI
        # v9), so writing it again was a no-op that still took the lock and made a
        # store call per cookie per request.
        prepared._cookie_additions = list(cookies) if cookies else []
        # The jar mirrors the store; remembering the resolved URL lets a caller
        # assignment that names no domain inherit a source for the store to
        # check it against.
        self.cookies.default_source = prepared.url
        self._flush_pending_store_cookies()
        return prepared

    def _browser_headers(self, request, prepared):
        """Merges the profile's captured header set under the caller's headers.

        ``impersonate=`` used to own only the ``User-Agent`` version string, so a
        ``chrome_155`` session sent four headers where that release sends twelve.
        The set now comes from ``profiles/chrome_<major>/h1/captures.json`` -- six
        independent captures of that release's own build, all identical -- via
        :mod:`.browser_headers`, and it is applied here rather than in the Core
        because ``net::URLRequestContextBuilder`` exposes no hook for it.

        Precedence, lowest first: the captured set, the session's ``headers``, the
        request's ``headers``.  A caller therefore overrides one field without
        losing the rest, which is what makes ``Sec-Fetch-*`` usable for a
        subresource fetch: pass the two fields you need and the other ten stay.

        Two things are deliberately *not* injected:

        * Nothing at all when ``impersonate`` is ``None`` or ``default_headers=False``.
          A session that never claimed a profile must not start looking like Chrome,
          and the switch is how a caller who wants the bare transport turns it off.
        * ``Accept-Language`` keeps the caller's value when the session set
          ``accept_language=``.  The Core emits that field itself, so injecting a
          second copy would send the header twice.

        Args:
            request: The caller's :class:`Request`, for its profile and headers.
            prepared: The :class:`PreparedRequest` being built; its
                ``header_order`` is set from the profile when the caller did not
                pick an order of its own.

        Returns:
            The merged :class:`Headers`.
        """
        profile = getattr(request, "impersonate", None) or self.impersonate
        # ``merge_setting`` implements requests' "a None value deletes a key" by
        # dropping the entry, which also erases the caller's *intent* to delete.
        # Without collecting it first, ``headers={"Sec-Fetch-User": None}`` did
        # nothing: the captured value came back on the next line.
        dropped = self._deleted_headers(request.headers)
        caller = merge_setting(request.headers, self.headers, dict_class=Headers)
        if not self.default_headers or not profile:
            return caller
        accept_language = self.accept_language
        if not accept_language and caller is not None:
            # The Core owns Accept-Language when the session set one; only inject
            # the captured value when nothing else will produce the header.
            accept_language = None
        captured = browser_headers(profile, accept_language, self.platform)
        if not captured:
            return caller
        if caller is not None or dropped:
            captured = [(name, value) for name, value in captured
                        if name.lower() not in caller and name.lower() not in dropped]
        merged = Headers(captured)
        if caller is not None:
            merged.update(caller)
        if not prepared.header_order:
            prepared.header_order = browser_header_order(profile)
        return merged

    @staticmethod
    def _deleted_headers(headers):
        """Returns the lower-cased names the caller asked to remove.

        ``requests`` spells deletion as ``headers={"Name": None}``.  A
        :class:`Headers` records the removal in ``deleted_names``; a plain
        mapping still carries the ``None`` values, so both spellings are read.
        Without this the injected default came straight back:
        ``get(url, headers={"Sec-Fetch-User": None})`` still sent the header.

        Args:
            headers: The caller's header mapping, or ``None``.

        Returns:
            A set of lower-cased header names.
        """
        if headers is None:
            return set()
        recorded = getattr(headers, "deleted_names", None)
        if recorded is not None:
            return set(recorded)
        try:
            items = headers.items()
        except AttributeError:
            return set()
        return {str(name).lower() for name, value in items if value is None}

    def _absolute_url(self, url):
        if self.base_url and not urlsplit(str(url)).scheme:
            return urljoin(self.base_url, str(url))
        return url

    def close(self):
        """Closes the session, releasing every engine it owns.

        Idempotent.  A request already in flight still completes: the Core's
        request object holds its own reference to the Chromium context, so
        closing the session ends the reuse of the pool, not the work running on
        it.  What a closed session refuses is a *new* request, which
        ``_ensure_open`` enforces.

        Returns:
            ``None``.
        """
        self._closed = True
        self._engines.close()
        # The cache just dropped its slots, but _seeded_slots is a second strong
        # reference to the same EngineSlot objects, and _prune_seeded_slots only
        # runs when a not-yet-seeded slot appears -- which a closed session never
        # produces. Left alone, close() would release the pool and keep every
        # engine, and its sockets, alive until the cyclic collector. Measured:
        # after close(), stats reported live=0 while the PyEngine count was still
        # 1 and its five sockets were still open.
        self._seeded_slots.clear()
        for adapter in self.adapters.values():
            try:
                adapter.close()
            except NotImplementedError:
                pass

    @property
    def stats(self):
        """Returns the transport counters for this session.

        ``builds`` counts engines that had to be constructed, ``hits`` the
        requests that reused one, and ``live`` how many are cached now.  A
        ``builds`` that keeps climbing is the signature of a per-request
        transport change -- a rotating proxy pool wider than ``max_engines``, for
        instance -- and every one of those builds is a fresh Chromium context and
        a fresh TCP connection (docs/LESSONS.md section 29).  This counter makes
        that cost visible instead of leaving it to be discovered as
        ``ERR_PROXY_CONNECTION_FAILED`` once the ephemeral port range runs out.

        Returns:
            A dict with ``live``, ``hits``, ``builds``, ``evictions``,
            ``deferred``, ``inflight``, ``builds`` and ``max_engines``.
        """
        snapshot = self._engines.stats
        snapshot["inflight"] = self._engines.inflight()
        snapshot["max_engines"] = self._engines.max_engines
        return snapshot

    def upkeep(self):
        """curl_cffi parity hook. Chromium keeps idle sockets warm itself.

        Returns:
            Always ``0``: there is no facade-level pool to prune.
        """
        return 0

    def _ensure_open(self):
        if self._closed:
            raise SessionClosed("Session is closed")

    # -- engine selection ---------------------------------------------------
    def binding(self, url=None):
        """Returns the transport context a request to *url* would use.

        This is the session as a value: the pinned profile (which fixes the
        JA3/JA4 ClientHello, the HTTP/2 SETTINGS and pseudo-header set, and the
        QUIC transport parameters together), the proxy the session routes
        through, and the TLS verification policy.  Two requests whose bindings
        compare equal share one Chromium ``URLRequestContext`` -- and therefore one
        socket pool, TLS session cache and HTTP/2/3 session pool.  Two that do
        not, do not.

        It is the same value the engine cache keys on, so it answers the question
        ``"is this request going to reuse the connection I just warmed up?"``
        before the request is sent.

        Args:
            url: The URL the request would go to; selects the proxy from a
                per-scheme ``proxies`` mapping.  ``None`` answers for the
                session-level ``proxy=`` only.

        Returns:
            A :class:`SessionBinding`.
        """
        proxy = self._effective_proxy(url, None, None)
        if proxy is None and self.proxies and url is None:
            # A per-scheme mapping cannot be resolved without a URL, and
            # guessing one scheme's proxy would answer for the wrong request.
            # Say so instead of reporting a direct connection that this session
            # would never make.
            raise InvalidProxyURL(
                "binding() needs a URL to resolve the session's per-scheme "
                "proxies= mapping; pass binding(url)")
        return self._binding(None, proxy, None, None, url, None, None)

    def _binding(self, impersonate, proxy, verify, http_version, url=None,
                 request_proxy=None, request_proxies=None):
        """Resolves one request into the transport context it will use.

        This is the only place that decides what a binding contains, so the
        engine cache, the WebSocket handshake and the public :meth:`binding`
        cannot drift apart.

        Args:
            impersonate: Per-request profile override, or ``None``.
            proxy: The proxy rule this request will use, already resolved.
            verify: Per-request certificate policy, or ``None``.
            http_version: Per-request protocol pin, or ``None``.
            url: The request URL, for ``proxy_auth`` scoping.
            request_proxy: The caller's per-request ``proxy=`` argument.
            request_proxies: The caller's per-request ``proxies=`` mapping.

        Returns:
            A :class:`SessionBinding`.
        """
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
        # ``verify`` is a flag or a CA bundle path. An unreadable bundle is an
        # SSL failure, not a URL one: it used to escape as a bare
        # FileNotFoundError, and the Core rejected the bytes as
        # InvalidArgument -> InvalidURL, which blamed the caller's URL.
        ca_pem = None
        if isinstance(verify, str):
            ca_pem = _read_ca_bundle(verify)
            verify_flag = True
        elif verify is None:
            verify_flag = True
            if isinstance(self.verify, str):
                ca_pem = _read_ca_bundle(self.verify)
            else:
                verify_flag = bool(self.verify)
        else:
            verify_flag = bool(verify)
        username, password = url_user, url_password
        if username is None and self.proxy_auth \
                and self._proxy_auth_applies(proxy, url, request_proxy,
                                            request_proxies):
            username, password = self.proxy_auth
        return SessionBinding(
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
            # Normalise per-request values too, for the same reason impersonate is:
            # ``Session(http_version="h2")`` is accepted at construction, and a
            # per-call ``http_version="h2"`` used to reach the Core verbatim and
            # fail with "unknown http version". It also split the engine cache:
            # "v2", "h2" and "HTTP/2" are one protocol but were three keys, so
            # one session built three contexts for the same wire identity.
            http_version=normalize_http_version(http_version)
            if http_version is not None else self.http_version,
            cache=self.cache,
            profile_namespace=None,
            host_mapping=self.host_mapping,
            dns_nameservers=self.dns_nameservers,
            doh_template=self.doh_template,
            dns_secure_mode=self.dns_secure_mode,
            dns_over_tls=self.dns_over_tls,
            dns_rotate=self.dns_rotate,
            dns_padding=self.dns_padding,
            hsts_bypass=self.hsts_bypass,
            proxy_chain=self.proxy_chain,
            platform=self.platform,
            max_sockets_per_group=self.max_sockets_per_group,
        )

    def _engine_config(self, impersonate, proxy, verify, http_version, url=None,
                        request_proxy=None, request_proxies=None):
        """Wraps the resolved binding in the engine cache key.

        The key is the binding, and nothing else: every
        field that changes the wire belongs to the binding, so a request that
        only changes its headers or its body cannot fork the connection pool.

        Args:
            impersonate: Per-request profile override, or ``None``.
            proxy: The proxy rule this request will use, already resolved.
            verify: Per-request certificate policy, or ``None``.
            http_version: Per-request protocol pin, or ``None``.
            url: The request URL, for ``proxy_auth`` scoping.
            request_proxy: The caller's per-request ``proxy=`` argument.
            request_proxies: The caller's per-request ``proxies=`` mapping.

        Returns:
            An :class:`EngineConfig` for these settings.
        """
        binding = self._binding(impersonate, proxy, verify, http_version, url,
                                request_proxy, request_proxies)
        return EngineConfig(**{name: getattr(binding, name)
                               for name in BINDING_FIELDS})

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

        The answer here is what the binding will carry, not what the wire will
        do: Chromium appends ``SubtractImplicitBypassesRule` to every proxy
        configuration, so a loopback or link-local target goes direct even when
        this method returns a proxy. Measured: with ``HTTP_PROXY`` pointing at a
        dead port, a loopback request still returned 200 while a request to a
        non-loopback name went through the proxy. See
        docs/COMPATIBILITY_BOUNDARY.md ("Chromium implicitly bypasses localhost").
        """
        if proxy is not None:
            return proxy
        mapping = dict(self.proxies)
        if proxies:
            mapping.update(proxies)
        selected = proxy_from_proxies(url, mapping) if mapping else None
        if selected is None and self.proxy is not None:
            selected = self.proxy
        if selected is None and self.trust_env:
            # ``resolve_proxies`` already applies NO_PROXY: it returns an empty
            # mapping when the URL bypasses, so asking ``should_bypass_proxies``
            # first ran the same suffix/CIDR match twice per request. Measured
            # 3 calls of that function per request against 2 needed, 17.5 us
            # each on this host. The environment lookup itself stays -- that is
            # what decides the answer.
            environment = resolve_proxies(url, {}, trust_env=True)
            selected = proxy_from_proxies(url, environment) if environment else None
        return selected

    def _slot(self, url, impersonate, proxy, proxies, verify, http_version):
        """Returns the engine slot for one request, without pinning it.

        Only the WebSocket handshake uses this form: the Core's WebSocket owns
        its own reference to the Chromium context, so nothing has to be held
        after the handshake call returns.  Request paths use :meth:`_lease`,
        which keeps the slot pinned for as long as the facade still reads or
        writes its state.

        Args:
            url: The request URL; selects the proxy from a mapping.
            impersonate: Per-request profile override, or ``None``.
            proxy: Per-request proxy override.
            proxies: Per-request proxy mapping.
            verify: Per-request certificate policy.
            http_version: Per-request protocol pin.

        Returns:
            The matching :class:`EngineSlot`.
        """
        self._ensure_open()
        # The per-request proxy= / proxies= have to reach _engine_config too:
        # proxy_auth may only be attached to the proxy the session itself
        # configured, and a per-request mapping can select a different one.
        config = self._engine_config(
            impersonate, self._effective_proxy(url, proxy, proxies), verify,
            http_version, url, proxy, proxies)
        return self._engines.get(config)

    def _lease(self, url, impersonate, proxy, proxies, verify, http_version):
        """Returns a context manager that pins one engine for a request.

        The pin is the facade's statement that this request is using the
        engine, so an eviction that happens while it is held is counted as
        deferred rather than free.  The Chromium context itself survives an
        eviction -- the Core request holds a reference to it -- so the pin is
        about bookkeeping accuracy, not about keeping the socket alive.

        Args:
            url: The request URL; selects the proxy from a mapping.
            impersonate: Per-request profile override, or ``None``.
            proxy: Per-request proxy override.
            proxies: Per-request proxy mapping.
            verify: Per-request certificate policy.
            http_version: Per-request protocol pin.

        Returns:
            A context manager yielding the :class:`EngineSlot`; the
            yielded slot is also reachable afterwards through its `slot` property.
        """
        self._ensure_open()
        config = self._engine_config(
            impersonate, self._effective_proxy(url, proxy, proxies), verify,
            http_version, url, proxy, proxies)
        return self._engines.acquire(config)

    def _throwaway_slot(self, config):
        """Builds an engine no other request can reach, for one discard request.

        Args:
            config: The :class:`EngineConfig` this request would have used. Its
                binding is kept, so the wire identity is the one the caller
                asked for; only the store is new.

        Returns:
            A fresh :class:`EngineSlot`, not registered in the engine cache.
        """
        return EngineSlot(config.build(), config, single_use=True)

    # -- client certificates (ABI v11) --------------------------------------

    def set_client_certificate(self, host, port, cert, key=None):
        """Registers a client certificate for one server at runtime.

        Use this when the certificate only becomes available after the session
        was built -- an enrolment exchange, a token refresh, a rotated key. For
        a certificate known up front, pass it to the constructor as
        ``cert=("cert.pem", "key.pem")`` instead, which applies it to every
        request this session makes.

        The registration is per (host, port) and per engine. Every engine the
        session currently holds gets it, and engines built later pick it up the
        first time a request goes to that host, so a certificate set before the
        first request is not lost to a lazily built engine.

        Replacing a certificate makes the Core refresh that origin's connection
        group, closing the idle connections opened under the old certificate.
        Without that, the next request reuses the pooled connection and the
        server keeps seeing the previous certificate: measured by swapping
        certificates on a live session and reading the CN the server received.

        Args:
            host: Server hostname, e.g. ``"api.internal"``.
            port: Server port, e.g. ``443``.
            cert: Path to a certificate file, either a combined PEM or the
                certificate half of a pair.
            key: Path to the private key when it is not in ``cert``.

        Raises:
            UnsupportedFeature: A file could not be read, or the value is not a
                path.
            RequestException: The Core rejected the certificate or key.

        Example:
            >>> session.set_client_certificate(  # doctest: +SKIP
            ...     "api.internal", 443, "new.pem", "new.key")
        """
        cert_pem, key_pem = normalize_client_cert(
            cert if key is None else (cert, key))
        host = str(host)
        port = int(port)
        record = getattr(self, "_client_certs", None)
        if record is None:
            record = self._client_certs = {}
        record[(host, port)] = (cert_pem, key_pem)
        slots = self._engines.slots()
        if not slots:
            # Validate against the Core even when no engine exists yet. Looping
            # over an empty list made this call a silent no-op: a malformed or
            # passphrase-protected key was accepted here and only failed at the
            # first request, which reads as success at the call site that was
            # supposed to report the problem. Materialising the default engine
            # costs what the next request would have paid anyway.
            slots = [self._store_slot()]
        for slot in slots:
            slot.set_client_certificate(host, port, cert_pem, key_pem)

    def clear_client_certificate(self, host, port):
        """Forgets the client certificate registered for (host, port).

        Forgetting one that was never registered is not an error, so a caller
        does not have to track what it set.

        Args:
            host: Server hostname.
            port: Server port.

        Raises:
            RequestException: The Core rejected the call.
        """
        host = str(host)
        port = int(port)
        record = getattr(self, "_client_certs", None)
        if record:
            record.pop((host, port), None)
        for slot in self._engines.slots():
            slot.clear_client_certificate(host, port)

    def _client_cert_for(self, url):
        """Returns ``(host, port, cert_pem, key_pem)`` for *url*, or ``None``.

        The Core keys a client certificate by (host, port) while an engine is
        chosen by profile/proxy/verify, so one engine can serve several mTLS
        servers and needs one registration per server. Only the request path
        knows which server a request targets, which is why this lookup happens
        per request rather than once at construction.

        Args:
            url: The request URL.

        Returns:
            The matching registration, or ``None`` when this URL has none.
        """
        record = getattr(self, "_client_certs", None)
        if not record:
            return None
        parts = urlsplit(url)
        if not parts.hostname:
            return None
        default_port = 443 if parts.scheme.lower() in ("https", "wss") else 80
        port = parts.port or default_port
        entry = record.get((parts.hostname, port))
        if entry is None:
            return None
        return parts.hostname, port, entry[0], entry[1]

    def _effective_client_cert(self, url, options):
        """Returns ``(host, port, cert_pem, key_pem)`` for one transfer.

        A per-request ``cert=`` wins over the session default for this one
        request. It is registered on the engine without being recorded on the
        session, so a one-off override cannot silently apply to every later
        request that happens to reuse the same engine.

        Args:
            url: The request URL.
            options: The resolved call options; ``cert`` may be set there.

        Returns:
            The registration to apply, or ``None``.
        """
        parts = urlsplit(url)
        if not parts.hostname:
            return None
        default_port = 443 if parts.scheme.lower() in ("https", "wss") else 80
        port = parts.port or default_port
        override = (options or {}).get("cert")
        if override:
            cert_pem, key_pem = normalize_client_cert(override)
            return parts.hostname, port, cert_pem, key_pem
        found = self._client_cert_for(url)
        if found is not None:
            return found
        default = getattr(self, "_client_cert_default", None)
        if default is not None:
            return parts.hostname, port, default[0], default[1]
        return None

    def _register_client_cert(self, slot, url):
        """Registers the session's certificate on *slot* for the URL's server.

        Args:
            slot: The :class:`EngineSlot` the request will run on.
            url: The request URL.
        """
        found = self._client_cert_for(url)
        if found is not None:
            slot.set_client_certificate(*found)

    # -- cookie store (ABI v9) ---------------------------------------------

    def _store_slot(self):
        """Returns the slot whose store these helpers read and write.

        The store is per engine, and a session may own several (one per
        profile/proxy/verify combination). The one every ordinary request in
        this session would use is the default configuration's, which is what
        ``session.cookies`` describes; a per-request override gets its own
        store and is not reachable from here by design.

        Returns:
            The :class:`EngineSlot` for the session defaults.
        """
        return self._engines.get(self._engine_config(None, self.proxy,
                                                    self.verify, self.http_version))

    def _engine_cookies(self, url=None):
        """Returns the Core's own cookie store.

        This is the store the requests actually send from, as opposed to
        ``session.cookies``, which is the jar the caller edits. Both describe
        the same cookies once the jar is reconciled against the store.

        Args:
            url: Restrict to the cookies a request to this URL would carry.

        Returns:
            A list of ``(name, value, domain, path, expiry, has_expiry,
            secure, http_only, same_site)`` tuples.
        """
        self._ensure_open()
        return self._store_slot().cookies(url)

    def _engine_cookies_set(self, name, value, domain, path, source_url,
                            secure=False, http_only=False, same_site="",
                            expires=None):
        """Writes one cookie into the Core's store.

        The store applies its own domain, path and size rules, so a cookie it
        rejects raises instead of being silently dropped (AGENTS §8).

        Args:
            name: Cookie name.
            value: Cookie value.
            domain: Cookie domain, as the store should record it.
            path: Cookie path.
            source_url: The URL the cookie is being set from; decides domain
                matching and origin-bound semantics.
            secure: The ``Secure`` attribute.
            http_only: The ``HttpOnly`` attribute.
            same_site: ``"lax"``, ``"strict"``, ``"none"`` or empty.
            expires: Unix seconds, or ``None`` for a session cookie.
        """
        self._ensure_open()
        self._store_slot().set_cookie(
            (name, value, domain, path, expires, secure, http_only, same_site),
            source_url)

    def _engine_cookies_delete(self, domain, path, name):
        """Deletes every matching cookie from the Core's store.

        Args:
            domain: Cookie domain.
            path: Cookie path.
            name: Cookie name.

        Returns:
            How many cookies were deleted.
        """
        self._ensure_open()
        return self._store_slot().delete_cookie(domain, path, name)

    def _engine_cookies_clear(self):
        """Empties the Core's store."""
        self._ensure_open()
        self._store_slot().clear_cookies()

    # -- cookie policy ------------------------------------------------------
    def _discard_config(self, slot, prepared):
        """Drops the outgoing Cookie header and returns the engine config to use.

        Dropping the header is not enough on its own: CookieMonster owns the
        Cookie header and re-attaches whatever IT holds
        (net/url_request/url_request_http_job.cc:761-838), so the request has to
        run on an engine whose store is empty. Clearing the SESSION's store used
        to stand in for that, which is the same thing only while one thread uses
        the session: measured with one plain request and one discard request
        running together, 19 of 20 readings of the plain request's Cookie header
        were wrong. A throwaway engine is the isolation without the shared state.

        Split out from :meth:`_resolve_cookies` because the two callers build the
        engine differently: the synchronous path builds it inline, the asyncio
        path hands the build to an executor so a burst of discard requests does
        not stall the loop (measured: 16 concurrent builds, 3.4 ms each, 72 ms of
        loop stall).

        The Cookie header is dropped from a COPY, never from the object the
        caller handed in. ``send()`` takes a PreparedRequest the caller owns and
        may reuse, and ``pop``-ing its header made that request permanently
        cookie-free: measured, ``session.send(prepared, discard_cookies=True)``
        followed by ``session.send(prepared)`` sent no Cookie header on the
        second call either, because the caller's own object had been rewritten.
        ``requests`` never mutates a request it sends.

        Args:
            slot: The slot the request would otherwise have used.
            prepared: The prepared request.

        Returns:
            ``(config, prepared)``: the :class:`EngineConfig` for the throwaway
            engine, and the request to send on it -- the same object when it
            carried no Cookie header, otherwise a copy without one.
        """
        if prepared.headers is None or "Cookie" not in prepared.headers:
            return slot.config, prepared
        effective = prepared.copy()
        effective.headers.pop("Cookie", None)
        return slot.config, effective

    def _resolve_cookies(self, slot, prepared, discard):
        """Decides the outgoing ``Cookie`` header.

        The store and the jar are the same now: ``ServerCookieJar`` is a view
        over ``CookieMonster`` (ABI v9), so there is nothing to reconcile. What
        is left here is the one thing a view cannot express on its own --
        ``discard_cookies``, which means "neither send nor record" and therefore
        has to empty the store before the request rather than skip a header.

        Args:
            slot: The slot the request would otherwise use.
            prepared: The prepared request.
            discard: Whether the caller asked for "neither send nor record".

        Returns:
            ``(slot, prepared)``. *slot* is the engine the request runs on --
            the session's own unless ``discard_cookies`` asked for a throwaway;
            *prepared* is the request to send, which is a copy when the caller's
            own object had to lose its Cookie header.
        """
        if discard:
            config, effective = self._discard_config(slot, prepared)
            return self._throwaway_slot(config), effective
        # A per-request ``cookies=`` mapping goes into the store for this
        # request. A header would not do: CookieMonster owns the Cookie header
        # and re-attaches whatever it holds, so a cookie the store also has
        # would come out twice -- the caller's value has to be the one the store
        # holds.
        # An engine owns its store, so an override that selects a different
        # configuration starts with an empty one. The session jar has to seed
        # it -- once, so the seeding does not overwrite that engine's own
        # cookies on every later request.
        self._seed_store_from_jar(slot, prepared.url)
        per_request = getattr(prepared, "_cookies", None)
        if per_request:
            # Only the cookies the CALLER added for this one request, not the
            # session's own jar that prepare_request merged in.
            #
            # This loop used to iterate the merged jar, which contains every
            # session cookie, and write each of them into the store again -- on
            # every request. The store already holds them (the jar is a view of
            # it since ABI v9), so the writes were no-ops that cost a lock
            # acquisition and a store call each. Measured with 200 session
            # cookies: 400 redundant store writes per request, p50 468 ms against
            # 12 ms for the same workload with an empty jar, and 17 req/s against
            # 614. Under 8 threads it collapsed to 17 req/s.
            source = prepared.url or self.cookies.default_source
            additions = getattr(prepared, "_cookie_additions", None)
            if additions is None:
                # A PreparedRequest built by hand (send() with a caller-supplied
                # object) has no record of which cookies were additions, so fall
                # back to writing them all. Correct, just not free.
                additions = list(per_request)
            if additions:
                with self._cookie_lock:
                    for cookie in additions:
                        try:
                            self.cookies._write_through(cookie, source)
                        except Exception:
                            # Same rule as the seed path: one request-scoped
                            # cookie the store refuses must not abort the
                            # request. The caller asked for this single request,
                            # and failing the whole transfer over one bad entry
                            # is not a faithful reading of that.
                            continue
        # The jar writes through to the store, so the header the Core emits is
        # the one the jar describes. Nothing to compare, nothing to replace.
        return slot, prepared
    def _seed_store_from_jar(self, slot, source_url):
        """Writes the session jar into a store the session has not filled yet.

        One store per engine means a per-request override (``verify=``,
        ``proxy=``, ``impersonate=``) runs on an engine whose store is empty.
        Without seeding it, a cookie the session already holds would stop being
        sent the moment a request used that override -- measured: the echo came
        back empty after a verify=False request.

        Seeded exactly once per slot: after that the store is the authority for
        that engine, and re-seeding would undo cookies it had received.

        Args:
            slot: The slot the request will run on.
            source_url: The resolved request URL, used as each cookie's source.
        """
        if not source_url:
            return
        with self._cookie_lock:

            if slot in self._seeded_slots:
                return
            self._prune_seeded_slots()
            self._seeded_slots.add(slot)
            for cookie in list(self.cookies):
                try:
                    slot.write_cookie(cookie, source_url)
                except Exception:
                    # One entry the store refuses must not keep the rest of the
                    # jar off this engine. Measured: a jar holding a cookie with
                    # an unusable domain made every request on an override
                    # configuration raise, so the override path was unusable
                    # until the bad entry was removed by hand.
                    continue

    def _prune_seeded_slots(self):
        """Forgets slots the engine cache no longer holds.

        Called under ``_cookie_lock`` from the only writer of
        ``_seeded_slots``.

        This is required, and the reason is not obvious: keying on the slot
        object was supposed to let CPython drop an evicted entry for free, and
        for a plain ``get()`` cycle it does. It does not when the request used
        an override -- the Core keeps the superseded engine alive through the
        request it served, so the object survives a ``gc.collect()``.
        Measured: 8 ``verify=False`` requests with ``max_engines=1`` (17 builds)
        left 9 entries behind the single live slot.
        """
        live = set(self._engines.slots())
        self._seeded_slots &= live

    def _flush_pending_store_cookies(self):
        """Writes the jar's pre-existing cookies into the store, once.

        ``Session(cookies={...})`` and any jar built before the first request
        predate the write-through sink, so those entries exist only in Python.
        They are flushed on the first request, when a URL exists to use as their
        source -- the store needs one to run its own domain checks, and inventing
        one earlier would mean writing cookies against the wrong origin.
        """
        if not self._pending_store_cookies:
            return
        source = self.cookies.default_source
        if not source:
            # No URL yet, so there is no origin to record these against. Leave the
            # flag set: the next request will have one. Clearing it here instead
            # would drop every pre-existing cookie the moment a request failed
            # before resolving its URL -- measured: the store stayed empty and the
            # cookies were never sent again, silently.
            return
        with self._cookie_lock:
            for cookie in list(self.cookies):
                try:
                    self.cookies._write_through(cookie, source)
                except Exception:
                    # One cookie the store rejects -- a domain that cannot apply to
                    # this URL, say -- must not stop the rest of the jar from
                    # reaching it. The first request is the only chance this batch
                    # gets, so a single throw here would drop every later cookie
                    # too.
                    continue
            # Only after the whole batch: a throw must leave the work pending.
            self._pending_store_cookies = False

    def _absorb_cookies(self, slot, response, discard=False):
        """Mirrors every ``Set-Cookie`` from the final hop and each redirect.

        Two jars are updated, and they serve different readers:

        * ``response.cookies`` reports what *this response* set.
        * ``self.cookies`` is the session jar the caller mutates and reads.

        There is no third copy for the engine: the store has already accepted
        these cookies, so recording them again would be duplicating the thing
        the jar is a view of.

        Both happen under ``_cookie_lock`` so a ``Set-Cookie`` is never visible
        to one reader and not the other.

        Args:
            slot: The engine slot the response arrived on.
            response: The assembled response, whose ``history_headers`` and
                headers carry the ``Set-Cookie`` lines.
            discard: When true the request asked for "neither send nor record",
                so nothing is absorbed anywhere.

        Returns:
            ``None``.
        """
        if discard:
            return
        # Scan before locking. Most responses carry no Set-Cookie at all, and
        # taking the session cookie lock for a response that cannot change the
        # jar serialises every concurrent request behind it for nothing:
        # measured 7.4 us per request against 0.4 us for the scan alone, on a
        # 0.7 ms synchronous prefix. The lock is still taken for the responses
        # that do carry cookies, which is what keeps "the jar and the store
        # never disagree" true.
        pending = [(url, headers.get_list("set-cookie"))
                   for url, headers in response.history_headers]
        pending = [(url, lines) for url, lines in pending if lines]
        final_lines = response.headers.get_list("set-cookie")
        if not pending and not final_lines:
            return
        # mirror=False on both: each call extracts into its own jar, which is
        # what fills ``response.cookies`` and the session jar. The extra jar
        # ``absorb_set_cookie`` would build and return is a copy of the same
        # lines that nothing here reads -- measured 47 us on a plain jar and
        # 270-500 us with a write-through sink attached, i.e. the whole cost of
        # a Set-Cookie response. The public method still builds it by default,
        # because its documented contract is to return it.
        with self._cookie_lock:
            for url, lines in pending:
                response.cookies.absorb_set_cookie(url, lines, mirror=False)
                self.cookies.absorb_set_cookie(url, lines, mirror=False)
            if final_lines:
                response.cookies.absorb_set_cookie(response.url, final_lines,
                                                   mirror=False)
                self.cookies.absorb_set_cookie(response.url, final_lines,
                                              mirror=False)

    # -- response assembly --------------------------------------------------
    def _new_response(self, async_mode=False):
        if self.response_class is not None:
            return self.response_class()
        return AsyncResponse() if async_mode else Response()

    def _finalize(self, response, prepared, status_code, raw_headers, hops, started,
                  slot, discard_cookies=False, body=None, http_version=None,
                  header_fields=None, reason_phrase=None, ech_status=None):
        if header_fields is not None:
            # The Core already split the block; building Headers from it is a
            # copy, not a parse. That is the point of the ABI change: Chromium
            # owns the grammar once, for every binding.
            headers = Headers()
            for name, value in header_fields:
                headers.add(name, value)
            status_line = compose_status_line(status_code, reason_phrase)
        else:
            # Only for a hop the Core never saw: a redirect assembled from the
            # redirect callback, which carries the raw block and nothing else.
            status_line, headers = parse_raw_headers(raw_headers)
        if body is not None:
            response.content = body
        response.status_code = status_code
        response.headers = headers
        response.status_line = status_line
        response.reason = (reason_phrase if reason_phrase is not None
                           else reason_from_status_line(status_line, status_code))
        response.ech_status = ech_status
        # ABI v9 reports the negotiated protocol, so this is the Core's answer
        # whenever the caller supplied one. Chromium normalizes an HTTP/2
        # response onto "HTTP/1.1 200" (net/spdy/spdy_http_utils.cc), so the
        # status line cannot stand in for it: parsing that reported HTTP/1.1
        # for every h2 and h3 connection.
        #
        # The fallback stays for the paths that have no live Core response to
        # ask -- a redirect hop assembled from the redirect callback, and a
        # cache hit where the Core itself answers UNKNOWN. Both are cases where
        # the status line is all there is, and it reports None unless it can
        # prove the protocol.
        if http_version is not None:
            response.http_version = http_version
        else:
            response.http_version = _negotiated_http_version(status_line)
        response.request = prepared
        response.elapsed = _elapsed(started)
        response.default_encoding = self.default_encoding
        response.redirect_count = len(hops)
        response.history_headers = []
        url = prepared.url
        history = []
        # Every hop gets its own request, because requests' Response.history is a
        # list of responses each describing the request that produced it: the
        # first entry is the request the caller made, the second the one the
        # first Location produced. Sharing one PreparedRequest across the chain
        # made hop[1..].request.url point at the ORIGINAL url -- measured, a
        # 3-hop chain reported request.url == "redirect?n=3" on all three hops --
        # so a caller reconstructing the chain from request.url saw the same URL
        # three times and could not tell which hop set which cookie.
        hop_request = prepared
        for hop_status, hop_raw, hop_url, _hop_method, hop_fields in hops:
            # ABI 12 hands the parsed fields over with the hop, so the raw block
            # only has to be split on a build that predates it. The status line
            # still comes from the raw block: the ABI carries the fields and the
            # reason phrase separately, and a hop has no reason phrase of its own.
            if hop_fields is not None:
                hop_headers = _headers_from_fields(hop_fields)
                hop_line = None
            else:
                hop_line, hop_headers = parse_raw_headers(hop_raw)
            hop = Response()
            hop.status_code = hop_status
            hop.headers = hop_headers
            hop.status_line = hop_line
            hop.reason = reason_from_status_line(hop_line, hop_status)
            hop.http_version = _negotiated_http_version(hop_line)
            hop.url = url
            hop.request = hop_request
            hop.content = b""
            response.history_headers.append((url, hop_headers))
            history.append(hop)
            # The next hop's request is this hop's URL with the Location applied.
            # It is a copy rather than a mutation so history stays a record of
            # what was sent, not a view of what will be sent.
            following = hop_request.copy()
            following.url = urljoin(url, hop_headers.get("location", hop_url))
            hop_request = following
            url = hop_url
        response.history = history
        response.url = url
        response.redirect_url = url if history else None
        self._absorb_cookies(slot, response, discard_cookies)
        # No store cleanup here. A discard request ran on its own engine
        # (_throwaway_slot), whose store no other request can reach, and that
        # engine goes away when this frame drops its last reference -- the Core
        # request holds its own reference for as long as the transfer runs.
        # There used to be a slot.clear_cookies() at this point, back when the
        # request shared the session's engine; it is what deleted other requests'
        # cookies.
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
        # ``take_body_buffers`` is passed by keyword, not positionally: the
        # eleven positional slots are the ones the two extensions have shared
        # since v8, and inserting a twelfth would shift every one after it.
        # It was accepted by the constructor and then never forwarded, so the
        # documented zero-copy option did nothing -- the copy it is supposed to
        # avoid still happened, and no gate could see it because both behaviours
        # return identical bytes.
        return slot.engine.request(
            prepared.method, prepared.url, prepared.wire_headers(), body,
            _split_timeout(timeout), bool(allow_redirects), None, cache_mode,
            priority, bool(prepared.stream_body), self.strip_authorization,
            take_body_buffers=self.take_body_buffers,
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
        for name in ("interface", "doh_url", "curl_options", "thread", "debug"):
            if values.get(name):
                raise UnsupportedFeature(
                    "%s= is not supported: ABI v11 exposes no Core setting for it" % name)
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
        _reject_core_owned_removals(request.headers, request.method)
        _reject_hop_by_hop_headers(request.headers)
        allow_redirects = self.allow_redirects \
            if values.get("allow_redirects") is None \
            else bool(values.get("allow_redirects"))
        effective_max = self.max_redirects \
            if values.get("max_redirects") is None \
            else _validate_max_redirects(values.get("max_redirects"))
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
            "allow_redirects": bool(allow_redirects),
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
        """Opens a WebSocket on the session's engine.

        The handshake and an HTTP request now resolve cookies the same way --
        through the Core's own store -- because the jar writes through to it.
        That is what removed the old disagreement this method used to work
        around: a caller who cleared ``session.cookies`` saw the Core's old
        value on the wire, and the fix was to build a throwaway engine.
        """
        slot = self._slot(url, impersonate, proxy, proxies, verify, None)
        # A wss:// server may demand a client certificate on the handshake too,
        # and the handshake is a normal TLS connection from the Core's point of
        # view, so the same per-(host, port) registration applies.
        self._register_client_cert(slot, url)
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
        if resolved.get("allow_redirects") is None:
            # A caller who builds the options by hand uses the requests
            # spelling ``allow_redirects=``; it used to be dropped on the floor
            # here, so the resolved policy always came out of the session.
            resolved["allow_redirects"] = self.allow_redirects
        if resolved.get("max_redirects") is None:
            resolved["max_redirects"] = self.max_redirects
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
        response = self._dispatch_hooks(response, request)
        if options.get("raise_for_status") if options.get("raise_for_status") is not None \
                else self.raise_for_status:
            response.raise_for_status()
        return response

    def _send_once(self, prepared, options):
        """Sends one prepared request on the engine its binding selects.

        The engine is held through :meth:`_lease` for the whole call, so the
        in-flight count describes what is really running and an eviction cannot
        look free while a request is using the slot.

        Args:
            prepared: The prepared request.
            options: The resolved call options.

        Returns:
            The assembled :class:`Response`.
        """
        discard = options.get("discard_cookies")
        discard = self.discard_cookies if discard is None else discard
        check_discard_cache_mode(discard, options.get("cache_mode"))
        lease = self._lease(prepared.url, options.get("impersonate"),
                            options.get("proxy"), options.get("proxies"),
                            options.get("verify"), options.get("http_version"))
        with lease:
            return self._send_on_slot(lease, prepared, options, discard)

    def _send_on_slot(self, lease, prepared, options, discard):
        """Performs the transfer on the pinned slot, rebuilding its store if needed.

        Args:
            lease: The pinned :class:`_SlotLease`.
            prepared: The prepared request.
            options: The resolved call options.
            discard: Whether cookies must be neither sent nor recorded.

        Returns:
            The assembled :class:`Response`.
        """
        slot, prepared = self._resolve_cookies(lease.slot, prepared, discard)
        # Cookie resolution may have replaced the engine, and the replacement is
        # the one this request runs on: move the pin so the in-flight count
        # follows the request instead of the engine it abandoned. It may also
        # have produced a copy of the request (``discard_cookies`` drops the
        # Cookie header from a copy rather than from the caller's object).
        lease.rebind(slot)
        throwaway = slot.single_use
        # After the rebind, so the certificate lands on the engine that will
        # actually open the connection rather than the one just abandoned.
        cert = self._effective_client_cert(prepared.url, options)
        if cert is not None:
            slot.set_client_certificate(*cert)
        limit = options.get("max_response_bytes")
        stream = bool(options.get("stream"))
        started = _now()
        follow = bool(options.get("allow_redirects", True))
        native = None
        try:
            return self._transfer_on_slot(
                slot, prepared, options, discard, throwaway, limit, stream,
                started, follow)
        finally:
            if throwaway:
                # The request is finished or has failed, and the Core holds its
                # own reference for as long as it runs. Releasing the engine here
                # would block this thread on the Core's network thread
                # (core/source/engine.cc:493-521); hand it to the retire worker.
                retire_engine_slot(slot)

    def _transfer_on_slot(self, slot, prepared, options, discard, throwaway, limit,
                          stream, started, follow):
        """Performs the transfer. Split out so the slot can be retired on every exit.

        Args:
            slot: The engine this request runs on.
            prepared: The prepared request.
            options: The resolved call options.
            discard: Whether cookies must be neither sent nor recorded.
            throwaway: Whether *slot* is a one-request engine.
            limit: ``max_response_bytes``, or ``None``.
            stream: Whether the body is handed back unread.
            started: The monotonic start time, for ``elapsed``.
            follow: Whether Chromium follows redirects inside the request.

        Returns:
            The assembled :class:`Response`.
        """
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
            # Only a bare RuntimeError is a Core error string. UnsupportedFeature
            # derives from NotImplementedError, which is a RuntimeError, so a
            # facade-raised UnsupportedFeature used to be fed back through
            # map_native_error and re-classified by its own wording -- measured:
            # the max_redirects and timeout=(connect, read) refusals came out as
            # Timeout because their messages contain the word.
            if isinstance(error, RuntimeError) and not isinstance(error, RequestException):
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
        # `head` is the Core's own response object; it carries the protocol the
        # connection actually used. It is None only on the paths that never got
        # a header block, where _finalize falls back to the status line.
        negotiated = getattr(head, "http_version", None) if head is not None else None
        fields = getattr(head, "header_fields", None) if head is not None else None
        reason = getattr(head, "reason_phrase", None) if head is not None else None
        return self._finalize(response, prepared, status_code, raw_headers,
                              hops, started, slot, discard,
                              http_version=negotiated, header_fields=fields,
                              reason_phrase=reason, ech_status=ech_of(head))

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
        """Yields each response in a redirect chain, as requests does.

        The hops are driven by Chromium, not by this generator: the request is
        held in the Core's manual redirect mode, and each step asks the Core
        for the redirect it computed (``new_url``, ``new_method``, referrer
        policy, the previous hop's cookies, dropped credentials) and then
        releases it with ``follow_redirect``. Nothing here decides what the next
        URL or method is, which is what keeps this method a thin wrapper instead
        of a second redirect implementation.

        The Core caps the chain at ``CHROMIUM_MAX_REDIRECTS``; a longer chain
        surfaces as :class:`TooManyRedirects` from the Core itself.

        Args:
            response: The redirect response to continue from.
            request: The ``PreparedRequest`` that produced *response*.
            stream: Passed through to each hop's ``send``.
            timeout: Passed through to each hop's ``send``.
            verify: Passed through to each hop's ``send``.
            cert: Accepted for requests parity; the Core owns TLS.
            proxies: Passed through to each hop's ``send``.
            yield_requests: Yield ``PreparedRequest`` hops instead of responses.

        Yields:
            Each :class:`Response` in the chain, or each ``PreparedRequest`` when
            ``yield_requests`` is true.

        Raises:
            TooManyRedirects: The chain exceeded Chromium's ceiling.
        """
        url = response.url
        slot = self._slot(url, kwargs.get("impersonate"),
                          kwargs.get("proxy"), proxies, verify,
                          kwargs.get("http_version"))
        # Manual mode: Chromium computes each hop and hands it to us, then waits
        # for `follow_redirect` before issuing it. That is the only path where
        # the binding can observe a hop without deciding what it is: `new_url`,
        # the method, the referrer policy and the cookie snapshot all come from
        # RedirectInfo, not from urljoin() and a copy of requests' rules.
        native = self._native_request(slot, request, timeout or self.timeout,
                                      False, kwargs.get("cache_mode"),
                                      kwargs.get("priority"))
        deadline = _split_timeout(timeout or self.timeout)
        try:
            native.start()
            while True:
                hop, head = native.wait_manual(deadline)
                if hop is not None:
                    status_code, raw_headers, url, _new_method, hop_fields = hop
                    if yield_requests:
                        following = request.copy()
                        following.url = url
                        yield following
                    else:
                        # A redirect hop carries no body of its own: Chromium
                        # has the 3xx headers and nothing else to read here.
                        hop_response = self._finalize(
                            self._new_response(), request, status_code,
                            raw_headers, [], _now(), slot, False, body=b"")
                        hop_response.url = url
                        yield hop_response
                    native.follow_redirect()
                    continue
                if head is not None:
                    if yield_requests:
                        # The chain is exhausted; requests' spelling yields one
                        # PreparedRequest per hop and stops before the final
                        # response, which the caller already holds.
                        return
                    status_code, raw_headers = head
                    body = b"" if stream else native.read_body()
                    # `head` is the Core's own response object, so it carries the
                    # parsed fields and the reason just like the non-streaming
                    # path does. Dropping them here sent this route back through
                    # the status-line parser for no reason.
                    final = self._finalize(
                        self._new_response(), request, status_code,
                        raw_headers, [], _now(), slot, False, body=body,
                        http_version=getattr(head, "http_version", None),
                        header_fields=getattr(head, "header_fields", None),
                        reason_phrase=getattr(head, "reason_phrase", None),
                        ech_status=ech_of(head))
                    final.url = url
                    yield final
                    return
                raise Timeout("request timed out")
        finally:
            _cancel_quietly(native)

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

    It shares everything :class:`BaseSession` owns -- the transport binding, the
    engine cache, the cookie lock and ``stats`` -- so the two sessions differ only
    in how a response is awaited, never in what the wire looks like.  A request
    sent through either one lands on the same engine for the same binding, which
    is why mixing them against one endpoint is safe.

    ``max_clients`` bounds how many requests are in flight at once.  Leaving it
    unset is fine for a few thousand; setting it is how a caller keeps a burst
    from opening more sockets than the far end tolerates.  It is not a
    connection limit: one HTTP/2 engine multiplexes every request it is given
    over a single connection, so ``max_clients`` is backpressure for the far end
    while the engine is where the sockets actually live.
    """

    def __init__(self, impersonate=None, proxy=None, proxies=None, proxy_auth=None,
                 verify=True, timeout=None, headers=None, cookies=None, params=None,
                 auth=None, cert=None, stream=False, hooks=None,
                 max_redirects=None, trust_env=True,
                 allow_redirects=True, max_response_bytes=None,
                 strip_authorization=False, take_body_buffers=False,
                 base_url=None,
                 http_version=None, ja3=None, akamai=None, perk=None, extra_fp=None,
                 default_headers=True, default_encoding="utf-8",
                 discard_cookies=False, raise_for_status=False, retry=0, cache=True,
                 user_agent=None, accept_language=None, interface=None, doh_url=None,
                 max_recv_speed=0, curl_options=None, max_engines=DEFAULT_MAX_ENGINES,
                 response_class=None, max_clients=None,
                 # Everything below was appended AFTER max_clients on purpose:
                 # this constructor has no keyword-only marker, so a parameter
                 # inserted before an existing one silently changes what an
                 # existing positional call means. Measured: with these eleven
                 # placed before max_clients, AsyncSession(*([None]*37 + [8]))
                 # -- a working call -- started binding 8 to host_mapping and
                 # raised "host_mapping accepts a rule string ... not 8".
                 host_mapping=None, dns_nameservers=None,
                 doh_template=None, dns_secure_mode=None, dns_over_tls=False,
                 dns_rotate=False, dns_padding=False, hsts_bypass=None,
                 proxy_chain=None, platform="auto", max_sockets_per_group=0):
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
            max_engines=max_engines, response_class=response_class,
            host_mapping=host_mapping, dns_nameservers=dns_nameservers,
            doh_template=doh_template, dns_secure_mode=dns_secure_mode,
            dns_over_tls=dns_over_tls, dns_rotate=dns_rotate,
            dns_padding=dns_padding, hsts_bypass=hsts_bypass,
            proxy_chain=proxy_chain, platform=platform,
            max_sockets_per_group=max_sockets_per_group)
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
        response = self._dispatch_hooks(response, request)
        raise_flag = options.get("raise_for_status")
        if self.raise_for_status if raise_flag is None else raise_flag:
            response.raise_for_status()
        return response

    async def _send_once(self, prepared, options):
        """Sends one prepared request, bounded by ``max_clients`` when set.

        The client gate bounds requests, and the lease pins the engine.  They are
        different limits on purpose: ``max_clients`` is how many requests may be
        in flight at once, while the engine is where their sockets and HTTP/2
        streams actually live.  A single HTTP/2 engine multiplexes many of them
        over one connection, so the gate is a backpressure knob for the far end,
        not a connection count.

        Args:
            prepared: The prepared request.
            options: The resolved call options.

        Returns:
            The assembled :class:`AsyncResponse`.
        """
        loop = _running_loop()
        gate = self._semaphore(loop)
        if gate is not None:
            await gate.acquire()
        try:
            lease = self._lease(prepared.url, options.get("impersonate"),
                                options.get("proxy"), options.get("proxies"),
                                options.get("verify"), options.get("http_version"))
            with lease:
                return await self._perform(lease, prepared, options, loop)
        finally:
            if gate is not None:
                gate.release()

    async def _perform(self, lease, prepared, options, loop):
        """Performs the transfer on the pinned slot.

        Args:
            lease: The pinned :class:`_SlotLease`.
            prepared: The prepared request.
            options: The resolved call options.
            loop: The running event loop.

        Returns:
            The assembled :class:`AsyncResponse`.
        """
        discard = options.get("discard_cookies")
        discard = self.discard_cookies if discard is None else discard
        check_discard_cache_mode(discard, options.get("cache_mode"))
        # ``_discard_config`` returns the request to send as well as the config:
        # it drops the caller's Cookie header from a COPY, because the caller
        # owns the PreparedRequest and may send it again.
        if discard:
            config, prepared = self._discard_config(lease.slot, prepared)
            # The build posts to the Core's network thread and waits there
            # (mn_engine_create -> Engine::Start, core/source/engine.cc:517-538),
            # 3.4 ms measured. Inline that is 3.4 ms of loop stall per discard
            # request; in an executor it is 3.4 ms of a worker thread.
            slot = await loop.run_in_executor(None, self._throwaway_slot, config)
        else:
            slot, prepared = self._resolve_cookies(lease.slot, prepared, discard)
        cert = self._effective_client_cert(prepared.url, options)
        if cert is not None:
            slot.set_client_certificate(*cert)
        # The replacement engine is the one this request runs on; move the pin so
        # the in-flight count follows the request rather than the engine it left.
        lease.rebind(slot)
        throwaway = slot.single_use
        try:
            return await self._perform_on_slot(slot, prepared, options, loop, discard)
        finally:
            if throwaway:
                # Dropping this engine blocks on the Core's network thread
                # (core/source/engine.cc:493-521 posts a shutdown task and waits
                # on a WaitableEvent). Doing that here would stall the event loop
                # for the length of that round trip per discard request --
                # measured 83 ms at 16 concurrent discards against 2.5 ms for the
                # same load without discard_cookies. Hand it to the retire worker.
                #
                # The Core request holds its own reference to the Chromium
                # context, so a streaming body keeps reading after this returns
                # (AGENTS section 2, measured).
                retire_engine_slot(slot)

    async def _perform_on_slot(self, slot, prepared, options, loop, discard):
        """Performs the transfer on *slot*, which the caller keeps alive.

        Args:
            slot: The engine this request runs on.
            prepared: The prepared request.
            options: The resolved call options.
            loop: The running event loop.
            discard: Whether cookies must be neither sent nor recorded.

        Returns:
            The assembled :class:`AsyncResponse`.
        """
        stream = bool(options.get("stream"))
        limit = options.get("max_response_bytes")
        timeout = _split_timeout(options.get("timeout"))
        started = _now()
        try:
            native = self._native_request(
                slot, prepared, options.get("timeout"),
                bool(options.get("allow_redirects", True)), options.get("cache_mode"),
                options.get("priority"))
        except RuntimeError as error:
            raise map_native_error(error, request=prepared)
        follow = bool(options.get("allow_redirects", True))
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
            # Same guard as the synchronous path: a facade-raised
            # UnsupportedFeature is a RuntimeError by inheritance, not a Core
            # error string, and must keep its own class.
            if isinstance(error, RuntimeError) and not isinstance(error, RequestException):
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
            # follow`, so with allow_redirects=False (manual redirect mode) the
            # body was read eagerly
            # from ``state.body`` -- which is None in streaming mode -- and the
            # response silently came back empty. The reader waits on the same
            # state, so it is correct for both redirect modes.
            response._async_body_reader = _AsyncBodyReader(state, limit)
        else:
            response.content = b"".join(state.body) if state.body is not None else b""
            state.finish()
            callback = options.get("content_callback")
            if callback is not None:
                callback(response.content)
        return self._finalize(response, prepared, state.status, state.raw_headers,
                              state.hops, started, slot, discard,
                              http_version=state.http_version,
                              header_fields=state.header_fields,
                              reason_phrase=state.reason_phrase,
                              ech_status=state.ech_status)

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
