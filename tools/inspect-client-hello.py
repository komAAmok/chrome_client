#!/usr/bin/env python3
"""Read back the ClientHello a profile actually sends.

Fingerprint claims in the profile table are only worth what the wire says. This
starts a listener that reads one TLS ClientHello, never replies, and parses the
extension list out of it. The request fails, which is fine -- the ClientHello is
already on the wire by then.

    PYTHONPATH=bindings/python:target/release \
    LD_LIBRARY_PATH=core/binaries/linux-x86_64 \
        tools/inspect-client-hello.py --profile chrome_151 --profile chrome_152

With two or more profiles it also prints the extension-set difference between
them, which is the check that matters when adding a profile: every other
profile's set has to stay exactly as it was.
"""

import argparse
import json
import socket
import struct
import sys
import threading

GREASE_VALUES = {0x0A0A, 0x1A1A, 0x2A2A, 0x3A3A, 0x4A4A, 0x5A5A, 0x6A6A, 0x7A7A,
                 0x8A8A, 0x9A9A, 0xAAAA, 0xBABA, 0xCACA, 0xDADA, 0xEAEA, 0xFAFA}

EXTENSION_NAMES = {
    0: "server_name", 5: "status_request", 10: "supported_groups",
    11: "ec_point_formats", 13: "signature_algorithms", 16: "alpn",
    18: "signed_certificate_timestamp", 21: "padding",
    23: "extended_master_secret", 27: "compress_certificate",
    35: "session_ticket", 41: "pre_shared_key", 43: "supported_versions",
    45: "psk_key_exchange_modes", 51: "key_share",
    17513: "application_settings_old", 17613: "application_settings",
    0xCA34: "trust_anchors", 0xFE0D: "encrypted_client_hello",
    0xFF01: "renegotiation_info",
}


def read_exact(sock, count):
    buffer = b""
    while len(buffer) < count:
        chunk = sock.recv(count - len(buffer))
        if not chunk:
            raise EOFError("peer closed mid-record")
        buffer += chunk
    return buffer


def read_client_hello(sock):
    """Returns the ClientHello handshake body, without the record header."""
    header = read_exact(sock, 5)
    kind, _major, _minor, length = struct.unpack("!BBBH", header)
    if kind != 22:
        raise ValueError(f"expected a handshake record, got type {kind}")
    body = read_exact(sock, length)
    if not body or body[0] != 1:
        raise ValueError("first handshake message is not a ClientHello")
    return body[4:]


def parse_client_hello(body):
    """Extracts the fields the profile table controls."""
    view = memoryview(body)
    offset = 2 + 32                       # legacy_version + random
    session_len = view[offset]
    offset += 1 + session_len
    cipher_len = struct.unpack_from("!H", view, offset)[0]
    offset += 2
    ciphers = list(struct.unpack_from(f"!{cipher_len // 2}H", view, offset))
    offset += cipher_len
    compression_len = view[offset]
    offset += 1 + compression_len

    extensions = []
    if offset + 2 <= len(view):
        total = struct.unpack_from("!H", view, offset)[0]
        offset += 2
        end = offset + total
        while offset + 4 <= end:
            ext_type, ext_len = struct.unpack_from("!HH", view, offset)
            offset += 4
            extensions.append((ext_type, bytes(view[offset:offset + ext_len])))
            offset += ext_len
    return ciphers, extensions


def decode_trust_anchors(payload):
    """Splits the extension body into its trust anchor IDs."""
    if len(payload) < 2:
        return []
    declared = struct.unpack_from("!H", payload, 0)[0]
    inner = payload[2:2 + declared]
    ids, offset = [], 0
    while offset < len(inner):
        length = inner[offset]
        if length == 0 or offset + 1 + length > len(inner):
            return ids
        ids.append(inner[offset + 1:offset + 1 + length].hex())
        offset += 1 + length
    return ids


