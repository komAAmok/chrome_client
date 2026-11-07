#!/usr/bin/env python3
"""Gate a new Chrome profile on three independent levels of evidence.

Adding a profile is a claim that this build reproduces what that Chrome release
put on the wire. Three things can go wrong, and each needs its own check because
the others cannot see it:

  LEVEL 1  SOURCE       the behaviour must exist in that release's own source.
                        A table derived from a neighbouring release can look
                        right and still describe code that release never had.
                        Resolved from chromium.googlesource.com at that major's
                        own tag, plus the BoringSSL and QUICHE revisions the
                        release's DEPS names. No Chromium checkout required.

  LEVEL 2  WIRE         the captured ClientHello / HTTP/2 / HTTP/3 handshakes
                        must align byte-for-byte on the fields a fingerprinter
                        reads, across six independent connections, and the
                        stochastic fields must differ on every connection.
                        Without this a profile is a guess with a version string.

  LEVEL 3  BINARY       the built artifact must reproduce those captures when
                        it actually connects. A source audit and a capture set
                        can both be right while the build is stale, miscompiled,
                        or serving a different table.

A profile that passes level 2 but not level 3 is a profile nobody has run.
A profile that passes level 3 but not level 1 was fitted to one capture.

    tools/audit-profile-addition.py --profile chrome_158
    tools/audit-profile-addition.py --profile chrome_158 --levels 2,3
    tools/audit-profile-addition.py --profile chrome_158 --levels 1 --json

Level 3 needs a working network path to the fingerprint endpoints and a built
Core; the other two do not. --levels exists so CI can run the parts that are
deterministic, and a developer can run the whole chain before claiming a profile
is supported.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import profile_captures as pc

ROOT = pathlib.Path(__file__).resolve().parents[1]

CHROMIUM = "https://chromium.googlesource.com/chromium/src"
BORINGSSL = "https://boring ssl.googlesource.com/boringssl".replace(" ", "")
QUICHE = "https://quiche.googlesource.com/quiche"

# The source surface a profile's behaviour actually comes from. Level 1 requires
# every one of these to exist at the release's own tag and to contain the signal
# the profile claims; a missing file is a hard failure, not a skip, because
# "this release has no such code" and "the fetch failed" must not look alike.
REQUIRED_SOURCES = {
    "alpn": ("chromium", "net/http/http_network_session.cc"),
    "features": ("chromium", "net/base/features.cc"),
    "http2": ("chromium", "net/spdy/spdy_session.cc"),
    "http3": ("chromium", "net/quic/quic_chromium_client_session.cc"),
    "tls": ("chromium", "net/socket/ssl_client_socket_impl.cc"),
    "websocket": ("chromium", "net/websockets/websocket_basic_handshake_stream.cc"),
    "boringssl_extensions": ("boringssl", "ssl/extensions.cc"),
    "boringssl_handshake_client": ("boringssl", "ssl/handshake_client.cc"),
    "quiche_h3": ("quiche", "quiche/quic/core/quic_config.cc"),
}

# Older releases put the same code somewhere else. This table has exactly one
# definition -- the collector's -- because two copies drifted apart before:
# this gate resolved chrome_157 to ssl/t1_lib.cc (the pre-93 path) while the
# collector resolved it correctly, so one tool certified what the other could
# not find. Importing it means a fix lands in both.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from importlib import import_module as _import_module
_collector = _import_module('collect-profile-evidence')
PATH_ERAS = _collector.PATH_ERAS
REQUIRED_SOURCES = {name: _collector.FILES[name][0:2]
                    for name in REQUIRED_SOURCES}

MIN_CONNECTIONS = 6

# 119..123 pad the ClientHello conditionally; see level_wire().
CONDITIONAL_PADDING_ERA = range(119, 124)


def resolve_path(major, name):
    """The upstream path this release actually has for `name`.

    PATH_ERAS is ordered oldest first, so the newest era at or below this
    major must win -- iterating forwards returns the OLDEST match, which sent
    chrome_157 to ssl/t1_lib.cc (the pre-93 path) and made a correct release
    look like it was missing the code. Iterating in reverse fixes it, and the
    same bug was in tools/collect-profile-evidence.py.
    """
    path = REQUIRED_SOURCES[name][1]
    for first, mapping in reversed(PATH_ERAS):
        if major >= first and name in mapping:
            return mapping[name]
    return path


def fetch(base, ref, path, retries=3):
    """One file from googlesource. Returns bytes, or None when it 404s."""
    url = f"{base}/+/{ref}/{path}?format=TEXT"
    last = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=45) as response:
                import base64
                return base64.b64decode(response.read())
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            last = error
        except Exception as error:  # network flake; retry
            last = error
    raise SystemExit(f"cannot fetch {url} after {retries} attempts: {last}")


def list_tags(major):
    """Every refs/tags/<major>.* on chromium/src, newest first by version tuple."""
    url = f"{CHROMIUM}/+refs/tags/?format=TEXT"
    with urllib.request.urlopen(url, timeout=60) as response:
        text = response.read().decode("utf-8", "replace")
    pattern = re.compile(r"^[0-9a-f]{40}\s+refs/tags/(%d\.\d+\.\d+\.\d+)$" % major,
                         re.MULTILINE)
    tags = pattern.findall(text)
    if not tags:
        return []
    def version(tag):
        return tuple(int(part) for part in tag.split("."))
    return sorted(tags, key=version, reverse=True)


def deps_revision(tag, kind):
    """The BoringSSL / QUICHE revision a release's own DEPS file names."""
    raw = fetch(CHROMIUM, f"refs/tags/{tag}", "DEPS")
    if raw is None:
        raise SystemExit(f"{tag}: DEPS is missing")
    text = raw.decode("utf-8", "replace")
    if kind == "boringssl":
        match = re.search(r"'boringssl_revision':\s*'([0-9a-f]{40})'", text)
        if not match:
            match = re.search(r"boringssl.*?([0-9a-f]{40})", text, re.DOTALL)
    else:
        match = re.search(r"'quiche_revision':\s*'([0-9a-f]{40})'", text)
        if not match:
            match = re.search(r"quiche.*?([0-9a-f]{40})", text, re.DOTALL)
    if not match:
        raise SystemExit(f"{tag}: DEPS names no {kind} revision")
    return match.group(1)


