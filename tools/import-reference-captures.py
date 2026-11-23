#!/usr/bin/env python3
"""Import the reference capture set into profiles/, one directory per release.

The reference set is three files, each holding every Chromium major from 86 to
157 with six independent connections:

    tls-results.json      HTTP/2   (Chromium's native negotiation)
    tls-results-h1.json   HTTP/1.1 (forced)
    tls-results-h3.json   HTTP/3   (forced; 86..92 could not complete a QUIC
                                   handshake and record why)

Each entry carries a top-level `data` block, which is byte-identical to the
first sample's `data` -- verified for every release, so nothing is lost by
treating the samples as authoritative -- plus per-sample `checks` metadata
(ja3/ja4/peetprint hashes, session id, client random, header count) that the
aggregate block does not have.

This tool writes:

    profiles/chrome_<major>/meta.json          version, targets, per-protocol status
    profiles/chrome_<major>/<proto>/captures.json   six records, the schema
                                                   tools/verify-wire-capture.py reads
    profiles/chrome_<major>/<proto>/samples.json    the six full samples, with checks
    profiles/chrome_<major>/<proto>/unsupported.json  only for h3 86..92
    profiles/chrome_<major>/README.md          what is here and how to reproduce it

`captures.json` is written as concatenated JSON objects, not an array, because
that is the format the wire gate already parses: one object per connection.

    tools/import-reference-captures.py --reference /path/to/reference-captures
    tools/import-reference-captures.py --reference ... --dry-run
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROFILES = ROOT / "profiles"

# protocol -> (reference file, directory name)
PROTOCOLS = (
    ("h2", "tls-results.json"),
    ("h1", "tls-results-h1.json"),
    ("h3", "tls-results-h3.json"),
)

FIRST_MAJOR = 86
#: Highest major the shipped reference set is known to cover. The set is a
#: snapshot; a new release arrives as its own directory of tls-results*.json
#: files, so the requirement is "every major from FIRST_MAJOR to here must be
#: present", not "the set stops at this number forever". Raise it with the
#: import that adds the release, and let --majors narrow the run.
LAST_MAJOR = 158

# The runtime inputs the table generator reads. They are not captures and this
# tool never touches them; they are listed so the README it writes can say so.
RUNTIME_INPUTS = (
    "normalized_profiles.json",
    "effective_protocol_params.json",
    "network_feature_snapshots.json",
)


def load_reference(reference: pathlib.Path):
    """protocol -> {major: entry}, with the file's own shape validated."""
    loaded = {}
    for protocol, filename in PROTOCOLS:
        path = reference / filename
        if not path.is_file():
            raise SystemExit(f"missing reference capture: {path}")
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise SystemExit(f"{path}: expected an object keyed by major")
        entries = {}
        for key, entry in document.items():
            if not str(key).isdigit():
                continue
            entries[int(key)] = entry
        loaded[protocol] = entries
    return loaded


def check_shape(loaded, majors=None):
    """Every expected major present, and every usable entry holding six samples.

    ``majors`` limits which releases are expected: importing ONE new release
    from its own directory of tls-results files must not demand that folder
    also contain the ninety-odd releases already on disk. The completeness and
    six-sample rules still apply to whatever the run selected.
    """
    problems = []
    expected = list(majors) if majors is not None else list(
        range(FIRST_MAJOR, LAST_MAJOR + 1))
    for protocol, entries in loaded.items():
        majors = sorted(entries)
        missing = [m for m in expected if m not in majors]
        if missing:
            problems.append(f"{protocol}: missing majors {missing}")
        for major, entry in entries.items():
            status = entry.get("status") or {}
            samples = entry.get("samples") or []
            usable = [s for s in samples if s.get("data")]
            if status.get("complete"):
                if len(usable) != 6:
                    problems.append(
                        f"{protocol} chrome_{major}: complete but {len(usable)} "
                        f"usable samples")
            elif protocol != "h3":
                problems.append(
                    f"{protocol} chrome_{major}: incomplete "
                    f"({status.get('collected')}/{status.get('total')})")
    return problems


def usable_samples(entry):
    return [s for s in (entry.get("samples") or []) if s.get("data")]