def capture_one(profile, timeout):
    """Issues one request with `profile` active and returns its ClientHello."""
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    result = {}

    def serve():
        try:
            conn, _ = listener.accept()
            conn.settimeout(timeout)
            with conn:
                result["hello"] = read_client_hello(conn)
        except Exception as error:              # noqa: BLE001 - reported below
            result["error"] = repr(error)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    import chrome_client
    # verify=False keeps the failure about the missing ServerHello rather than
    # about the certificate; either way the ClientHello is already sent.
    with chrome_client.Client(impersonate=profile, verify=False) as client:
        try:
            client.get(f"https://127.0.0.1:{port}/", timeout=timeout)
        except Exception:                       # noqa: BLE001 - expected
            pass
    thread.join(timeout + 1)
    listener.close()
    if "hello" not in result:
        raise SystemExit(f"{profile}: no ClientHello captured ({result.get('error')})")
    return parse_client_hello(result["hello"])


def describe(profile, samples):
    """Reports what is constant across samples and what is not.

    Repeats matter: BoringSSL's RFC 7685 padding is length-dependent, and the ECH
    GREASE payload changes length between connections, so `padding` comes and
    goes for the same profile. One sample per profile would read that as a
    fingerprint change.
    """
    sets = [frozenset(t for t, _ in extensions if t not in GREASE_VALUES)
            for _, extensions in samples]
    stable = sorted(set.intersection(*(set(s) for s in sets)))
    variable = sorted(set.union(*(set(s) for s in sets)) - set(stable))
    ciphers, extensions = samples[0]
    ids = [t for t, _ in extensions]
    anchors = next((sorted(decode_trust_anchors(p)) for t, p in extensions
                    if t == 0xCA34), [])
    anchor_sets = {tuple(sorted(decode_trust_anchors(p)))
                   for _, exts in samples for t, p in exts if t == 0xCA34}
    anchor_orders = {tuple(decode_trust_anchors(p))
                     for _, exts in samples for t, p in exts if t == 0xCA34}

    print(f"--- {profile}  ({len(samples)} samples)")
    print(f"    ciphers      {len([c for c in ciphers if c not in GREASE_VALUES])}"
          f" (+{len([c for c in ciphers if c in GREASE_VALUES])} GREASE)")
    print(f"    extensions   {len(stable)} stable"
          + (f", {len(variable)} varying: "
             + ", ".join(EXTENSION_NAMES.get(t, f'0x{t:04x}') for t in variable)
             if variable else ""))
    print("      order (first sample): " + ", ".join(
        EXTENSION_NAMES.get(t, f"0x{t:04x}") if t not in GREASE_VALUES
        else "GREASE" for t in ids))
    if anchors:
        print(f"    trust_anchors {len(anchors)} IDs, "
              f"{len(anchor_sets)} distinct set(s), "
              f"{len(anchor_orders)} distinct order(s) across samples")
    return {"stable_extensions": stable,
            "varying_extensions": variable,
            "trust_anchors": anchors,
            "trust_anchor_distinct_sets": len(anchor_sets),
            "trust_anchor_distinct_orders": len(anchor_orders)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", action="append", required=True)
    parser.add_argument("--repeat", type=int, default=1,
                        help="samples per profile; >1 separates the "
                             "length-dependent padding extension from a real change")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--out")
    args = parser.parse_args()

    seen = {}
    for profile in args.profile:
        samples = [capture_one(profile, args.timeout) for _ in range(args.repeat)]
        seen[profile] = describe(profile, samples)

    if len(seen) > 1:
        print("\nstable extension-set differences:")
        names = list(seen)
        base = names[0]
        for other in names[1:]:
            a = set(seen[base]["stable_extensions"])
            b = set(seen[other]["stable_extensions"])
            fmt = lambda ts: ", ".join(  # noqa: E731 - local formatting helper
                EXTENSION_NAMES.get(t, f"0x{t:04x}") for t in sorted(ts)) or "(none)"
            print(f"  {base} vs {other}")
            print(f"    only in {base}:  {fmt(a - b)}")
            print(f"    only in {other}: {fmt(b - a)}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(seen, indent=2, sort_keys=True) + "\n")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
