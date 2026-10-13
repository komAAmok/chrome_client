#!/usr/bin/env python3
"""Build a chrome_15x entry for profiles/normalized_profiles.json.

The repository's own docs say normalized_profiles.json cannot be regenerated:
`generated_by` names tools/build-normalized-profiles.py, which lives only in the
pre-migration `new/` tree and stops at chrome_151. chrome_152 and chrome_153 were
added by hand, and a new profile has no generator.

This script rebuilds the derivable half from the release's own inputs, and is
validated by regenerating an existing entry and diffing it against the committed
one. The split is deliberate:

  * `tls` semantic constants (cipher/curve/signature lists, extension order
    policy, GREASE policy) are carried over from the template, because they are
    the project's curated names rather than raw wire strings and the wire gate
    proves they did not move. Inheritance is not assumed: every stable value the
    capture reports is asserted equal to the template's expectation first.
  * `tls.trust_anchor_ids_encoded` is taken from the new capture, because its
    permutation is a per-process absl property that differs release to release.
  * `alpn`/`http2`/`http3`/`websocket`/`feature_list` source_markers and
    source_values are recomputed from the release's own source lines and must
    equal the audited evidence's signals.
  * `evidence.source_files` is the audited evidence verbatim.

    tools/build-profile-entry.py --profile chrome_154 \
        --captures profiles/chrome-154/captures.json \
        --evidence profiles/chrome-154/source-evidence.json \
        --trust-anchors profiles/chrome-154/trust-anchor-ids.json \
        --sources /tmp/src154 --tag 154.0.8037.100 \
        --commit 81ab43db91f352ce9134362efe32529b9761c4eb --out /tmp/p154.json
"""

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# wire display name -> the key normalized_profiles.json uses.
WIRE_EXTENSION_NAMES = {
    "application_layer_protocol_negotiation (16)": "application_layer_protocol_negotiation",
    "application_settings (17613)": "application_settings",
    "compress_certificate (27)": "compress_certificate",
    "ec_point_formats (11)": "ec_point_formats",
    "extended_master_secret (23)": "extended_master_secret",
    "extensionEncryptedClientHello (boringssl) (65037)": "extensionEncryptedClientHello",
    "extensionRenegotiationInfo (boringssl) (65281)": "extensionRenegotiationInfo",
    "key_share (51)": "key_share",
    "psk_key_exchange_modes (45)": "psk_key_exchange_modes",
    "server_name (0)": "server_name",
    "session_ticket (35)": "session_ticket",
    "signature_algorithms (13)": "signature_algorithms",
    "signed_certificate_timestamp (18)": "signed_certificate_timestamp",
    "status_request (5)": "status_request",
    "supported_groups (10)": "supported_groups",
    "supported_versions (43)": "supported_versions",
    "Unknown extension 51764": "trust_anchors",
}

SOURCE_FILE = {
    "alpn": "net/http/http_network_session.cc",
    "http2": "net/spdy/spdy_session.cc",
    "http3": "net/quic/quic_chromium_client_session.cc",
    "websocket": "net/websockets/websocket_basic_handshake_stream.cc",
    "features": "net/base/features.cc",
}

