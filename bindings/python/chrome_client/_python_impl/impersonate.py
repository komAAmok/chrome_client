"""Impersonation and protocol selection.

The Core registers one profile per pinned Chromium major (``chrome_98`` through
``chrome_152``).  Everything about the TLS ClientHello, the HTTP/2 SETTINGS and
priority frames, the HTTP/3 transport parameters, and the default header order
lives inside that profile; there is no knob to bend one profile into another.

That is why ``ja3``, ``akamai``, ``perk`` and most ``extra_fp`` fields raise
instead of being ignored.  Accepting a JA3 string and then sending Chromium's
own ClientHello would report a fidelity this build does not have.
"""

from .exceptions import ImpersonateError, UnsupportedFeature

#: Highest pinned profile.  ``impersonate="chrome"`` resolves here.
LATEST_CHROME = 152
OLDEST_CHROME = 99

#: curl_cffi accepts bare family names; only the Chrome family exists here.
ALIASES = {
    "chrome": "chrome_%d" % LATEST_CHROME,
    "chromium": "chrome_%d" % LATEST_CHROME,
}

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
    """Maps curl_cffi-style targets onto Core profile identifiers."""
    if value is None:
        return None
    if not isinstance(value, str):
        target = getattr(value, "profile", None) or getattr(value, "impersonate", None)
        if target is None:
            raise ImpersonateError("impersonate must be a string profile name")
        value = target
    name = value.strip()
    lowered = name.lower()
    if lowered in ALIASES:
        return ALIASES[lowered]
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
    return name


class ExtraFingerprints(object):
    """curl_cffi-shaped fingerprint overrides.

    Only fields whose requested value already matches the pinned Chromium
    profile are accepted; anything else raises ``UnsupportedFeature`` so a caller
    never believes an override took effect.
    """

    __slots__ = (
        "tls_min_version", "tls_grease", "tls_permute_extensions",
        "tls_cert_compression", "tls_signature_algorithms",
        "tls_delegated_credential", "tls_record_size_limit",
        "http2_stream_weight", "http2_stream_exclusive", "http2_no_priority",
        "header_order", "split_cookies", "form_boundary",
        "http3_sig_hash_algs", "http3_tls_extension_order",
    )

    def __init__(self, tls_min_version=None, tls_grease=None,
                 tls_permute_extensions=None, tls_cert_compression=None,
                 tls_signature_algorithms=None, tls_delegated_credential="",
                 tls_record_size_limit=0, http2_stream_weight=None,
                 http2_stream_exclusive=None, http2_no_priority=False,
                 header_order=None, split_cookies=None, form_boundary=None,
                 http3_sig_hash_algs=None, http3_tls_extension_order=None):
        self.tls_min_version = tls_min_version
        self.tls_grease = tls_grease
        self.tls_permute_extensions = tls_permute_extensions
        self.tls_cert_compression = tls_cert_compression
        self.tls_signature_algorithms = tls_signature_algorithms
        self.tls_delegated_credential = tls_delegated_credential
        self.tls_record_size_limit = tls_record_size_limit
        self.http2_stream_weight = http2_stream_weight
        self.http2_stream_exclusive = http2_stream_exclusive
        self.http2_no_priority = http2_no_priority
        self.header_order = header_order
        self.split_cookies = split_cookies
        self.form_boundary = form_boundary
        self.http3_sig_hash_algs = http3_sig_hash_algs
        self.http3_tls_extension_order = http3_tls_extension_order

    def as_dict(self):
        return {name: getattr(self, name) for name in self.__slots__}


# Fields the facade can honour without touching the Core: header order is
# applied by emitting headers in the requested sequence, and the multipart
# boundary is generated in Python.
_HONOURED_EXTRA_FP = ("header_order", "form_boundary")
_NEUTRAL_EXTRA_FP = {
    "tls_delegated_credential": ("", None),
    "tls_record_size_limit": (0, None),
    "http2_no_priority": (False, None),
}


def validate_extra_fp(extra_fp):
    """Returns ``(header_order, form_boundary)`` or raises for unsupported asks."""
    if extra_fp is None:
        return None, None
    values = extra_fp.as_dict() if isinstance(extra_fp, ExtraFingerprints) else dict(extra_fp)
    unsupported = []
    for name, value in values.items():
        if name in _HONOURED_EXTRA_FP:
            continue
        if name in _NEUTRAL_EXTRA_FP and value in _NEUTRAL_EXTRA_FP[name]:
            continue
        if value in (None, False):
            continue
        unsupported.append(name)
    if unsupported:
        raise UnsupportedFeature(
            "extra_fp field(s) %s cannot be applied: the Chromium profile owns "
            "the TLS and HTTP/2 fingerprint. Select a different impersonate "
            "profile instead." % ", ".join(sorted(unsupported)))
    return values.get("header_order"), values.get("form_boundary")


def reject_fingerprint_overrides(ja3=None, akamai=None, perk=None):
    for name, value in (("ja3", ja3), ("akamai", akamai), ("perk", perk)):
        if value:
            raise UnsupportedFeature(
                "%s= is not supported: the TLS and HTTP/2 fingerprints come from "
                "the pinned Chromium profile selected by impersonate=." % name)


def available_profiles():
    return ["chrome_%d" % major for major in range(OLDEST_CHROME, LATEST_CHROME + 1)]
