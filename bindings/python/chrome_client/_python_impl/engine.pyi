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

from typing import Any, List, Optional, Tuple

from .cookies import RequestsCookieJar
from .impersonate import ChromeProfileName, HttpVersionNormalized

#: Extra engines a single session keeps alive for per-request overrides.
DEFAULT_MAX_ENGINES: int


class SessionBinding:
    """An immutable transport context: profile, proxy and TLS policy.

    A browser decides its TLS fingerprint, its HTTP/2 pseudo-header set and its
    proxy once per profile, not per request, and every connection that profile
    makes inherits them. This value is that decision: it is what an
    :class:``EngineConfig`` is built from and what the engine cache keys on.

    Attributes:
        impersonate: Canonical profile name, or ``None``.
        proxy: Normalised proxy rule, or ``None`` for a direct connection.
        proxy_username: Proxy user name, or ``None``.
        proxy_password: Proxy password, or ``None``.
        verify: Whether certificate verification is on.
        ca_pem: PEM bytes of a custom CA bundle, when one was given.
        user_agent: Engine-level ``User-Agent`` override, or ``None``.
        accept_language: Engine-level ``Accept-Language``, or ``None``.
        http_version: ``"v1"``/``"v2"``/``"v3"``, or ``None``.
        cache: Whether the Chromium HTTP cache is enabled.
        profile_namespace: Reserved isolation key for the profile.
        host_mapping: Chromium MappedHostResolver rules, or ``None``. Part of
            the key: two engines given different rules reach different addresses.
        key: The hashable tuple of every field above.
        fingerprint: The profile whose wire behaviour this context reproduces.
    """

    impersonate: Optional[ChromeProfileName]
    proxy: Optional[str]
    proxy_username: Optional[str]
    proxy_password: Optional[str]
    verify: bool
    ca_pem: Optional[bytes]
    user_agent: Optional[str]
    accept_language: Optional[str]
    http_version: Optional[HttpVersionNormalized]
    cache: bool
    profile_namespace: Optional[str]
    host_mapping: Optional[str]
    dns_nameservers: Optional[str]
    doh_template: Optional[str]
    dns_secure_mode: int
    dns_over_tls: bool
    dns_rotate: bool
    dns_padding: bool
    hsts_bypass: Optional[str]
    proxy_chain: Optional[str]
    platform: str
    max_sockets_per_group: int

    def __init__(
        self,
        impersonate: Optional[str] = None,
        proxy: Optional[str] = None,
        proxy_username: Optional[str] = None,
        proxy_password: Optional[str] = None,
        verify: bool = True,
        ca_pem: Optional[bytes] = None,
        user_agent: Optional[str] = None,
        accept_language: Optional[str] = None,
        http_version: Optional[str] = None,
        cache: bool = True,
        profile_namespace: Optional[str] = None,
        host_mapping: Optional[str] = None,
        dns_nameservers: Optional[str] = None,
        doh_template: Optional[str] = None,
        dns_secure_mode: int = 0,
        dns_over_tls: bool = False,  # refused; see the constructor docstring
        dns_rotate: bool = False,
        dns_padding: bool = False,
        hsts_bypass: Optional[str] = None,
        proxy_chain: Optional[str] = None,
        platform: str = "auto",
        max_sockets_per_group: int = 0,
    ) -> None:
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
            host_mapping: Chromium MappedHostResolver rules, or ``None``.
        """
        ...

    def replace(self, **changes: Any) -> "SessionBinding":
        """Returns a copy with fields replaced.

        Args:
            **changes: Field names to change.

        Returns:
            A new :class:``SessionBinding``; this one is never mutated.

        Raises:
            TypeError: A field name is not part of the binding.
        """
        ...

    @property
    def key(self) -> Tuple[Any, ...]:
        """The hashable identity of this context.

        Returns:
            A tuple of every field, used as the engine-cache key.
        """
        ...

    @property
    def fingerprint(self) -> str:
        """The profile whose wire behaviour this context reproduces.

        Returns:
            The canonical profile name, or ``"chromium_current"``.
        """
        ...

    def describe(self) -> dict:
        """Returns the fields this caller actually chose.

        A field is listed when it differs from its default, so a bare
        ``SessionBinding()`` describes nothing and a binding built from
        ``SessionBinding(impersonate=...)`` describes exactly one field.

        Returns:
            A mapping of field name to value.
        """
        ...

    def __eq__(self, other: object) -> bool:
        """Compares bindings by their full key.

        Args:
            other: The other object.

        Returns:
            ``True`` when every field matches.
        """
        ...

    def __ne__(self, other: object) -> bool:
        """Negation of :meth:``__eq__``.

        Args:
            other: The other object.

        Returns:
            ``True`` when any field differs.
        """
        ...

    def __hash__(self) -> int:
        """Hashes the binding key.

        Returns:
            A hash stable for equal bindings.
        """
        ...

    def __repr__(self) -> str:
        """Returns a summary listing only the fields that were set.

        Returns:
            e.g. a SessionBinding(...) summary of the non-default fields.
        """
        ...


class EngineConfig:
    """Hashable Engine configuration.

    There is no cookie-store generation: the store is writable in place since
    ABI v9, so a cookie edit never asks for another engine.

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
        host_mapping: Chromium MappedHostResolver rules, or ``None``. Part of
            the key: two engines given different rules reach different addresses.
        key: The hashable tuple of every field above.
    """

    #: The canonical profile this engine was built for, already normalised, or
    #: ``None`` for Chromium's own defaults.
    impersonate: Optional[ChromeProfileName]
    proxy: Optional[str]
    verify: bool
    ca_pem: Optional[bytes]
    user_agent: Optional[str]
    accept_language: Optional[str]
    proxy_username: Optional[str]
    proxy_password: Optional[str]
    #: ``"v1"``/``"v2"``/``"v3"``, or ``None`` to let Chromium negotiate.
    http_version: Optional[HttpVersionNormalized]
    cache: bool
    profile_namespace: Optional[str]
    host_mapping: Optional[str]
    dns_nameservers: Optional[str]
    doh_template: Optional[str]
    dns_secure_mode: int
    dns_over_tls: bool
    dns_rotate: bool
    dns_padding: bool
    hsts_bypass: Optional[str]
    proxy_chain: Optional[str]
    platform: str
    max_sockets_per_group: int

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
        host_mapping: Optional[str] = None,
        dns_nameservers: Optional[str] = None,
        doh_template: Optional[str] = None,
        dns_secure_mode: int = 0,
        dns_over_tls: bool = False,  # refused; see the constructor docstring
        dns_rotate: bool = False,
        dns_padding: bool = False,
        hsts_bypass: Optional[str] = None,
        proxy_chain: Optional[str] = None,
        platform: str = "auto",
        max_sockets_per_group: int = 0,
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
            host_mapping: Chromium MappedHostResolver rules, or ``None``.
        """
        ...

    @property
    def binding(self) -> SessionBinding:
        """The transport context this engine reproduces.

        Returns:
            A :class:``SessionBinding`` built from every field except ``generation``.
        """
        ...

    def replace(self, **changes: Any) -> "EngineConfig":
        """Returns a copy with fields replaced.

        Args:
            **changes: Field names to change.

        Returns:
            A new :class:`EngineConfig`.

        Example:
            >>> EngineConfig(proxy="http://h:1").replace(proxy="http://h:2").proxy
            'http://h:2'
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
            e.g. ``"EngineConfig(impersonate='chrome_155', cache=True)"``."""
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
            >>> engine = EngineConfig(impersonate="chrome_155").build()
        """
        ...


class EngineSlot:
    """One engine plus the cookie store tied to it.

    There is no mirror any more: the store is readable and writable through the
    ABI since v9, so this class forwards to it instead of keeping a copy to
    compare against.

    Attributes:
        engine: The native engine.
        config: The configuration that produced it.
    """

    engine: Any
    config: EngineConfig

    def __init__(self, engine: Any, config: EngineConfig,
                 single_use: bool = False) -> None:
        """Pairs an engine with its configuration.

        Args:
            engine: The native engine.
            config: The configuration it was built from.
            single_use: Whether no cache owns this slot, i.e. it was built for
                one request and may be released as soon as that request ends.
        """
        ...


    @property
    def single_use(self) -> bool:
        """True when no cache owns this slot.

        Returns:
            ``True`` for an engine built for one discard request.
        """
        ...

    def release_engine(self) -> None:
        """Drops the native engine reference, which is what frees the context.

        Idempotent, so a slot that reached the retire queue twice cannot raise.
        """
        ...
    @property
    def binding(self) -> SessionBinding:
        """The transport context this slot reproduces.

        Returns:
            The :class:`SessionBinding` of the configuration it was built from.
        """
        ...

    @property
    def inflight(self) -> int:
        """How many requests are using this slot right now.

        Returns:
            The number of acquired, not yet released requests.
        """
        ...

    def cookies(
        self, url: Optional[str] = None
    ) -> List[Tuple[str, str, str, str, int, bool, bool, bool, str]]:
        """Returns the cookies the store holds.

        Args:
            url: Restrict to the cookies a request to this URL would carry.

        Returns:
            ``(name, value, domain, path, expiry, has_expiry, secure,
            http_only, same_site)`` tuples.
        """
        ...

    def set_cookie(self, cookie: Tuple[Any, ...], source_url: str) -> None:
        """Writes one cookie through the store's own validation.

        Args:
            cookie: ``(name, value, domain, path, expires, secure, http_only,
                same_site)``.
            source_url: The URL the cookie is being set from.
        """
        ...

    def delete_cookie(self, domain: str, path: str, name: str) -> int:
        """Deletes every matching cookie.

        Args:
            domain: Cookie domain.
            path: Cookie path.
            name: Cookie name.

        Returns:
            How many cookies were deleted.
        """
        ...

    def clear_cookies(self) -> None:
        """Empties the store."""
        ...

    def set_client_certificate(
        self, host: str, port: int, cert_pem: bytes, key_pem: bytes
    ) -> None:
        """Registers the client certificate this engine presents to one server.

        Args:
            host: The server hostname the certificate is registered for.
            port: The server port.
            cert_pem: PEM bytes; CERTIFICATE blocks, optionally followed by
                the private key.
            key_pem: PEM bytes holding exactly one unencrypted private key.

        Raises:
            SSLError: The Core rejected the certificate or key.
        """
        ...

    def clear_client_certificate(self, host: str, port: int) -> None:
        """Forgets the certificate registered for (host, port).

        Args:
            host: The server hostname to unregister.
            port: The server port.

        Raises:
            SSLError: The Core rejected the call.
        """
        ...

    def write_cookie(self, cookie: Any, source_url: str) -> None:
        """Mirrors one jar cookie into the store.

        Args:
            cookie: A ``cookielib.Cookie``.
            source_url: The URL the cookie came from.
        """
        ...

    def remove_cookie(self, domain: str, path: str, name: str) -> None:
        """Mirrors one jar removal into the store.

        Args:
            domain: Cookie domain.
            path: Cookie path.
            name: Cookie name.
        """
        ...

class EngineCache:
    """Per-session, bounded, insertion-ordered engine cache.

    Thread-safe: sessions are documented as usable from a thread pool, so two
    workers can race to materialise the same override.

    Example:
        >>> cache = EngineCache(max_engines=2)
        >>> slot = cache.get(EngineConfig(impersonate="chrome_155"))
        >>> len(cache)
        1
    """

    def __init__(self, max_engines: int = ...) -> None:
        """Creates a cache holding at most ``max_engines`` engines.

        Args:
            max_engines: Ceiling, at least 1.

        Raises:
            ValueError: ``max_engines`` is not a positive integer. A wrong type
                or a non-positive count is refused here rather than silently
                coerced, matching every other limit on the session.
        """
        ...

    def acquire(self, config: EngineConfig) -> Any:
        """Returns a context manager that pins a slot for one request.

        Args:
            config: The configuration to look up.

        Returns:
            A context manager yielding the :class:``EngineSlot``; leaving it releases
            the in-flight reference.
        """
        ...

    @property
    def max_engines(self) -> int:
        """The ceiling this cache enforces.

        Returns:
            The coerced engine budget, at least ``1``.
        """
        ...

    @property
    def stats(self) -> dict:
        """Returns a snapshot of the cache counters.

        Returns:
            A dict with ``hits``, ``builds``, ``evictions``, ``deferred`` and ``live``.
        """
        ...

    def inflight(self) -> int:
        """Returns how many requests the cached engines are serving.

        Returns:
            The sum of every slot's in-flight count.
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

    def slots(self) -> List[EngineSlot]:
        """Returns the slots the cache currently holds.

        A snapshot, not a view: the caller iterates it while another thread may
        evict, and a live view would raise mid-iteration.

        Returns:
            A list of the live :class:`EngineSlot` objects.
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
        The ABI number the loaded Core was built against. It is not repeated as
        a literal here: a literal did sit in this docstring and drifted two
        releases behind ``core/abi/minicronet.h``, because nothing runs it.

    Example:
        >>> import chrome_client
        >>> chrome_client.abi_version() == chrome_client._python_impl.engine.abi_version()
        True
    """
    ...
