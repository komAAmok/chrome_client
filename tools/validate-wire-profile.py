#!/usr/bin/env python3
"""Compare BrowserLeaks TLS responses and ClientHello captures with a profile."""

import argparse
import hashlib
import json
import re
import struct
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def extension(tls, prefix):
    return next((item for item in tls.get("extensions", [])
                 if item.get("name", "").startswith(prefix)), {})


def is_grease(value):
    if "GREASE" in str(value).upper():
        return True
    match = re.fullmatch(r"0x([0-9a-fA-F]{4})", str(value))
    if not match:
        return False
    number = int(match.group(1), 16)
    return number >> 8 == number & 0xff and number & 0x0f == 0x0a


def grease_token(value):
    """One spelling for a GREASE value, whichever schema reported it.

    The captures spell the same number two ways -- tls.peet.ws sends the
    extension display name (``"TLS_GREASE (0x4a4a)"``) while the pcap parser
    produces the bare ``"0x4a4a"``. Comparing those two directly made the
    response/capture cross-check fail on every peet.ws capture even though the
    values agreed, so both sides are canonicalised to the bare form.
    """
    match = re.search(r"0x([0-9a-fA-F]{4})", str(value))
    if match:
        return "0x" + match.group(1).lower()
    if str(value).upper().startswith("GREASE"):
        return "GREASE"
    return str(value)


def without_grease(values):
    return [value for value in values if not is_grease(value)]


def extension_name(value):
    return re.sub(r" \(\d+\)$", "", value)


def digest(values):
    return hashlib.sha256("|".join(values).encode()).hexdigest()


EXTENSION_NAMES = {
    0: "server_name",
    5: "status_request",
    10: "supported_groups",
    11: "ec_point_formats",
    13: "signature_algorithms",
    16: "application_layer_protocol_negotiation",
    18: "signed_certificate_timestamp",
    21: "padding",
    23: "extended_master_secret",
    27: "compress_certificate",
    35: "session_ticket",
    43: "supported_versions",
    45: "psk_key_exchange_modes",
    51: "key_share",
    17513: "application_settings_old",
    17613: "application_settings",
    65037: "extensionEncryptedClientHello (boringssl)",
    65281: "extensionRenegotiationInfo (boringssl)",
}

GROUP_NAMES = {
    29: "X25519",
    23: "P-256",
    24: "P-384",
    4588: "X25519MLKEM768",
    25497: "X25519Kyber768Draft00",
}


def canonical_extension(extension_id, name=""):
    if is_grease(f"0x{extension_id:04x}"):
        return "TLS_GREASE"
    return EXTENSION_NAMES.get(extension_id, name)


def canonical_group(item):
    group_id = item.get("id")
    if is_grease(f"0x{group_id:04x}"):
        return f"TLS_GREASE (0x{group_id:04x})"
    name = GROUP_NAMES.get(group_id, item.get("name", ""))
    return f"{name} ({group_id})"


def header_pair(value):
    separator = value.find(": ", 1 if value.startswith(":") else 0)
    if separator < 0:
        return {"name": value, "value": ""}
    return {"name": value[:separator], "value": value[separator + 2:]}


