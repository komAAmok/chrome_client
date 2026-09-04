#!/usr/bin/env python3
"""Turn raw wire captures into the validation report a profile needs.

A profile may only be activated when `wire_verified` holds, which means more
than "one handshake looked right". The gate has two halves:

  stable    values that must be byte-identical on every connection: the
            de-GREASEd cipher/group/signature lists, the extension set and the
            HTTP/2 settings fingerprint.
  stochastic values that must differ on every connection: client random,
            session id, extension order, GREASE values, key share and ECH
            payloads. A fixed value here would mean a seeded RNG, which the
            project forbids outright.

Input is a file of concatenated JSON objects as returned by tls.peet.ws
(`/api/all`), one per connection. The chrome_99--151 captures under `new/` use
the browserleaks schema instead and were verified by the original pipeline; this
tool deliberately accepts only one schema rather than guessing between them.

    tools/verify-wire-capture.py --profile chrome_152 \
        --captures profiles/chrome-152/captures.json \
        --out profiles/chrome-152/validation.json
"""

import argparse
import hashlib
import json
import re
import sys

GREASE_VALUES = {0x0A0A, 0x1A1A, 0x2A2A, 0x3A3A, 0x4A4A, 0x5A5A, 0x6A6A, 0x7A7A,
                 0x8A8A, 0x9A9A, 0xAAAA, 0xBABA, 0xCACA, 0xDADA, 0xEAEA, 0xFAFA}

# Connections that resume a session carry pre_shared_key; the first one cannot.
# It is therefore expected to vary and is excluded from the stable extension set.
RESUMPTION_EXTENSIONS = {41}

MIN_CONNECTIONS = 3


def read_records(path):
    """Reads concatenated JSON objects, which is how the endpoint is usually saved."""
    raw = open(path, encoding="utf-8").read()
    decoder = json.JSONDecoder()
    records, index = [], 0
    while index < len(raw):
        while index < len(raw) and raw[index] in " \t\r\n":
            index += 1
        if index >= len(raw):
            break
        record, index = decoder.raw_decode(raw, index)
        records.append(record)
    return records


def is_grease(value):
    text = str(value)
    if "GREASE" in text:
        return True
    match = re.fullmatch(r"0x([0-9a-fA-F]{4})", text)
    return bool(match) and int(match.group(1), 16) in GREASE_VALUES


def extension_id(name):
    """Extension id from a display name like 'key_share (51)' or 'Unknown extension 51764'."""
    match = re.search(r"\((?:0x)?([0-9a-fA-F]+)\)\s*$", name)
    if match:
        token = match.group(1)
        return int(token, 16) if re.search(r"[a-fA-F]", token) else int(token)
    match = re.search(r"(\d+)\s*$", name)
    return int(match.group(1)) if match else None


def degrease(values):
    return [v for v in values if not is_grease(v)]


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True).encode()).hexdigest()


def extension(record, wanted):
    for item in record["tls"]["extensions"]:
        if extension_id(item["name"]) == wanted:
            return item
    return {}


def describe(record):
    tls = record["tls"]
    ids = [extension_id(e["name"]) for e in tls["extensions"]]
    return {
        "extension_ids": ids,
        "extension_set": sorted(i for i in ids
                                if i is not None and i not in GREASE_VALUES),
        "grease_values": [i for i in ids if i in GREASE_VALUES],
        "ciphers": degrease(tls["ciphers"]),
        "groups": degrease(extension(record, 10).get("supported_groups", [])),
        "signature_algorithms": degrease(
            extension(record, 13).get("signature_algorithms", [])),
        "client_random": tls.get("client_random"),
        "session_id": tls.get("session_id"),
        "key_share_digest": digest(extension(record, 51).get("shared_keys")),
        "ech_digest": digest(extension(record, 65037).get("data")),
        "trust_anchors_payload": extension(record, 51764).get("data"),
        "http2_fingerprint": record.get("http2", {}).get("akamai_fingerprint"),
        "user_agent": record.get("user_agent"),
        "source_port": record.get("tcpip", {}).get("tcp_syn", {}).get("src_port"),
    }


STABLE_FIELDS = ["ciphers", "groups", "signature_algorithms",
                 "http2_fingerprint", "trust_anchors_payload", "user_agent"]
