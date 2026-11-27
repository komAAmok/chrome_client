"""The request header set Chrome sends, as a function of the profile.

``impersonate=`` used to change only the ``User-Agent`` version string, so a
``chrome_155`` session announced itself as Chrome 155 and then sent four headers
where real Chrome sends twelve.  Any detector comparing ``sec-ch-ua`` against
``User-Agent`` caught that immediately, and it was the largest remaining
fingerprint gap (``docs/NEXT_STEPS.md``).

This module closes it from the *evidence*: ``client_hints.CAPTURED_HEADERS`` is
generated from the six h1 captures each profile already carries, and this file
turns one entry into the ordered header list a request should carry.

Two deliberate deviations from the captures, both of which the evidence cannot
settle on its own:

* **The platform follows the host, not the capture.**  The captures came from a
  Windows desktop; a client must not claim a platform the socket is not on.  The
  Core already derives ``User-Agent`` from ``BUILDFLAG(IS_WIN/IS_MAC)`` and
  ``ARCH_CPU_*`` (``core/source/minicronet.cc``), so ``sec-ch-ua-platform`` is
  rewritten to match what the Core will actually send.  A ``"Windows"`` hint next
  to an ``X11; Linux`` User-Agent is a louder contradiction than a missing hint.
* **``Sec-Fetch-*`` describe a top-level navigation.**  Every capture is a
  navigation, because that is what a browser does when a person opens a URL, and
  ``session.get(url)`` is the same act.  A subresource fetch wants
  ``Sec-Fetch-Site: same-origin`` and no ``Sec-Fetch-User``; a caller doing that
  passes its own headers, which override these field by field.

Nothing here runs when ``impersonate`` is ``None``: a session with no profile is
not claiming to be a browser, and injecting browser headers into it would make
every non-impersonating caller look like Chrome.
"""


from .client_hints import CAPTURED_HEADERS
from .exceptions import UnsupportedFeature

#: Headers the facade fills in from the host rather than from the capture.
PLATFORM_HEADERS = ("User-Agent", "sec-ch-ua-platform")

#: Chrome major whose ``Accept-Encoding`` gained ``zstd``.  Measured across the
#: 72 captured profiles: 86..122 send "gzip, deflate, br", 123..157 add zstd.
#: Recorded so a reader can see the boundary is evidence rather than a guess;
#: the value itself still comes from the table.
ZSTD_FIRST_MAJOR = 123


#: Header names no capture can contain, with the position they must occupy.
#:
#: A capture is a navigation with a cold cache: Chrome never sends
#: ``Cache-Control`` or ``Pragma`` there, so the evidence cannot say where they
#: go. Chromium source can, and does -- URLRequestHttpJob adds them before
#: merging the caller's extra headers
#: (net/http/http_network_transaction.cc:1483-1487):
#:
#:     if (load_flags & LOAD_BYPASS_CACHE) {
#:       SetHeader(kPragma, "no-cache");
#:       SetHeader(kCacheControl, "no-cache");
#:     } else if (load_flags & LOAD_VALIDATE_CACHE) {
#:       SetHeader(kCacheControl, "max-age=0");
#:     }
#:     ...
#:     request_headers_.MergeFrom(request_->extra_headers);
#:
#: Order, from the same source and not from taste:
#:
#:   Host, Connection            (BuildRequestHeaders, 1451-1459)
#:   Pragma, Cache-Control       (BuildRequestHeaders, 1483-1487)
#:   MergeFrom(request_->extra_headers)   (1497)
#:
#: The whole captured block is extra_headers (core/source/request.cc:290 calls
#: SetExtraRequestHeaders), so Pragma and Cache-Control belong BEFORE the first
#: captured field -- not before Accept-Language, which is itself a captured
#: field. The frozen baseline confirms the frame: request_head.txt carries Host,
#: Connection, then sec-ch-ua ... with nothing in between.
#:
#: SetHeaderInternal rewrites an existing key in place, so seeding the slot here
#: is what makes a caller's Cache-Control land in Chromium's position instead of
#: being appended after the last captured field.
#:
#: ``Referer`` is deliberately absent. Chromium routes it out of the header
#: block entirely (core/source/request.cc:244-248 calls SetReferrer), so it has
#: no position among these; the referrer machinery emits it.
SEEDED_HEADERS = (
    ("Pragma", "no-cache"),
    ("Cache-Control", "no-cache"),
)