def browserleaks_values(response):
    tls = response["tls"]
    extensions = tls.get("extensions", [])
    by_id = {item["id"]: item for item in extensions}
    names = [canonical_extension(item["id"], item.get("name", ""))
             for item in extensions]
    groups = by_id.get(10, {}).get("data", {}).get("named_group_list", [])
    shares = by_id.get(51, {}).get("data", {}).get("client_shares", [])
    sigalgs = by_id.get(13, {}).get("data", {}).get(
        "supported_signature_algorithms", [])
    alpn = by_id.get(16, {}).get("data", {}).get("protocol_name_list", [])
    compression = by_id.get(27, {}).get("data", {}).get("algorithms", [])
    frames = response.get("http2", [])
    settings_frame = next((item for item in frames
                           if item.get("name") == "SETTINGS"), {})
    window_frame = next((item for item in frames
                         if item.get("name") == "WINDOW_UPDATE"), {})
    headers_frame = next((item for item in frames
                          if item.get("name") == "HEADERS"), {})
    settings = [
        f"{item['name'].removeprefix('SETTINGS_')} = {item['value']}"
        for item in settings_frame.get("settings", [])
    ]
    flags = [
        f"{''.join(part.title() for part in item['name'].split('_'))} "
        f"(0x{item['id']:x})"
        for item in headers_frame.get("flags", [])
    ]
    priority = headers_frame.get("priority")
    if priority:
        priority = {
            # BrowserLeaks reports Chromium's encoded 255 weight as zero.
            "weight": 256 if priority.get("weight") == 0 else priority.get("weight"),
            "depends_on": priority.get("stream_dependency"),
            "exclusive": priority.get("exclusive"),
        }
    grease_values = []
    for item in tls.get("cipher_suites", []) + extensions + groups + sigalgs:
        if item.get("name") == "GREASE" or is_grease(f"0x{item['id']:04x}"):
            grease_values.append(f"0x{item['id']:04x}")
    return {
        "http_version": "h2" if frames else "http/1.1",
        # BrowserLeaks calls the ClientHello legacy version handshake_version.
        "tls_version_record": str(tls.get("handshake_version", {}).get("id")),
        "tls_version_negotiated": str(tls.get("selected_version", {}).get("id")),
        "cipher_suites": [item["name"] for item in tls.get("cipher_suites", [])
                          if item.get("name") != "GREASE"],
        "extension_set": sorted(set(without_grease(names))),
        "supported_groups": [canonical_group(item) for item in groups
                             if item.get("name") != "GREASE"],
        "key_share_groups": [canonical_group(item["group"]) for item in shares
                             if item.get("group", {}).get("name") != "GREASE"],
        "signature_algorithms": [
            f"0x{item['id']:x}" if item["id"] in (2308, 2309, 2310)
            else item["name"]
            for item in sigalgs if item.get("name") != "GREASE"],
        "alpn": alpn,
        "certificate_compression": [
            f"{item['name']} ({item['id']})" for item in compression],
        "alps_codepoint": next((item["id"] for item in extensions
                                if item["id"] in (17513, 17613)), None),
        "ech_present": 65037 in by_id,
        "padding_present": 21 in by_id,
        "padding_length_observed": by_id.get(21, {}).get("data", {}).get(
            "padding_data_length"),
        "http2_settings": settings,
        "http2_connection_window_increment": window_frame.get(
            "window_size_increment"),
        "http2_headers_flags": flags,
        "http2_priority": priority,
        "request_headers": [
            {"name": name, "value": value}
            for name, value in headers_frame.get("headers", {}).items()],
        "grease_present": bool(grease_values),
        "client_random": tls.get("random"),
        "session_id": tls.get("session_id"),
        "extension_order": tuple(without_grease(names)),
        "grease_values": tuple(grease_values),
        # BrowserLeaks intentionally omits raw KeyShare and ECH bytes. They
        # are filled from the matching packet capture below.
        "key_share_digest": None,
        "ech_digest": None,
    }


