"""Type surface of :mod:`chrome_client.impersonate`.

The Core pins one Chromium major per profile (``chrome_99`` .. ``chrome_153``).
Everything about the TLS ClientHello, the HTTP/2 SETTINGS and priority frames,
the HTTP/3 transport parameters and the default header order comes from that
profile; there is no knob that bends one profile into another.

That is why ``ja3``, ``akamai``, ``perk`` and most ``extra_fp`` fields raise
:class:`~chrome_client.exceptions.UnsupportedFeature` instead of being ignored:
accepting a JA3 string and then sending Chromium's own ClientHello would report
a fidelity this build does not have.

The ``Literal`` aliases below are the point of this stub: hovering
``impersonate=`` or ``http_version=`` in an IDE lists every value the runtime
accepts, and completion offers them.
"""

from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

from typing_extensions import Final, Literal, TypeAlias

#: Highest pinned profile. ``impersonate="chrome"`` resolves here.
LATEST_CHROME: Final[int]
#: Lowest pinned profile.
OLDEST_CHROME: Final[int]
#: curl_cffi accepts bare family names; only the Chrome family exists here.
ALIASES: Final[Mapping[str, str]]

#: Canonical profile identifiers, ``chrome_99`` .. ``chrome_153``.
#:
#: This is the spelling to prefer: it names the exact Chromium major whose
#: wire behaviour was captured and verified.
ChromeProfileName: TypeAlias = Literal[
    "chrome_99", "chrome_100", "chrome_101", "chrome_102",
    "chrome_103", "chrome_104", "chrome_105", "chrome_106",
    "chrome_107", "chrome_108", "chrome_109", "chrome_110",
    "chrome_111", "chrome_112", "chrome_113", "chrome_114",
    "chrome_115", "chrome_116", "chrome_117", "chrome_118",
    "chrome_119", "chrome_120", "chrome_121", "chrome_122",
    "chrome_123", "chrome_124", "chrome_125", "chrome_126",
    "chrome_127", "chrome_128", "chrome_129", "chrome_130",
    "chrome_131", "chrome_132", "chrome_133", "chrome_134",
    "chrome_135", "chrome_136", "chrome_137", "chrome_138",
    "chrome_139", "chrome_140", "chrome_141", "chrome_142",
    "chrome_143", "chrome_144", "chrome_145", "chrome_146",
    "chrome_147", "chrome_148", "chrome_149", "chrome_150",
    "chrome_151", "chrome_152", "chrome_153",
]

#: curl-cffi's spelling of the same profiles, without the underscore
#: (``chrome110``). Accepted as an alias so ports of curl_cffi code run
#: unchanged; ``normalize_impersonate()`` maps it onto the canonical name.
ChromeProfileAlias: TypeAlias = Literal[
    "chrome99", "chrome100", "chrome101", "chrome102",
    "chrome103", "chrome104", "chrome105", "chrome106",
    "chrome107", "chrome108", "chrome109", "chrome110",
    "chrome111", "chrome112", "chrome113", "chrome114",
    "chrome115", "chrome116", "chrome117", "chrome118",
    "chrome119", "chrome120", "chrome121", "chrome122",
    "chrome123", "chrome124", "chrome125", "chrome126",
    "chrome127", "chrome128", "chrome129", "chrome130",
    "chrome131", "chrome132", "chrome133", "chrome134",
    "chrome135", "chrome136", "chrome137", "chrome138",
    "chrome139", "chrome140", "chrome141", "chrome142",
    "chrome143", "chrome144", "chrome145", "chrome146",
    "chrome147", "chrome148", "chrome149", "chrome150",
    "chrome151", "chrome152", "chrome153",
]

#: Bare family names. Both resolve to the newest pinned profile, never to a
#: guess about which Chrome the host has installed.
ChromeFamilyAlias: TypeAlias = Literal[
    "chrome",
    "chromium",
]

#: Everything ``impersonate=`` accepts. ``None`` means "send Chromium's own
#: defaults for the linked revision" and is expressed by the ``Optional`` in the
#: signatures that take it.
#:
#: Anything else -- ``edge99``, ``safari17_0``, ``firefox133``, ``chrome_200``,
#: ``tor_145`` -- raises :class:`~chrome_client.exceptions.ImpersonateError`
#: rather than being silently downgraded to Chrome.
Impersonate: TypeAlias = Union[ChromeProfileName, ChromeProfileAlias, ChromeFamilyAlias]

