"""Transport identity, engine ownership, and the pool registry.

Three objects, three jobs, and the split is the whole design:

``SessionBinding``
    *What the wire looks like*: the pinned Chromium profile (JA3/JA4, ALPN,
    HTTP/2 SETTINGS and pseudo-header set, QUIC transport parameters), the proxy,
    the TLS verification policy and the HTTP cache mode.  Immutable and hashable,
    because every one of these is frozen into a Chromium ``URLRequestContext`` at
    ``mn_engine_create`` time and cannot be changed afterwards.

``EngineSlot``
    One live ``URLRequestContext`` -- its own socket pools, TLS session cache,
    HTTP/2 and HTTP/3 session pools, Alt-Svc cache, DNS cache, HTTP cache and
    cookie store -- plus the cookie mirror that makes the Core's store readable
    through ABI v8, plus the in-flight count that keeps the context alive while
    work is still running on it.

``EngineCache``
    The bounded registry of slots one session owns.  Bounded because a slot is
    megabytes of threads and caches: a proxy pool larger than the budget turns
    every request into a fresh context and a fresh TCP connection (measured in
    ``docs/LESSONS.md`` §29: 200 requests over a 20-endpoint pool built 200
    contexts and left 200 TIME_WAITs behind).

Two rules follow from the split:

* Engines are never shared *between* sessions.  Two ``Session`` objects with
  identical configuration must not see each other's cookies.
* Within a session, a binding that compares equal hits the same slot.  There is
  no third outcome: either the transport state is reused or a new context is
  built, and ``EngineCache.stats`` reports which happened.
"""

import os
import threading

from .exceptions import RequestException, map_native_error
from ._native import native

#: Extra engines a single session keeps alive for per-request overrides.  Eight
#: covers proxy rotation across a handful of endpoints without letting a runaway
#: caller hold an unbounded number of Chromium contexts.
DEFAULT_MAX_ENGINES = 8

#: Every field that participates in transport identity, in key order.  The order
#: is part of the cache key and therefore of ``__hash__``; adding a field is a
#: deliberate act, not an incidental one.
BINDING_FIELDS = ("impersonate", "proxy", "proxy_username", "proxy_password",
                  "verify", "ca_pem", "user_agent", "accept_language",
                  "http_version", "cache", "profile_namespace")

#: The value every binding field takes when the caller did not set it.  Used by
#: :meth:`SessionBinding.describe` so "which fields did this caller choose?" is
#: answered by comparison against this table rather than by a list of field
#: names that has to be kept in step with the constructor by hand.
BINDING_DEFAULTS = {"verify": True, "cache": True}


