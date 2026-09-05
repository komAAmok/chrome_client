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
import os
import random
import time
from collections import deque
from urllib.parse import urljoin, urlsplit

try:
    from collections.abc import Mapping
except ImportError:  # Python 3.6
    from collections import Mapping

from . import multipart as _multipart
from .auth import build_auth
from .cookies import RequestsCookieJar, cookiejar_from_dict, merge_cookies
from .engine import DEFAULT_MAX_ENGINES, EngineConfig, EngineCache
from .exceptions import (InvalidURL, RequestException, ResponseTooLarge,
                         SessionClosed, Timeout, TooManyRedirects,
                         UnsupportedFeature, map_native_error)
from .impersonate import (normalize_http_version, normalize_impersonate,
                          reject_fingerprint_overrides, validate_extra_fp)
from .models import (DEFAULT_REDIRECT_LIMIT, PreparedRequest, Request, Response,
                     AsyncResponse, build_url, http_version_from_status_line,
                     parse_raw_headers, reason_from_status_line)
from .structures import CaseInsensitiveDict, Headers
from .utils import (default_headers as _default_headers, get_netrc_auth,
                    requote_uri, resolve_proxies, should_bypass_proxies,
                    to_key_val_list)

#: Events drained from the Core per event-loop wakeup.  Batching is what keeps a
#: large download from costing one loop round trip per Core chunk.
ASYNC_POLL_BATCH = 64

#: Bytes buffered per streaming async response before the reader is throttled.
STREAM_BUFFER_LIMIT = 1024 * 1024


#: Chromium removes any caller-set ``Referer`` extra header, and ABI v8 has no
#: referrer field, so the request cannot be honoured either way.
_REFERER_MESSAGE = (
    "Referer cannot be set: Chromium owns the referrer and strips a "
    "caller-supplied Referer header, and ABI v8 exposes no referrer field. "
    "Nothing this client sends would reach the wire."
)


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
        return max(values) if values else None
    return timeout