#: ``http_version=`` pins the protocol instead of letting Chromium negotiate
#: through Alt-Svc and ALPN. ``None``/``""``/``"native"``/``"auto"`` all mean
#: "negotiate"; ``int`` and ``float`` spellings are accepted so
#: ``CurlHttpVersion.V2_0``-style constants and ``2.0`` both work.
HttpVersion: TypeAlias = Literal[
    None,
    "",
    "native",
    "auto",
    "v1",
    "1",
    "1.0",
    "1.1",
    "h1",
    "http/1.1",
    "http1",
    "v2",
    "2",
    "2.0",
    "h2",
    "http/2",
    "http2",
    "v3",
    "3",
    "3.0",
    "h3",
    "http/3",
    "http3",
]


class CurlHttpVersion:
    """``curl_cffi.const`` spelling of the protocol pins the Core supports.

    Use these instead of literals when porting curl_cffi code::

        from chrome_client import Session, CurlHttpVersion
        Session(http_version=CurlHttpVersion.V2_0)
    """

    NONE: None
    V1_0: Literal["v1"]
    V1_1: Literal["v1"]
    V2_0: Literal["v2"]
    V2: Literal["v2"]
    V2TLS: Literal["v2"]
    V3: Literal["v3"]
    V3ONLY: Literal["v3"]


#: Every accepted ``http_version`` spelling mapped to the Core's ``v1``/``v2``/
#: ``v3``/``None``. Not part of the documented surface; kept public because the
#: regression suite reads it.
HTTP_VERSIONS: Final[Mapping[Optional[str], Optional[str]]]


def normalize_http_version(value: Optional[Union[str, int]]) -> Optional[str]:
    """Maps any accepted spelling onto ``"v1"``/``"v2"``/``"v3"``/``None``.

    Args:
        value: One of the :data:`HttpVersion` spellings, already-stripped case is
            not required -- ``"V2"``, ``" http/2 "`` and ``2.0`` all work.

    Returns:
        ``"v1"``, ``"v2"``, ``"v3"``, or ``None`` when the caller asked for
        Chromium's own negotiation.

    Raises:
        UnsupportedFeature: ``value`` is a spelling the Core cannot pin, e.g.
            ``"h4"`` or ``"quic"``. The message names the accepted set.

    Example:
        >>> normalize_http_version("HTTP/2")
        'v2'
        >>> normalize_http_version(None) is None
        True
    """
    ...


def normalize_impersonate(value: Optional[Any]) -> Optional[str]:
    """Maps curl_cffi-style targets onto Core profile identifiers.

    Args:
        value: An :data:`Impersonate` string (``"chrome_153"``, ``"chrome153"``,
            ``"chrome"``), ``None``, or any object exposing a ``profile`` or
            ``impersonate`` attribute -- the shape curl_cffi's own enums have.
            Names are stripped and matched case-insensitively.

    Returns:
        The canonical ``chrome_<major>`` name, or ``None`` when nothing was
        requested.

    Raises:
        ImpersonateError: The value names a non-Chromium family (Edge, Safari,
            Firefox, Tor, Chrome Android, Safari iOS, OkHttp), or a Chrome major
            outside the pinned range. Nothing is silently downgraded.
        ValueError: The value is neither a string nor an object carrying a
            profile name.

    Example:
        >>> normalize_impersonate("chrome152")
        'chrome_152'
        >>> normalize_impersonate("chrome")
        'chrome_153'
    """
    ...


def validate_extra_fp(
    extra_fp: Optional[Union["ExtraFingerprints", Mapping[str, Any]]],
) -> Tuple[Optional[List[str]], Optional[str]]:
    """Accepts the ``extra_fp`` fields the facade can honour, rejects the rest.

    Only ``header_order`` (emission order of caller-supplied headers) and
    ``form_boundary`` (the multipart boundary) can be applied without touching
    the Core. Fields that ask for a different TLS or HTTP/2 fingerprint are
    rejected, because the pinned profile owns them.

    Args:
        extra_fp: An :class:`ExtraFingerprints` instance, a plain mapping with
            the same keys, or ``None``.

    Returns:
        ``(header_order, form_boundary)``; either element is ``None`` when the
        caller did not pin it.

    Raises:
        UnsupportedFeature: At least one field asked for a value the profile
            cannot produce. The message lists the offending field names.

    Example:
        >>> validate_extra_fp({"header_order": ["accept", "cookie"]})
        (['accept', 'cookie'], None)
    """
    ...