class SessionBinding(object):
    """An immutable transport context: profile, proxy and TLS policy.

    A browser decides its TLS fingerprint, its HTTP/2 pseudo-header set and its
    proxy once per profile, not per request, and every connection that profile
    makes inherits them.  This value is that decision.

    Attributes:
        impersonate: Canonical ``chrome_<major>`` profile, or ``None`` for the
            Core's compiled Chromium defaults.  The profile freezes the
            ClientHello extension order, GREASE, ALPN, the SETTINGS frame and the
            QUIC transport parameters together, so they cannot be mixed.
        proxy: Normalised Chromium proxy rule, or ``None`` for a direct
            connection.  Chromium rejects userinfo inside a rule, so credentials
            travel in the two fields below.
        proxy_username: Proxy user name, or ``None``.
        proxy_password: Proxy password, or ``None``.
        verify: Whether the certificate chain and host name are validated.
        ca_pem: PEM bytes of a custom CA bundle, or ``None``.  Part of the key
            because the Core copies the bundle into the engine.
        user_agent: Engine-level ``User-Agent`` override, or ``None``.  A
            profile plus an explicit ``User-Agent`` is a conflict the Core
            reports as ``ProfileConflict``.
        accept_language: Engine-level ``Accept-Language``, or ``None``.
        http_version: ``"v1"``, ``"v2"``, ``"v3"`` or ``None`` to let Chromium
            negotiate.
        cache: Whether the engine owns an in-memory Chromium HTTP cache.
        profile_namespace: Reserved isolation key; the Core accepts only
            ``"minicronet/" + profile``.
    """

    __slots__ = BINDING_FIELDS + ("_key",)

    def __init__(self, impersonate=None, proxy=None, proxy_username=None,
                 proxy_password=None, verify=True, ca_pem=None, user_agent=None,
                 accept_language=None, http_version=None, cache=True,
                 profile_namespace=None):
        """Describes one transport context.

        Args:
            impersonate: Canonical profile name.
            proxy: Normalised proxy rule.
            proxy_username: Proxy user name.
            proxy_password: Proxy password.
            verify: Certificate verification; coerced to ``bool``.
            ca_pem: PEM bytes for a custom CA bundle.
            user_agent: Engine-level ``User-Agent``.
            accept_language: Engine-level ``Accept-Language``.
            http_version: Pinned protocol.
            cache: Enable the HTTP cache; coerced to ``bool``.
            profile_namespace: Reserved isolation key.
        """
        self.impersonate = impersonate
        self.proxy = proxy
        self.proxy_username = proxy_username
        self.proxy_password = proxy_password
        self.verify = bool(verify)
        self.ca_pem = ca_pem
        self.user_agent = user_agent
        self.accept_language = accept_language
        self.http_version = http_version
        self.cache = bool(cache)
        self.profile_namespace = profile_namespace
        self._key = tuple(getattr(self, name) for name in BINDING_FIELDS)

    def replace(self, **changes):
        """Returns a copy with fields replaced.

        Args:
            **changes: Field names to change.

        Returns:
            A new :class:`SessionBinding`; this one is never mutated.

        Raises:
            TypeError: A field name is not part of the binding.
        """
        unknown = sorted(set(changes) - set(BINDING_FIELDS))
        if unknown:
            raise TypeError("unknown binding field(s): %s" % ", ".join(unknown))
        values = {name: getattr(self, name) for name in BINDING_FIELDS}
        values.update(changes)
        return SessionBinding(**values)

    @property
    def key(self):
        """The hashable identity of this context.

        Returns:
            A tuple of every field, used as the engine-cache key.
        """
        return self._key

    @property
    def fingerprint(self):
        """The profile whose wire behaviour this context reproduces.

        Returns:
            The canonical profile name, or ``"chromium_current"`` when the
            Core's compiled defaults are in use.
        """
        return self.impersonate or "chromium_current"

    def describe(self):
        """Returns the fields this caller actually chose.

        A field is listed when it differs from its default, so a bare
        ``SessionBinding()`` describes nothing and a binding built from
        ``SessionBinding(impersonate="chrome_151")`` describes exactly one field.
        ``None`` is the default of every field except ``verify`` and
        ``cache``, which default to ``True``.

        Returns:
            A mapping of field name to value.
        """
        described = {}
        for name in BINDING_FIELDS:
            value = getattr(self, name)
            if value == BINDING_DEFAULTS.get(name):
                continue
            described[name] = value
        return described

    def __eq__(self, other):
        """Compares bindings by their full key.

        Args:
            other: The other object; a non-``SessionBinding`` compares unequal.

        Returns:
            ``True`` when every field matches.
        """
        return isinstance(other, SessionBinding) and self._key == other._key

    def __ne__(self, other):
        """Negation of :meth:`__eq__`.

        Args:
            other: The other object.

        Returns:
            ``True`` when any field differs.
        """
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self):
        """Hashes the binding key.

        Returns:
            A hash stable for equal bindings.
        """
        return hash(self._key)

    def __repr__(self):
        """Returns a summary listing only the fields that were set.

        Returns:
            e.g. ``"SessionBinding(impersonate='chrome_151')"``.
        """
        parts = ", ".join("%s=%r" % item for item in sorted(self.describe().items()))
        return "SessionBinding(%s)" % parts