def level_source(major, profile):
    """LEVEL 1: the behaviour exists in this release's own source."""
    failures = []
    facts = {}

    tags = list_tags(major)
    if not tags:
        return {"status": "FAIL", "failures": [f"no refs/tags/{major}.* upstream"],
                "facts": facts}
    latest = tags[0]
    recorded = profile.get("chromium_tag")
    facts["latest_tag"] = latest
    facts["recorded_tag"] = recorded
    if recorded != latest:
        # Not a failure by itself: pinning an older patch release is legitimate.
        # It is a fact the caller has to accept knowingly, so it is reported.
        facts["recorded_tag_is_latest"] = False
        facts["newer_tags_exist"] = tags[:5]
    else:
        facts["recorded_tag_is_latest"] = True

    ref = f"refs/tags/{recorded}"
    boringssl_rev = deps_revision(recorded, "boringssl")
    quiche_rev = deps_revision(recorded, "quiche")
    facts["boringssl_revision_from_deps"] = boringssl_rev
    facts["quiche_revision_from_deps"] = quiche_rev
    if profile.get("boringssl_revision") not in (None, boringssl_rev):
        failures.append(
            f"profile records boringssl_revision "
            f"{profile.get('boringssl_revision')} but {recorded}'s DEPS names "
            f"{boringssl_rev}")
    if profile.get("quiche_revision") not in (None, quiche_rev):
        failures.append(
            f"profile records quiche_revision {profile.get('quiche_revision')} "
            f"but {recorded}'s DEPS names {quiche_rev}")

    recorded_files = (profile.get("evidence") or {}).get("source_files") or {}
    facts["sources"] = {}
    for name in REQUIRED_SOURCES:
        repository, _ = REQUIRED_SOURCES[name]
        path = resolve_path(major, name)
        if repository == "chromium":
            base, target_ref = CHROMIUM, ref
        elif repository == "boringssl":
            base, target_ref = BORINGSSL, boringssl_rev
        else:
            base, target_ref = QUICHE, quiche_rev

        raw = fetch(base, target_ref, path)
        if raw is None:
            failures.append(
                f"{name}: {path} does not exist at {repository} "
                f"{target_ref[:12]}; this release's source does not have the "
                f"code the profile claims")
            continue
        digest = hashlib.sha256(raw).hexdigest()
        recorded_entry = recorded_files.get(name) or {}
        recorded_digest = recorded_entry.get("sha256")
        facts["sources"][name] = {
            "repository": repository,
            "path": path,
            "bytes": len(raw),
            "sha256": digest,
            "recorded_sha256": recorded_digest,
        }
        if recorded_digest is None:
            failures.append(
                f"{name}: profile records no sha256 for this source, so the "
                f"claim cannot be tied to a specific upstream tree")
        elif recorded_digest != digest:
            failures.append(
                f"{name}: {path} is {digest[:16]} upstream but the profile "
                f"records {recorded_digest[:16]}")

    return {
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "facts": facts,
    }


