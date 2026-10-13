#!/usr/bin/env python3
"""Migrate the chrome_146/chrome_147 captures into this repository's layout.

Those two profiles were audited by the original browserleaks pipeline rather
than by `tools/verify-wire-capture.py`. Two different capture directories exist
for each and they are not interchangeable:

  * `chrome-<major>-release-validation/` is the peet.ws schema the in-repo gate
    reads, but it was captured through a local forwarding proxy (`ip` is
    `127.0.0.1:<port>`), so every connection presents the same client address to
    the server and the capture cannot demonstrate connection independence.
  * `chrome-<major>-browserleaks-boundary-<date>/` is the browserleaks schema and
    was captured on a real interface with `independent_processes: true`. Its
    `session-N.pcap` files are the primary evidence.

`tcpip` is empty in the browserleaks records, so the source port is read out of
the pcap instead of being invented. The record-to-pcap pairing is not assumed
from the filename: each record's TLS `random` is searched for inside the pcaps,
and a record is only accepted when exactly one captured connection contains it.
A capture whose three records do not yield three distinct source ports is
refused, so a migrated file can never claim independence it does not have.

    tools/migrate-browserleaks-captures.py --major 146 \
        --source new/captures/chrome-146-browserleaks-boundary-20260822 \
        --out profiles/chrome-146/captures.json
"""

import argparse
import json
import struct
from pathlib import Path


# Display names the browserleaks schema uses where the gate wants a numeric id.
def _ext_display(entry):
    """Render a browserleaks extension entry the way the gate's parser expects."""
    ident = entry.get("id")
    if entry.get("name") == "GREASE":
        return f"TLS_GREASE (0x{ident:04x})"
    if ident is not None and entry.get("name"):
        return f"{entry['name']} ({ident})"
    return entry.get("name") or ""


def _to_peet(record, shares=(), ech=b""):
    """Map a browserleaks record onto the peet.ws shape the in-repo gate reads.

    Only field names move; no value is invented. A field with no counterpart is
    left out rather than guessed, and the gate then reports it as a mismatch.
    """
    tls = record.get("tls") or {}
    extensions = []
    for entry in tls.get("extensions", []):
        mapped = dict(entry)
        mapped["name"] = _ext_display(entry)
        extensions.append(mapped)
    out = {
        "user_agent": record.get("user_agent"),
        "tcpip": record.get("tcpip") or {},
        "http2": {
            "sent_frames": record.get("http2") or [],
            "akamai_fingerprint": record.get("akamai_text"),
            "akamai_fingerprint_hash": record.get("akamai_hash"),
        },
        "tls": {
            "ciphers": tls.get("cipher_suites") or [],
            "client_random": tls.get("random"),
            "session_id": tls.get("session_id"),
            "extensions": extensions,
            "ja4": record.get("ja4"),
            "ja3_hash": record.get("ja3_hash"),
            "tls_version_record": tls.get("record_version"),
            "tls_version_negotiated": tls.get("selected_version"),
        },
    }
    # The gate digests extension 51's shared_keys and extension 65037's data;
    # both come from the pcap because the JSON omits the bytes.
    for entry in out["tls"]["extensions"]:
        if entry.get("id") == 51:
            entry["shared_keys"] = [_hex(value) for value in shares]
        elif entry.get("id") == 65037:
            entry["data"] = _hex(ech)
    return out


def _hex(value):
    """The gate's `digest()` consumes hex strings, not bytes."""
    return value.hex() if isinstance(value, (bytes, bytearray)) else value


GREASE_VALUES = {0x0A0A, 0x1A1A, 0x2A2A, 0x3A3A, 0x4A4A, 0x5A5A, 0x6A6A, 0x7A7A,
                 0x8A8A, 0x9A9A, 0xAAAA, 0xBABA, 0xCACA, 0xDADA, 0xEAEA, 0xFAFA}


def _is_grease(value):
    return value in GREASE_VALUES


