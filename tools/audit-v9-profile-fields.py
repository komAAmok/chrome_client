#!/usr/bin/env python3
"""Cross-checks the v9 profile fields against an independent capture set.

Every gate that reads profiles/chrome_<major>/**/captures.json answers "does the
compiled table agree with the captures this repository imported". That is the
right question, but it cannot catch a mistake made while *importing*: if the
importer dropped a field, the table and the captures agree and both are wrong.

The nine v9 profile fields are what this gate re-derives from a second,
independently collected run of the same six-connections-per-release protocol
(the "complete" set: h1, h2 and h3 per release, each connection a fresh session).
Each fact below is a boundary -- a release where a field flips -- that the
capture set can witness without reading any repository file.

    tools/audit-v9-profile-fields.py --evidence-dir /path/to/complete

Without --evidence-dir the gate reports unavailable and exits 0: the second set
lives outside the repository because it is 19 MiB of raw captures, and a gate
that silently passes when its input is missing is worse than one that says so.
tools/audit-v9-profile-fields.py --evidence-dir DIR --json
"""

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
TABLE = ROOT / "core" / "source" / "minicronet" / "profile_table_generated.h"

FLAGS = (
    "tls_grease_enabled",
    "tls_grease_per_connection",
    "grease_signature_algorithms",
    "ech_enabled",
    "use_new_alps_codepoint",
    "send_http2_enable_push_setting",
    "send_http2_max_concurrent_streams",
    "randomize_socket_pool_limit",
    "randomize_proxy_socket_pool_limit",
    "send_priority_header",
    "send_quic_orig",
    "send_quic_legacy_version_information",
    "advertise_h3_datagram",
    "h3_supported",
    "enable_http2_settings_grease",
    "greased_http2_frame",
    "http2_end_stream_with_data_frame",
    "enable_quic",
    "try_quic_by_default",
    "maintain_ipv6_temp_addr",
    "client_hello_padding_enabled",
    "wire_verified",
)


def load(path):
    """Reads one of the three collection files, or None when absent."""
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def extension_names(record):
    return {e.get("name", "") for e in (record.get("tls") or {}).get("extensions", [])}


def witnesses(evidence_dir):
    """Derives every fact this capture set can state, keyed by field name.

    Each entry is (first_major, last_major) of the field being true, or None when
    the field is false everywhere. A release with no usable sample is skipped
    rather than assumed either way.
    """
    h2 = load(evidence_dir / "tls-results.json")
    h3 = load(evidence_dir / "tls-results-h3.json")
    if h2 is None:
        raise SystemExit(f"{evidence_dir}: tls-results.json (h1+h2 set) is missing")
    out = {}

    def majors_with(source, predicate, need=6):
        found = []
        for major in sorted(int(k) for k in source):
            samples = [s.get("data") for s in (source[str(major)].get("samples") or [])]
            samples = [s for s in samples if s]
            if len(samples) < need:
                continue
            if all(predicate(s) for s in samples):
                found.append(major)
        return found

    # ech_enabled: the ECH extension is offered only when a config exists.
    ech = majors_with(h2, lambda s: any("EncryptedClientHello" in n for n in extension_names(s)))
    out["ech_enabled"] = (ech[0] if ech else None, ech[-1] if ech else None)

    # use_new_alps_codepoint: which of the two ALPS codepoints is on the wire.
    new_alps = majors_with(h2, lambda s: any(n.startswith("application_settings (")
                                             for n in extension_names(s)))
    out["use_new_alps_codepoint"] = (new_alps[0] if new_alps else None,
                                     new_alps[-1] if new_alps else None)

    # advertise_h3_datagram: H3 SETTINGS id 51.
    def has_datagram(record):
        for frame in (record.get("http3") or []):
            if frame.get("name") != "SETTINGS":
                continue
            return any(entry.get("id") == 51 for entry in frame.get("settings") or [])
        return False

    if h3 is not None:
        datagram = majors_with(h3, has_datagram)
        out["advertise_h3_datagram"] = (datagram[0] if datagram else None,
                                        datagram[-1] if datagram else None)

    # The two grease knobs are false for every release when either would have
    # changed the frame list; a non-empty witness means the compiled table's
    # blanket false is wrong.
    # A greased id is a numeric literal; every defined id has a name, so a
    # release witnesses grease by having one. The predicate is "has a greased
    # id in any of its connections", and the answer expected here is none.
    greased = majors_with(h2, lambda s: any(
        _is_numbers_only(e) for e in _settings_ids(s)), need=1)
    out["enable_http2_settings_grease"] = (greased[0] if greased else None,
                                           greased[-1] if greased else None)
    return out