# Copied verbatim from new/tools/extract-protocol-strategies.py so a profile
# built here is directly comparable with the 53 audited ones.
MARKERS = {
    "alpn": {"h1": r"HTTP/1|kProtoHTTP11|HTTP11", "h2": r"HTTP/2|kProtoHTTP2|HTTP2",
             "h3": r"HTTP/3|kProtoHTTP3|HTTP3", "alpn": r"ALPN|GetAlpnProtos|NextProto"},
    "http2": {"settings": r"SETTINGS|settings", "window": r"WINDOW|window|flow control",
              "priority": r"priority|PRIORITY|exclusive",
              "pseudo_headers": r"pseudo|:method|:authority|:scheme|:path",
              "connect": r"CONNECT_PROTOCOL|extended CONNECT|connect"},
    "http3": {"transport": r"transport|TransportParameter", "settings": r"SETTINGS|settings",
              "flow_control": r"flow.control|window",
              "connection_id": r"connection.id|ConnectionId",
              "grease": r"grease|GREASE", "websocket": r"websocket|WebSocket|CONNECT"},
    "websocket": {"upgrade": r"Upgrade|upgrade", "origin": r"Origin|origin",
                  "sec_headers": r"Sec-WebSocket|sec-websocket",
                  "connect": r"CONNECT|connect", "extensions": r"extension|Extension"},
    "features": {"base_feature": r"BASE_FEATURE|BASE_DECLARE_FEATURE",
                 "quic": r"QUIC|QUICHE|HTTP3", "tls": r"ECH|ALPS|TLS|certificate",
                 "http2": r"HTTP2|HTTP/2|SPDY", "websocket": r"WebSocket|WEBSOCKET",
                 "variation": r"variation|field.trial|Finch"},
}

VALUE_MARKERS = {
    "alpn": (r"next_protos_|application_settings_|kProtoHTTP", 80),
    "http2": (r"initial_settings|SETTINGS_[A-Z_]+|window_size|priority", 120),
    "http3": (r"QuicConfig|TransportParameter|SETTINGS_[A-Z_]+|flow.control", 120),
    "websocket": (r"Sec-WebSocket|Upgrade|Origin|extensions", 100),
    "features": (r"BASE_FEATURE|BASE_DECLARE_FEATURE", 120),
}


# A signature scheme is a 16-bit code point. The curated list spells two of
# them as bare hex (`0x0503`) and the capture spells the same schemes by name
# (`ecdsa_secp384r1_sha384`), so comparisons run on the code point, not the
# string.
SIGNATURE_CODE_POINTS = {
    "ecdsa_secp256r1_sha256": 0x0403,
    "rsa_pss_rsae_sha256": 0x0804,
    "rsa_pkcs1_sha256": 0x0401,
    "ecdsa_secp384r1_sha384": 0x0503,
    "rsa_pss_rsae_sha384": 0x0805,
    "rsa_pkcs1_sha384": 0x0501,
    "rsa_pss_rsae_sha512": 0x0806,
    "rsa_pkcs1_sha512": 0x0601,
    "0x0904": 0x0904,
    "0x0905": 0x0905,
    "0x0906": 0x0906,
}


def signature_code_point(value):
    if value in SIGNATURE_CODE_POINTS:
        return SIGNATURE_CODE_POINTS[value]
    match = re.fullmatch(r"0x([0-9a-fA-F]+)", value)
    if match:
        return int(match.group(1), 16)
    raise SystemExit(f"unknown signature scheme: {value}")


def read_captures(path):
    raw = Path(path).read_text(encoding="utf-8")
    decoder, records, index = json.JSONDecoder(), [], 0
    while index < len(raw):
        while index < len(raw) and raw[index] in " \t\r\n":
            index += 1
        if index >= len(raw):
            break
        record, index = decoder.raw_decode(raw, index)
        records.append(record)
    return records


def source_values_and_markers(source_dir, category):
    filename = SOURCE_FILE[category].replace("/", "_")
    text = (Path(source_dir) / filename).read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    signals = {}
    for name, pattern in MARKERS[category].items():
        hits = [i for i, line in enumerate(lines, 1)
                if re.search(pattern, line, re.IGNORECASE)]
        signals[name] = {"count": len(hits), "lines": hits[:32]}
    pattern, limit = VALUE_MARKERS[category]
    values = []
    for i, line in enumerate(lines, 1):
        if re.search(pattern, line, re.IGNORECASE):
            values.append({"line": i, "text": line.strip()[:240]})
            if len(values) >= limit:
                break
    return signals, values