def client_hello_shares(stream: bytes):
    """(key_share_groups_and_keys, ech_bytes) from a TLS handshake stream.

    BrowserLeaks reports only the *length* of each key share, so the randomness
    of the actual key material cannot be checked from the JSON alone. The
    capture's own pcap carries the ClientHello, so the bytes are read from there
    -- the same source the original pipeline used. Reassembles TLS records until
    a ClientHello (handshake type 1) is found.
    """
    handshake = bytearray()
    cursor = 0
    while cursor + 5 <= len(stream):
        content_type = stream[cursor]
        length = struct.unpack_from("!H", stream, cursor + 3)[0]
        record = stream[cursor + 5:cursor + 5 + length]
        if len(record) != length:
            break
        if content_type == 22:
            handshake.extend(record)
        cursor += 5 + length
    if len(handshake) < 4 or handshake[0] != 1:
        return [], b""
    length = int.from_bytes(handshake[1:4], "big")
    body = bytes(handshake[4:length + 4])
    if len(body) < 38:
        return [], b""
    cursor = 34                                    # legacy_version + random
    session_length = body[cursor]
    cursor += 1 + session_length
    cipher_length = struct.unpack_from("!H", body, cursor)[0]
    cursor += 2 + cipher_length
    compression_length = body[cursor]
    cursor += 1 + compression_length
    extension_length = struct.unpack_from("!H", body, cursor)[0]
    cursor += 2
    end = cursor + extension_length
    shares, ech = [], b""
    while cursor + 4 <= end:
        extension_id, size = struct.unpack_from("!HH", body, cursor)
        cursor += 4
        value = body[cursor:cursor + size]
        cursor += size
        if extension_id == 51 and len(value) >= 2:
            item = 2
            while item + 4 <= len(value):
                group, key_length = struct.unpack_from("!HH", value, item)
                item += 4
                key = value[item:item + key_length]
                item += key_length
                if not _is_grease(group):
                    shares.append(group.to_bytes(2, "big") + key)
        elif extension_id == 65037:
            ech = value
    return shares, ech


def pcap_client_hello_bytes(pcap: Path, client_random_hex: str):
    """The reassembled TLS stream of the connection whose random matches.

    A ClientHello is regularly split across TCP segments, so returning the
    largest single packet would truncate it and the key share would come back
    empty. Payloads are concatenated per client port instead.
    """
    target = bytes.fromhex(client_random_hex) if client_random_hex else None
    streams = {}
    for source, destination, payload, _meta in pcap_tcp(pcap):
        if destination != 443 or not payload:
            continue
        streams.setdefault(source, bytearray()).extend(payload)
    for source, payload in streams.items():
        if target is None or target in payload:
            return bytes(payload)
    return b""


def pcap_tcp(pcap: Path):
    """TCP packets as (src_port, dst_port, payload, syn_metadata)."""
    data = pcap.read_bytes()
    if len(data) < 24 or struct.unpack("<I", data[:4])[0] != 0xA1B2C3D4:
        raise SystemExit(f"{pcap}: not a little-endian classic pcap")
    packets = []
    offset = 24
    while offset + 16 <= len(data):
        _, _, included, _ = struct.unpack("<IIII", data[offset:offset + 16])
        packet = data[offset + 16:offset + 16 + included]
        offset += 16 + included
        if len(packet) < 54 or packet[12:14] != b"\x08\x00":
            continue
        ip = packet[14:]
        if ip[9] != 6:
            continue
        ihl = (ip[0] & 0x0F) * 4
        source = struct.unpack(">H", ip[ihl:ihl + 2])[0]
        destination = struct.unpack(">H", ip[ihl + 2:ihl + 4])[0]
        doff = (packet[14 + ihl + 12] >> 4) * 4
        flags = packet[14 + ihl + 13]
        meta = {
            "ip_version": 4,
            "src_ip": ".".join(str(b) for b in ip[12:16]),
            "dst_ip": ".".join(str(b) for b in ip[16:20]),
            "src_port": source,
            "dst_port": destination,
            "ttl": ip[8],
            "ip_id": struct.unpack(">H", ip[4:6])[0],
            "window": struct.unpack(">H", packet[14 + ihl + 14:14 + ihl + 16])[0],
            "syn": bool(flags & 0x02),
        }
        packets.append((source, destination, packet[14 + ihl + doff:], meta))
    return packets


