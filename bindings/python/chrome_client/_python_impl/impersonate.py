"""Impersonation and protocol selection.

The Core registers one profile per pinned Chromium major (``chrome_86`` through
``chrome_157``).  Everything about the TLS ClientHello, the HTTP/2 SETTINGS and
priority frames, and the HTTP/3 transport parameters lives inside that profile;
there is no knob to bend one profile into another.

A profile owns the default request header set, and the facade applies it.  Each
pinned major carries the exact ordered header list its own Chrome build sent,
captured in ``profiles/chrome_<major>/h1/captures.json``; :mod:`.browser_headers`
turns that into the request's headers and :meth:`.sessions.BaseSession._browser_headers`
merges it under the caller's own.  The Core cannot produce these itself --
Chrome generates ``sec-ch-ua*`` and ``Sec-Fetch-*`` in the ``services/network``
URLLoader factory layer, which the ``net`` layer this Core builds on does not
reach -- so the facade supplies them as extra headers.  ``Priority`` is the one
exception: ``net`` serialises it itself, so it needs no help.

That is why ``ja3``, ``akamai``, ``perk`` and most ``extra_fp`` fields raise
instead of being ignored.  Accepting a JA3 string and then sending Chromium's
own ClientHello would report a fidelity this build does not have.
"""

from .exceptions import ImpersonateError, UnsupportedFeature

#: Bounds of the pinned profile range.  There is no "latest" alias: a caller must
#: name the exact major, because "whatever is newest" silently re-points at a
#: different TLS/H2 fingerprint the moment a profile is added, and a fingerprint
#: that drifts without the caller asking is worse than one that is missing.
#:
#: This is the range the CORE accepts (core/source/minicronet.cc
#: kLastProfileMajor), not the range profiles/ holds captures for: the facade
#: must not offer a name the engine will reject, and must not hide one it can
#: serve. Raising it needs a rebuilt Core, which is why it moves with the
#: artifact rather than with the capture set.
LATEST_CHROME = 158
OLDEST_CHROME = 86

#: First major whose requests carry an RFC 9218 priority header.  Measured
#: against the pinned profiles: chrome_86..chrome_123 send no such header on
#: HTTP/2 or HTTP/3, chrome_124 and later send one.  tools/generate-profile-table.py
#: encodes the same boundary as the send_priority_header span.
PRIORITY_HEADER_MIN_CHROME = 124

_UNSUPPORTED_FAMILIES = ("edge", "safari", "firefox", "tor", "chrome_android",
                         "chrome-android", "safari_ios", "okhttp")


class CurlHttpVersion(object):
    """``curl_cffi.const`` spelling of the protocol pins the Core supports."""

    NONE = None
    V1_0 = "v1"
    V1_1 = "v1"
    V2_0 = "v2"
    V2 = "v2"
    V2TLS = "v2"
    V3 = "v3"
    V3ONLY = "v3"


HTTP_VERSIONS = {
    None: None, "": None, "native": None, "auto": None,
    "v1": "v1", "1": "v1", "1.0": "v1", "1.1": "v1", "h1": "v1",
    "http/1.1": "v1", "http1": "v1",
    "v2": "v2", "2": "v2", "2.0": "v2", "h2": "v2", "http/2": "v2", "http2": "v2",
    "v3": "v3", "3": "v3", "3.0": "v3", "h3": "v3", "http/3": "v3", "http3": "v3",
}


def normalize_http_version(value):
    if value is None:
        return None
    key = value.strip().lower() if isinstance(value, str) else value
    if key in HTTP_VERSIONS:
        return HTTP_VERSIONS[key]
    raise UnsupportedFeature(
        "http_version=%r is not supported; use one of v1, v2, v3 or None" % (value,))