def write_json(path: pathlib.Path, payload, *, compact=False, dry_run=False):
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if compact:
        text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    else:
        text = json.dumps(payload, ensure_ascii=True, indent=2)
    path.write_text(text + "\n", encoding="utf-8")


def write_captures(path: pathlib.Path, samples, *, dry_run=False):
    """Concatenated JSON objects, one per connection -- the gate's input format.

    A JSON array would parse as a single record and the gate would report one
    connection; the concatenated form is what the endpoint itself returns when
    it is saved to a file, which is why the gate reads it that way.
    """
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(
        json.dumps(sample["data"], ensure_ascii=True, separators=(",", ":"))
        for sample in samples)
    path.write_text(body + "\n", encoding="utf-8")


def sample_record(sample):
    """One sample without its data block, which captures.json already holds."""
    return {
        "index": sample.get("index"),
        "capturedAt": sample.get("capturedAt"),
        "durationMs": sample.get("durationMs"),
        "status": sample.get("status"),
        "checks": sample.get("checks"),
    }


def readme_for(major, entry_versions, protocol_summary):
    lines = [
        f"# chrome_{major}",
        "",
        f"Reference captures for Chromium {entry_versions}. Six independent",
        "connections per protocol, captured against tls.peet.ws /api/all (h1, h2)",
        "and quic.tools.scrapfly.io/api/fp/quic (h3).",
        "",
        "## Layout",
        "",
        "| Path | Contents |",
        "| --- | --- |",
        "| `meta.json` | version, capture target and completeness per protocol |",
        "| `h2/captures.json` | six h2 records, concatenated JSON objects |",
        "| `h2/samples.json` | the same six samples with their `checks` metadata |",
        "| `h1/captures.json`, `h1/samples.json` | the same, forced HTTP/1.1 |",
        "| `h3/captures.json`, `h3/samples.json` | the same, forced HTTP/3 |",
        "",
        "`captures.json` holds the endpoint's own response objects, unmodified.",
        "`samples.json` adds what the response does not carry: ja3/ja4/peetprint",
        "hashes, the session id, the client random and the header count, per",
        "connection.",
        "",
        "## Protocol status",
        "",
        "| Protocol | Connections | Status |",
        "| --- | --- | --- |",
    ]
    for protocol in ("h2", "h1", "h3"):
        info = protocol_summary[protocol]
        if info["captured"]:
            lines.append(
                f"| {protocol} | {info['captured']} | complete |")
        else:
            lines.append(
                f"| {protocol} | 0 | not reproducible -- see "
                f"`{protocol}/unsupported.json` |")
    lines += [
        "",
        "## Reproduce the wire gate",
        "",
        "```sh",
        f"tools/verify-wire-capture.py --profile chrome_{major} \\",
        f"  --captures profiles/chrome_{major}/h2/captures.json \\",
        f"  --out profiles/chrome_{major}/h2/validation.json",
        "```",
        "",
        "## Provenance",
        "",
        "Imported from the reference capture set by",
        "`tools/import-reference-captures.py`. The set is not part of this",
        "repository: it is what a real Chrome build produced on the machine that",
        "ran the capture, and `meta.json` records the executable path and target",
        "each entry was collected against.",
        "",
    ]
    return "\n".join(lines)


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=pathlib.Path, required=True,
                        help="directory holding tls-results*.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-old", action="store_true",
                        help="do not remove the superseded per-version directories")
    parser.add_argument("--majors", default="",
                        help="comma-separated majors to import (default: every "
                             "major the reference set holds). Use this to add "
                             "ONE new release from its own directory without "
                             "re-importing the rest.")
    args = parser.parse_args(argv)

    reference = args.reference.resolve()
    loaded = load_reference(reference)
    if args.majors.strip():
        selected = [int(part) for part in args.majors.split(",") if part.strip()]
        absent = [m for m in selected
                  if any(m not in entries for entries in loaded.values())]
        if absent:
            print(f"reference capture set has no entry for {absent}",
                  file=sys.stderr)
            return 1
        loaded = {protocol: {m: entries[m] for m in selected if m in entries}
                  for protocol, entries in loaded.items()}
    problems = check_shape(loaded, args.majors.strip() and selected or None)
    if problems:
        print("reference capture set is not usable:", file=sys.stderr)
        for problem in problems:
            print("  " + problem, file=sys.stderr)
        return 1

    majors = sorted(loaded["h2"])
    print(f"reference: {reference}")
    for protocol, entries in loaded.items():
        complete = sum(1 for e in entries.values()
                       if (e.get("status") or {}).get("complete"))
        print(f"  {protocol}: {len(entries)} majors, {complete} complete")

    written = []
    for major in majors:
        target_dir = PROFILES / f"chrome_{major}"
        versions = {protocol: loaded[protocol][major].get("version")
                    for protocol in loaded}
        if len(set(versions.values())) != 1:
            print(f"chrome_{major}: protocol entries disagree on version: "
                  f"{versions}", file=sys.stderr)
            return 1
        version = versions["h2"]

        meta = {
            "schema": "chrome_client.profile_captures.v1",
            "profile_id": f"chrome_{major}",
            "major": major,
            "version": version,
            "reference": {
                "protocols": {
                    protocol: {
                        "source_file": dict(
                            (p, f) for p, f in PROTOCOLS)[protocol],
                        "target": loaded[protocol][major].get("target"),
                        "executablePath":
                            loaded[protocol][major].get("executablePath"),
                        "folderVersion":
                            loaded[protocol][major].get("folderVersion"),
                        "status": loaded[protocol][major].get("status"),
                    }
                    for protocol in loaded
                },
            },
            "protocols": {},
        }

        summary = {}
        for protocol in ("h2", "h1", "h3"):
            entry = loaded[protocol][major]
            status = entry.get("status") or {}
            samples = usable_samples(entry)
            proto_dir = target_dir / protocol
            if samples:
                write_captures(proto_dir / "captures.json", samples, dry_run=args.dry_run)
                write_json(proto_dir / "samples.json",
                           {"schema": "chrome_client.profile_samples.v1",
                            "profile_id": f"chrome_{major}",
                            "protocol": protocol,
                            "target": entry.get("target"),
                            "version": version,
                            "samples": [sample_record(s) for s in samples]},
                           dry_run=args.dry_run)
                summary[protocol] = {"captured": len(samples)}
                meta["protocols"][protocol] = {
                    "captured": len(samples),
                    "status": status,
                }
            else:
                reason = entry.get("unsupported") or {}
                write_json(proto_dir / "unsupported.json",
                           {"schema": "chrome_client.profile_unsupported.v1",
                            "profile_id": f"chrome_{major}",
                            "protocol": protocol,
                            "target": entry.get("target"),
                            "version": version,
                            "status": status,
                            "reason": reason.get("reason"),
                            "detectedAt": reason.get("detectedAt"),
                            "quicFailures": entry.get("quicFailures")},
                           dry_run=args.dry_run)
                summary[protocol] = {"captured": 0}
                meta["protocols"][protocol] = {
                    "captured": 0,
                    "status": status,
                    "unsupported": reason.get("reason"),
                }

        write_json(target_dir / "meta.json", meta, dry_run=args.dry_run)
        if not args.dry_run:
            (target_dir / "README.md").write_text(
                readme_for(major, version, summary), encoding="utf-8")
        written.append(major)

    verb = 'would write' if args.dry_run else 'wrote'
    print(f"{verb} {len(written)} profile directories "
          f"(chrome_{written[0]}..chrome_{written[-1]})")

    if args.dry_run:
        print("--dry-run: nothing written, nothing removed")
        return 0

    if not args.keep_old:
        removed = []
        for path in sorted(PROFILES.iterdir()):
            if not path.is_dir():
                continue
            name = path.name
            if not (name.startswith("chrome-") or name.startswith("chrome_")):
                continue
            # The directory just written has an underscore spelling; keep it.
            suffix = name.split("-", 1)[1] if "-" in name else name.split("_", 1)[1]
            if name.startswith("chrome_") and suffix.isdigit() \
                    and int(suffix) in written \
                    and (path / "meta.json").is_file():
                continue
            if args.dry_run:
                removed.append(name)
                continue
            shutil.rmtree(path)
            removed.append(name)
        print(f"removed {len(removed)} superseded directories: "
              f"{', '.join(removed[:6])}"
              f"{' ...' if len(removed) > 6 else ''}")

    kept = [name for name in RUNTIME_INPUTS if (PROFILES / name).is_file()]
    print(f"kept runtime inputs (table generator): {', '.join(kept)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