def assert_wire_matches_template(captures, validation, template):
    """The inheritance above is only sound if the wire agrees; check it."""
    tls = template["tls"]
    wire_ciphers = [c for c in captures[0]["tls"]["ciphers"] if "GREASE" not in c]
    expected_ciphers = [c for c in tls["cipher_suites"] if c != "TLS_GREASE"]
    if wire_ciphers != expected_ciphers:
        raise SystemExit(
            f"cipher suites moved: template has {expected_ciphers}, "
            f"capture has {wire_ciphers}")

    wire_extensions = set()
    for entry in captures[0]["tls"]["extensions"]:
        wire = entry["name"]
        if "GREASE" in wire:
            wire_extensions.add("TLS_GREASE")
            continue
        try:
            wire_extensions.add(WIRE_EXTENSION_NAMES[wire])
        except KeyError:
            raise SystemExit(f"unmapped wire extension: {wire}")
    if wire_extensions != set(tls["extensions"]):
        raise SystemExit(
            "extension set moved: template has "
            f"{sorted(set(tls['extensions']))}, capture has {sorted(wire_extensions)}")

    def strip_group(value):
        match = re.match(r"(.+?)\s*\(\d+\)$", value)
        return match.group(1) if match else value

    wire_groups = [strip_group(g)
                   for g in validation["stable_expectations"]["supported_groups"]]
    if wire_groups != tls["curves"]:
        raise SystemExit(
            f"supported groups moved: template has {tls['curves']}, "
            f"capture has {wire_groups}")

    wire_sigs = [signature_code_point(s) for s in
                 validation["stable_expectations"]["signature_algorithms"]]
    template_sigs = [signature_code_point(s) for s in tls["signature_algorithms"]]
    if wire_sigs != template_sigs:
        raise SystemExit(
            f"signature algorithms moved: template has {template_sigs}, "
            f"capture has {wire_sigs}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--captures", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--trust-anchors", required=True)
    parser.add_argument("--sources", required=True)
    parser.add_argument("--tag", required=True, help="pinned release tag")
    parser.add_argument("--commit", required=True, help="the tag's commit")
    parser.add_argument("--template", default="chrome_153")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    captures = read_captures(args.captures)
    validation = json.loads(
        (Path(args.captures).parent / "validation.json").read_text())
    if not validation.get("wire_verified"):
        raise SystemExit("capture did not pass the wire gate; refusing to build")
    evidence = json.loads(Path(args.evidence).read_text())
    release = evidence["releases"][args.tag]
    anchors = json.loads(Path(args.trust_anchors).read_text())

    all_profiles = json.loads(
        (ROOT / "profiles/normalized_profiles.json").read_text())["profiles"]
    template = next(p for p in all_profiles
                    if p["profile_id"] == args.template)
    assert_wire_matches_template(captures, validation, template)

    profile = json.loads(json.dumps(template))
    profile["profile_id"] = args.profile
    profile["namespace"] = "minicronet/" + args.profile
    profile["chrome_version"] = args.tag
    profile["chromium_tag"] = args.tag
    profile["chromium_commit"] = args.commit
    profile["boringssl_revision"] = release["boringssl_revision"]
    profile["quiche_revision"] = release["quiche_revision"]
    profile["tls"]["trust_anchor_ids_encoded"] = anchors["encoded_payload_hex"]

    for category in SOURCE_FILE:
        signals, values = source_values_and_markers(args.sources, category)
        audited = release["source_files"][category]["signals"]
        if signals != audited:
            raise SystemExit(
                f"{category}: reconstructed signals disagree with the audited "
                "evidence; refusing to emit a profile they contradict")
        if category == "alpn":
            profile["alpn"] = {
                "source_audited": True,
                "wire_protocols": None,
                "source_values": values,
                "note": template["alpn"]["note"],
            }
        else:
            key = "feature_list" if category == "features" else category
            profile[key] = {"source_markers": signals, "source_values": values}

    profile["evidence"] = {
        "source_files": release["source_files"],
        "wire_verified": True,
        "status": "wire_verified",
    }
    # chrome_152/153 carry no `activation` block; a new profile follows them,
    # so the template's is dropped rather than inherited.
    profile.pop("activation", None)

    Path(args.out).write_text(
        json.dumps(profile, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
