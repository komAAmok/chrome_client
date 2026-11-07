#!/usr/bin/env python3
"""Align our Core's live fingerprint against the imported per-release captures.

This replaces reading a pre-recorded `docs/fingerprint-logs/*/alignment.json`.
Those files recorded the Core's output from a run on one machine at one time;
they could not be re-derived, and because the alignment tool only recorded the
fields it knew about, `supported_versions` was never in them -- which is why
three separate conclusions in the docs had to be argued indirectly.

Here the reference side comes from `profiles/chrome_<major>/<proto>/captures.json`
(tools/import-reference-captures.py) and the "our side" comes from actually
running the built Core against the fingerprint endpoint. Nothing is trusted from
a previous run.

    tools/align-live-fingerprint.py --profile chrome_86 --protocol h2
    tools/align-live-fingerprint.py --profile chrome_86 --protocol h2 --offline

`--offline` compares the reference against itself, which proves the comparator
and the reference reader agree before any network is involved. It is what CI
can run.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import profile_captures as pc

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROBE = ROOT / "target" / "release" / "examples" / "fingerprint_probe"
# Each protocol was captured against a different endpoint, and the endpoint is
# part of what the ClientHello contains: an h3 request has to go to the QUIC
# fingerprint service, and pointing it at the TLS endpoint fails with
# ERR_QUIC_PROTOCOL_ERROR (-356) before any fingerprint is produced.
DEFAULT_URL = {
    "h2": "https://tls.peet.ws/api/all",
    "h1": "https://tls.peet.ws/api/all",
    "h3": "https://quic.tools.scrapfly.io/api/fp/quic",
}

# GREASE tokens and bare hex GREASE ids, which are per-connection by design.
GREASE_HEX = {format(v, "x") for v in range(0x0A0A, 0xFAFA + 1, 0x1010)}


def is_grease(value):
    text = str(value)
    if "GREASE" in text:
        return True
    return text.startswith("0x") and text[2:].lower() in GREASE_HEX


def degrease(values):
    return [v for v in values if not is_grease(v)]


def extension_values(record, wanted_id, key):
    item = pc.extension(record, wanted_id)
    return list(item.get(key) or []) if item else []


def fingerprint(record):
    """The stable, comparable fields of one ClientHello."""
    tls = record.get("tls") or {}
    http2 = record.get("http2") or {}
    frames = http2.get("sent_frames") or []
    settings = None
    for frame in frames:
        if frame.get("frame_type") == "SETTINGS":
            settings = frame.get("settings")
            break
    return {
        "ciphers": degrease(tls.get("ciphers") or []),
        "extension_set": sorted(
            name.split(" (")[0] for name in pc.extension_names(record)
            if not is_grease(name)),
        "supported_groups": degrease(extension_values(record, 10, "supported_groups")),
        "signature_algorithms": degrease(
            extension_values(record, 13, "signature_algorithms")),
        "supported_versions": degrease(pc.supported_versions(record) or []),
        "alpn": sorted(sum([p.get("protocols") or []
                            for p in [pc.extension(record, 16)] if p], [])),
        "padding_data_length": pc.padding_length(record),
        "http2_settings": settings,
        "user_agent": pc.user_agent_chrome_version(record),
    }


STABLE_FIELDS = tuple(fingerprint({}).keys())


# One field legitimately varies across connections for one era, and comparing it
# with a single expected value would call correct data broken:
#
#   119..123  the padding extension appears only when the per-connection ECH
#             GREASE payload happens to leave the ClientHello inside the
#             256..511 window. 28 bytes on h2, 52 on h1, and absent otherwise.
#             From 124 the key share alone pushes the ClientHello past 511, so
#             it can never appear again; before 119 it is always present.
#
# The rule is a set of tolerated values per (major, protocol), not a widening
# of the comparison: anything outside the set still fails.
CONDITIONAL_PADDING_ERA = range(119, 124)
CONDITIONAL_PADDING_WIDTH = {"h2": 28, "h1": 52}


def tolerated_values(major, protocol, field, reference_records):
    """Values this field may take, or None when it must be a single value.

    In the conditional era the padding EXTENSION itself comes and goes, so two
    fields move together: padding_data_length (28/52 vs absent) and
    extension_set (which gains or loses the "padding" entry). Handling only
    the first made the second report a mismatch against itself, which is how
    this was found: an offline run compares the reference against the
    reference, so any FAIL there is the comparator's fault, not the data's.
    """
    if protocol not in CONDITIONAL_PADDING_WIDTH:
        # h3 is a QUIC record: no ClientHello padding extension and no HTTP/2
        # SETTINGS, so the conditional era does not describe it.
        return None
    if major not in CONDITIONAL_PADDING_ERA:
        return None
    if field == "padding_data_length":
        return {None, CONDITIONAL_PADDING_WIDTH[protocol]}
    if field == "extension_set":
        # Whatever the reference itself produced is the tolerated set -- but
        # only for this one field in this one era, and only because the
        # mechanism that makes it vary is known and stated above.
        return {json.dumps(fingerprint(r)[field], sort_keys=True)
                for r in reference_records}
    return None


def compare(reference_records, actual_records, major, protocol):
    """Field-by-field verdicts for one protocol and release."""
    results = []
    for field in STABLE_FIELDS:
        expected = {json.dumps(fingerprint(r)[field], sort_keys=True)
                    for r in reference_records}
        actual = {json.dumps(fingerprint(r)[field], sort_keys=True)
                  for r in actual_records}
        allowed = tolerated_values(major, protocol, field,
                                   reference_records)
        if allowed is not None:
            # Everything here is compared as serialised JSON: padding_data_length
            # is an int-or-null, extension_set is a list, and a set of lists is
            # not hashable.
            allowed_text = {
                v if isinstance(v, str) else json.dumps(v, sort_keys=True)
                for v in allowed}
            outside = {v for v in actual if v not in allowed_text}
            results.append({
                "field": field,
                "status": "PASS" if not outside else "FAIL",
                "expected": sorted(allowed_text),
                "actual": sorted(actual),
                "reason": "this era pads conditionally: the extension is "
                          "present only when the ECH GREASE payload leaves "
                          "the ClientHello inside the 256..511 window",
            })
            continue
        if len(expected) != 1:
            results.append({
                "field": field,
                "status": "REFERENCE_UNSTABLE",
                "expected": sorted(expected),
                "actual": sorted(actual),
                "reason": "the reference captures disagree with each other on a "
                          "field that is supposed to be stable; the reference "
                          "cannot be used to judge our output until that is "
                          "explained",
            })
            continue
        # Compare the serialised forms: several of these fields are lists, and a
        # set of lists is not hashable.
        want = next(iter(expected))
        ok = actual == {want}
        results.append({
            "field": field,
            "status": "PASS" if ok else "FAIL",
            "expected": json.loads(want),
            "actual": [json.loads(v) for v in sorted(actual)],
        })
    return results


# The capture file name and the protocol pin are not the same thing.
#
# The h2 reference was captured from a browser that was free to negotiate, and
# its ClientHello offers ALPN [h2, http/1.1] -- so the pin that reproduces it is
# NATIVE, not ForceH2. Forcing h2 narrows ALPN to [h2] and, because the
# ClientHello then loses the http/1.1 token, moves the padding total as well
# (418 -> 436 on chrome_86). Comparing a forced-h2 run against the h2 captures
# therefore reports two failures that are the comparison's fault, not the
# build's.
#
# h1 and h3 ARE forced captures: their files were collected with the protocol
# pinned, which is the only way to get an h1-only or h3-only ClientHello.
PROBE_PROTOCOL = {"h2": "native", "h1": "h1", "h3": "h3"}


def probe_refusal(profile, protocol, url):
    """Ask the built Core to start this protocol; report a refusal verbatim.

    For chrome_86..chrome_92 there are no h3 captures, because those releases
    could not complete a QUIC handshake. The contract is that the Core refuses
    a forced-H3 engine on those profiles rather than sending a QUIC ClientHello
    the release never sent -- so the refusal is the thing to verify, not a gap
    to skip.
    """
    if not PROBE.is_file():
        raise SystemExit(
            f"{PROBE} is missing; build it with "
            "cargo build --release -p minicronet --example fingerprint_probe")
    env = dict(os.environ)
    env["MINICRONET_PROTOCOL"] = PROBE_PROTOCOL[protocol]
    completed = subprocess.run(
        [str(PROBE), profile, url],
        capture_output=True, text=True, env=env, timeout=120)
    if completed.returncode == 0:
        return None
    text = (completed.stderr or completed.stdout).strip()
    if "ProfileUnsupported" in text:
        return {"result": "MN_ERROR_PROFILE_UNSUPPORTED", "error": text[-400:]}
    return {"result": f"exit {completed.returncode}", "error": text[-400:]}


def run_probe(profile, protocol, url, repeat):
    """Six independent requests through the built Core, one process each."""
    if not PROBE.is_file():
        raise SystemExit(
            f"{PROBE} is missing; build it with "
            "cargo build --release -p minicronet --example fingerprint_probe")
    env = dict(os.environ)
    env["MINICRONET_PROTOCOL"] = PROBE_PROTOCOL[protocol]
    records = []
    for index in range(1, repeat + 1):
        completed = subprocess.run(
            [str(PROBE), profile, url],
            capture_output=True, text=True, env=env, timeout=120)
        if completed.returncode != 0:
            raise SystemExit(
                f"probe failed on connection {index} "
                f"(exit {completed.returncode}): "
                f"{(completed.stderr or completed.stdout).strip()[:400]}")
        body = completed.stdout
        start = body.find("{")
        if start < 0:
            raise SystemExit(f"probe produced no JSON on connection {index}")
        records.append(json.loads(body[start:]))
    return records


def profile_dir_unsupported(major, protocol):
    path = pc.profile_dir(major) / protocol / "unsupported.json"
    if not path.is_file():
        return None
    document = json.loads(path.read_text(encoding="utf-8"))
    return document.get("reason")


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True,
                        help="canonical profile id, e.g. chrome_86")
    parser.add_argument("--protocol", default="h2", choices=("h1", "h2", "h3"))
    parser.add_argument("--url", default=None,
                        help="defaults to the endpoint the reference "
                             "captures for this protocol were taken against")
    parser.add_argument("--repeat", type=int, default=6)
    parser.add_argument("--offline", action="store_true",
                        help="compare the reference against itself; no network")
    parser.add_argument("--out", type=pathlib.Path,
                        help="write the verdict document here")
    args = parser.parse_args(argv)

    _, _, suffix = args.profile.partition("_")
    if not suffix.isdigit():
        raise SystemExit(f"{args.profile}: expected chrome_<major>")
    major = int(suffix)

    if args.url is None:
        args.url = DEFAULT_URL[args.protocol]

    reference = pc.load_protocol(major, args.protocol)
    if reference is None:
        # No captures for this release and protocol. That is only acceptable if
        # the Core refuses the combination -- otherwise we would be shipping a
        # profile that claims a protocol it cannot reproduce.
        unsupported = profile_dir_unsupported(major, args.protocol)
        if args.offline:
            print(f"chrome_{major} has no {args.protocol} captures "
                  f"({unsupported or 'no unsupported.json either'})",
                  file=sys.stderr)
            print("--offline: nothing to compare against", file=sys.stderr)
            return 2
        refusal = probe_refusal(args.profile, args.protocol, args.url)
        if refusal and refusal["result"] == "MN_ERROR_PROFILE_UNSUPPORTED":
            print(f"chrome_{major} {args.protocol}: no captures, and the Core "
                  f"refuses this profile with "
                  f"MN_ERROR_PROFILE_UNSUPPORTED -- which is the contract.")
            print(f"  recorded reason: {unsupported}")
            return 0
        print(f"chrome_{major} {args.protocol}: no captures and the Core did "
              f"NOT refuse it ({refusal})", file=sys.stderr)
        return 1

    if args.offline:
        actual = reference
        mode = "offline: reference compared against itself"
    else:
        actual = run_probe(args.profile, args.protocol, args.url, args.repeat)
        mode = (f"live: {len(actual)} connections through the built "
                f"Core, MINICRONET_PROTOCOL={PROBE_PROTOCOL[args.protocol]}")

    results = compare(reference, actual, major, args.protocol)
    failures = [r for r in results if r["status"] != "PASS"]
    document = {
        "schema": "chrome_client.fingerprint_alignment.v2",
        "profile_id": args.profile,
        "protocol": args.protocol,
        "mode": mode,
        "reference": f"profiles/chrome_{major}/{args.protocol}/captures.json",
        "connections_reference": len(reference),
        "connections_actual": len(actual),
        "results": results,
        "summary": {
            "required_checks": len(results),
            "required_failures": len(failures),
            "result": "PASS" if not failures else "FAIL",
        },
    }

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(document, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8")
        print(f"wrote {args.out}")

    print(f"{args.profile} {args.protocol}: {mode}")
    for result in results:
        marker = "ok  " if result["status"] == "PASS" else "FAIL"
        print(f"  {marker} {result['field']}")
        if result["status"] != "PASS":
            print(f"        expected {json.dumps(result['expected'])[:160]}")
            print(f"        actual   {json.dumps(result['actual'])[:160]}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