def level_wire(major):
    """LEVEL 2: six captured connections per protocol, aligned and stochastic."""
    failures = []
    facts = {}

    for protocol in ("h1", "h2", "h3"):
        records = pc.load_protocol(major, protocol)
        if records is None:
            unsupported = pc.profile_dir(major) / protocol / "unsupported.json"
            if unsupported.is_file():
                document = json.loads(unsupported.read_text(encoding="utf-8"))
                facts[protocol] = {
                    "captures": 0,
                    "status": "UNSUPPORTED_BY_DESIGN",
                    "reason": document.get("reason"),
                }
                continue
            failures.append(
                f"{protocol}: no captures and no unsupported.json; a missing "
                f"capture is not evidence of anything")
            continue

        if len(records) != MIN_CONNECTIONS:
            failures.append(
                f"{protocol}: {len(records)} connections, {MIN_CONNECTIONS} "
                f"required")
            continue

        # This file must be THIS release's data. A capture set that belongs to a
        # different major still has six connections and still has stable fields,
        # so nothing above would notice -- but every downstream comparison would
        # be against the wrong reference.
        for index, record in enumerate(records, 1):
            version = pc.user_agent_chrome_version(record)
            if version is None:
                failures.append(
                    f"{protocol} connection {index}: no Chrome/<version> token in "
                    f"the user agent, so the capture cannot be tied to a release")
                continue
            found = int(version.split('.')[0])
            if found != major:
                failures.append(
                    f"{protocol} connection {index}: this capture is from Chrome "
                    f"{found} ({version}) but it is filed under chrome_{major}; the "
                    f"file does not belong to this release")
                break

        # Stable fields must agree across all six; stochastic must not.
        stable = {}
        for record in records:
            for name, value in _stable_view(record, protocol).items():
                stable.setdefault(name, set()).add(json.dumps(value, sort_keys=True))
        unstable = [name for name, values in stable.items() if len(values) != 1]
        # One exception, and it is a known mechanism rather than tolerance:
        # 119..123 pad conditionally. The padding extension is present only when
        # the per-connection ECH GREASE payload leaves the ClientHello inside
        # the 256..511 window, so the extension SET genuinely differs between
        # connections of the same release. Both forms are legitimate; a third
        # form is not, and is caught below by comparing against what the
        # captures themselves contain.
        if major in CONDITIONAL_PADDING_ERA and protocol in ('h1', 'h2'):
            if unstable == ['extensions'] and len(stable['extensions']) == 2:
                unstable = []
        if unstable:
            failures.append(
                f"{protocol}: stable fields disagree across the six connections: "
                f"{sorted(unstable)}")

        stochastic = {}
        for record in records:
            for name, value in _stochastic_view(record, protocol).items():
                stochastic.setdefault(name, set()).add(json.dumps(value, sort_keys=True))
        # "Stochastic" does not mean "all six distinct" for every field.
        #   client_random / session_id / dcid / scid  are 128+ bit draws: six
        #     distinct values are the only non-seeded outcome, and a repeat is a
        #     real red flag.
        #   grease_values  has a 16-value domain, so six draws WILL repeat by the
        #     pigeonhole principle. Demanding distinctness there reported correct
        #     captures as seeded. The property worth asserting is that the field
        #     is not constant -- a fixed GREASE would be the seeded case.
        UNIQUE_REQUIRED = {"client_random", "session_id",
                           "destination_connection_id", "source_connection_id"}
        fixed = []
        for name, values in stochastic.items():
            if name in UNIQUE_REQUIRED:
                if len(values) < len(records):
                    fixed.append(name)
            elif len(values) < 2:
                fixed.append(name + " (constant)")
        if fixed:
            failures.append(
                f"{protocol}: stochastic fields do not vary as they must, which "
                f"means a seeded RNG: {sorted(fixed)}")

        facts[protocol] = {
            "captures": len(records),
            "status": "CAPTURED",
            "stable_fields": sorted(stable),
            "stochastic_fields": sorted(stochastic),
        }

    return {
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "facts": facts,
    }


