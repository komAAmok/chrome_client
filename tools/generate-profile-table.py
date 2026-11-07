#!/usr/bin/env python3
"""Emit the small runtime-only profile table used by the C++ Core.

Verbose source hashes, marker lines and audit text intentionally stay in JSON;
only values needed by runtime lookup are emitted into the library.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "profiles" / "normalized_profiles.json"
EFFECTIVE = ROOT / "profiles" / "effective_protocol_params.json"
FEATURES = ROOT / "profiles" / "network_feature_snapshots.json"
OUTPUT = ROOT / "core" / "source" / "minicronet" / "profile_table_generated.h"

# The table's span. Both ends name their evidence: FIRST_PROFILE_MAJOR is
# chrome_86, the oldest release the reference capture set covers, and its
# profiles were derived from that release's own capture and tag source by
# tools/derive-profile-from-capture.py (see docs/CHROME_86_98_SUPPORT.md), not
# inherited from chrome_99. Widening either end requires a wire-verified capture
# set and a source-evidence entry for the release being added.
FIRST_PROFILE_MAJOR = 86
LAST_PROFILE_MAJOR = 157

FEATURE_BITS = {
    "alps_for_http2": 1 << 0,
    "try_quic_by_default": 1 << 1,
}


def load_feature_flags():
    snapshot = json.loads(FEATURES.read_text())
    if set(snapshot["features"]) != set(FEATURE_BITS):
        raise SystemExit("network feature snapshot/codebook mismatch")
    flags = {}
    for item in snapshot["ranges"]:
        enabled = set(item["enabled"])
        if not enabled <= set(FEATURE_BITS):
            raise SystemExit("unknown network feature in snapshot")
        value = sum(FEATURE_BITS[name] for name in enabled)
        for major in range(item["first"], item["last"] + 1):
            profile_id = f"chrome_{major}"
            if profile_id in flags:
                raise SystemExit(f"overlapping feature range for {profile_id}")
            flags[profile_id] = value
    return snapshot, flags


def c(s):
    return json.dumps(s, ensure_ascii=True)


# ClientHello padding override, per release era.
#
# Every era is 0, and 0 means "no override": BoringSSL applies Chrome's one rule
# for all of them (pad the ClientHello to a 512-byte total while it sits in the
# 256..511 window, RFC 7685). The per-release widths the captures report are
# the OUTPUT of that rule, not an input to it:
#
#   86..90   h2 418  h1 424
#   91..97   h2 400  h1 424
#   98..118  h2 408  h1 432
#   119..123 conditional (ECH GREASE payload decides whether the window is hit)
#   124+     never (X25519Kyber768 key share pushes the ClientHello past 511)
#
# The h1/h2 difference is the ALPS extension h1 does not send; the 86..97 vs
# 98..118 difference is the 10 bytes of TLS 1.0/1.1 in supported_versions.
# Freezing any of those numbers here would pin a payload constant that cannot
# hold for both protocols at once, and would hide a real ClientHello size bug
# behind a padding override. tools/audit-padding-rule.py asserts the OUTPUT;
# this table is asserted to stay 0 by the same gate.
PADDING_LENGTH_ERAS = ((86, 90, 0), (91, 97, 0), (98, 118, 0), (119, 157, 0))


def padding_length_for(major):
    for first, last, length in PADDING_LENGTH_ERAS:
        if first <= major <= last:
            return length
    raise SystemExit(f'no padding length era covers chrome_{major}')


CODEBOOKS = {
    "cipher_suites": {
        "TLS_GREASE": 0x0A0A, "TLS_AES_128_GCM_SHA256": 0x1301,
        "TLS_AES_256_GCM_SHA384": 0x1302,
        "TLS_CHACHA20_POLY1305_SHA256": 0x1303,
        "TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256": 0xC02B,
        "TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256": 0xC02F,
        "TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384": 0xC02C,
        "TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384": 0xC030,
        "TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256": 0xCCA9,
        "TLS_ECDHE_RSA_WITH_CHACHA20_POLY1305_SHA256": 0xCCA8,
        "TLS_ECDHE_RSA_WITH_AES_128_CBC_SHA": 0xC013,
        "TLS_ECDHE_RSA_WITH_AES_256_CBC_SHA": 0xC014,
        "TLS_RSA_WITH_AES_128_GCM_SHA256": 0x009C,
        "TLS_RSA_WITH_AES_256_GCM_SHA384": 0x009D,
        "TLS_RSA_WITH_AES_128_CBC_SHA": 0x002F,
        "TLS_RSA_WITH_AES_256_CBC_SHA": 0x0035,
    },
    "curves": {"X25519": 29, "P-256": 23, "P-384": 24,
                "X25519KYBER768": 0x6399, "X25519MLKEM768": 0x11EC},
    "signature_algorithms": {
        "ecdsa_secp256r1_sha256": 0x0403, "rsa_pss_rsae_sha256": 0x0804,
        "rsa_pkcs1_sha256": 0x0401, "0x0503": 0x0503,
        "rsa_pss_rsae_sha384": 0x0805, "rsa_pkcs1_sha384": 0x0501,
        "rsa_pss_rsae_sha512": 0x0806, "0x0601": 0x0601,
        "0x0904": 0x0904, "0x0905": 0x0905, "0x0906": 0x0906,
    },
    # Runtime stores a set of numeric IDs. A captured order is evidence only;
    # BoringSSL chooses a fresh order for every connection.
    "tls_extensions": {
        "server_name": 0,
        "status_request": 5,
        "supported_groups": 10,
        "ec_point_formats": 11,
        "signature_algorithms": 13,
        "application_layer_protocol_negotiation": 16,
        "signed_certificate_timestamp": 18,
        "padding": 21,
        "compress_certificate": 27,
        "extended_master_secret": 23,
        "session_ticket": 35,
        "supported_versions": 43,
        "psk_key_exchange_modes": 45,
        "key_share": 51,
        "extensionRenegotiationInfo": 0xff01,
        "application_settings_old": 17513,
        "application_settings": 17613,
        "extensionEncryptedClientHello": 0xfe0d,
        # TLSEXT_TYPE_trust_anchors. Chrome 152 flipped kTLSTrustAnchorIDs to
        # enabled and switched to SelectAllTrustAnchorIDs(), so 152 is the first
        # profile that carries it. The payload is a separate field: unlike every
        # other extension its content is not a constant of the release.
        "trust_anchors": 0xca34,
    },
}


def emit_array(name, values, out, field):
    out.append(f"inline constexpr uint16_t {name}[] = {{")
    out.extend(f"    0x{CODEBOOKS[field][value]:04x}," for value in values)
    out.append("};")


def main():
    profiles = json.loads(INPUT.read_text())["profiles"]
    effective = json.loads(EFFECTIVE.read_text())["profiles"]
    _, feature_flags = load_feature_flags()
    if [p["profile_id"] for p in profiles] != [
            f"chrome_{n}" for n in range(FIRST_PROFILE_MAJOR,
                                          LAST_PROFILE_MAJOR + 1)]:
        raise SystemExit(
            f"profile table must contain chrome_{FIRST_PROFILE_MAJOR}.."
            f"chrome_{LAST_PROFILE_MAJOR} in order")
    if set(feature_flags) != {p["profile_id"] for p in profiles}:
        raise SystemExit(
            f"network feature snapshot must cover chrome_{FIRST_PROFILE_MAJOR}.."
            f"chrome_{LAST_PROFILE_MAJOR}")
    # Boundary assertions are spans, not hand-listed profiles. The previous
    # per-profile dictionaries stopped at chrome_151 while the table grew to
    # chrome_155, so four profiles (and the whole 99..121 span) were never
    # checked at all -- the gate silently covered less as the table grew. A
    # span names its endpoints, and `assert_span` refuses to accept a span that
    # does not tile FIRST_PROFILE_MAJOR..LAST_PROFILE_MAJOR exactly, so a gap is a failure here
    # rather than a missing check.
    def assert_span(field, spans, default=None):
        covered = []
        for first, last, expected in spans:
            for major in range(first, last + 1):
                profile_id = f"chrome_{major}"
                actual = effective[profile_id].get(field, default)
                if actual != expected:
                    raise SystemExit(
                        f"unexpected {field} for {profile_id}: {actual} "
                        f"(span {first}..{last} expects {expected})")
                covered.append(major)
        expected_majors = list(range(FIRST_PROFILE_MAJOR, LAST_PROFILE_MAJOR + 1))
        if sorted(covered) != expected_majors:
            missing = sorted(set(expected_majors) - set(covered))
            raise SystemExit(
                f"{field}: spans do not cover chrome_{FIRST_PROFILE_MAJOR}.."
                f"chrome_{LAST_PROFILE_MAJOR}; uncovered majors: {missing}")

    # kSocketPoolLimitRandomizationForProxy / the hardened socket pool made size
    # randomization unconditional in 147; the proxy variant follows in 151.
    assert_span("randomize_socket_pool_limit",
                [(FIRST_PROFILE_MAJOR, 146, False), (147, 157, True)])
    assert_span("randomize_proxy_socket_pool_limit",
                [(FIRST_PROFILE_MAJOR, 150, False), (151, 157, True)])
    # The HTTP/2 priority header appears in 124 and stays.
    assert_span("send_priority_header", [(FIRST_PROFILE_MAJOR, 123, False), (124, 157, True)])
    # QUIC ORIG (RFC 9287) is sent from 149.
    assert_span("send_quic_orig", [(FIRST_PROFILE_MAJOR, 148, False), (149, 157, True)])
    # kMaintainConnectionsOnIpv6TempAddrChange does not exist before 142; when
    # upstream added it in 142 it defaulted DISABLED, and 148 flipped it to
    # ENABLED where it stays through 156 (the pinned revision). Verified per
    # tag against raw.githubusercontent.com/chromium/chromium/<tag>/net/base/
    # features.cc. Pre-142 profiles get false because the code path predating
    # the feature does not special-case IPv6 temporary addresses either.
    #
    # This value is derived from the major version below rather than stored in
    # effective_protocol_params.json, so the check reads the derivation instead
    # of the JSON: asserting on JSON here would only re-state that the key is
    # absent.
    expected_maintain_ipv6 = {
        major: major >= 148
        for major in range(FIRST_PROFILE_MAJOR, LAST_PROFILE_MAJOR + 1)
    }
    if sorted(expected_maintain_ipv6) != list(range(FIRST_PROFILE_MAJOR,
                                                    LAST_PROFILE_MAJOR + 1)):
        raise SystemExit("maintain_ipv6_temp_addr spans miss a profile major")
    if expected_maintain_ipv6[147] or not expected_maintain_ipv6[148]:
        raise SystemExit(
            "maintain_ipv6_temp_addr boundary is wrong: upstream flips it at 148")
    # HTTP/3 SETTINGS_H3_DATAGRAM (51). The reference captures show it absent on
    # 93-119 and present from 120, which is the boundary encoded here. Nothing
    # before 93 has an H3 capture, so the span starts at FIRST_PROFILE_MAJOR
    # with the same value 93 carries; that is a stated extrapolation for 86-92,
    # not an observation.
    assert_span("advertise_h3_datagram",
                [(FIRST_PROFILE_MAJOR, 119, False), (120, 157, True)],
                default=True)
    # Padding length eras must tile the whole table. A gap here silently
    # produces a release whose padding value belongs to no capture.
    covered = []
    for first, last, _length in PADDING_LENGTH_ERAS:
        covered.extend(range(first, last + 1))
    if sorted(covered) != list(range(FIRST_PROFILE_MAJOR, LAST_PROFILE_MAJOR + 1)):
        raise SystemExit("padding length eras do not tile the profile range")
    # Legacy QUIC version information was briefly on for 144-145 only.
    assert_span("send_quic_legacy_version_information",
                [(FIRST_PROFILE_MAJOR, 143, False), (144, 145, True), (146, 157, False)],
                default=False)

    # Capability and default-attempt policy are independent. Chromium's
    # native HttpNetworkSessionParams default keeps QUIC enabled in every
    # supported revision; kTryQuicByDefault is the separate feature bit.
    for profile_id in effective:
        if effective[profile_id].get("enable_quic") is not True:
            raise SystemExit(f"QUIC capability must remain enabled: {profile_id}")
        expected_try = int(profile_id.split("_", 1)[1]) >= 123
        if effective[profile_id].get("try_quic_by_default") is not expected_try:
            raise SystemExit(
                f"unexpected default QUIC-attempt policy: {profile_id}")
    expected_legacy_version_information_boundary = {
        "chrome_122": False,
        "chrome_123": False,
        "chrome_124": False,
        "chrome_125": False,
        "chrome_126": False,
        "chrome_127": False,
        "chrome_128": False,
        "chrome_129": False,
        "chrome_130": False,
        "chrome_131": False,
        "chrome_132": False,
        "chrome_133": False,
        "chrome_134": False,
        "chrome_135": False,
        "chrome_136": False,
        "chrome_137": False,
        "chrome_138": False,
        "chrome_139": False,
        "chrome_140": False,
        "chrome_141": False,
        "chrome_142": False,
        "chrome_143": False,
        "chrome_144": True,
        "chrome_145": True,
        "chrome_146": False,
    }
    for profile_id, expected in expected_legacy_version_information_boundary.items():
        actual = effective[profile_id].get(
            "send_quic_legacy_version_information", False)
        if actual != expected:
            raise SystemExit(
                "unexpected legacy QUIC version-information boundary for "
                f"{profile_id}: {actual}")

    out = [
        "// Generated by tools/generate-profile-table.py; do not edit.",
        "#ifndef MINICRONET_CORE_PROFILE_TABLE_GENERATED_H_",
        "#define MINICRONET_CORE_PROFILE_TABLE_GENERATED_H_",
        "",
        "#include <cstddef>",
        "#include <cstdint>",
        "#include <iterator>",
        "#include \"base/containers/span.h\"",
        "",
        "namespace minicronet {",
        "struct H2RuntimeParams {",
        "  uint32_t header_table_size;",
        "  uint32_t initial_window_size;",
        "  uint32_t max_frame_size;",
        "  uint32_t max_header_list_size;",
        "  uint32_t session_recv_window_size;",
        "  bool send_max_frame_size;",
        "  base::span<const uint16_t> settings_order;",
        "};",
        "",
        "struct RuntimeProfileData {",
        "  const char* id;",
        "  base::span<const uint16_t> cipher_suites;",
        "  base::span<const uint16_t> curves;",
        "  uint8_t key_share_count;",
        "  base::span<const uint16_t> signature_algorithms;",
        "  base::span<const uint16_t> tls_extension_ids;",
        "  enum class TlsExtensionOrderPolicy : uint8_t {",
        "    kBoringSslRandomFisherYates = 0,",
        "    kBoringSslNative = 1,",
        "  };",
        "  TlsExtensionOrderPolicy tls_extension_order_policy;",
        "  bool tls_grease_enabled;",
        "  bool tls_grease_per_connection;",
        "  bool grease_signature_algorithms;",
        "  bool ech_enabled;",
        "  bool use_new_alps_codepoint;",
        "  bool send_http2_enable_push_setting;",
        "  bool send_http2_max_concurrent_streams;",
        "  uint8_t h2_params_index;",
        "  bool randomize_socket_pool_limit;",
        "  bool randomize_proxy_socket_pool_limit;",
        "  bool send_priority_header;",
        "  bool send_quic_orig;",
        "  bool send_quic_legacy_version_information;",
        "  // HTTP/3 SETTINGS_H3_DATAGRAM (51). Captured: chrome_93..chrome_119 do not",
        "  // advertise it, chrome_120 and later do.",
        "  bool advertise_h3_datagram;",
        "  // False for a release whose HTTP/3 behaviour cannot be reproduced from the",
        "  // reference evidence. A forced H3 request against such a profile must fail",
        "  // closed instead of sending a QUIC ClientHello the release never sent.",
        "  bool h3_supported;",
        "  // TLS version range this release advertised, and what the Core applies",
        "  // to the socket. Chrome 86..97 set kDefaultSSLVersionMin = TLS1, so their",
        "  // supported_versions lists 1.3/1.2/1.1/1.0; 98 raised the floor to TLS1_2",
        "  // and the two legacy entries disappeared. Upstream",
        "  // ssl_client_socket_impl.cc rejects a floor below TLS1_2 with",
        "  // ERR_UNEXPECTED; core/patches/profile-tls-net.patch relaxes that only when",
        "  // ProfileSSLConfigService sets profile_allows_legacy_tls_versions, which it",
        "  // derives from version_min < TLS1_2. Without that patch the 0x0301 below",
        "  // would break the handshake instead of reproducing the older list.",
        "  uint16_t tls_version_min;",
        "  uint16_t tls_version_max;",
        "  bool enable_quic;",
        "  bool try_quic_by_default;",
        "  // net::features::kMaintainConnectionsOnIpv6TempAddrChange. The feature",
        "  // itself only exists from 142; upstream defaults it DISABLED for",
        "  // 142-147 and ENABLED from 148. Profiles before 142 have no such",
        "  // behaviour at all, so they use the same false upstream would.",
        "  bool maintain_ipv6_temp_addr;",
        "  bool client_hello_padding_enabled;",
        "  uint16_t client_hello_padding_length;",
        "  uint8_t network_feature_flags;",
        "  // Encoded trust_anchors (0xca34) payload: a sequence of",
        "  // one-byte-length-prefixed IDs, exactly as",
        "  // net::AddTrustAnchorIdToEncodedList emits them. Empty for every",
        "  // profile before 152, which is what keeps",
        "  // SSLContextConfig::ShouldAdvertiseTrustAnchorIDs() false and the",
        "  // extension off the wire for them.",
        "  base::span<const uint8_t> trust_anchor_ids;",
        "  bool wire_verified;",
        "};",
        "",
        "enum class NetworkFeature : uint8_t {",
        "  kAlpsForHttp2 = 1u << 0,",
        "  kTryQuicByDefault = 1u << 1,",
        "};",
        "using NetworkFeatureFlags = uint8_t;",
        "constexpr bool HasNetworkFeature(NetworkFeatureFlags flags,",
        "                                  NetworkFeature feature) {",
        "  return (flags & static_cast<NetworkFeatureFlags>(feature)) != 0;",
        "}",
        "inline constexpr NetworkFeatureFlags kCurrentNetworkFeatureFlags =",
        "    static_cast<NetworkFeatureFlags>(NetworkFeature::kAlpsForHttp2) |",
        "    static_cast<NetworkFeatureFlags>(NetworkFeature::kTryQuicByDefault);",
        "",
    ]
    array_names = {}
    arrays = []
    for field in ("cipher_suites", "curves", "signature_algorithms"):
        for p in profiles:
            values = tuple(p["tls"][field])
            if (field, values) not in array_names:
                name = f"{field}_{len([x for x in arrays if x[0] == field])}"
                array_names[(field, values)] = name
                arrays.append((field, values, name))
    for field, values, name in arrays:
        emit_array(name, values, out, field)
        out.append("")
    out.extend([
        "inline constexpr uint16_t h2_settings_order_0[] = {",
        "    1, 2, 3, 4, 5, 6,",
        "};",
        "",
    ])
    extension_arrays = {}
    for p in profiles:
        if p["tls"].get("extension_order") not in (
                "boringssl_random_fisher_yates", "boringssl_native"):
            raise SystemExit(
                f"unsupported TLS extension order in {p['profile_id']}")
        grease = p["tls"].get("grease", {})
        if not grease.get("enabled") or not grease.get("per_connection") or \
                grease.get("fixed_seed"):
            raise SystemExit(
                f"TLS GREASE must be per-connection and non-fixed in {p['profile_id']}")
        names = set(p["tls"]["extensions"])
        unknown = names - set(CODEBOOKS["tls_extensions"]) - {"TLS_GREASE"}
        if unknown:
            raise SystemExit(
                f"unknown TLS extension(s) in {p['profile_id']}: {sorted(unknown)}")
        values = tuple(sorted(CODEBOOKS["tls_extensions"][name]
                              for name in names if name != "TLS_GREASE"))
        if not values:
            raise SystemExit(f"empty TLS extension set in {p['profile_id']}")
        if values not in extension_arrays:
            extension_arrays[values] = f"tls_extension_ids_{len(extension_arrays)}"
    for values, name in extension_arrays.items():
        out.append(f"inline constexpr uint16_t {name}[] = {{")
        out.extend(f"    0x{value:04x}," for value in values)
        out.append("};")
        out.append("")

    # Encoded trust_anchors payloads. A profile without one references no array
    # and gets an empty span, so the extension stays off its wire.
    anchor_arrays = {}
    for p in profiles:
        encoded = p["tls"].get("trust_anchor_ids_encoded")
        if not encoded:
            if "trust_anchors" in set(p["tls"]["extensions"]):
                raise SystemExit(
                    f"{p['profile_id']} lists the trust_anchors extension but "
                    "carries no trust_anchor_ids_encoded payload")
            continue
        if "trust_anchors" not in set(p["tls"]["extensions"]):
            raise SystemExit(
                f"{p['profile_id']} carries a trust_anchor_ids_encoded payload "
                "but does not list the trust_anchors extension")
        payload = bytes.fromhex(encoded)
        # Reject a payload whose length prefixes do not tile it exactly; a
        # truncated list would be sent verbatim and silently mismatch.
        index = 0
        while index < len(payload):
            length = payload[index]
            if length == 0 or index + 1 + length > len(payload):
                raise SystemExit(
                    f"{p['profile_id']}: malformed trust_anchor_ids payload")
            index += 1 + length
        if payload not in anchor_arrays:
            anchor_arrays[payload] = f"trust_anchor_ids_{len(anchor_arrays)}"
    for payload, name in anchor_arrays.items():
        out.append(f"inline constexpr uint8_t {name}[] = {{")
        for start in range(0, len(payload), 12):
            chunk = payload[start:start + 12]
            out.append("    " + " ".join(f"0x{b:02x}," for b in chunk))
        out.append("};")
        out.append("")
    out.extend([
        "inline constexpr H2RuntimeParams kH2RuntimeParams[] = {{",
        "    65536, 6291456, 16384, 262144, 15728640, false,",
        "    base::span(h2_settings_order_0),",
        "}};",
        "inline const H2RuntimeParams& GetH2RuntimeParams(uint8_t index) {",
        "  switch (index) {",
        "    case 0:",
        "    default:",
        "      return kH2RuntimeParams[0];",
        "  }",
        "}",
        "",
    ])
    out.append("inline constexpr RuntimeProfileData kRuntimeProfiles[] = {")

    def anchor_span(profile):
        encoded = profile["tls"].get("trust_anchor_ids_encoded")
        if not encoded:
            return "{}"
        return f"base::span({anchor_arrays[bytes.fromhex(encoded)]})"

    for p in profiles:
        ident = p["profile_id"].replace("-", "_")
        tls = p["tls"]
        protocol = effective[p["profile_id"]]
        major = int(p["profile_id"].split("_", 1)[1])
        required_h2 = (
            "h2_header_table_size", "h2_initial_window_size",
            "h2_max_frame_size", "h2_max_header_list_size",
            "h2_session_recv_window_size", "h2_settings_order")
        if any(key not in protocol for key in required_h2):
            raise SystemExit(f"incomplete HTTP/2 profile: {p['profile_id']}")
        if protocol["h2_settings_order"] != [1, 2, 3, 4, 5, 6]:
            raise SystemExit(
                f"unexpected HTTP/2 SETTINGS order: {p['profile_id']}")
        expected_h2 = {
            "h2_header_table_size": 65536,
            "h2_initial_window_size": 6291456,
            "h2_max_frame_size": 16384,
            "h2_max_header_list_size": 262144,
            "h2_session_recv_window_size": 15728640,
        }
        if any(protocol[key] != value for key, value in expected_h2.items()):
            raise SystemExit(
                f"unshared HTTP/2 value requires a separate params object: "
                f"{p['profile_id']}")
        key_share_count = tls["key_share_count"]
        if not 0 < key_share_count <= len(tls["curves"]):
            raise SystemExit(f"invalid key-share count for {p['profile_id']}")
        refs = {
            field: array_names[(field, tuple(tls[field]))]
            for field in ("cipher_suites", "curves", "signature_algorithms")
        }
        extension_values = tuple(sorted(
            CODEBOOKS["tls_extensions"][name]
            for name in set(tls["extensions"])
            if name != "TLS_GREASE"))
        flags = feature_flags[p["profile_id"]]
        extension_order = p["tls"]["extension_order"]
        extension_order_enum = (
            "RuntimeProfileData::TlsExtensionOrderPolicy::"
            + ("kBoringSslNative" if extension_order == "boringssl_native"
               else "kBoringSslRandomFisherYates"))
        out.append("  {")
        out.extend([
            f"    {c(p['profile_id'])},",
            f"    base::span({refs['cipher_suites']}),",
            f"    base::span({refs['curves']}),",
            f"    {key_share_count},",
            f"    base::span({refs['signature_algorithms']}),",
            f"    base::span({extension_arrays[extension_values]}),",
            f"    {extension_order_enum},",
            f"    {'true' if tls['grease']['enabled'] else 'false'},",
            f"    {'true' if tls['grease']['per_connection'] else 'false'},",
            f"    {'true' if tls['grease_signature_algorithms'] else 'false'},",
            f"    {'true' if 'extensionEncryptedClientHello' in tls['extensions'] else 'false'},",
            f"    {'true' if 'application_settings' in tls['extensions'] else 'false'},",
            f"    {'true' if protocol['h2_enable_push'] else 'false'},",
            f"    {'true' if protocol['h2_max_concurrent_streams'] else 'false'},",
            # h2_params_index: every profile currently shares one HTTP/2 params
            # object, so the index is a constant. It stays a field so a future
            # release with different SETTINGS values does not need a schema change.
            "    0,",
            f"    {'true' if protocol['randomize_socket_pool_limit'] else 'false'},",
            f"    {'true' if protocol['randomize_proxy_socket_pool_limit'] else 'false'},",
            f"    {'true' if protocol['send_priority_header'] else 'false'},",
            f"    {'true' if protocol['send_quic_orig'] else 'false'},",
            f"    {'true' if protocol.get('send_quic_legacy_version_information', False) else 'false'},",
            f"    {'true' if major >= 120 else 'false'},",
            # Two different facts, deliberately kept apart:
            #   activation.h3_disabled     "Chromium does not attempt QUIC by
            #                              default here" (== !try_quic_by_default,
            #                              true for 86..122)
            #   activation.h3_unsupported  "this release cannot do HTTP/3 at all;
            #                              a forced H3 engine must be refused"
            # Collapsing them made chrome_93..122 look incapable of H3 although
            # each has six passing H3 captures.
            f"    {'false' if p.get('activation', {}).get('h3_unsupported') else 'true'},",
            # TLS1_0 = 0x0301, TLS1_2 = 0x0303, TLS1_3 = 0x0304. The floor moved at
            # 98, which is exactly where supported_versions loses 1.1 and 1.0.
            f"    0x{0x0301 if major <= 97 else 0x0303:04x},",
            "    0x0304,",
            f"    {'true' if protocol['enable_quic'] else 'false'},",
            f"    {'true' if protocol['try_quic_by_default'] else 'false'},",
            # Derived from the version rather than read from JSON: this is
            # upstream behaviour that changed at 148, and assert_span above
            # proves the whole 86..157 range against it.
            f"    {'true' if major >= 148 else 'false'},",
            # Chrome 119+ uses BoringSSL's length-dependent RFC 7685
            # padding. Do not freeze the observed 28-byte result: ECH GREASE
            # payload rotation can make the extension appear or disappear.
            "    true,",
            f"    {padding_length_for(major)},",
            f"    0x{flags:04x},",
            f"    {anchor_span(p)},",
            f"    {'true' if p['evidence']['wire_verified'] else 'false'},",
        ])
        out.append("  },")
    out.extend([
        "};",
        "inline constexpr size_t kRuntimeProfileCount = std::size(kRuntimeProfiles);",
        "}  // namespace minicronet",
        "#endif  // MINICRONET_CORE_PROFILE_TABLE_GENERATED_H_",
        "",
    ])
    OUTPUT.write_text("\n".join(out))


if __name__ == "__main__":
    main()