def _platform_identity(platform="auto"):
    """Returns ``(user_agent_group, sec_ch_ua_platform)`` from the Core.

    Both strings are read out of the loaded Core, never derived here. The reason
    is not tidiness: the Core decides them from the flags it was compiled with
    (``BUILDFLAG(IS_WIN/IS_MAC)`` plus ``ARCH_CPU_*``), while Python can only
    observe the host it runs on (``sys.platform``, ``platform.machine()``). On a
    single x86_64 machine serving the 32-bit Core the two answers differ --
    ``"X11; Linux i686"`` against ``"X11; Linux x86_64"`` -- and each side looks
    correct on its own. Reading them means the two agree by construction.

    Args:
        platform: ``"auto"``, ``"windows"``, ``"macos"`` or ``"linux"``.

    Returns:
        A ``(group, hint)`` pair. *group* is the parenthesised User-Agent group
        without parentheses; *hint* is the ``sec-ch-ua-platform`` value with the
        quotes Chrome writes.

    Raises:
        UnsupportedFeature: The Core cannot answer, which means the loaded
            artifact predates ABI v14. Raising beats falling back to a local
            guess: a guess that is right on this host and wrong on the next one
            is the defect this function exists to remove.
    """
    # Imported here rather than at module scope: the gate that reads the captured
    # table (tools/audit-session-parity.py) runs on a machine where the extension
    # has not been built, and a module-level import made the whole facade
    # unimportable there -- measured as an ImportError from CI before any test ran.
    # Only a caller that actually asks for a platform identity needs the Core.
    from ._native import native as _native_module

    try:
        return _native_module.PyEngine.platform_identity(platform or "auto")
    except AttributeError as error:
        # One message used to cover two different failures, and it named the
        # Core -- wrong for the case that actually happened: a wheel whose
        # extension predates this method. The Core refusing (an artifact older
        # than ABI v14) is a NativeError raised by platform_identity itself and
        # never reaches this branch.
        loaded = getattr(_native_module, "__file__", None) or getattr(
            _native_module, "__name__", "the native module")
        raise UnsupportedFeature(
            "the loaded extension (%s) has no PyEngine.platform_identity, so the "
            "User-Agent group and sec-ch-ua-platform cannot be read from it. The "
            "python36 extension (chrome_client_native36) shipped without this "
            "method while the Python facade that calls it is shared by both "
            "wheels; the cp39-abi3 wheel for this interpreter, or any release "
            "that declares the method in both extensions, does not have this "
            "problem." % (loaded,)) from error


def _platform_token(platform="auto"):
    """The User-Agent group, without parentheses, as the Core reports it."""
    return _platform_identity(platform)[0]


def _sec_ch_ua_platform(platform="auto"):
    """The ``sec-ch-ua-platform`` value, quotes included, as the Core reports it."""
    return _platform_identity(platform)[1]


def profile_major(profile):
    """Parses the major version out of a canonical profile name.

    Args:
        profile: A ``chrome_<major>`` name, already normalised.

    Returns:
        The major as an int, or ``None`` when the name is not one of the pinned
        profiles.
    """
    if not profile or not profile.startswith("chrome_"):
        return None
    digits = profile[len("chrome_"):]
    if not digits.isdigit():
        return None
    major = int(digits)
    return major if major in CAPTURED_HEADERS else None


#: Memoised results for the two pure functions below.
#:
#: Both are a function of the capture table, which is a module-level constant
#: read from profiles/chrome_<major>/h1/captures.json at import and never
#: mutated afterwards. Each is called once per request (the header set, then the
#: emission order) and rebuilt the same twelve pairs every time: measured 9.4 us
#: of the 113 us prepare_request costs, on a path the async loop charges to every
#: coroutine's synchronous prefix.
#:
#: The cache holds tuples and every call returns a fresh list, so a caller that
#: mutates what it receives cannot corrupt the next request. Both caches are
#: bounded: the key space is profiles x accept-language x platform, and a clear
#: on overflow is cheaper than an LRU that would run on every hit.
_BROWSER_HEADERS_CACHE = {}
_FULL_HEADER_ORDER_CACHE = {}
_CACHE_LIMIT = 256


