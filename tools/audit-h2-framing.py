#!/usr/bin/env python3
"""Proves the HTTP/2 framing knobs in the compiled profile table match the captures.

Three knobs change what an HTTP/2 connection puts on the wire, and all three are
field-trial or command-line controls in Chromium that default to off
(components/network_session_configurator/browser/network_session_configurator.cc:
146-183):

  enable_http2_settings_grease    adds a random SETTINGS id
  greased_http2_frame             sends a frame of reserved type
  http2_end_stream_with_data_frame ends a body-carrying request in DATA

The Core now writes all three from profile data instead of inheriting the C++
default, because "what a browser build ships today" and "what these captures
show" are different claims. This gate keeps the two in step in both directions:

  * every captured connection must still be consistent with all three being off
    (no unknown SETTINGS id, no reserved frame type);
  * every profile in the generated table must carry them as false, unless its
    own captures show otherwise.

Without the second half the table could drift from the evidence silently: the
struct field would compile, the fingerprint would change, and nothing would say
so.

    tools/audit-h2-framing.py
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROFILES = ROOT / "profiles"
TABLE = ROOT / "core" / "source" / "minicronet" / "profile_table_generated.h"

#: SETTINGS identifiers Chrome defines. A greased id is random and lands outside
#: this set by construction (spdy_session.cc:2273-2282).
DEFINED_SETTINGS = {
    "HEADER_TABLE_SIZE",
    "ENABLE_PUSH",
    "MAX_CONCURRENT_STREAMS",
    "INITIAL_WINDOW_SIZE",
    "MAX_HEADER_LIST_SIZE",
    "MAX_FRAME_SIZE",
    "MAX_HEADER_LIST_SIZE",
}

#: Frame types a connection can legitimately carry when the test request has no
#: body and nothing is greased. A reserved type would appear here as an unknown
#: name; spdy_session_pool.cc sends it as an extra frame before HEADERS.
DEFINED_FRAMES = {
    "SETTINGS",
    "WINDOW_UPDATE",
    "HEADERS",
    "CONTINUATION",
    "DATA",
    "PING",
    "RST_STREAM",
    "GOAWAY",
    "PRIORITY",
}


def captured_connections():
    """Yields (profile, record) for every h2 capture in profiles/."""
    for directory in sorted(PROFILES.glob("chrome_*")):
        if not directory.is_dir():
            continue
        capture = directory / "h2" / "captures.json"
        if not capture.is_file():
            continue
        for line in capture.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield directory.name, json.loads(line)
            except json.JSONDecodeError:
                raise SystemExit(f"{capture}: unparsable capture line")


def table_field_names():
    """The RuntimeProfileData field order, read from the generated header.

    Reading the order rather than hard-coding an offset is what keeps this gate
    working when a field is added: the index of each framing flag is looked up by
    name, and a field inserted before it moves both together.
    """
    text = TABLE.read_text()
    start = text.find("struct RuntimeProfileData {")
    if start < 0:
        raise SystemExit("cannot find RuntimeProfileData in the compiled table")
    end = text.find("\n};", start)
    body = text[start:end]
    fields = []
    for line in body.splitlines():
        line = line.strip()
        if not line.endswith(";"):
            continue
        match = re.search(r"^(.*?[\s*])(\w+)\s*;$", line)
        if match:
            fields.append((match.group(2), " ".join(match.group(1).split())))
    return fields


def is_bool_field(type_text):
    """True when the emitted value for this field is a bare true/false.

    Structural, not a name list: a hand-maintained exclusion list is what goes
    stale the moment a field is added, and the failure looks like a shifted
    column rather than a missing entry.
    """
    return type_text == "bool"


FRAMING_FLAGS = (
    "enable_http2_settings_grease",
    "greased_http2_frame",
    "http2_end_stream_with_data_frame",
)


def generated_flag(profile, bool_fields, flag_indices):
    """Returns the three framing flags for one table entry, by position.

    The table is a positional aggregate, so the flags are read from the entry
    own boolean run -- the same way tools/audit-h3-profile-support.py reads the
    HTTP/3 flags, and for the same reason: a comment next to a value is not part
    of the value, so the reader must not depend on one.
    """
    text = TABLE.read_text()
    start = text.find('    "%s",' % profile)
    if start < 0:
        return None
    end = text.find("\n  },", start)
    if end < 0:
        raise SystemExit(f"{profile}: unterminated table entry")
    body = text[start:end]
    tokens = []
    for line in body.splitlines():
        stripped = line.strip().rstrip(",")
        if stripped in ("true", "false"):
            tokens.append(stripped == "true")
    if len(tokens) != len(bool_fields):
        raise SystemExit(
            f"{profile}: {len(tokens)} boolean values for {len(bool_fields)} "
            f"boolean fields")
    return tuple(tokens[index] for index in flag_indices)

def main():
    if not TABLE.is_file():
        raise SystemExit(f"missing {TABLE}; the compiled table is the source of truth")

    fields = table_field_names()
    by_name = dict(fields)
    for flag in FRAMING_FLAGS:
        if flag not in by_name:
            raise SystemExit(
                f"the compiled table no longer carries {flag}; the field it describes "
                f"cannot be audited")
        if not is_bool_field(by_name[flag]):
            raise SystemExit(
                f"{flag} is declared {by_name[flag]!r}, so its value is not a boolean "
                f"token and this audit cannot read it")
    bool_fields = [name for name, type_text in fields if is_bool_field(type_text)]
    flag_indices = [bool_fields.index(flag) for flag in FRAMING_FLAGS]

    problems = []
    seen = set()
    connections = 0
    for profile, record in captured_connections():
        connections += 1
        seen.add(profile)
        frames = record.get("http2", {}).get("sent_frames", [])
        for frame in frames:
            name = frame.get("frame_type", "")
            if name not in DEFINED_FRAMES:
                problems.append(f"{profile}: reserved frame type {name!r} on the wire "
                                f"contradicts greased_http2_frame = false")
        settings = next((f for f in frames if f.get("frame_type") == "SETTINGS"), None)
        for entry in (settings or {}).get("settings", []):
            identifier = entry.split(" = ")[0].strip()
            if identifier not in DEFINED_SETTINGS:
                problems.append(f"{profile}: SETTINGS id {identifier!r} on the wire "
                                f"contradicts enable_http2_settings_grease = false")

    if not connections:
        raise SystemExit("no h2 captures found; the evidence this gate reads is absent")

    for profile in sorted(seen):
        flags = generated_flag(profile, bool_fields, flag_indices)
        if flags is None:
            problems.append(f"{profile}: captured but absent from the compiled table")
            continue
        if any(flags):
            problems.append(
                f"{profile}: table sets {flags} but the captures show no greased "
                f"SETTINGS id and no reserved frame, so all three must be false")

    if problems:
        print("HTTP/2 framing audit FAILED:", file=sys.stderr)
        for problem in problems[:20]:
            print("  " + problem, file=sys.stderr)
        if len(problems) > 20:
            print(f"  ... and {len(problems) - 20} more", file=sys.stderr)
        raise SystemExit(1)

    print(f"HTTP/2 framing audit passed: {len(seen)} profiles, "
          f"{connections} captured connections carry no greased SETTINGS id, "
          f"no reserved frame type, and the compiled table agrees")
    return 0


if __name__ == "__main__":
    sys.exit(main())