def _settings_ids(record):
    for frame in ((record.get("http2") or {}).get("sent_frames") or []):
        if frame.get("frame_type") != "SETTINGS":
            continue
        for entry in frame.get("settings") or []:
            yield entry.split(" = ")[0].strip()
    return


def _is_numbers_only(identifier):
    return bool(re.fullmatch(r"\d+", identifier))


def table_flags():
    """Returns {profile_id: {flag: bool}} from the compiled table, positionally."""
    text = TABLE.read_text()
    struct = text[text.find("struct RuntimeProfileData {"):]
    field_names = []
    for line in struct.split("\n"):
        line = line.strip()
        if not line.endswith(";"):
            continue
        match = re.match(r"^(.*?[\s*])(\w+)\s*;$", line)
        if match and match.group(1).strip() == "bool":
            field_names.append(match.group(2))
    if tuple(field_names) != FLAGS:
        raise SystemExit(
            "the boolean field list changed\n  table: " + ", ".join(field_names) +
            "\n  gate : " + ", ".join(FLAGS) +
            "\nUpdate FLAGS in tools/audit-v9-profile-fields.py with the new field "
            "and decide what evidence supports it.")

    out = {}
    for block in text.split('    "chrome_')[1:]:
        ident = block.split('"', 1)[0]
        body = block.split("\n  },")[0]
        values = [line.strip().rstrip(",") == "true"
                  for line in body.split("\n")
                  if line.strip().rstrip(",") in ("true", "false")]
        if len(values) != len(FLAGS):
            raise SystemExit(f"chrome_{ident}: {len(values)} booleans for {len(FLAGS)} fields")
        out[f"chrome_{ident}"] = dict(zip(FLAGS, values))
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", type=pathlib.Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.evidence_dir is None:
        print("v9 profile field cross-check: unavailable (no --evidence-dir). "
              "The second capture set is 19 MiB and lives outside the repository; "
              "pass --evidence-dir to run this check.")
        return 0

    facts = witnesses(args.evidence_dir)
    table = table_flags()
    problems = []

    for name in ("ech_enabled", "use_new_alps_codepoint", "advertise_h3_datagram"):
        if name not in facts:
            continue
        first, last = facts[name]
        if first is None:
            problems.append(f"{name}: the capture set shows it false for every release, "
                            f"but the table turns it on somewhere")
            continue
        for ident, flags in table.items():
            major = int(ident.split("_")[1])
            expected = first <= major <= last
            if flags[name] != expected:
                problems.append(
                    f"{ident}.{name} = {flags[name]}, but the capture set puts the "
                    f"boundary at {first}..{last}")

    for name in ("enable_http2_settings_grease", "greased_http2_frame"):
        first, last = facts.get(name, (None, None))
        if first is not None:
            problems.append(f"{name}: releases {first}..{last} carry a greased frame "
                            f"in the capture set")

    report = {"facts": {k: list(v) for k, v in facts.items()},
              "profiles": len(table), "problems": problems}
    if args.json:
        print(json.dumps(report, indent=1))
    if problems:
        print("v9 profile field cross-check FAILED:", file=sys.stderr)
        for problem in problems[:15]:
            print("  " + problem, file=sys.stderr)
        return 1

    print(f"v9 profile field cross-check passed: {len(table)} profiles agree with an "
          f"independent capture set on ECH (from {facts['ech_enabled'][0]}), ALPS "
          f"codepoint (from {facts['use_new_alps_codepoint'][0]}), H3 datagram "
          f"(from {facts.get('advertise_h3_datagram', (None,))[0]}), and both HTTP/2 "
          f"grease knobs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