def actual_values(response):
    if isinstance(response.get("http2"), list):
        return browserleaks_values(response)
    tls = response.get("tls", {})
    http2 = response.get("http2", {})
    extensions = tls.get("extensions", [])
    names = [extension_name(item.get("name", "")) for item in extensions]
    groups = extension(tls, "supported_groups").get("supported_groups", [])
    key_shares = extension(tls, "key_share").get("shared_keys", [])
    key_share_groups = without_grease(
        [name for item in key_shares for name in item])
    key_share_values = [value for item in key_shares
                        for name, value in item.items()
                        if not is_grease(name)]
    sigalgs = extension(tls, "signature_algorithms").get(
        "signature_algorithms", [])
    alpn = extension(tls, "application_layer_protocol_negotiation").get(
        "protocols", [])
    compression = extension(tls, "compress_certificate").get("algorithms", [])
    alps = next((item for item in extensions
                 if item.get("name", "").startswith("application_settings")), {})
    alps_match = re.search(r"\((\d+)\)$", alps.get("name", ""))
    padding = extension(tls, "padding")
    ech = next((item for item in extensions
                if item.get("name", "").startswith(
                    "extensionEncryptedClientHello")), {})
    grease_values = [grease_token(value)
                     for value in tls.get("ciphers", []) + names +
                     groups + sigalgs if is_grease(value)]
    frames = http2.get("sent_frames", [])
    settings = next((frame.get("settings", []) for frame in frames
                     if frame.get("frame_type") == "SETTINGS"), [])
    window = next((frame.get("increment") for frame in frames
                   if frame.get("frame_type") == "WINDOW_UPDATE"), None)
    headers = next((frame for frame in frames
                    if frame.get("frame_type") == "HEADERS"), {})
    return {
        "http_version": response.get("http_version"),
        "tls_version_record": tls.get("tls_version_record"),
        "tls_version_negotiated": tls.get("tls_version_negotiated"),
        "cipher_suites": without_grease(tls.get("ciphers", [])),
        "extension_set": sorted(set(without_grease(names))),
        "supported_groups": without_grease(groups),
        "key_share_groups": key_share_groups,
        "signature_algorithms": without_grease(sigalgs),
        "alpn": alpn,
        "certificate_compression": compression,
        "alps_codepoint": int(alps_match.group(1)) if alps_match else None,
        "ech_present": any(name.startswith("extensionEncryptedClientHello")
                           for name in names),
        "padding_present": bool(padding),
        "padding_length_observed": padding.get("padding_length"),
        "http2_settings": settings,
        "http2_connection_window_increment": window,
        "http2_headers_flags": headers.get("flags", []),
        "http2_priority": headers.get("priority"),
        "request_headers": [header_pair(value)
                            for value in headers.get("headers", [])],
        "grease_present": any(is_grease(value) for value in (
            tls.get("ciphers", []) + names + groups + sigalgs)),
        "client_random": tls.get("client_random"),
        "session_id": tls.get("session_id"),
        "extension_order": tuple(without_grease(names)),
        "grease_values": tuple(grease_values),
        "key_share_digest": digest(key_share_values),
        "ech_digest": digest([ech.get("data", "")]),
    }


def pcap_client_hello(path, expected_random=None):
    data = path.read_bytes()
    if len(data) < 24:
        raise ValueError(f"truncated PCAP: {path}")
    magic = data[:4]
    if magic == b"\xd4\xc3\xb2\xa1":
        endian = "<"
    elif magic == b"\xa1\xb2\xc3\xd4":
        endian = ">"
    else:
        raise ValueError(f"classic PCAP required: {path}")
    link_type = struct.unpack_from(endian + "I", data, 20)[0]
    if link_type != 1:
        raise ValueError(f"Ethernet PCAP required, got link type {link_type}")
    streams = {}
    offset = 24
    while offset + 16 <= len(data):
        _, _, captured, _ = struct.unpack_from(endian + "IIII", data, offset)
        packet = data[offset + 16:offset + 16 + captured]
        offset += 16 + captured
        if len(packet) < 14:
            continue
        ethertype = struct.unpack_from("!H", packet, 12)[0]
        ip_offset = 14
        if ethertype == 0x0800 and len(packet) >= ip_offset + 20:
            header_length = (packet[ip_offset] & 15) * 4
            if packet[ip_offset + 9] != 6:
                continue
            source = packet[ip_offset + 12:ip_offset + 16]
            destination = packet[ip_offset + 16:ip_offset + 20]
            tcp_offset = ip_offset + header_length
        elif ethertype == 0x86DD and len(packet) >= ip_offset + 40:
            if packet[ip_offset + 6] != 6:
                continue
            source = packet[ip_offset + 8:ip_offset + 24]
            destination = packet[ip_offset + 24:ip_offset + 40]
            tcp_offset = ip_offset + 40
        else:
            continue
        if len(packet) < tcp_offset + 20:
            continue
        source_port, destination_port = struct.unpack_from("!HH", packet, tcp_offset)
        if destination_port != 443:
            continue
        sequence = struct.unpack_from("!I", packet, tcp_offset + 4)[0]
        tcp_length = (packet[tcp_offset + 12] >> 4) * 4
        payload = packet[tcp_offset + tcp_length:]
        if payload:
            key = (source, source_port, destination, destination_port)
            streams.setdefault(key, {})[sequence] = payload
    for chunks in streams.values():
        assembled = bytearray()
        next_sequence = None
        for sequence, payload in sorted(chunks.items()):
            if next_sequence is None:
                assembled.extend(payload)
                next_sequence = sequence + len(payload)
            elif sequence <= next_sequence:
                overlap = next_sequence - sequence
                if overlap < len(payload):
                    assembled.extend(payload[overlap:])
                    next_sequence += len(payload) - overlap
            else:
                break
        handshake = bytearray()
        cursor = 0
        while cursor + 5 <= len(assembled):
            content_type = assembled[cursor]
            length = struct.unpack_from("!H", assembled, cursor + 3)[0]
            record = assembled[cursor + 5:cursor + 5 + length]
            if len(record) != length:
                break
            if content_type == 22:
                handshake.extend(record)
            cursor += 5 + length
        if len(handshake) >= 4 and handshake[0] == 1:
            length = int.from_bytes(handshake[1:4], "big")
            if len(handshake) >= length + 4:
                candidate = parse_client_hello(bytes(handshake[4:length + 4]))
                if (expected_random is None or
                        candidate["client_random"] == expected_random):
                    return candidate
    raise ValueError(f"outbound TLS ClientHello not found: {path}")


