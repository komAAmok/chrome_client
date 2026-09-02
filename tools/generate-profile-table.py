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
OUTPUT = ROOT / "core" / "profile_table_generated.h"

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
    if [p["profile_id"] for p in profiles] != [f"chrome_{n}" for n in range(99, 152)]:
        raise SystemExit("profile table must contain chrome_99..chrome_151 in order")
    if set(feature_flags) != {p["profile_id"] for p in profiles}:
        raise SystemExit("network feature snapshot must cover chrome_99..chrome_151")
    expected_pool_boundary = {
        "chrome_122": (False, False),
        "chrome_123": (False, False),
        "chrome_124": (False, False),
        "chrome_125": (False, False),
        "chrome_126": (False, False),
        "chrome_127": (False, False),
        "chrome_128": (False, False),
        "chrome_146": (False, False),
        "chrome_147": (True, False),
        "chrome_148": (True, False),
        "chrome_149": (True, False),
        "chrome_150": (True, False),
        "chrome_151": (True, True),
    }
    for profile_id, expected in expected_pool_boundary.items():
        actual = (effective[profile_id]["randomize_socket_pool_limit"],
                  effective[profile_id]["randomize_proxy_socket_pool_limit"])
        if actual != expected:
            raise SystemExit(f"unexpected socket-pool boundary for {profile_id}: {actual}")
    expected_priority_header_boundary = {
        "chrome_122": False,
        "chrome_123": False,
        "chrome_124": True,
    }
    for profile_id, expected in expected_priority_header_boundary.items():
        actual = effective[profile_id]["send_priority_header"]
        if actual != expected:
            raise SystemExit(
                f"unexpected priority-header boundary for {profile_id}: {actual}")
    expected_quic_boundary = {
        "chrome_122": False,
        "chrome_123": False,
        "chrome_124": False,
        "chrome_125": False,
        "chrome_126": False,
        "chrome_127": False,
        "chrome_128": False,
        "chrome_146": False,
        "chrome_147": False,
        "chrome_148": False,
        "chrome_149": True,
        "chrome_150": True,
        "chrome_151": True,
    }
    for profile_id, expected in expected_quic_boundary.items():
        actual = effective[profile_id]["send_quic_orig"]
        if actual != expected:
            raise SystemExit(
                f"unexpected QUIC ORIG boundary for {profile_id}: {actual}")
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
        "  bool enable_quic;",
        "  bool try_quic_by_default;",
        "  bool client_hello_padding_enabled;",
        "  uint16_t client_hello_padding_length;",
        "  uint8_t network_feature_flags;",
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
        "    0,",
            f"    {'true' if protocol['randomize_socket_pool_limit'] else 'false'},",
            f"    {'true' if protocol['randomize_proxy_socket_pool_limit'] else 'false'},",
            f"    {'true' if protocol['send_priority_header'] else 'false'},",
            f"    {'true' if protocol['send_quic_orig'] else 'false'},",
            f"    {'true' if protocol.get('send_quic_legacy_version_information', False) else 'false'},",
            f"    {'true' if protocol['enable_quic'] else 'false'},",
            f"    {'true' if protocol['try_quic_by_default'] else 'false'},",
            # Chrome 119+ uses BoringSSL's length-dependent RFC 7685
            # padding. Do not freeze the observed 28-byte result: ECH GREASE
            # payload rotation can make the extension appear or disappear.
            "    true,",
            f"    {408 if major <= 118 else 0},",
            f"    0x{flags:04x},",
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