# GREASE entries are per-connection by design: Chrome draws a fresh value for
# each connection and the point of GREASE is that nothing may depend on which.
# Comparing raw lists therefore reports a correct capture as unstable -- and it
# did, for ciphers, supported_versions and the extension list on every release.
# Stable comparison strips them; the presence of GREASE is asserted separately
# in the stochastic view, where it belongs.
GREASE_HEX = {format(v, 'x') for v in range(0x0A0A, 0xFAFA + 1, 0x1010)}


def is_grease(value):
    text = str(value)
    if 'GREASE' in text.upper():
        return True
    return text.startswith('0x') and text[2:].lower() in GREASE_HEX


def degrease(values):
    if values is None:
        return None
    if isinstance(values, str):
        return values if not is_grease(values) else None
    return [v for v in values if not is_grease(v)]


def _stable_view(record, protocol):
    if protocol == "h3":
        tls = record.get("tls") or {}
        quic = record.get("quic") or {}
        return {
            "ciphers": tls.get("cipher_suites"),
            "extensions": sorted(n for n in (e.get("name") for e in (tls.get("extensions") or [])) if not is_grease(n)),
            "selected_version": tls.get("selected_version"),
            "quic_version": quic.get("version"),
            "transport_parameters": quic.get("transport_parameters"),
            "h3_text": record.get("h3_text"),
        }
    tls = record.get("tls") or {}
    return {
        "ciphers": degrease(tls.get("ciphers")),
        "extensions": sorted(n for n in (e.get("name") for e in (tls.get("extensions") or [])) if not is_grease(n)),
        "supported_versions": degrease(pc.supported_versions(record)),
        "http2_settings": _http2_settings(record),
        "user_agent_version": pc.user_agent_chrome_version(record),
    }


def _stochastic_view(record, protocol):
    if protocol == "h3":
        tls = record.get("tls") or {}
        quic = record.get("quic") or {}
        return {
            "client_random": tls.get("random"),
            "destination_connection_id": quic.get("dcid"),
            "source_connection_id": quic.get("scid"),
        }
    tls = record.get("tls") or {}
    return {
        "client_random": tls.get("client_random"),
        "session_id": tls.get("session_id"),
        # GREASE values themselves are the stochastic part: BoringSSL draws a
        # fresh one per connection. The ORDER of the real extensions is not
        # stochastic before 152 -- it only appeared to be, because the GREASE
        # entries at the head of the list carried different ids on each
        # connection. Asserting order here reported nine correct releases as
        # having a seeded RNG. Extension order is asserted as a stable property
        # instead, where it belongs for the releases that do permute it.
        "grease_values": [e.get("name") for e in (tls.get("extensions") or [])
                          if is_grease(e.get("name"))],
    }


def _http2_settings(record):
    for frame in (record.get("http2") or {}).get("sent_frames") or []:
        if frame.get("frame_type") == "SETTINGS":
            return frame.get("settings")
    return None


def level_binary(major, profile, protocol):
    """LEVEL 3: the built artifact reproduces the captures when it connects."""
    import subprocess
    import os

    failures = []
    facts = {}

    probe = ROOT / "target/release/examples/fingerprint_probe"
    if not probe.is_file():
        return {
            "status": "FAIL",
            "failures": [
                f"{probe} is missing; build it with cargo build --release "
                f"-p minicronet --example fingerprint_probe"],
            "facts": facts,
        }

    library = ROOT / "core/binaries/linux-x86_64"
    profile_id = f"chrome_{major}"
    reference = pc.load_protocol(major, protocol)
    if reference is None:
        return {
            "status": "FAIL",
            "failures": [f"{protocol}: no captures to compare against"],
            "facts": facts,
        }

    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = str(library)
    env["MINICRONET_PROTOCOL"] = {"h2": "native", "h1": "h1", "h3": "h3"}[protocol]
    url = {"h1": "https://tls.peet.ws/api/all",
           "h2": "https://tls.peet.ws/api/all",
           "h3": "https://quic.tools.scrapfly.io/api/fp/quic"}[protocol]

    live = []
    for index in range(1, MIN_CONNECTIONS + 1):
        completed = subprocess.run([str(probe), profile_id, url],
                                   capture_output=True, text=True, env=env,
                                   timeout=120)
        if completed.returncode != 0:
            failures.append(
                f"{protocol}: connection {index} failed (exit "
                f"{completed.returncode}): "
                f"{(completed.stderr or completed.stdout).strip()[:200]}")
            break
        body = completed.stdout
        start = body.find("{")
        live.append(json.loads(body[start:]))
    facts["live_connections"] = len(live)

    if len(live) == MIN_CONNECTIONS:
        expected = _stable_view(reference[0], protocol)
        for index, record in enumerate(live, 1):
            actual = _stable_view(record, protocol)
            for name, value in expected.items():
                if json.dumps(actual.get(name), sort_keys=True) != json.dumps(value, sort_keys=True):
                    failures.append(
                        f"{protocol} connection {index}: {name} is "
                        f"{json.dumps(actual.get(name))[:120]} but the captures "
                        f"say {json.dumps(value)[:120]}")

    return {
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "facts": facts,
    }