class EngineConfig(object):
    """A binding plus the cookie-store generation an engine was built for.

    ``generation`` is not a Core setting.  Bumping it asks for a structurally
    identical engine with an empty cookie store, which is the only way to drop
    cookies the Core already accepted: ABI v8 exposes no entry point that clears
    ``CookieMonster``.

    Attributes:
        impersonate: Canonical profile name, or ``None``.
        proxy: Proxy rule, or ``None`` for a direct connection.
        proxy_username: Proxy credentials, when configured.
        proxy_password: Proxy credentials, when configured.
        verify: Whether certificate verification is on.
        ca_pem: PEM bytes of a custom CA bundle, when one was given.
        user_agent: Engine-level ``User-Agent`` override.
        accept_language: Engine-level ``Accept-Language`` override.
        http_version: Pinned ``"v1"``/``"v2"``/``"v3"``, or ``None``.
        cache: Whether the Chromium HTTP cache is enabled.
        profile_namespace: Reserved isolation key for the profile.
        generation: Cookie-store generation counter.
        key: The hashable tuple of every field above.
    """

    __slots__ = BINDING_FIELDS + ("generation", "_key")

    def __init__(self, impersonate=None, proxy=None, verify=True, ca_pem=None,
                 user_agent=None, accept_language=None, proxy_username=None,
                 proxy_password=None, http_version=None, cache=True,
                 profile_namespace=None, generation=0):
        """Describes one engine.

        Args:
            impersonate: Canonical profile name.
            proxy: Proxy URL.
            verify: Certificate verification; coerced to ``bool``.
            ca_pem: PEM bytes for a custom CA bundle.
            user_agent: Engine-level ``User-Agent``.
            accept_language: Engine-level ``Accept-Language``.
            proxy_username: Proxy user name.
            proxy_password: Proxy password.
            http_version: Pinned protocol.
            cache: Enable the HTTP cache; coerced to ``bool``.
            profile_namespace: Reserved isolation key.
            generation: Cookie-store generation.
        """
        self.impersonate = impersonate
        self.proxy = proxy
        self.proxy_username = proxy_username
        self.proxy_password = proxy_password
        self.verify = bool(verify)
        self.ca_pem = ca_pem
        self.user_agent = user_agent
        self.accept_language = accept_language
        self.http_version = http_version
        self.cache = bool(cache)
        self.profile_namespace = profile_namespace
        self.generation = generation
        self._key = tuple(getattr(self, name) for name in BINDING_FIELDS) + (generation,)

    @property
    def binding(self):
        """The transport context this engine reproduces.

        Returns:
            A :class:`SessionBinding` built from every field except ``generation``.
        """
        return SessionBinding(**{name: getattr(self, name) for name in BINDING_FIELDS})

    def replace(self, **changes):
        """Returns a copy with fields replaced.

        Args:
            **changes: Field names to change, including ``generation``.

        Returns:
            A new :class:`EngineConfig`.

        Raises:
            TypeError: A field name is not part of the configuration.
        """
        allowed = set(BINDING_FIELDS) | {"generation"}
        unknown = sorted(set(changes) - allowed)
        if unknown:
            raise TypeError("unknown engine field(s): %s" % ", ".join(unknown))
        values = {name: getattr(self, name) for name in BINDING_FIELDS}
        values["generation"] = self.generation
        values.update(changes)
        return EngineConfig(**values)

    @property
    def key(self):
        """The hashable identity of this configuration.

        Returns:
            A tuple of every setting, used as the cache key.
        """
        return self._key

    def __eq__(self, other):
        """Compares configurations by their full key.

        Args:
            other: The other object; a non-``EngineConfig`` compares unequal.

        Returns:
            ``True`` when every setting matches.
        """
        return isinstance(other, EngineConfig) and self._key == other._key

    def __ne__(self, other):
        """Negation of :meth:`__eq__`.

        Args:
            other: The other object.

        Returns:
            ``True`` when any setting differs.
        """
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self):
        """Hashes the configuration key.

        Returns:
            A hash that is stable for equal configurations.
        """
        return hash(self._key)

    def __repr__(self):
        """Returns a summary listing only the fields that were set.

        Returns:
            e.g. ``"EngineConfig(impersonate='chrome_155', cache=True)"``.
        """
        parts = ", ".join("%s=%r" % (name, getattr(self, name))
                         for name in BINDING_FIELDS + ("generation",)
                         if getattr(self, name) not in (None, False))
        return "EngineConfig(%s)" % parts

    def build(self):
        """Creates the Core engine, mapping a rejected configuration.

        The Core rejects contradictions such as a profile plus an explicit
        ``user_agent`` with ``ProfileConflict``; that must not surface as a bare
        ``RuntimeError``.

        Returns:
            The native engine object.

        Raises:
            ImpersonateError: The profile conflicts with another setting, or is
                unsupported by this Core.
            RequestException: Any other Core-side initialisation failure.
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
    """One engine, its cookie mirror, and its in-flight count.

    The count is what makes graceful teardown possible: a slot that still has
    work running on it must not be dropped, because dropping the last reference
    tears down the ``URLRequestContext`` that work is using.

    Attributes:
        engine: The native engine.
        config: The configuration that produced it.
        mirror: Copy of what this engine's Chromium cookie store has accepted.
            The store itself is unreachable through ABI v8 and overrides any
            caller-supplied ``Cookie`` header for URLs it has a cookie for, so
            the facade keeps its own copy to know when the two disagree.
    """

    __slots__ = ("engine", "config", "mirror", "_lock", "_inflight")

    def __init__(self, engine, config):
        """Pairs an engine with a fresh mirror jar.

        Args:
            engine: The native engine.
            config: The configuration it was built from.
        """
        from .cookies import RequestsCookieJar

        self.engine = engine
        self.config = config
        self.mirror = RequestsCookieJar()
        #: Guards the compound mirror operations.  ``cookielib`` already locks a
        #: single jar, but "read the outgoing header, compare it with the store,
        #: decide whether the store has to be replaced" is three steps over two
        #: jars, and two workers interleaving them can pick different
        #: generations for the same request.
        self._lock = threading.RLock()
        self._inflight = 0

    @property
    def binding(self):
        """The transport context this slot reproduces.

        Returns:
            The :class:`SessionBinding` of the configuration it was built from.
        """
        return self.config.binding

    @property
    def inflight(self):
        """How many requests are using this slot right now.

        Returns:
            The number of acquired, not yet released requests.
        """
        with self._lock:
            return self._inflight

    def cookie_header(self, url):
        """Returns the ``Cookie`` line the Core's store would emit for *url*.

        Args:
            url: The request URL.

        Returns:
            The header value, or ``None`` when the store holds no cookie for it.
        """
        with self._lock:
            return self.mirror.cookie_header(url)

    def absorb(self, url, lines):
        """Records ``Set-Cookie`` lines the Core accepted for *url*.

        Args:
            url: The URL the response came from.
            lines: The raw ``Set-Cookie`` field values.
        """
        with self._lock:
            self.mirror.absorb_set_cookie(url, lines)

    def _retain(self):
        with self._lock:
            self._inflight += 1

    def _release(self):
        with self._lock:
            if self._inflight:
                self._inflight -= 1


class EngineCache(object):
    """Per-session, bounded, insertion-ordered registry of engines.

    Thread-safe: sessions are documented as usable from a thread pool, so two
    workers can race to materialise the same override.

    Example:
        >>> cache = EngineCache(max_engines=2)
        >>> slot = cache.get(EngineConfig(impersonate="chrome_155"))
        >>> len(cache)
        1
    """

    def __init__(self, max_engines=DEFAULT_MAX_ENGINES):
        """Creates a cache holding at most ``max_engines`` engines.

        Args:
            max_engines: Ceiling; coerced to at least 1.
        """
        self._slots = {}
        self._order = []
        self._max = max(1, int(max_engines))
        self._lock = threading.RLock()
        self._pid = os.getpid()
        self._closed = False
        self._stats = {"hits": 0, "builds": 0, "evictions": 0, "deferred": 0}

    def _reset_after_fork_locked(self):
        # Chromium's threads do not survive fork(); a child inheriting the parent
        # engine would hang on the first request. Drop them and rebuild lazily.
        if os.getpid() != self._pid:
            self._slots.clear()
            del self._order[:]
            self._pid = os.getpid()
            self._stats = {"hits": 0, "builds": 0, "evictions": 0, "deferred": 0}

    def get(self, config):
        """Returns the engine for a configuration, building it on first use.

        Reusing a slot is what preserves the connection pool, the TLS session
        cache and the cookie store across requests. A fork (detected by pid) drops
        every inherited engine, because Chromium's threads do not survive
        ``fork()``.

        Args:
            config: The configuration to look up.

        Returns:
            The matching :class:`EngineSlot`, refreshing its recency.

        Raises:
            RequestException: The session was closed.

        Example:
            >>> slot = cache.get(EngineConfig(proxy="http://127.0.0.1:8080"))
        """
        with self._lock:
            if self._closed:
                raise RequestException("Session is closed")
            self._reset_after_fork_locked()
            slot = self._slots.get(config.key)
            if slot is not None:
                self._order.remove(config.key)
                self._order.append(config.key)
                self._stats["hits"] += 1
                return slot
            slot = EngineSlot(config.build(), config)
            self._slots[config.key] = slot
            self._order.append(config.key)
            self._stats["builds"] += 1
            while len(self._order) > self._max:
                evicted = self._order.pop(0)
                victim = self._slots.pop(evicted, None)
                self._stats["evictions"] += 1
                if victim is not None and victim.inflight:
                    # Dropping a slot with work running would tear down the
                    # context underneath it. The slot object stays alive through
                    # the response holding it; only the cache entry is gone, and
                    # the count records that this eviction was not free.
                    self._stats["deferred"] += 1
            return slot

    def acquire(self, config):
        """Returns a context manager that pins a slot for one request.

        Args:
            config: The configuration to look up.

        Returns:
            A context manager yielding the :class:`EngineSlot`; leaving it
            releases the in-flight reference.
        """
        return _SlotLease(self, config)

    def discard(self, config):
        """Removes an engine from the cache.

        The engine itself stays alive through whoever still holds its slot: a
        response, a WebSocket, or the Core request that is reading from it.
        Discarding only makes the slot unreachable for *future* lookups, which
        is what a superseded cookie-store generation needs -- it must not be
        selected again, and it must not occupy one of the bounded cache slots.

        Args:
            config: The configuration to drop. Dropping an absent key is a no-op.

        Returns:
            ``None``.
        """
        with self._lock:
            if self._slots.pop(config.key, None) is not None:
                try:
                    self._order.remove(config.key)
                except ValueError:
                    pass

    def clear(self):
        """Empties the cache, releasing the engines it held."""
        with self._lock:
            self._slots.clear()
            del self._order[:]

    def close(self):
        """Closes the cache; later :meth:`get` calls raise."""
        with self._lock:
            self._closed = True
            self._slots.clear()
            del self._order[:]

    @property
    def closed(self):
        """Reports whether the cache was closed.

        Returns:
            ``True`` once :meth:`close` has run.
        """
        return self._closed

    @property
    def max_engines(self):
        """The ceiling this cache enforces.

        Returns:
            The coerced engine budget, at least ``1``.
        """
        return self._max

    @property
    def stats(self):
        """Returns a snapshot of the cache counters.

        Returns:
            A dict with ``hits``, ``builds``, ``evictions``, ``deferred`` and
            ``live``.
        """
        with self._lock:
            snapshot = dict(self._stats)
            snapshot["live"] = len(self._slots)
            return snapshot

    def inflight(self):
        """Returns how many requests the cached engines are serving.

        Returns:
            The sum of every slot's in-flight count.
        """
        with self._lock:
            slots = list(self._slots.values())
        return sum(slot.inflight for slot in slots)

    def __len__(self):
        """Returns how many engines are cached.

        Returns:
            The number of live slots.
        """
        with self._lock:
            return len(self._slots)


class _SlotLease(object):
    """Pins one engine slot for the duration of a request.

    The pin is what tells an eviction that the slot is still in use.  A
    streaming response keeps reading from the slot's context after the lease
    ends -- the Core request holds its own reference to the Chromium context --
    but the facade's mirror and in-flight count belong to the lease, so the
    window the lease covers is exactly the window in which the facade still
    reads or writes slot state.

    Attributes:
        slot: The pinned :class:`EngineSlot`, or ``None`` outside the block.
    """

    __slots__ = ("_cache", "_config", "_slot")

    def __init__(self, cache, config):
        """Prepares a lease.

        Args:
            cache: The :class:`EngineCache` to pin against.
            config: The configuration to look up.
        """
        self._cache = cache
        self._config = config
        self._slot = None

    @property
    def slot(self):
        """The pinned slot.

        Returns:
            The :class:`EngineSlot` acquired by :meth:`__enter__`, or ``None``.
        """
        return self._slot

    def rebind(self, slot):
        """Moves the pin to a different slot of the same cache.

        Cookie resolution may replace the engine when the caller's jar and the
        Core's store disagree, and the replacement is the one the request
        actually runs on.  Without moving the pin, the in-flight count would
        describe an engine nobody is using while the one in use looked idle.

        Args:
            slot: The :class:`EngineSlot` now serving the request.
        """
        if slot is self._slot:
            return
        previous, self._slot = self._slot, slot
        slot._retain()
        if previous is not None:
            previous._release()

    def __enter__(self):
        slot = self._cache.get(self._config)
        slot._retain()
        self._slot = slot
        return slot

    def __exit__(self, *_exc):
        slot, self._slot = self._slot, None
        if slot is not None:
            slot._release()
        return False


def core_version():
    """Returns the version string reported by the loaded Core.

    Returns:
        The Core's version, or ``None`` when it reports none.

    Example:
        >>> core_version()      # doctest: +SKIP
        '153.0.8010.37'
    """
    return native.PyEngine.core_version()


def abi_version():
    """Returns the Core ABI version this build links against.

    Returns:
        The ABI number, ``8`` for this release.

    Example:
        >>> abi_version()
        8
    """
    return native.PyEngine.abi_version()
