"""Type surface of :mod:`chrome_client.engine`.

A Core ``Engine`` is a whole Chromium ``URLRequestContext``: its own threads,
socket pools, DNS cache, TLS session cache, HTTP cache and cookie store.
Creating one per request is what made per-request overrides both slow and
stateless, so engines are cached per session and keyed by the configuration that
actually has to differ.

Two rules follow from that:

* Engines are never shared *between* sessions. Two ``Session`` objects with
  identical configuration must not see each other's cookies.
* Within a session, an override such as ``verify=False`` reuses the same extra
  engine on every later request instead of building a new one.
"""

from typing import Any, Optional, Tuple

from .cookies import RequestsCookieJar

#: Extra engines a single session keeps alive for per-request overrides.
DEFAULT_MAX_ENGINES: int


class EngineConfig:
    """Hashable Engine configuration.

    ``generation`` is not a Core setting.  Bumping it asks for a structurally
    identical engine with an empty cookie store, which is the only way to drop
    cookies the Core already accepted.

    Attributes:
        impersonate: Canonical profile name, or ``None``.
        proxy: Proxy URL, or ``None`` for a direct connection.
        verify: Whether certificate verification is on.
        ca_pem: PEM bytes of a custom CA bundle, when one was given.
        user_agent: Engine-level User-Agent override.
        accept_language: Engine-level ``Accept-Language`` override.
        proxy_username: Proxy credentials, when configured.
        proxy_password: Proxy credentials, when configured.
        http_version: Pinned ``"v1"``/``"v2"``/``"v3"``, or ``None``.
        cache: Whether the Chromium HTTP cache is enabled.
        profile_namespace: Reserved isolation key for the profile.
        generation: Cookie-store generation counter.
        key: The hashable tuple of every field above.
    """

    impersonate: Optional[str]
    proxy: Optional[str]
    verify: bool
    ca_pem: Optional[bytes]
    user_agent: Optional[str]
    accept_language: Optional[str]
    proxy_username: Optional[str]
    proxy_password: Optional[str]
    http_version: Optional[str]
    cache: bool
    profile_namespace: Optional[str]
    generation: int

    def __init__(
        self,
        impersonate: Optional[str] = None,
        proxy: Optional[str] = None,
        verify: bool = True,
        ca_pem: Optional[bytes] = None,
        user_agent: Optional[str] = None,
        accept_language: Optional[str] = None,
        proxy_username: Optional[str] = None,
        proxy_password: Optional[str] = None,
        http_version: Optional[str] = None,
        cache: bool = True,
        profile_namespace: Optional[str] = None,
        generation: int = 0,
    ) -> None:
        """Describes one engine.

        Args:
            impersonate: Canonical profile name.
            proxy: Proxy URL.
            verify: Certificate verification; coerced to ``bool``.
            ca_pem: PEM bytes for a custom CA bundle.
            user_agent: Engine-level User-Agent.
            accept_language: Engine-level ``Accept-Language``.
            proxy_username: Proxy user name.
            proxy_password: Proxy password.
            http_version: Pinned protocol.
            cache: Enable the HTTP cache; coerced to ``bool``.
            profile_namespace: Reserved isolation key.
            generation: Cookie-store generation.
        """
        ...

    def replace(self, **changes: Any) -> "EngineConfig":
        """Returns a copy with fields replaced.

        Args:
            **changes: Field names to change.

        Returns:
            A new :class:`EngineConfig`.

        Example:
            >>> config.replace(generation=1).generation
            1
        """
        ...

    @property
    def key(self) -> Tuple[Any, ...]:
        """The hashable identity of this configuration.

        Returns:
            A tuple of every setting, used as the cache key.
        """
        ...

    def __eq__(self, other: object) -> bool:
        """Compares configurations by their full key.

        Args:
            other: The other object; a non-``EngineConfig`` compares unequal.

        Returns:
            ``True`` when every setting matches."""
        ...
    def __ne__(self, other: object) -> bool:
        """Negation of :meth:`__eq__`.

        Args:
            other: The other object.

        Returns:
            ``True`` when any setting differs."""
        ...
    def __hash__(self) -> int:
        """Hashes the configuration key.

        Returns:
            A hash that is stable for equal configurations."""
        ...
    def __repr__(self) -> str:
        """Returns a summary listing only the fields that were set.

        Returns:
            e.g. ``"EngineConfig(impersonate='chrome_153', cache=True)"``."""
        ...

    def build(self) -> Any:
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

        Example:
            >>> engine = EngineConfig(impersonate="chrome_153").build()
        """
        ...


class EngineSlot:
    """One engine plus the cookie bookkeeping tied to its store.

    Attributes:
        engine: The native engine.
        config: The configuration that produced it.
        mirror: Copy of what this engine's Chromium cookie store has accepted.
            The store itself is unreachable through ABI v8 and overrides any
            caller-supplied ``Cookie`` header for URLs it has a cookie for, so
            the facade keeps its own copy to know when the two disagree.
    """

    engine: Any
    config: EngineConfig
    mirror: RequestsCookieJar

    def __init__(self, engine: Any, config: EngineConfig) -> None:
        """Pairs an engine with a fresh mirror jar.

        Args:
            engine: The native engine.
            config: The configuration it was built from.
        """
        ...


class EngineCache:
    """Per-session, bounded, insertion-ordered engine cache.

    Thread-safe: sessions are documented as usable from a thread pool, so two
    workers can race to materialise the same override.

    Example:
        >>> cache = EngineCache(max_engines=2)
        >>> slot = cache.get(EngineConfig(impersonate="chrome_153"))
        >>> len(cache)
        1
    """

    def __init__(self, max_engines: int = ...) -> None:
        """Creates a cache holding at most ``max_engines`` engines.

        Args:
            max_engines: Ceiling; coerced to at least 1.
        """
        ...

    def get(self, config: EngineConfig) -> EngineSlot:
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
        ...

    def discard(self, config: EngineConfig) -> None:
        """Removes an engine from the cache.

        Args:
            config: The configuration to drop. Dropping an absent key is a no-op.
        """
        ...

    def clear(self) -> None:
        """Empties the cache, releasing the engines it held."""
        ...

    def close(self) -> None:
        """Closes the cache; later :meth:`get` calls raise."""
        ...

    @property
    def closed(self) -> bool:
        """Reports whether the cache was closed.

        Returns:
            ``True`` once :meth:`close` has run."""
        ...

    def __len__(self) -> int:
        """Returns how many engines are cached.

        Returns:
            The number of live slots.
        """
        ...


def core_version() -> Optional[str]:
    """Returns the version string reported by the loaded Core.

    Returns:
        The Core's version, or ``None`` when it reports none.

    Example:
        >>> core_version()      # doctest: +SKIP
        '153.0.8010.37'
    """
    ...


def abi_version() -> int:
    """Returns the Core ABI version this build links against.

    Returns:
        The ABI number, ``8`` for this release.

    Example:
        >>> abi_version()
        8
    """
    ...