def reject_fingerprint_overrides(
    ja3: Optional[str] = ...,
    akamai: Optional[str] = ...,
    perk: Optional[str] = ...,
) -> None:
    """Raises if any of ``ja3=``/``akamai=``/``perk=`` was set to a truthy value.

    Args:
        ja3: The caller's JA3 string, if any.
        akamai: The caller's Akamai HTTP/2 fingerprint string, if any.
        perk: The caller's Peetprint string, if any.

    Raises:
        UnsupportedFeature: One of the three was supplied. Sending Chromium's
            own ClientHello while claiming a caller-supplied fingerprint would
            be a false report, so the call fails closed.

    Example:
        >>> reject_fingerprint_overrides()          # no-op
        >>> reject_fingerprint_overrides(ja3="771,4865-4866")  # doctest: +SKIP
        Traceback (most recent call last):
        UnsupportedFeature: ja3= is not supported: ...
    """
    ...


def available_profiles() -> List[str]:
    """Lists every profile identifier this build ships.

    Returns:
        The canonical names in ascending Chromium-major order, e.g.
        ``["chrome_99", "chrome_100", ..., "chrome_153"]``. The aliases are not
        repeated here; they are accepted by :func:`normalize_impersonate`.

    Example:
        >>> available_profiles()[0], available_profiles()[-1]
        ('chrome_99', 'chrome_153')
    """
    ...


class ExtraFingerprints:
    """curl_cffi-shaped fingerprint overrides, most of which are rejected.

    Only fields whose requested value already matches the pinned Chromium
    profile (or that the facade implements itself) are accepted; anything else
    raises :class:`~chrome_client.exceptions.UnsupportedFeature`, so a caller
    never believes an override took effect.

    Honoured: ``header_order``, ``form_boundary``.
    Accepted when left neutral: ``tls_delegated_credential=""``,
    ``tls_record_size_limit=0``, ``http2_no_priority=False``.
    Rejected when set: everything else.
    """

    tls_min_version: Optional[str]
    tls_grease: Optional[bool]
    tls_permute_extensions: Optional[bool]
    tls_cert_compression: Optional[str]
    tls_signature_algorithms: Optional[List[str]]
    tls_delegated_credential: str
    tls_record_size_limit: int
    http2_stream_weight: Optional[int]
    http2_stream_exclusive: Optional[bool]
    http2_no_priority: bool
    header_order: Optional[List[str]]
    split_cookies: Optional[bool]
    form_boundary: Optional[str]
    http3_sig_hash_algs: Optional[List[str]]
    http3_tls_extension_order: Optional[bool]

    def __init__(
        self,
        tls_min_version: Optional[str] = ...,
        tls_grease: Optional[bool] = ...,
        tls_permute_extensions: Optional[bool] = ...,
        tls_cert_compression: Optional[str] = ...,
        tls_signature_algorithms: Optional[List[str]] = ...,
        tls_delegated_credential: str = ...,
        tls_record_size_limit: int = ...,
        http2_stream_weight: Optional[int] = ...,
        http2_stream_exclusive: Optional[bool] = ...,
        http2_no_priority: bool = ...,
        header_order: Optional[List[str]] = ...,
        split_cookies: Optional[bool] = ...,
        form_boundary: Optional[str] = ...,
        http3_sig_hash_algs: Optional[List[str]] = ...,
        http3_tls_extension_order: Optional[bool] = ...,
    ) -> None:
        """Records the requested overrides; validation happens in the session.

        Args:
            tls_min_version: Rejected when set -- the profile owns the TLS
                version bracket.
            tls_grease: Rejected when set -- GREASE values are generated by
                BoringSSL's CSPRNG per connection and are a fingerprint element.
            tls_permute_extensions: Rejected when set -- extension permutation
                comes from the Chromium profile.
            tls_cert_compression: Rejected when set.
            tls_signature_algorithms: Rejected when set.
            tls_delegated_credential: Accepted only as ``""`` (off).
            tls_record_size_limit: Accepted only as ``0`` (profile default).
            http2_stream_weight: Rejected when set.
            http2_stream_exclusive: Rejected when set.
            http2_no_priority: Accepted only as ``False``.
            header_order: **Honoured.** Header names in the order they should be
                emitted; only reorders headers the caller supplied.
            split_cookies: Rejected when set.
            form_boundary: **Honoured.** The exact ``multipart/form-data``
                boundary to use instead of a generated one.
            http3_sig_hash_algs: Rejected when set.
            http3_tls_extension_order: Rejected when set.

        Example:
            >>> from chrome_client import Session, ExtraFingerprints
            >>> session = Session(extra_fp=ExtraFingerprints(
            ...     header_order=["accept", "accept-language", "cookie"]))
        """
        ...

    def as_dict(self) -> Dict[str, Any]:
        """Returns every field as a plain mapping, including the unset ones.

        Returns:
            ``{field_name: value}`` for all fifteen fields, which is what
            :func:`validate_extra_fp` walks.

        Example:
            >>> ExtraFingerprints(header_order=["accept"]).as_dict()["header_order"]
            ['accept']
        """
        ...
