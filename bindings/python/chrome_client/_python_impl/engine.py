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
    cookie store, all reachable through the ABI since v9, plus the in-flight
    count that keeps the context alive while work is still running on it.

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

from .exceptions import RequestException, SSLError, map_native_error
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
                  "http_version", "cache", "profile_namespace",
                  "host_mapping", "dns_nameservers", "doh_template",
                  "dns_secure_mode", "dns_over_tls", "dns_rotate",
                  "dns_padding", "hsts_bypass", "proxy_chain", "platform",
                  "max_sockets_per_group")

#: The value every binding field takes when the caller did not set it.  Used by
#: :meth:`SessionBinding.describe` so "which fields did this caller choose?" is
#: answered by comparison against this table rather than by a list of field
#: names that has to be kept in step with the constructor by hand.
BINDING_DEFAULTS = {"verify": True, "cache": True, "dns_secure_mode": 0,
                    "dns_over_tls": False, "dns_rotate": False,
                    "dns_padding": False, "platform": "auto",
                    "max_sockets_per_group": 0}


def _client_cert_error(error):
    """Maps a native client-certificate failure onto the public hierarchy.

    ``map_native_error`` sends every ``InvalidArgument`` to ``InvalidURL``,
    which reads as "your URL is malformed" -- true for a request but wrong here:
    the URL was fine and the certificate or key was not. A caller catching
    ``SSLError`` around an mTLS call used to get a URL error instead, and one
    catching ``InvalidURL`` would have been told to look in the wrong place.

    Args:
        error: The ``RuntimeError`` raised by the extension.

    Returns:
        An ``SSLError`` for a rejected certificate or key, otherwise whatever
        ``map_native_error`` produces.
    """
    message = str(error)
    if "InvalidArgument" in message:
        return SSLError(
            "the client certificate or key was rejected: the Core accepts a "
            "chain of PEM CERTIFICATE blocks plus one unencrypted PEM private "
            "key, and refuses an encrypted key rather than sending nothing "
            "silently")
    return map_native_error(error)

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
                 profile_namespace=None, host_mapping=None, dns_nameservers=None,
                 doh_template=None, dns_secure_mode=0, dns_over_tls=False,
                 dns_rotate=False, dns_padding=False, hsts_bypass=None,
                 proxy_chain=None, platform="auto", max_sockets_per_group=0):
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
        # Part of the key, not a per-request option: two engines given different
        # rules reach different addresses, and sharing one would route a request
        # to whichever mapping was built first.
        self.host_mapping = host_mapping
        # DNS, HSTS, proxy chain and platform all change which addresses are
        # reachable or what the connection claims to be, so they belong in the
        # key for the same reason host_mapping does.
        self.dns_nameservers = dns_nameservers
        self.doh_template = doh_template
        self.dns_secure_mode = int(dns_secure_mode)
        self.dns_over_tls = bool(dns_over_tls)
        self.dns_rotate = bool(dns_rotate)
        self.dns_padding = bool(dns_padding)
        self.hsts_bypass = hsts_bypass
        self.proxy_chain = proxy_chain
        self.platform = platform
        self.max_sockets_per_group = int(max_sockets_per_group)
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
    """The transport context one engine reproduces.

    There is no cookie-store generation any more. It existed because ABI v8
    could not clear ``CookieMonster``, so the only way to drop cookies the Core
    had accepted was a structurally identical engine with an empty store -- and
    that engine became a cache key of its own. v9 clears the store in place, so
    the counter, the key dimension and everything that bumped it are gone.

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
        key: The hashable tuple of every field above.
    """

    __slots__ = BINDING_FIELDS + ("_key",)

    def __init__(self, impersonate=None, proxy=None, verify=True, ca_pem=None,
                 user_agent=None, accept_language=None, proxy_username=None,
                 proxy_password=None, http_version=None, cache=True,
                 profile_namespace=None, host_mapping=None, dns_nameservers=None,
                 doh_template=None, dns_secure_mode=0, dns_over_tls=False,
                 dns_rotate=False, dns_padding=False, hsts_bypass=None,
                 proxy_chain=None, platform="auto", max_sockets_per_group=0):
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
        # Part of the key, not a per-request option: two engines given different
        # rules reach different addresses, and sharing one would route a request
        # to whichever mapping was built first.
        self.host_mapping = host_mapping
        # DNS, HSTS, proxy chain and platform all change which addresses are
        # reachable or what the connection claims to be, so they belong in the
        # key for the same reason host_mapping does.
        self.dns_nameservers = dns_nameservers
        self.doh_template = doh_template
        self.dns_secure_mode = int(dns_secure_mode)
        self.dns_over_tls = bool(dns_over_tls)
        self.dns_rotate = bool(dns_rotate)
        self.dns_padding = bool(dns_padding)
        self.hsts_bypass = hsts_bypass
        self.proxy_chain = proxy_chain
        self.platform = platform
        self.max_sockets_per_group = int(max_sockets_per_group)
        self._key = tuple(getattr(self, name) for name in BINDING_FIELDS)

    @property
    def binding(self):
        """The transport context this engine reproduces.

        Returns:
            A :class:`SessionBinding` built from every field.
        """
        return SessionBinding(**{name: getattr(self, name) for name in BINDING_FIELDS})

    def replace(self, **changes):
        """Returns a copy with fields replaced.

        Args:
            **changes: Field names to change.

        Returns:
            A new :class:`EngineConfig`.

        Raises:
            TypeError: A field name is not part of the configuration.
        """
        allowed = set(BINDING_FIELDS)
        unknown = sorted(set(changes) - allowed)
        if unknown:
            raise TypeError("unknown engine field(s): %s" % ", ".join(unknown))
        values = {name: getattr(self, name) for name in BINDING_FIELDS}
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
                         for name in BINDING_FIELDS
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
        # The first eleven are positional because the extension's signature has
        # been that shape since v8; everything added later is passed by keyword,
        # so a field inserted here cannot silently shift the argument list.
        return native.PyEngine(
            self.impersonate, self.proxy, self.verify, self.ca_pem,
            self.user_agent, self.accept_language, self.proxy_username,
            self.proxy_password, self.http_version, self.cache,
            self.profile_namespace,
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


class EngineSlot(object):
    """One engine, its cookie store, and its in-flight count.

    The count is what makes graceful teardown possible: a slot that still has
    work running on it must not be dropped, because dropping the last
    reference tears down the ``URLRequestContext`` that work is using.

    There is no mirror any more. ABI v8 could not read ``CookieMonster``, so the
    facade kept a copy and compared it against the jar to guess when the Core
    would send something else. v9 reads and writes the store directly, so the
    copy had nothing left to reconcile -- every question it answered is now a
    question for the store itself.

    Attributes:
        engine: The native engine.
        config: The configuration that produced it.
    """

    __slots__ = ("engine", "config", "_lock", "_inflight", "_applied_certs",
                   "_pending_certs", "_cert_cond", "_single_use")

    def __init__(self, engine, config, single_use=False):
        """Pairs an engine with its configuration.

        Args:
            engine: The native engine.
            config: The configuration it was built from.
            single_use: Whether this engine exists for one request only, i.e. it
                was built by ``BaseSession._throwaway_slot`` and no cache owns
                it. Only such a slot may be released as soon as its request
                finishes.
        """
        self.engine = engine
        self.config = config
        #: Set only by the construction site that builds an engine no cache owns.
        #: If this were inferred from cache membership instead, an EVICTED pooled
        #: slot would look single-use too: measured with max_engines=1, building a
        #: second configuration evicts the first, the evicted slot stops being
        #: findable in the cache, and the request still running on it would have
        #: its engine torn down underneath it.
        self._single_use = bool(single_use)
        self._lock = threading.RLock()
        self._inflight = 0
        #: ``(host, port) -> (cert_pem, key_pem)``: what the Core currently holds,
        #: as far as this slot can tell. Re-registering identical bytes is not
        #: free -- ``SSLClientContext::SetClientCertificate`` synchronously
        #: refreshes that origin's connection group and closes its idle
        #: connections (core/source/engine.cc:1322-1327, quoting
        #: net/http/http_stream_pool.cc ``Refresh(kSslConfigChanged)``); measured
        #: on an mTLS server with a constructor ``cert=``: the re-registration made
        #: every request open a fresh TCP connection and handshake -- 8 requests,
        #: 8 connections, 8 handshakes, p50 46.97 ms -- and skipping it left 1
        #: connection and 1.81 ms. Written by whichever Core call COMPLETES last,
        #: because that is the one Chromium keeps: SetClientCertificate overwrites
        #: the cache entry for the server and then refreshes that origin's
        #: connection group (net/socket/ssl_client_socket.cc:274-287), so the last
        #: call to return is the last call to land.
        self._applied_certs = {}
        #: (host, port) -> (cert_pem, key_pem) currently being registered. Two
        #: identical calls that overlap share one Core call instead of queueing two
        #: refreshes of the same connection group; a call that has to wait returns
        #: as soon as the value it wants is the one the Core holds.
        self._pending_certs = {}
        #: Signals a registration finishing, so a second caller that wants the
        #: same value waits for the first Core call instead of repeating it. Tied
        #: to the slot lock, which is reentrant, so the bookkeeping below can hold
        #: it across the whole decision without deadlocking on itself.
        self._cert_cond = threading.Condition(self._lock)

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

    def cookies(self, url=None):
        """Returns the store's cookies as 9-tuples, or raises ``RequestException``.

        Args:
            url: Restrict to the cookies a request to this URL would carry.

        Returns:
            A list of ``(name, value, domain, path, expiry, has_expiry,
            secure, http_only, same_site)``.
        """
        try:
            return self.engine.cookies(url)
        except RuntimeError as error:
            raise map_native_error(error)

    def set_cookie(self, cookie, source_url):
        """Writes one cookie through the store's own validation.

        Args:
            cookie: ``(name, value, domain, path, expires, secure, http_only,
                same_site)``. ``expires`` is Unix seconds or ``None``.
            source_url: The URL the cookie is being set from.
        """
        name, value, domain, path, expires, secure, http_only, same_site = cookie
        try:
            self.engine.set_cookie(
                name, value, domain, path, source_url,
                bool(secure), bool(http_only), same_site or "",
                expires is not None, int(expires or 0))
        except RuntimeError as error:
            raise map_native_error(error)

    def delete_cookie(self, domain, path, name):
        """Deletes every cookie matching the triple; returns the count.

        Args:
            domain: The cookie domain.
            path: The cookie path.
            name: The cookie name.
        """
        try:
            return self.engine.delete_cookie(domain, path, name)
        except RuntimeError as error:
            raise map_native_error(error)

    # -- jar write-through ---------------------------------------------------
    #
    # The jar owns the caller's view; the store owns what goes on the wire.
    # These three are the seam between them: every jar mutation calls one, so
    # the two cannot drift. That is what replaced the old compare-and-replace
    # in _resolve_cookies -- a reconciliation only exists once something has
    # already diverged.

    def write_cookie(self, cookie, source_url):
        """Mirrors one jar cookie into the store.

        Args:
            cookie: A ``cookielib.Cookie``.
            source_url: The URL the cookie came from.
        """
        expires = cookie.expires
        self.set_cookie(
            (cookie.name, cookie.value, cookie.domain or "", cookie.path or "/",
             expires, cookie.secure, cookie.has_nonstandard_attr("HttpOnly")
             or bool(getattr(cookie, "_rest", {}).get("HttpOnly")),
             (cookie._rest.get("SameSite", "") if isinstance(
                 getattr(cookie, "_rest", None), dict) else "")),
            source_url)

    def remove_cookie(self, domain, path, name):
        """Mirrors one jar removal into the store.

        Args:
            domain: Cookie domain.
            path: Cookie path.
            name: Cookie name.
        """
        self.delete_cookie(domain, path, name)

    def clear_cookies(self):
        """Empties the store."""
        try:
            self.engine.clear_cookies()
        except RuntimeError as error:
            raise map_native_error(error)

    def set_client_certificate(self, host, port, cert_pem, key_pem):
        """Registers the client certificate this engine presents to one server.

        The Core parses the PEM itself, so `cert_pem` may be a certificate
        chain followed by its key: `PemBlocksToDer` reads the CERTIFICATE
        blocks and the private key out of the same buffer. That is what lets the
        facade pass a single combined file through without splitting PEM in
        Python, where the delimiter rules would have to be reimplemented and
        could drift from BoringSSL's.

        Args:
            host: The server hostname the certificate is registered for.
            port: The server port.
            cert_pem: PEM bytes; one or more CERTIFICATE blocks, optionally
                followed by the private key.
            key_pem: PEM bytes holding exactly one unencrypted private key.

        Raises:
            RequestException: The Core rejected the certificate. An encrypted
                key, a malformed block and an empty host all land here.
        """
        key = (str(host), int(port))
        value = (bytes(cert_pem), bytes(key_pem))
        with self._cert_cond:
            while True:
                if self._applied_certs.get(key) == value:
                    # Same bytes, same server: the Core's registration is already
                    # what this call would produce, and re-issuing it would close
                    # the connections this engine is reusing.
                    return
                if key not in self._pending_certs:
                    # Nothing in flight for this server. Claim it and call the Core.
                    self._pending_certs[key] = value
                    break
                # A registration for this server is in flight. Waiting is right for
                # both cases: an identical value must not refresh the same
                # connection group twice, and a DIFFERENT value is a rotation that
                # has to be applied after the one in flight, not racing it. Either
                # way the loop re-decides when the wait returns.
                self._cert_cond.wait()
        try:
            # Outside the lock: the Core posts to its network thread and waits on a
            # WaitableEvent, and holding the slot lock across that wait would
            # serialise unrelated reservations on this slot.
            self.engine.set_client_certificate(host, key[1], value[0], value[1])
        except RuntimeError as error:
            with self._cert_cond:
                if self._pending_certs.get(key) == value:
                    self._pending_certs.pop(key, None)
                self._cert_cond.notify_all()
            raise _client_cert_error(error)
        with self._cert_cond:
            if self._pending_certs.get(key) == value:
                self._pending_certs.pop(key, None)
            # The record follows COMPLETION order, because that is the order
            # Chromium keeps: SetClientCertificate overwrites this server's entry
            # and then refreshes the origin's connection group
            # (net/socket/ssl_client_socket.cc:274-287), so whichever call returns
            # last is the certificate the Core holds. A sequence number taken
            # before the call would instead name whichever thread entered last --
            # which is a different one whenever two rotations overlap, and would
            # leave the record describing a certificate the Core does not hold, so
            # the NEXT identical registration would be skipped and the stale one
            # would stay live.
            #
            # No further guard is needed around this write: clear_client_certificate
            # waits for in-flight registrations of the same server before it acts,
            # and both the claim and this record happen under the same lock, so a
            # clear cannot slip between them.
            self._applied_certs[key] = value
            self._cert_cond.notify_all()

    def clear_client_certificate(self, host, port):
        """Forgets the certificate registered for (host, port).

        Args:
            host: The server hostname to unregister.
            port: The server port.

        Raises:
            RequestException: The Core rejected the call.
        """
        key = (str(host), int(port))
        with self._cert_cond:
            # Wait for a registration of this server that is already in the Core.
            # Without this the clear would return while a certificate it was meant
            # to remove is still landing: the caller would read "cleared" from
            # _applied_certs while the Core held the certificate just set, and the
            # next identical registration would then be skipped against a record
            # that describes neither state. The wait is one Core round trip, which
            # this call is a synchronous round trip anyway.
            while key in self._pending_certs:
                self._cert_cond.wait()
            self._applied_certs.pop(key, None)
        try:
            self.engine.clear_client_certificate(host, int(port))
        except RuntimeError as error:
            raise _client_cert_error(error)

    @property
    def single_use(self):
        """True when no cache owns this slot.

        Returns:
            ``True`` for an engine built for one discard request.
        """
        return self._single_use

    def release_engine(self):
        """Drops the native engine reference, which is what frees the context.

        Idempotent: a second call is a no-op, so a slot that reached the retire
        queue twice cannot raise from the worker thread.

        Returns:
            ``None``.
        """
        if self.engine is not None:
            self.engine = None

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
        >>> cache = EngineCache(max_engines=2)          # doctest: +SKIP
        >>> slot = cache.get(EngineConfig(impersonate="chrome_155"))  # doctest: +SKIP
        >>> len(cache)  # doctest: +SKIP
        1
    """

    def __init__(self, max_engines=DEFAULT_MAX_ENGINES):
        """Creates a cache holding at most ``max_engines`` engines.

        Args:
            max_engines: Ceiling; coerced to at least 1.
        """
        self._slots = {}
        #: Insertion-ordered set of keys, oldest first, implemented as a dict whose
        #: values are always ``None``. A dict rather than a list so "move this key
        #: to the back" is pop + reinsert instead of a linear scan on every cache
        #: hit, which is the path every request takes.
        #:
        #: The values are never read: only key order and membership are. Nothing
        #: may store data here.
        self._order = {}
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
            self._order.clear()
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
            >>> slot = cache.get(EngineConfig(proxy="http://127.0.0.1:8080"))  # doctest: +SKIP
        """
        with self._lock:
            if self._closed:
                raise RequestException("Session is closed")
            self._reset_after_fork_locked()
            slot = self._slots.get(config.key)
            if slot is not None:
                self._order.pop(config.key, None)
                self._order[config.key] = None
                self._stats["hits"] += 1
                return slot
            slot = EngineSlot(config.build(), config)
            self._slots[config.key] = slot
            self._order[config.key] = None
            self._stats["builds"] += 1
            while len(self._order) > self._max:
                # dict iteration is insertion-ordered, so the first key is the
                # oldest -- the same slot the list version evicted.
                evicted = next(iter(self._order))
                del self._order[evicted]
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

    def slots(self):
        """Returns the slots the cache currently holds.

        A snapshot, not a view: the caller iterates it while another thread may
        evict, and a live view would raise mid-iteration.

        Returns:
            A list of the live :class:`EngineSlot` objects.
        """
        with self._lock:
            return list(self._slots.values())

    def discard(self, config):
        """Removes an engine from the cache.

        The engine itself stays alive through whoever still holds its slot: a
        response, a WebSocket, or the Core request that is reading from it.
        Discarding only makes the slot unreachable for *future* lookups. It is
        how a configuration that is no longer wanted stops occupying one of the
        bounded cache slots while the work already using it finishes.

        Args:
            config: The configuration to drop. Dropping an absent key is a no-op.

        Returns:
            ``None``.
        """
        with self._lock:
            if self._slots.pop(config.key, None) is not None:
                self._order.pop(config.key, None)

    def clear(self):
        """Empties the cache, releasing the engines it held."""
        with self._lock:
            self._slots.clear()
            self._order.clear()

    def close(self):
        """Closes the cache; later :meth:`get` calls raise."""
        with self._lock:
            self._closed = True
            self._slots.clear()
            self._order.clear()

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
    but the in-flight count belongs to the lease, so the
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

    The number is whatever ``core/abi/minicronet.h`` declares as
    ``MN_ABI_VERSION``, and it is deliberately not repeated as a literal here.
    A literal did sit in this docstring and drifted two releases behind the
    header -- it still said 8 when the header said 10 -- because nothing runs
    these examples. The same text is copied into the stub by
    ``tools/generate-python-stubs.py``, so a wrong number here is wrong in two
    published files.

    Returns:
        The ABI number the loaded Core was built against.

    Example:
        >>> abi_version() == native.PyEngine.abi_version()
        True
    """
    return native.PyEngine.abi_version()