def check_one(entry, levels, args):
    """Run the requested levels for one profile and return its report."""
    profile_id = entry["profile_id"]
    major = int(profile_id.split("_", 1)[1])
    report = {"profile": profile_id, "major": major, "levels": {}}
    failed = False
    if "1" in levels:
        result = level_source(major, entry)
        report["levels"]["1_source"] = result
        failed = failed or result["status"] != "PASS"
    if "2" in levels:
        result = level_wire(major)
        report["levels"]["2_wire"] = result
        failed = failed or result["status"] != "PASS"
    if "3" in levels:
        result = level_binary(major, entry, args.protocol)
        report["levels"]["3_binary"] = result
        failed = failed or result["status"] != "PASS"
    report["result"] = "FAIL" if failed else "PASS"
    return report


def run_all(profiles, levels, args):
    """Check every profile in the table; report the ones that fail.

    A table entry that fails any level is a profile that was added without the
    evidence AGENTS.md section 9 requires. It is reported, not skipped: the
    whole point of the rule is that such an entry should not exist.
    """
    reports = []
    failed = []
    for entry in profiles:
        report = check_one(entry, levels, args)
        reports.append(report)
        if report["result"] != "PASS":
            failed.append(report)

    if args.json:
        print(json.dumps({"profiles": reports,
                          "result": "FAIL" if failed else "PASS"},
                         ensure_ascii=True, indent=2))
        return 1 if failed else 0

    print(f"checked {len(reports)} profiles at levels {','.join(levels)}")
    print(f"passed {len(reports) - len(failed)}, failed {len(failed)}")
    for report in failed:
        print()
        print(f"{report['profile']}  ({report['result']})")
        for name, result in report["levels"].items():
            if result["status"] == "PASS":
                continue
            print(f"  {name}: {result['status']}")
            for failure in result["failures"][:3]:
                print(f"    - {failure}")
    return 1 if failed else 0


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", help="e.g. chrome_157; omit with --all")
    parser.add_argument("--all", action="store_true",
                        help="check every profile in the table")
    parser.add_argument("--levels", default="1,2,3",
                        help="comma separated subset of 1,2,3")
    parser.add_argument("--protocol", default="h2", choices=("h1", "h2", "h3"),
                        help="which protocol level 3 drives")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if not args.all and not args.profile:
        raise SystemExit("--profile is required unless --all is given")
    levels = [level.strip() for level in args.levels.split(",") if level.strip()]

    normalized = ROOT / "profiles/normalized_profiles.json"
    profiles = json.loads(normalized.read_text())["profiles"]

    if args.all:
        return run_all(profiles, levels, args)

    entry = next((p for p in profiles if p["profile_id"] == args.profile), None)
    if entry is None:
        raise SystemExit(
            f"{args.profile} is not in profiles/normalized_profiles.json; add "
            f"the entry before gating it")
    report = check_one(entry, levels, args)
    failed = report["result"] != "PASS"

    if args.json:
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return 1 if failed else 0

    print(f"{args.profile}  ({report['result']})")
    for name, result in report["levels"].items():
        print(f"  {name}: {result['status']}")
        for failure in result["failures"]:
            print(f"    - {failure}")
        for key, value in (result.get("facts") or {}).items():
            if isinstance(value, dict):
                print(f"    {key}: {json.dumps(value)[:160]}")
            else:
                print(f"    {key}: {value}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
