"""Engine ownership.

A Core ``Engine`` is a whole Chromium ``URLRequestContext``: its own threads,
socket pools, DNS cache, TLS session cache, HTTP cache, and cookie store.
Creating one per request is what made per-request overrides both slow and
stateless, so engines are cached per session and keyed by the configuration that
actually has to differ.

Two rules follow from that:

* Engines are never shared *between* sessions.  Two ``Session`` objects with
  identical configuration must not see each other's cookies.
* Within a session, an override such as ``verify=False`` reuses the same extra
  engine on every later request instead of building a new one.
"""

import os
import threading

from .exceptions import RequestException, map_native_error
from ._native import native

#: Extra engines a single session keeps alive for per-request overrides.  Eight
#: covers proxy rotation across a handful of endpoints without letting a runaway
#: caller hold an unbounded number of Chromium contexts.
DEFAULT_MAX_ENGINES = 8

_FIELDS = ("impersonate", "proxy", "verify", "ca_pem", "user_agent",
           "accept_language", "proxy_username", "proxy_password",
           "http_version", "cache", "profile_namespace", "generation")


class EngineConfig(object):
    """Hashable Engine configuration.

    ``generation`` is not a Core setting.  Bumping it asks for a structurally
    identical engine with an empty cookie store, which is the only way to drop
    cookies the Core already accepted.
    """

    __slots__ = _FIELDS + ("_key",)

    def __init__(self, impersonate=None, proxy=None, verify=True, ca_pem=None,
                 user_agent=None, accept_language=None, proxy_username=None,
                 proxy_password=None, http_version=None, cache=True,
                 profile_namespace=None, generation=0):
        self.impersonate = impersonate
        self.proxy = proxy
        self.verify = bool(verify)
        self.ca_pem = ca_pem
        self.user_agent = user_agent
        self.accept_language = accept_language
        self.proxy_username = proxy_username
        self.proxy_password = proxy_password
        self.http_version = http_version
        self.cache = bool(cache)
        self.profile_namespace = profile_namespace
        self.generation = generation
        self._key = tuple(getattr(self, name) for name in _FIELDS)

    def replace(self, **changes):
        values = {name: getattr(self, name) for name in _FIELDS}
        values.update(changes)
        return EngineConfig(**values)

    @property
    def key(self):
        return self._key

    def __eq__(self, other):
        return isinstance(other, EngineConfig) and self._key == other._key

    def __ne__(self, other):
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self):
        return hash(self._key)

    def __repr__(self):
        parts = ", ".join("%s=%r" % (name, getattr(self, name))
                         for name in _FIELDS if getattr(self, name) not in (None, False))
        return "EngineConfig(%s)" % parts

    def build(self):
        """Creates the Core engine, mapping a rejected configuration.

        The Core rejects contradictions such as a profile plus an explicit
        ``user_agent`` with ``ProfileConflict``; that must not surface as a bare
        ``RuntimeError``.
        """
        try:
            return self._build()
        except RuntimeError as error:
            raise map_native_error(error)

    def _build(self):
        return native.PyEngine(
            self.impersonate, self.proxy, self.verify, self.ca_pem,
            self.user_agent, self.accept_language, self.proxy_username,
            self.proxy_password, self.http_version, self.cache,
            self.profile_namespace,
        )


class EngineSlot(object):
    """One engine plus the cookie bookkeeping tied to its store."""

    __slots__ = ("engine", "config", "mirror")

    def __init__(self, engine, config):
        from .cookies import RequestsCookieJar

        self.engine = engine
        self.config = config
        #: Mirror of what this engine's Chromium cookie store has accepted.  The
        #: store itself is unreachable through ABI v8, and it overrides any
        #: caller-supplied ``Cookie`` header for URLs it has a cookie for, so the
        #: facade needs its own copy to know when the two disagree.
        self.mirror = RequestsCookieJar()


class EngineCache(object):
    """Per-session, bounded, insertion-ordered engine cache.

    Thread-safe: sessions are documented as usable from a thread pool, so two
    workers can race to materialise the same override.
    """

    def __init__(self, max_engines=DEFAULT_MAX_ENGINES):
        self._slots = {}
        self._order = []
        self._max = max(1, int(max_engines))
        self._lock = threading.RLock()
        self._pid = os.getpid()
        self._closed = False

    def _reset_after_fork_locked(self):
        # Chromium's threads do not survive fork(); a child inheriting the parent
        # engine would hang on the first request. Drop them and rebuild lazily.
        if os.getpid() != self._pid:
            self._slots.clear()
            del self._order[:]
            self._pid = os.getpid()

    def get(self, config):
        with self._lock:
            if self._closed:
                raise RequestException("Session is closed")
            self._reset_after_fork_locked()
            slot = self._slots.get(config.key)
            if slot is not None:
                self._order.remove(config.key)
                self._order.append(config.key)
                return slot
            slot = EngineSlot(config.build(), config)
            self._slots[config.key] = slot
            self._order.append(config.key)
            while len(self._order) > self._max:
                evicted = self._order.pop(0)
                self._slots.pop(evicted, None)
            return slot

    def discard(self, config):
        with self._lock:
            if self._slots.pop(config.key, None) is not None:
                self._order.remove(config.key)

    def clear(self):
        with self._lock:
            self._slots.clear()
            del self._order[:]

    def close(self):
        with self._lock:
            self._closed = True
            self._slots.clear()
            del self._order[:]

    @property
    def closed(self):
        return self._closed

    def __len__(self):
        with self._lock:
            return len(self._slots)


def core_version():
    return native.PyEngine.core_version()


def abi_version():
    return native.PyEngine.abi_version()