STOCHASTIC_FIELDS = ["client_random", "session_id", "extension_ids",
                     "grease_values", "key_share_digest", "ech_digest"]


def verify(profile_id, records):
    views = [describe(r) for r in records]
    report = {
        "profile_id": profile_id,
        "connections": len(views),
        "independent_source_ports": len({v["source_port"] for v in views}),
    }

    stable, mismatches = {}, {}
    for field in STABLE_FIELDS:
        values = {json.dumps(v[field], sort_keys=True) for v in views}
        stable[field] = len(values) == 1
        if len(values) != 1:
            mismatches[field] = sorted(values)

    # The extension set may differ by exactly the resumption extensions, because
    # only the first connection of a run has no ticket to offer.
    sets = [frozenset(v["extension_set"]) for v in views]
    base = min(sets, key=len)
    extra = {i for s in sets for i in (s - base)}
    stable["extension_set"] = extra <= RESUMPTION_EXTENSIONS
    if not stable["extension_set"]:
        mismatches["extension_set"] = sorted(extra - RESUMPTION_EXTENSIONS)

    unique = {f: len({json.dumps(v[f], sort_keys=True) for v in views})
              for f in STOCHASTIC_FIELDS}

    report["stable_expectations"] = {
        "cipher_suites": views[0]["ciphers"],
        "supported_groups": views[0]["groups"],
        "signature_algorithms": views[0]["signature_algorithms"],
        "extension_set": sorted(base),
        "resumption_extensions_observed": sorted(extra),
        "http2_fingerprint": views[0]["http2_fingerprint"],
        "trust_anchors_payload_bytes":
            len(views[0]["trust_anchors_payload"] or "") // 2,
    }
    report["matches"] = stable
    report["mismatches"] = mismatches
    report["stochastic"] = {
        "unique_counts": unique,
        "minimum_required": MIN_CONNECTIONS,
        "fixed_seed_allowed": False,
        "multi_connection_randomness_verified":
            all(count >= MIN_CONNECTIONS for count in unique.values()),
        "grease_present": all(v["grease_values"] for v in views),
    }
    report["wire_verified"] = bool(
        len(views) >= MIN_CONNECTIONS
        and report["independent_source_ports"] == len(views)
        and all(stable.values())
        and report["stochastic"]["multi_connection_randomness_verified"]
        and report["stochastic"]["grease_present"])
    return report


REQUIRED_TLS_FIELDS = ("client_random", "session_id", "ciphers", "extensions")


def check_schema(records, path):
    """Rejects a capture in a different schema instead of reporting empty values."""
    for index, record in enumerate(records, 1):
        tls = record.get("tls")
        if not isinstance(tls, dict):
            raise SystemExit(f"{path}: record {index} has no tls object")
        missing = [f for f in REQUIRED_TLS_FIELDS if f not in tls]
        if missing:
            raise SystemExit(
                f"{path}: record {index} is missing tls.{', tls.'.join(missing)}; "
                "this tool expects the tls.peet.ws /api/all schema")
        if "akamai_fingerprint" not in record.get("http2", {}):
            raise SystemExit(
                f"{path}: record {index} has no http2.akamai_fingerprint; "
                "this tool expects the tls.peet.ws /api/all schema")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--captures", required=True)
    parser.add_argument("--out")
    args = parser.parse_args()

    records = read_records(args.captures)
    if not records:
        raise SystemExit(f"{args.captures}: no capture records")
    check_schema(records, args.captures)
    report = verify(args.profile, records)

    print(f"{report['profile_id']}: {report['connections']} connections, "
          f"{report['independent_source_ports']} distinct source ports")
    for field, ok in report["matches"].items():
        print(f"  stable     {field:24} {'ok' if ok else 'MISMATCH'}")
    for field, count in report["stochastic"]["unique_counts"].items():
        ok = count >= MIN_CONNECTIONS
        print(f"  stochastic {field:24} {count} unique "
              f"{'ok' if ok else f'NEED >= {MIN_CONNECTIONS}'}")
    print(f"  wire_verified: {report['wire_verified']}")

    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
        print(f"wrote {args.out}")
    return 0 if report["wire_verified"] else 1


if __name__ == "__main__":
    sys.exit(main())