def _validate_max_response_bytes(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("max_response_bytes must be a non-negative integer or None")
    return value


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

    #: Options named by curl_cffi that need Core support this ABI does not have.
    #: They are rejected rather than ignored so a caller never assumes an
    #: unimplemented fingerprint or transport setting took effect.
    _UNSUPPORTED = ("cert", "interface", "doh_url", "curl_options")

    def __init__(self, impersonate=None, proxy=None, proxies=None, proxy_auth=None,
                 verify=True, timeout=None, headers=None, cookies=None, params=None,
                 auth=None, cert=None, stream=False, hooks=None,
                 max_redirects=DEFAULT_REDIRECT_LIMIT, trust_env=True,
                 allow_redirects=True, max_response_bytes=None, base_url=None,
                 http_version=None, ja3=None, akamai=None, perk=None, extra_fp=None,
                 default_headers=True, default_encoding="utf-8",
                 discard_cookies=False, raise_for_status=False, retry=0, cache=True,
                 user_agent=None, accept_language=None, interface=None, doh_url=None,
                 max_recv_speed=0, curl_options=None, max_engines=DEFAULT_MAX_ENGINES,
                 response_class=None):
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
        self.max_redirects = max_redirects
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
        if "referer" in self.headers:
            raise UnsupportedFeature(_REFERER_MESSAGE)
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
        #: Jar revision last mirrored from a response.  A later value means the
        #: caller edited ``self.cookies`` and the Core's store must be replaced.
        self._cookie_revision = self.cookies.revision
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
            for key, value in environment_proxies.items():
                proxies.setdefault(key, value)
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
    def _engine_config(self, impersonate, proxy, verify, http_version):
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
        username = password = None
        if self.proxy_auth:
            username, password = self.proxy_auth
        return EngineConfig(
            impersonate=impersonate if impersonate is not None else self.impersonate,
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

    def _effective_proxy(self, url, proxy, proxies):
        if proxy is not None:
            return proxy
        mapping = dict(self.proxies)
        if proxies:
            mapping.update(proxies)
        selected = proxy_from_proxies(url, mapping) if mapping else None
        if selected is None and self.trust_env and not should_bypass_proxies(url):
            environment = resolve_proxies(url, {}, trust_env=True)
            selected = proxy_from_proxies(url, environment) if environment else None
        return selected

    def _slot(self, url, impersonate, proxy, proxies, verify, http_version):
        self._ensure_open()
        config = self._engine_config(
            impersonate, self._effective_proxy(url, proxy, proxies), verify, http_version)
        return self._engines.get(config)

    # -- cookie policy ------------------------------------------------------
    def _resolve_cookies(self, slot, prepared, discard):
        """Decides the outgoing ``Cookie`` header, rebuilding the engine if needed.

        Returns the ``EngineSlot`` to use, which differs from *slot* only when the
        Core's cookie store has to be discarded for a caller edit to be visible.
        """
        if discard:
            prepared.headers.pop("Cookie", None)
            return slot
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
        if desired:
            prepared.headers["Cookie"] = desired
        return replacement

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
        self._cookie_revision = self.cookies.revision

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
        response.http_version = http_version_from_status_line(status_line)
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
            hop.http_version = http_version_from_status_line(hop_line)
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
        if values.get("referer") is not None:
            raise UnsupportedFeature(_REFERER_MESSAGE)
        if headers is not None and "referer" in headers:
            # Chromium owns the referrer through `URLRequest::SetReferrer` and
            # strips a caller-supplied `Referer` extra header. Dropping it
            # silently would leave a fingerprint gap nobody could debug.
            raise UnsupportedFeature(_REFERER_MESSAGE)
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

        allow_redirects = values.get("allow_redirects")
        allow_redirects = self.allow_redirects if allow_redirects is None else allow_redirects
        max_redirects = values.get("max_redirects")
        effective_max = self.max_redirects if max_redirects is None else max_redirects
        python_redirects = self._drive_redirects_in_python(max_redirects, allow_redirects)
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
            "priority": values.get("priority"),
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

    # -- redirect policy ----------------------------------------------------
    def _drive_redirects_in_python(self, max_redirects, allow_redirects):
        """True when the caller's redirect cap is tighter than Chromium's.

        Chromium follows redirects inside one ``URLRequest``, which preserves the
        original request's site-for-cookies and is the higher-fidelity path, but
        it enforces its own cap (20) rather than the caller's.  A tighter cap is
        therefore honoured by re-issuing each hop from Python instead.
        """
        return bool(allow_redirects) and max_redirects is not None \
            and max_redirects < DEFAULT_REDIRECT_LIMIT

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


class Session(BaseSession):
    """Synchronous session.

    Safe to share across threads: every blocking Core call releases the GIL, and
    the engine cache is locked.  One session per thread still performs better,
    because Chromium serialises requests that share an HTTP cache key inside one
    engine.
    """

    def request(self, method, url, params=None, data=None, headers=None, cookies=None,
                files=None, auth=None, timeout=None, allow_redirects=True, proxies=None,
                hooks=None, stream=None, verify=None, cert=None, json=None,
                content=None, multipart=None, impersonate=None, proxy=None,
                http_version=None, max_redirects=None, max_response_bytes=None,
                referer=None, accept_encoding=None, default_encoding=None,
                discard_cookies=None, retry=None, cache_mode=None, priority=None,
                ja3=None, akamai=None, perk=None, extra_fp=None, content_callback=None,
                raise_for_status=None, quote=None, curl_options=None, interface=None,
                doh_url=None, max_recv_speed=None, thread=None, debug=None):
        request, options = self._prepare_call(locals())
        return self.send(self.prepare_request(request), **options)

    def send(self, request, **options):
        """Sends a ``PreparedRequest``, as ``requests.Session.send`` does."""
        self._ensure_open()
        if not isinstance(request, PreparedRequest):
            raise ValueError("You can only send PreparedRequests")
        adapter = self.get_adapter(request.url)
        from .adapters import HTTPAdapter
        if not isinstance(adapter, HTTPAdapter):
            return adapter.send(
                request, stream=bool(options.get("stream")),
                timeout=options.get("timeout"), verify=options.get("verify", True),
                cert=options.get("cert"), proxies=options.get("proxies"))
        retry = options.get("retry") or self.retry
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._send_once(request, options)
            except ResponseTooLarge:
                raise
            except RequestException:
                if attempt > retry.count:
                    raise
                time.sleep(retry.sleep_for(attempt))
                continue
            if retry.should_retry_status(response.status_code) and attempt <= retry.count:
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
                    native.cancel()
                    raise Timeout("request timed out")
                if hop is not None:
                    # Chromium holds the hop open waiting for `follow_redirect`;
                    # the caller asked not to follow, so the 3xx is the response.
                    native.cancel()
                    return self._finalize(self._new_response(), prepared, hop[0], hop[1],
                                          [], started, slot, discard, body=b"")
                status_code, raw_headers = head
        except RuntimeError as error:
            raise map_native_error(error, request=prepared)
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
        while response.is_redirect:
            if count >= self.max_redirects:
                raise TooManyRedirects("Exceeded %d redirects." % self.max_redirects,
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

    def __init__(self, *args, **kwargs):
        max_clients = kwargs.pop("max_clients", None)
        BaseSession.__init__(self, *args, **kwargs)
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
                      allow_redirects=True, proxies=None, hooks=None, stream=None,
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
        self._ensure_open()
        if not isinstance(request, PreparedRequest):
            raise ValueError("You can only send PreparedRequests")
        retry = options.get("retry") or self.retry
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._send_once(request, options)
            except ResponseTooLarge:
                raise
            except RequestException:
                if attempt > retry.count:
                    raise
                await asyncio.sleep(retry.sleep_for(attempt))
                continue
            if retry.should_retry_status(response.status_code) and attempt <= retry.count:
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
        except RuntimeError as error:
            state.finish(cancel=True)
            raise map_native_error(error, request=prepared)
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
        if stream and follow:
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
        while not state.status:
            if state.error is not None:
                raise state.error
            if state.done:
                break
            await state.wait()
        if state.error is not None:
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
