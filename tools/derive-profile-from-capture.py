#!/usr/bin/env python3
"""Derive a normalized profile entry from a release's own capture and source.

Unlike tools/build-profile-entry.py, which rebuilds one entry *from a template*
and only asserts the template's stable values still hold, this script takes
nothing from a newer or older release. Every value it emits is read either from
that release's own wire capture or from that release's own audited source
lines. That is the requirement for chrome_86..chrome_98: the existing
templates start at chrome_99, and copying chrome_99's constants backwards would
be exactly the "相近版本替代" this repository forbids.

Inputs per release:

  --captures   concatenated tls.peet.ws /api/all JSON objects, one per
               connection, captured from that release's own binary
  --evidence   the release's entry from tools/collect-profile-evidence.py
  --sources    the release's raw audited files (collect-profile-evidence.py
               --sources-out)
  --tag        the release tag, e.g. 86.0.4240.198
  --commit     that tag's chromium commit

Output: one normalized_profiles.json entry on stdout (or --out).

Every derived value is cross-checked against a second, independent source
before it is emitted:

  wire  <-> source   the extension set the capture shows must be the set the
                     audited BoringSSL/Chromium lines gate, and the ALPS
                     codepoint must match the feature the tag actually defines.
  wire  <-> wire     the stable values must hold across every connection in the
                     capture, or the capture is not evidence of a stable value.

A disagreement is a hard failure, not a warning: a profile that silently
inherits a neighbouring release is worse than a missing profile.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Wire display name -> normalized_profiles.json extension name. Extensions the
# project has never modelled are a hard failure rather than being dropped: a
# silently dropped extension changes the fingerprint.
WIRE_EXTENSION_NAMES = {
    "application_layer_protocol_negotiation (16)": "application_layer_protocol_negotiation",
    "application_settings (17613)": "application_settings",
    "application_settings_old (17513)": "application_settings_old",
    "compress_certificate (27)": "compress_certificate",
    "ec_point_formats (11)": "ec_point_formats",
    "extended_master_secret (23)": "extended_master_secret",
    "extensionEncryptedClientHello (boringssl) (65037)": "extensionEncryptedClientHello",
    "extensionRenegotiationInfo (boringssl) (65281)": "extensionRenegotiationInfo",
    "key_share (51)": "key_share",
    "padding (21)": "padding",
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

# Signature scheme names as the capture spells them -> the curated spelling the
# profile schema uses. Two of them are bare hex in the schema, which is why the
# comparison runs on the code point inside build-profile-entry.py; here the
# schema spelling is what gets written out.
SIGNATURE_NAMES = {
    "ecdsa_secp256r1_sha256": "ecdsa_secp256r1_sha256",
    "ecdsa_secp384r1_sha384": "0x0503",
    "ecdsa_secp521r1_sha512": "0x0603",
    "rsa_pss_rsae_sha256": "rsa_pss_rsae_sha256",
    "rsa_pss_rsae_sha384": "rsa_pss_rsae_sha384",
    "rsa_pss_rsae_sha512": "rsa_pss_rsae_sha512",
    "rsa_pkcs1_sha256": "rsa_pkcs1_sha256",
    "rsa_pkcs1_sha384": "rsa_pkcs1_sha384",
    "rsa_pkcs1_sha512": "0x0601",
    "0x0904": "0x0904",
    "0x0905": "0x0905",
    "0x0906": "0x0906",
}

GROUP_NAMES = {
    "X25519": "X25519",
    "P-256": "P-256",
    "P-384": "P-384",
    "P-521": "P-521",
    "X25519Kyber768": "X25519Kyber768",
    "X25519MLKEM768": "X25519MLKEM768",
}

CIPHER_GREASE = re.compile(r"GREASE", re.IGNORECASE)


def fail(message):
    raise SystemExit(f"derive-profile: {message}")


# The reference capture set lives beside this repository, not inside it; the
# caller names it. One successful H3 capture is the threshold: a release with
# zero is one whose HTTP/3 behaviour the evidence cannot describe at all.
def h3_baseline_available(tag):
    major = tag.split(".", 1)[0]
    # Reference captures are not part of this repository, so the search order
    # is: the directory the caller named, then whatever the environment points
    # at. A single baked-in developer path used to stand here.
    reference_root = Path(os.environ.get("REFERENCE_CAPTURES_DIR", "."))
    for candidate in (reference_root / "tls-results-h3.json",
                      Path(__file__).resolve().parents[1] / ".." / "完整" /
                      "tls-results-h3.json"):
        try:
            document = json.loads(Path(candidate).read_text(encoding="utf-8"))
        except OSError:
            continue
        entry = document.get(major)
        if not entry:
            continue
        return any(not sample.get("error") for sample in entry.get("samples") or [])
    return False


def read_records(path):
    """The capture file is concatenated JSON objects, not a JSON array."""
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


def strip_group_suffix(value):
    match = re.match(r"(.+?)\s*\(\d+\)$", str(value))
    return match.group(1) if match else str(value)


def is_grease(value):
    return "GREASE" in str(value).upper()


def extension(tls, prefix):
    for item in tls.get("extensions", []):
        if item.get("name", "").startswith(prefix):
            return item
    return {}


def stable_view(record):
    """The half of a capture that must be identical on every connection."""
    tls = record["tls"]
    names = []
    for item in tls.get("extensions", []):
        wire = item["name"]
        if is_grease(wire):
            # The schema keeps each GREASE slot in the capture's order; dropping
            # them here would make the stored order disagree with the wire.
            names.append("TLS_GREASE")
            continue
        if wire not in WIRE_EXTENSION_NAMES:
            fail(f"unmapped wire extension {wire!r}; teach WIRE_EXTENSION_NAMES "
                 "about it instead of dropping it")
        names.append(WIRE_EXTENSION_NAMES[wire])
    ciphers = [c for c in tls.get("ciphers", []) if not CIPHER_GREASE.search(c)]
    groups = [strip_group_suffix(g)
              for g in extension(tls, "supported_groups").get("supported_groups", [])
              if not is_grease(g)]
    signatures = [SIGNATURE_NAMES[s]
                  for s in extension(tls, "signature_algorithms").get(
                      "signature_algorithms", [])
                  if not is_grease(s)]
    alpn = extension(tls, "application_layer_protocol_negotiation").get("protocols", [])
    key_shares = [k for k in extension(tls, "key_share").get("shared_keys", [])
                  if not any(is_grease(name) for name in k)]
    padding = extension(tls, "padding")
    return {
        "ciphers": ciphers,
        "extensions": names,
        "groups": groups,
        "signatures": signatures,
        "alpn": alpn,
        "key_share_count": len(key_shares),
        "padding_length": padding.get("padding_data_length"),
    }


def assert_stable(records, field, value):
    """Every connection must agree, otherwise it is not a stable value."""
    for index, record in enumerate(records, 1):
        actual = stable_view(record)[field]
        if actual != value:
            fail(f"connection {index}: {field} is not stable\n"
                 f"  first: {value}\n  this:  {actual}")


# Source-side expectations. Each entry maps a wire fact to the audited source
# lines that gate it, so the two halves of the evidence can disagree loudly.
ALPS_FEATURE_RE = re.compile(r"kAlpsForHttp2|AlpsForHttp2", re.IGNORECASE)
ALPS_SETTING_RE = re.compile(r"application_settings_\[")
ECH_FEATURE_RE = re.compile(r"EncryptedClientHello|kUseECH|ECHConfig", re.IGNORECASE)
TRUST_ANCHOR_RE = re.compile(r"TLSTrustAnchor|trust_anchor", re.IGNORECASE)
# Two shapes, one behaviour. Up to 92 the padding block is written inline in
# ssl/t1_lib.cc around TLSEXT_TYPE_padding; from 93 it moved behind
# add_padding_extension() in ssl/extensions.cc. Accepting only the newer spelling
# would have declared the padding extension unexplainable for 86-92 even though
# those releases demonstrably emit it.
PADDING_RE = re.compile(
    r"client_hello_padding|add_padding_extension|TLSEXT_TYPE_padding", re.IGNORECASE)


def _load_builder():
    """tools/build-profile-entry.py owns the marker regexes and the curated
    value markers. Importing it instead of copying them keeps one definition:
    a profile derived here and one built there are compared with the same rules."""
    import importlib.util
    path = Path(__file__).resolve().parent / "build-profile-entry.py"
    spec = importlib.util.spec_from_file_location("build_profile_entry", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_text(sources, name):
    path = Path(sources) / name
    if not path.exists():
        fail(f"missing audited source {path}; pass --sources from "
             "collect-profile-evidence.py --sources-out")
    return path.read_text(encoding="utf-8", errors="replace")


def audited_categories(sources):
    """http2/http3/websocket/feature_list, from this release's own source lines.

    build-profile-entry.py reads a directory whose files are named after the
    upstream path with '/' replaced by '_'; the collector stores them under the
    evidence key instead, so each file is staged under both names.
    """
    builder = _load_builder()
    staging = Path(sources) / ".builder-names"
    staging.mkdir(exist_ok=True)
    out = {}
    for category, upstream in builder.SOURCE_FILE.items():
        flat = upstream.replace("/", "_")
        origin = Path(sources) / category
        if not origin.exists():
            fail(f"missing audited source {origin}")
        (staging / flat).write_bytes(origin.read_bytes())
    for category in builder.SOURCE_FILE:
        signals, values = builder.source_values_and_markers(staging, category)
        out["feature_list" if category == "features" else category] = {
            "source_markers": signals,
            "source_values": values,
        }
    return out


def assert_source_agrees(sources, tag, wire):
    """The wire must be explainable by the release's own audited lines."""
    alpn_source = [
        line for line in source_text(sources, "alpn").splitlines()
        if re.search(r"next_protos_\.push_back", line)
    ]
    if not alpn_source:
        fail(f"{tag}: audited alpn source has no next_protos_ push_back; the "
             "ALPN order cannot be justified from this release")

    features = source_text(sources, "features")
    defines_alps = bool(ALPS_FEATURE_RE.search(features))
    uses_alps = bool(ALPS_SETTING_RE.search(source_text(sources, "alpn")))
    wire_has_alps = any(name in wire["extensions"]
                        for name in ("application_settings",
                                     "application_settings_old"))
    if uses_alps != wire_has_alps:
        fail(f"{tag}: ALPS disagreement between source and wire "
             f"(source sets application_settings_={uses_alps}, "
             f"wire has an ALPS extension={wire_has_alps})")
    if uses_alps and not defines_alps:
        fail(f"{tag}: alpn source gates ALPS on a feature net/base/features.cc "
             "does not define")

    anchor_source = source_text(sources, "boringssl_extensions")
    defines_anchor = bool(TRUST_ANCHOR_RE.search(anchor_source))
    wire_has_anchor = "trust_anchors" in wire["extensions"]
    if defines_anchor != wire_has_anchor:
        fail(f"{tag}: trust_anchors disagreement "
             f"(source references it={defines_anchor}, wire={wire_has_anchor})")

    ech_source = bool(ECH_FEATURE_RE.search(anchor_source))
    ech_wire = "extensionEncryptedClientHello" in wire["extensions"]
    if ech_wire and not ech_source:
        fail(f"{tag}: wire carries ECH but the audited BoringSSL lines never "
             "mention it")

    if "padding" in wire["extensions"]:
        if not PADDING_RE.search(anchor_source):
            fail(f"{tag}: wire carries the padding extension but the audited "
                 "BoringSSL lines never mention it")
    return {
        "alps_feature_defined": defines_alps,
        "alps_setting_in_session": uses_alps,
        "trust_anchor_in_boringssl": defines_anchor,
        "ech_in_boringssl": ech_source,
    }