def parse_client_hello(body):
    cursor = 2
    client_random = body[cursor:cursor + 32].hex()
    cursor += 32
    session_length = body[cursor]
    cursor += 1
    session_id = body[cursor:cursor + session_length].hex()
    cursor += session_length
    cipher_length = struct.unpack_from("!H", body, cursor)[0]
    cursor += 2
    ciphers = [struct.unpack_from("!H", body, item)[0]
               for item in range(cursor, cursor + cipher_length, 2)]
    cursor += cipher_length
    compression_length = body[cursor]
    cursor += 1 + compression_length
    extension_length = struct.unpack_from("!H", body, cursor)[0]
    cursor += 2
    end = cursor + extension_length
    extensions = []
    while cursor + 4 <= end:
        extension_id, length = struct.unpack_from("!HH", body, cursor)
        cursor += 4
        value = body[cursor:cursor + length]
        cursor += length
        extensions.append((extension_id, value))
    extension_ids = [item[0] for item in extensions]
    groups = []
    sigalgs = []
    shares = []
    ech = b""
    for extension_id, value in extensions:
        if extension_id == 10 and len(value) >= 2:
            groups = [struct.unpack_from("!H", value, item)[0]
                      for item in range(2, min(len(value), 2 + int.from_bytes(value[:2], "big")), 2)]
        elif extension_id == 13 and len(value) >= 2:
            sigalgs = [struct.unpack_from("!H", value, item)[0]
                       for item in range(2, min(len(value), 2 + int.from_bytes(value[:2], "big")), 2)]
        elif extension_id == 51 and len(value) >= 2:
            item = 2
            while item + 4 <= len(value):
                group, length = struct.unpack_from("!HH", value, item)
                item += 4
                key = value[item:item + length]
                item += length
                if not is_grease(f"0x{group:04x}"):
                    shares.append(group.to_bytes(2, "big") + key)
        elif extension_id == 65037:
            ech = value
    grease = [value for value in ciphers + extension_ids + groups + sigalgs
              if is_grease(f"0x{value:04x}")]
    return {
        "client_hello_length": len(body) + 4,
        "client_random": client_random,
        "session_id": session_id,
        "extension_order": tuple(canonical_extension(value) for value in extension_ids
                                 if not is_grease(f"0x{value:04x}")),
        "grease_values": tuple(f"0x{value:04x}" for value in grease),
        "key_share_digest": hashlib.sha256(b"".join(shares)).hexdigest(),
        "ech_digest": hashlib.sha256(ech).hexdigest(),
    }