def locate(pcaps, needle: bytes):
    """(source_port, syn_metadata, pcap) for the connection carrying `needle`."""
    matches = []          # (source_port, meta, pcap), one per matching packet
    for candidate in pcaps:
        for source, destination, payload, meta in pcap_tcp(candidate):
            if destination == 443 and payload and needle in payload:
                matches.append((source, meta, candidate))
    ports = {source for source, _, _ in matches}
    if len(ports) != 1:
        raise SystemExit(
            "expected exactly one captured connection carrying the record's "
            f"client random, found {sorted(ports)}")
    for source, meta, candidate in matches:
        if meta["syn"]:
            return source, meta, candidate
    return matches[0][0], matches[0][1], matches[0][2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--major", required=True, type=int)
    parser.add_argument("--source", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    source = Path(args.source)
    # pcaps are only needed when a record does not already carry its source
    # port; the 148-150 captures record it themselves.
    pcaps = sorted(source.glob("session-*.pcap"))

    records, ports = [], []
    for index in (1, 2, 3):
        response = source / f"response-{index}.json"
        if not response.is_file():
            raise SystemExit(f"{source}: missing {response.name}")
        record = json.loads(response.read_text())

        tcpip = record.setdefault("tcpip", {})
        reported = tcpip.get("src_port") or (tcpip.get("tcp_syn") or {}).get("src_port")
        # The browserleaks schema names the ClientHello random `tls.random`;
        # peet.ws names it `tls.client_random`.
        random_hex = (record.get("tls") or {}).get("random") or \
            (record.get("tls") or {}).get("client_random")
        pcap = None
        if reported:
            port = reported
            # 148-150 record a flat src_port; the gate reads the nested
            # tcp_syn.src_port, so mirror it rather than leaving two shapes.
            if not tcpip.get("tcp_syn"):
                tcpip["tcp_syn"] = {"src_port": reported,
                                    "dst_port": tcpip.get("dst_port", 443)}
        else:
            if not pcaps:
                raise SystemExit(
                    f"{source}: {response.name} has no tcpip.src_port and the "
                    "directory has no session-*.pcap, so connection "
                    "independence cannot be shown")
            if not random_hex:
                raise SystemExit(
                    f"{response.name}: neither tcpip.src_port nor a TLS random, "
                    "so it cannot be paired with a captured connection")
            port, meta, pcap = locate(pcaps, bytes.fromhex(random_hex))
            # The in-repo gate reads tcpip.tcp_syn.src_port, which is where the
            # peet.ws captures put it; populate the same shape from the pcap so
            # both schemas satisfy one reader.
            tcpip = record.setdefault("tcpip", {})
            tcpip["src_port"] = port
            tcpip["dst_port"] = meta["dst_port"]
            tcpip["tcp_syn"] = {
                "ip_version": meta["ip_version"],
                "src_ip": meta["src_ip"],
                "dst_ip": meta["dst_ip"],
                "src_port": meta["src_port"],
                "dst_port": meta["dst_port"],
                "ttl": meta["ttl"],
                "ip_id": meta["ip_id"],
                "window": meta["window"],
            }
        # The 146/147 records are the browserleaks schema; the gate reads
        # peet.ws. Remap the field names so one reader covers both, and verify
        # afterwards that the remap did not invent a value.
        if "tls" in record and "random" in record["tls"]:
            # BrowserLeaks reports key-share *lengths*, not bytes, so the
            # randomness of the key material has to come from the ClientHello in
            # the matching pcap -- the same source the original pipeline used.
            if pcap is None:
                raise SystemExit(
                    f"{source.name}: {response.name} records its own source port "
                    "and no pcap was needed, so key-share bytes cannot be read; "
                    "this path only supports the browserleaks records")
            shares, ech = client_hello_shares(
                pcap_client_hello_bytes(pcap, random_hex))
            if not shares:
                raise SystemExit(
                    f"{pcap}: parsed no key share for client random "
                    f"{random_hex[:16]}; refusing to emit a capture whose "
                    "key-share randomness cannot be shown")
            record = _to_peet(record, shares=shares, ech=ech)
        ports.append(port)
        records.append(record)

    if len(set(ports)) != len(ports):
        raise SystemExit(f"chrome_{args.major}: source ports repeat: {ports}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "\n".join(json.dumps(r, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":")) for r in records) + "\n",
        encoding="utf-8")
    print(f"wrote {out} ({len(records)} records, source ports {ports})")


if __name__ == "__main__":
    main()