def browser_headers(profile, accept_language=None, platform="auto"):
    """Returns the ordered headers Chrome of *profile* would send.

    Args:
        profile: A ``chrome_<major>`` name, or ``None`` for no browser headers.
        accept_language: Overrides the captured ``Accept-Language``.  The Core
            sends its own value from the engine field, so passing the same string
            here keeps the two from disagreeing.
        platform: The engine's platform identity. The User-Agent group and
            ``sec-ch-ua-platform`` are derived from it, so they agree with what
            the Core writes for the same field.

    Returns:
        A list of ``(name, value)`` pairs in capture order, or an empty list when
        *profile* is not a pinned profile.
    """
    major = profile_major(profile)
    if major is None:
        return []
    key = (major, accept_language, platform)
    cached = _BROWSER_HEADERS_CACHE.get(key)
    if cached is None:
        cached = _build_browser_headers(major, accept_language, platform)
        if len(_BROWSER_HEADERS_CACHE) >= _CACHE_LIMIT:
            _BROWSER_HEADERS_CACHE.clear()
        _BROWSER_HEADERS_CACHE[key] = cached
    return list(cached)


def _build_browser_headers(major, accept_language, platform):
    """Builds the pair list behind :func:`browser_headers`.

    Split out so the public entry point can answer from the memo without
    duplicating the field substitutions below.

    Args:
        major: The profile major, already known to be a captured one.
        accept_language: Overrides the captured Accept-Language.
        platform: The engine's platform identity.

    Returns:
        A tuple of (name, value) pairs in capture order.
    """
    token = _platform_token(platform)
    platform_hint = _sec_ch_ua_platform(platform)
    pairs = []
    for name, value in CAPTURED_HEADERS[major]:
        if name == "User-Agent":
            # Replace only the parenthesised group: the version string and the
            # rest of the UA come from the profile and must not be rewritten.
            head, _, rest = value.partition("(")
            _group, _, tail = rest.partition(")")
            value = "%s(%s)%s" % (head, token, tail)
        elif name == "sec-ch-ua-platform":
            value = platform_hint
        elif name == "Accept-Language" and accept_language:
            value = accept_language
        pairs.append((name, value))
    return tuple(pairs)


def full_header_order(profile):
    """Returns the emission order a request to *profile* should use.

    ``header_order`` is the capture, and the capture is a cold-cache
    navigation: it has no ``Pragma``/``Cache-Control`` and no trailing
    credentials. This function is that order with the slots Chromium reserves
    for them seeded in, so a caller header the capture cannot contain still
    lands where the browser would put it rather than after the last captured
    field.

    Slot placement, all from Chromium rather than from taste:

    * ``Pragma`` before ``Cache-Control``, both before the caller block
      (http_network_transaction.cc:1483-1487).
    * Everything else after the capture, because that is where
      ``MergeFrom(extra_headers)`` appends and nothing in the source orders it
      relative to the captured fields.

    Args:
        profile: A ``chrome_<major>`` name, or ``None``.

    Returns:
        A list of lower-case header names. The capture's own order, with the
        seeded slots inserted; empty for an unknown profile, matching
        :func:`header_order`.
    """
    cached = _FULL_HEADER_ORDER_CACHE.get(profile)
    if cached is not None:
        return list(cached)
    order = header_order(profile)
    if not order:
        return order
    lowered = [name.lower() for name in order]
    # Prepended, because BuildRequestHeaders sets these before MergeFrom and the
    # captured block is that MergeFrom's argument. Host and Connection precede
    # even these, but Chromium emits those from BuildRequestHeaders directly and
    # they never appear in extra_headers, so this list cannot express them.
    seeded = [name for name, _value in SEEDED_HEADERS if name.lower() not in lowered]
    built = tuple([name.lower() for name in seeded] + lowered)
    if len(_FULL_HEADER_ORDER_CACHE) >= _CACHE_LIMIT:
        _FULL_HEADER_ORDER_CACHE.clear()
    _FULL_HEADER_ORDER_CACHE[profile] = built
    return list(built)


def header_order(profile):
    """Returns the order the profile browser emits its headers in.

    Args:
        profile: A ``chrome_<major>`` name, or ``None``.

    Returns:
        A list of header names, or an empty list for an unknown profile.
    """
    return [name for name, _value in browser_headers(profile)]