def expected_for_url(expected, url):
    expected = dict(expected)
    target = urlsplit(url)
    path = target.path or "/"
    if target.query:
        path += "?" + target.query
    expected["request_headers"] = [
        {**header, "value": target.netloc if header["name"] == ":authority"
         else path if header["name"] == ":path" else header["value"]}
        for header in expected["request_headers"]
    ]
    return expected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("profile_id")
    parser.add_argument("response", type=Path, nargs="+")
    parser.add_argument("--capture", type=Path, action="append", default=[])
    parser.add_argument("--url", default="https://tls.browserleaks.com/")
    parser.add_argument("--expectations", type=Path,
                        default=ROOT / "profiles/wire_expectations.json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    profiles = json.loads(args.expectations.read_text())["profiles"]
    if args.profile_id not in profiles:
        raise SystemExit(f"unknown profile: {args.profile_id}")
    expected = expected_for_url(
        profiles[args.profile_id]["stable_expectations"], args.url)
    stochastic = profiles[args.profile_id]["stochastic_expectations"]
    actuals = [actual_values(json.loads(path.read_text()))
               for path in args.response]
    if args.capture:
        if len(args.capture) != len(actuals):
            raise SystemExit("one --capture is required per response")
        for actual, capture in zip(actuals, args.capture):
            wire = pcap_client_hello(capture, actual["client_random"])
            for field in ("client_random", "session_id", "extension_order",
                          "grease_values"):
                if actual[field] != wire[field]:
                    raise SystemExit(
                        f"response/capture mismatch for {capture}: {field}")
            actual.update(wire)
    elif any(item["key_share_digest"] is None or item["ech_digest"] is None
             for item in actuals):
        raise SystemExit(
            "BrowserLeaks omits raw KeyShare/ECH bytes; matching --capture files are required")
    padding_policy = stochastic.get("padding_policy")
    padding_policy_verified = True
    if padding_policy == "rfc7685_clienthello_length_dependent":
        for item in actuals:
            item["extension_set"] = [
                name for name in item["extension_set"] if name != "padding"]
        padding_policy_verified = all(
            (item["padding_present"] and
             isinstance(item["padding_length_observed"], int) and
             item["padding_length_observed"] >= 1 and
             item["client_hello_length"] == 512) or
            (not item["padding_present"] and
             (item["client_hello_length"] <= 255 or
              item["client_hello_length"] >= 512))
            for item in actuals)
    actual = actuals[0]
    fields = tuple(expected)
    matches = {field: all(item.get(field) == expected[field]
                          for item in actuals) for field in fields}
    random_fields = ["client_random", "session_id", "extension_order",
                     "grease_values", "key_share_digest"]
    if expected["ech_present"]:
        random_fields.append("ech_digest")
    unique_counts = {
        field: len({item[field] for item in actuals})
        for field in random_fields
    }
    randomness_verified = (len(actuals) >= 3 and all(
        count == len(actuals) for count in unique_counts.values()))
    result = {
        "profile_id": args.profile_id,
        "target_url": args.url,
        "stable_match": all(matches.values()),
        "matches": matches,
        "mismatches": {
            field: {"expected": expected[field], "actual": actual.get(field)}
            for field in fields if not matches[field]
        },
        "stochastic": {
            "grease_required": stochastic["grease_required"],
            "grease_present": actual["grease_present"],
            "fixed_seed_allowed": stochastic["fixed_seed_allowed"],
            "unique_counts": unique_counts,
            "multi_connection_randomness_verified": randomness_verified,
            "padding_policy": padding_policy,
            "padding_policy_verified": padding_policy_verified,
        },
    }
    output = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(output)
    print(output, end="")
    raise SystemExit(0 if (result["stable_match"] and randomness_verified and
                           padding_policy_verified) else 1)


if __name__ == "__main__":
    main()
