#!/usr/bin/env python3
"""Shared readers for the imported per-release captures.

The captures live at `profiles/chrome_<major>/<proto>/captures.json`, written by
`tools/import-reference-captures.py`: concatenated JSON objects, one per
connection, exactly as the fingerprint endpoint returns them.

Three gates used to read `docs/fingerprint-logs/*/alignment.json` -- reports
produced by a separate alignment tool run. That indirection had a real cost: the
alignment file carried only the fields the alignment tool happened to record, so
`supported_versions` was asserted by name (the extension is present) and never
by value, and three separate conclusions in the documentation were reduced to
"inferred from the padding width". Reading the captures directly removes the
intermediate format, so every gate now sees what the wire saw.

    from profile_captures import load_protocol, capture_dirs
"""

from __future__ import annotations

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROFILES = ROOT / "profiles"

PROTOCOLS = ("h1", "h2", "h3")
FIRST_MAJOR = 86
LAST_MAJOR = 157

# Chrome 86..92 could not complete a QUIC handshake against the reference
# endpoint, so their h3 directory holds unsupported.json instead of captures.
H3_UNSUPPORTED_LAST_MAJOR = 92

_WS = " \t\r\n"


def profile_dir(major: int) -> pathlib.Path:
    return PROFILES / f"chrome_{major}"


def captures_path(major: int, protocol: str) -> pathlib.Path:
    return profile_dir(major) / protocol / "captures.json"


def read_records(path: pathlib.Path):
    """Concatenated JSON objects -> list of records.

    Not `json.load`: the file holds one object per connection with no enclosing
    array, which is the shape the endpoint produces when it is saved. A file
    that happens to be an array is rejected rather than guessed at, because
    treating six connections as one record would make every stochastic check
    pass vacuously.
    """
    raw = path.read_text(encoding="utf-8")
    stripped = raw.lstrip()
    if stripped.startswith("["):
        raise SystemExit(
            f"{path}: expected concatenated JSON objects, found an array; "
            "regenerate it with tools/import-reference-captures.py")
    decoder = json.JSONDecoder()
    records, index = [], 0
    while index < len(raw):
        while index < len(raw) and raw[index] in _WS:
            index += 1
        if index >= len(raw):
            break
        record, index = decoder.raw_decode(raw, index)
        records.append(record)
    return records


def load_protocol(major: int, protocol: str):
    """The records for one release and protocol, or None when not captured."""
    path = captures_path(major, protocol)
    if not path.is_file():
        return None
    return read_records(path)


def capture_dirs(major: int):
    """Protocols that actually have captures for this release."""
    return [p for p in PROTOCOLS if captures_path(major, p).is_file()]


def extension(record, wanted_id: int):
    """The extension record with this numeric id, or None.

    The captures name extensions as "key_share (51)" or "Unknown extension
    51764", so the id is parsed out of the name; matching on the name alone
    would miss the releases where the same extension has a different label.
    """
    for item in record.get("tls", {}).get("extensions") or []:
        if extension_id(item.get("name", "")) == wanted_id:
            return item
    return None


def extension_id(name: str):
    import re
    match = re.search(r"\((?:0x)?([0-9a-fA-F]+)\)\s*$", name)
    if match:
        token = match.group(1)
        return int(token, 16) if re.search(r"[a-fA-F]", token) else int(token)
    match = re.search(r"(\d+)\s*$", name)
    return int(match.group(1)) if match else None


def extension_names(record):
    return [item.get("name", "") for item in
            record.get("tls", {}).get("extensions") or []]


def supported_versions(record):
    """The TLS versions advertised in the supported_versions extension.

    This is the value three documents used to call "inferred": the alignment
    reports never recorded it, so 86..97 listing TLS 1.0/1.1 could only be
    argued from the ClientHello being ten bytes longer. It is a plain field on
    the wire record, so it is asserted directly now.
    """
    item = extension(record, 43)
    return list(item.get("versions") or []) if item else None


def padding_length(record):
    """The padding extension's payload length, or None when it is absent."""
    item = extension(record, 21)
    return item.get("padding_data_length") if item else None


def user_agent_chrome_version(record):
    """The Chrome/<version> token, which is the only UA part a profile pins."""
    ua = record.get("user_agent") or ""
    marker = "Chrome/"
    start = ua.find(marker)
    if start < 0:
        return None
    return ua[start + len(marker):].split(" ")[0]


def iter_majors():
    return range(FIRST_MAJOR, LAST_MAJOR + 1)