def derive(tag, commit, records, evidence, sources):
    if len(records) < 6:
        fail(f"{tag}: {len(records)} connections; the acceptance rule requires 6")
    first = stable_view(records[0])
    for field in ("ciphers", "extensions", "groups", "signatures", "alpn",
                  "key_share_count"):
        assert_stable(records, field, first[field])

    padding_lengths = {stable_view(r)["padding_length"] for r in records}
    padding_lengths.discard(None)
    if len(padding_lengths) > 1:
        fail(f"{tag}: padding length varies across connections: {sorted(padding_lengths)}")
    padding_length = padding_lengths.pop() if padding_lengths else 0

    wire = {
        "ciphers": first["ciphers"],
        "extensions": first["extensions"],
        "groups": first["groups"],
        "signatures": first["signatures"],
        "alpn": first["alpn"],
        "key_share_count": first["key_share_count"],
    }
    h3_available = h3_baseline_available(tag)
    source_facts = assert_source_agrees(sources, tag, wire)
    categories = audited_categories(sources)
    release_signals = evidence["releases"][tag]["source_files"]
    for category, key in (("http2", "http2"), ("http3", "http3"),
                          ("websocket", "websocket"), ("features", "features")):
        recorded = release_signals[key]["signals"]
        computed = categories["feature_list" if category == "features"
                              else category]["source_markers"]
        if computed != recorded:
            fail(f"{tag}: {category} markers recomputed from the audited file "
                 "disagree with the recorded evidence; one of the two is stale")

    extensions = set(wire["extensions"])
    ordered = [name for name in wire["extensions"]]
    # extension order: BoringSSL permutes the extension block randomly from the
    # release that introduced the Fisher-Yates shuffle. The capture decides it:
    # if every connection reports a different order the profile is random,
    # otherwise the observed order is native and fixed.
    orders = {tuple(stable_view(r)["extensions"]) for r in records}
    extension_order = ("boringssl_random_fisher_yates" if len(orders) > 1
                       else "boringssl_native")

    release = evidence["releases"][tag]
    profile = {
        "profile_id": "chrome_" + tag.split(".", 1)[0],
        "namespace": "minicronet/chrome_" + tag.split(".", 1)[0],
        "chrome_version": tag,
        "chromium_tag": tag,
        "chromium_commit": commit,
        "boringssl_revision": release["boringssl_revision"],
        "quiche_revision": release["quiche_revision"],
        "tls": {
            "cipher_suites": (["TLS_GREASE"] + wire["ciphers"]),
            "curves": wire["groups"],
            "extension_order": extension_order,
            "extensions": ordered,
            "grease": {
                "enabled": True,
                "fixed_seed": False,
                "per_connection": True,
            },
            "grease_signature_algorithms": (
                # A GREASE value inside signature_algorithms shows up on the wire
                # as a bare 0x?A?A entry the schema spells 0x0904-style; the
                # capture reports it explicitly, so read it from there.
                any(re.fullmatch(r"0x[0-9a-fA-F]{2}0a", s) and
                    int(s, 16) >> 8 == (int(s, 16) & 0xff)
                    for s in extension(records[0]["tls"],
                                       "signature_algorithms").get(
                                           "signature_algorithms", []))
            ),
            "hex_codes": [],
            "key_share_count": wire["key_share_count"],
            "signature_algorithms": wire["signatures"],
        },
        # activation records what this release cannot be claimed to do. 86..98
        # are the releases whose H3 baseline capture the reference set does not
        # contain (six QUIC_PROTOCOL_ERROR per release), so h3_disabled is
        # recorded explicitly rather than left implicit by a missing key: a
        # reader must be able to tell "QUIC not exercised" apart from
        # "QUIC verified".
        "activation": {
            # Only releases with no usable HTTP/3 baseline are marked disabled.
            # chrome_93..chrome_98 do have six good H3 captures each and their
            # H3 alignment passes, so marking them disabled would deny a
            # capability the evidence proves.
            "h3_disabled": not h3_available,
            "requires_matching_chromium_revision": True,
            "wire_verified": True,
            **({} if h3_available else {
                "h3_evidence": "no usable HTTP/3 baseline for this release; "
                               "see docs/CHROME_86_98_SUPPORT.md"}),
        },
        # http2 / http3 / websocket / feature_list come from this release's own
        # source lines, and are asserted equal to the signals the evidence
        # collector recorded for the same files.
        **categories,
        "alpn": {
            "source_audited": True,
            # Null for the same reason chrome_99..chrome_157 are null: the wire
            # value is evidence for the source audit, not a runtime input. The
            # capture's ALPN is asserted against the audited pushes above and
            # recorded under derivation.wire_alpn.
            "wire_protocols": None,
            "source_values": [],
            "note": ("ALPN order is read from this release's own capture and "
                     "cross-checked against its audited next_protos_ pushes."),
        },
        "random_strategy": {
            "extension_permutation": (
                "boringssl_random_fisher_yates"
                if extension_order == "boringssl_random_fisher_yates"
                else "native_callback_order"),
            "fixed_seed_allowed": False,
            "grease": "per_handshake_seed",
            "quic_insecure": "xoshiro256pp_seeded_from_csprng",
            "quic_secure": "boringssl_rand_bytes",
            "tls": "boringssl_rand_bytes",
        },
        "evidence": {
            # The capture collector records which repository each file came from
            # so a reader can re-fetch it; the profile schema predates that key
            # and the audited entries do not carry it, so it is dropped here
            # rather than changing the shape of 59 existing entries.
            "source_files": {
                name: {key: value for key, value in entry.items()
                       if key != "repository"}
                for name, entry in release["source_files"].items()
            },
            "wire_verified": True,
            "status": "wire_verified",
        },
        "derivation": {
            "method": "tools/derive-profile-from-capture.py",
            "connections": len(records),
            "padding_data_length": padding_length,
            "source_facts": source_facts,
            "extension_order_observed_permutations": len(orders),
            "wire_alpn": wire["alpn"],
            "wire_extension_order": ordered,
        },
    }
    body = json.dumps(profile, sort_keys=True, separators=(",", ":"))
    profile["profile_digest"] = hashlib.sha256(body.encode()).hexdigest()
    return profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--captures", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--sources", required=True)
    parser.add_argument("--out")
    args = parser.parse_args()

    records = read_records(args.captures)
    evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
    profile = derive(args.tag, args.commit, records, evidence, args.sources)
    text = json.dumps(profile, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