def normalize_impersonate(value):
    """Maps curl_cffi-style targets onto Core profile identifiers.

    Only exact majors are accepted.  The bare family names ``chrome`` and
    ``chromium`` are rejected: curl_cffi resolves them to whatever its own build
    considers newest, which here would mean a fingerprint that changes under the
    caller the moment a profile is added.  Name the major you audited.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        target = getattr(value, "profile", None) or getattr(value, "impersonate", None)
        if target is None:
            raise ImpersonateError("impersonate must be a string profile name")
        value = target
    name = value.strip()
    lowered = name.lower()
    # An empty or whitespace-only string means "no profile", the same way
    # ``normalize_http_version`` reads "" as "let Chromium negotiate". It is a
    # real spelling in curl_cffi ports, so it is accepted rather than rejected.
    if not lowered:
        return None
    if lowered in ("chrome", "chromium"):
        raise ImpersonateError(
            "impersonate=%r is ambiguous: it would resolve to whatever profile "
            "this build happens to ship last, so the fingerprint would change "
            "whenever a profile is added. Name the exact major instead, for "
            "example %r." % (value, "chrome_%d" % LATEST_CHROME))
    for family in _UNSUPPORTED_FAMILIES:
        if lowered.startswith(family):
            raise ImpersonateError(
                "impersonate=%r is not available: this build ships Chromium "
                "desktop profiles only (chrome_%d .. chrome_%d)"
                % (value, OLDEST_CHROME, LATEST_CHROME))
    digits = lowered[len("chrome"):] if lowered.startswith("chrome") else None
    if digits is not None:
        digits = digits.lstrip("_-")
        major = digits.split(".")[0]
        if major.isdigit():
            number = int(major)
            if not OLDEST_CHROME <= number <= LATEST_CHROME:
                raise ImpersonateError(
                    "impersonate=%r is outside the pinned range chrome_%d .. chrome_%d"
                    % (value, OLDEST_CHROME, LATEST_CHROME))
            return "chrome_%d" % number
    raise ImpersonateError(
        "impersonate=%r is not a profile this build ships: use chrome_%d .. "
        "chrome_%d or the curl_cffi spelling chrome%d"
        % (value, OLDEST_CHROME, LATEST_CHROME, LATEST_CHROME))


class ExtraFingerprints(object):
    """The two fingerprint knobs a binding can still honour.

    Everything curl_cffi lets a caller override in the TLS or HTTP/2
    fingerprint is owned by the pinned Chromium profile: a ClientHello field,
    a SETTINGS value or a header-order preference that disagrees with the
    profile is either ignored by BoringSSL or reported as a fingerprint the
    browser never had. The rest of curl_cffi 15 fields were therefore removed
    rather than accepted-and-refused; an unknown attribute now raises
    AttributeError instead of pretending.

    What is left is real: header_order reorders the caller own headers before
    they reach the Core, and form_boundary sets the multipart boundary, both of
    which the facade produces itself.
    """

    __slots__ = ("header_order", "form_boundary")

    def __init__(self, header_order=None, form_boundary=None):
        self.header_order = header_order
        self.form_boundary = form_boundary

    def as_dict(self):
        return {name: getattr(self, name) for name in self.__slots__}


#: Fields the facade can honour without touching the Core: header order is
#: applied by emitting headers in the requested sequence, and the multipart
#: boundary is generated in Python.
_HONOURED_EXTRA_FP = ("header_order", "form_boundary")


def validate_extra_fp(extra_fp):
    """Returns (header_order, form_boundary), or raises for anything else.

    A mapping is accepted as well as an ExtraFingerprints, and any key this
    facade cannot honour raises rather than being dropped: a caller who built
    the mapping by hand is asking for something the profile owns.
    """
    if extra_fp is None:
        return None, None
    values = (extra_fp.as_dict() if isinstance(extra_fp, ExtraFingerprints)
              else dict(extra_fp))
    unsupported = sorted(set(values) - set(_HONOURED_EXTRA_FP))
    if unsupported:
        raise UnsupportedFeature(
            "extra_fp field(s) %s cannot be applied: the Chromium profile owns "
            "the TLS and HTTP/2 fingerprint, and those fields were removed "
            "rather than accepted and ignored. Keep %s, or select a different "
            "impersonate profile."
            % (", ".join(unsupported), " and ".join(_HONOURED_EXTRA_FP)))
    return values.get("header_order"), values.get("form_boundary")


def available_profiles():
    return ["chrome_%d" % major for major in range(OLDEST_CHROME, LATEST_CHROME + 1)]


def default_priority(profile):
    """The request priority this profile's own browser would use, or None.

    ``profile`` is a normalized chrome_<major> name or None.  A Chrome 124+
    profile returns "highest": that is the priority the browser puts on the
    requests this library can make, and it is what makes the wire carry
    "priority: u=0, i".  Every earlier profile returns None so the Core keeps
    its own default, because those profiles send no priority header at all and
    a different priority would move the HTTP/2 fingerprint they were verified
    with.

    None also comes back for an unrecognised profile string.  Callers reach
    here before the Core validates the name, so returning a priority for
    garbage would turn one error (ProfileUnsupported) into two.
    """
    if not profile:
        return None
    try:
        major = int(profile.split("_", 1)[1])
    except (IndexError, ValueError):
        return None
    return "highest" if major >= PRIORITY_HEADER_MIN_CHROME else None
