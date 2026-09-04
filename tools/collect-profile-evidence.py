#!/usr/bin/env python3
"""Collect a profile's `evidence.source_files` from the pinned release tag.

The normalized profile schema requires, for each audited source file, the exact
byte count, SHA-256 and the line numbers of the signals that justify the
profile's parameters. Those must come from the tree the release was built from,
which is not the tree in this repository's `CHROMIUM_REVISION`. Rather than
check out a second 70 GB tree, this fetches the twelve files individually from
googlesource: seven from chromium/src at the release tag, three from BoringSSL
and two from QUICHE at the revisions that tag's DEPS pins.

    tools/collect-profile-evidence.py --version 152.0.7977.83 \
        --out docs/profile-evidence-chrome-152.json

Passing several --version values fetches each of them and reports whether the
audited files are identical across the patch releases, which is what lets a
capture that only reveals the major version be attributed to a branch instead of
one build.
"""

import argparse
import base64
import hashlib
import http.client
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request

CHROMIUM = "https://chromium.googlesource.com/chromium/src"
BORINGSSL = "https://boringssl.googlesource.com/boringssl"
QUICHE = "https://quiche.googlesource.com/quiche"

# name -> (repo key, path). Mirrors the twelve entries the chrome_151 profile
# carries, so a new profile is directly comparable with the audited one.
FILES = {
    "boringssl_extensions": ("boringssl", "ssl/extensions.cc"),
    "boringssl_handshake_client": ("boringssl", "ssl/handshake_client.cc"),
    "boringssl_random": ("boringssl", "crypto/rand/rand.cc"),
    "chromium_base_random": ("chromium", "base/rand_util.cc"),
    "quiche_random": ("quiche", "quiche/common/quiche_random.cc"),
    "alpn": ("chromium", "net/http/http_network_session.cc"),
    "features": ("chromium", "net/base/features.cc"),
    "http2": ("chromium", "net/spdy/spdy_session.cc"),
    "http3": ("chromium", "net/quic/quic_chromium_client_session.cc"),
    "quiche_h3": ("quiche", "quiche/quic/core/quic_config.cc"),
    "tls": ("chromium", "net/socket/ssl_client_socket_impl.cc"),
    "websocket": ("chromium", "net/websockets/websocket_basic_handshake_stream.cc"),
}

# Signal name -> regex, per category. These are the patterns the original
# pipeline used (new/tools/extract-historical-random-strategies.py and
# extract-protocol-strategies.py); they are reproduced verbatim so a profile
# collected here is directly comparable with the 53 audited ones. They are
# applied case-insensitively and only the first 32 line numbers are kept, which
# is also what the original did.
RANDOM_MARKERS = {
    "csprng": r"RAND_bytes|RAND_priv_bytes|GetRandBytes|RandomBytes",
    "grease": r"grease|GREASE",
    "extension_permutation": r"permute_extensions|permut.{0,12}extension|extension_permutation",
    "key_share": r"key.?share|key_shares|supported_groups",
    "quic_random": r"QuicRandom|QuicheRandom|Generate.*Random|RandUint",
}

PROTOCOL_MARKERS = {
    "tls": {
        "alpn": r"ALPN|alpn_protos|SerializeNextProtos",
        "ech": r"ECH|ech_",
        "grease": r"grease|GREASE",
        "groups": r"supported_groups|key_shares|SSL_set1_group",
        "alps": r"ALPS|application_settings",
        "ticket": r"session_ticket|ticket",
        "compression": r"cert.*compress|compression",
    },
    "alpn": {
        "h1": r"HTTP/1|kProtoHTTP11|HTTP11",
        "h2": r"HTTP/2|kProtoHTTP2|HTTP2",
        "h3": r"HTTP/3|kProtoHTTP3|HTTP3",
        "alpn": r"ALPN|GetAlpnProtos|NextProto",
    },
    "http2": {
        "settings": r"SETTINGS|settings",
        "window": r"WINDOW|window|flow control",
        "priority": r"priority|PRIORITY|exclusive",
        "pseudo_headers": r"pseudo|:method|:authority|:scheme|:path",
        "connect": r"CONNECT_PROTOCOL|extended CONNECT|connect",
    },
    "http3": {
        "transport": r"transport|TransportParameter",
        "settings": r"SETTINGS|settings",
        "flow_control": r"flow.control|window",
        "connection_id": r"connection.id|ConnectionId",
        "grease": r"grease|GREASE",
        "websocket": r"websocket|WebSocket|CONNECT",
    },
    "websocket": {
        "upgrade": r"Upgrade|upgrade",
        "origin": r"Origin|origin",
        "sec_headers": r"Sec-WebSocket|sec-websocket",
        "connect": r"CONNECT|connect",
        "extensions": r"extension|Extension",
    },
    "features": {
        "base_feature": r"BASE_FEATURE|BASE_DECLARE_FEATURE",
        "quic": r"QUIC|QUICHE|HTTP3",
        "tls": r"ECH|ALPS|TLS|certificate",
        "http2": r"HTTP2|HTTP/2|SPDY",
        "websocket": r"WebSocket|WEBSOCKET",
        "variation": r"variation|field.trial|Finch",
    },
    "quiche_h3": {
        "transport": r"TransportParameter|transport_parameter|idle_timeout",
        "settings": r"SETTINGS|settings",
        "flow_control": r"flow.control|window",
        "connection_id": r"connection.id|ConnectionId",
        "grease": r"grease|GREASE",
    },
}

# The five random-strategy files carry RANDOM_MARKERS; the seven protocol files
# carry their own category's markers.
MARKERS_FOR_FILE = {
    "boringssl_extensions": RANDOM_MARKERS,
    "boringssl_handshake_client": RANDOM_MARKERS,
    "boringssl_random": RANDOM_MARKERS,
    "chromium_base_random": RANDOM_MARKERS,
    "quiche_random": RANDOM_MARKERS,
    "tls": PROTOCOL_MARKERS["tls"],
    "alpn": PROTOCOL_MARKERS["alpn"],
    "http2": PROTOCOL_MARKERS["http2"],
    "http3": PROTOCOL_MARKERS["http3"],
    "websocket": PROTOCOL_MARKERS["websocket"],
    "features": PROTOCOL_MARKERS["features"],
    "quiche_h3": PROTOCOL_MARKERS["quiche_h3"],
}


def fetch(base, ref, path, attempts=5):
    """Fetches one file. googlesource throttles and drops connections, so retry
    with backoff rather than failing a whole collection on one transient error."""
    url = f"{base}/+/{ref}/{path}?format=TEXT"
    request = urllib.request.Request(
        url, headers={"User-Agent": "chrome_client-evidence/1"})
    last = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = response.read()
            # A gerrit XSSI guard prefix appears on some responses.
            if payload.startswith(b")]}'"):
                payload = payload.split(b"\n", 1)[1]
            return base64.b64decode(payload)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise
            last = error
        except (urllib.error.URLError, TimeoutError, ssl.SSLError,
                http.client.HTTPException, OSError) as error:
            last = error
        if attempt + 1 < attempts:
            time.sleep(2 ** attempt)
    raise SystemExit(f"fetch failed after {attempts} attempts: {url}: {last}")


def deps_revisions(tag):
    """Returns the BoringSSL and QUICHE revisions the tag's DEPS pins."""
    deps = fetch(CHROMIUM, f"refs/tags/{tag}", "DEPS").decode("utf-8", "replace")
    out = {}
    for name in ("boringssl_revision", "quiche_revision"):
        match = re.search(rf"'{name}':\s*'([0-9a-f]{{40}})'", deps)
        if not match:
            raise SystemExit(f"{tag}: DEPS has no {name}")
        out[name] = match.group(1)
    return out


def signal_lines(text, markers):
    """Line numbers per signal, 1-based, capped at 32 as the original pipeline did."""
    lines = text.splitlines()
    out = {}
    for name, pattern in markers.items():
        compiled = re.compile(pattern, re.IGNORECASE)
        hits = [i for i, line in enumerate(lines, 1) if compiled.search(line)]
        out[name] = {"count": len(hits), "lines": hits[:32]}
    return out


def collect(tag):
    revisions = deps_revisions(tag)
    refs = {
        "chromium": (CHROMIUM, f"refs/tags/{tag}"),
        "boringssl": (BORINGSSL, revisions["boringssl_revision"]),
        "quiche": (QUICHE, revisions["quiche_revision"]),
    }
    source_files = {}
    for name, (repo, path) in FILES.items():
        base, ref = refs[repo]
        blob = fetch(base, ref, path)
        text = blob.decode("utf-8", "replace")
        source_files[name] = {
            "repository": repo,
            "path": path,
            "bytes": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
            "signals": signal_lines(text, MARKERS_FOR_FILE[name]),
        }
        print(f"  {name:28} {len(blob):>8} bytes  {source_files[name]['sha256'][:16]}",
              file=sys.stderr, flush=True)
    return {
        "chrome_version": tag,
        "chromium_tag": tag,
        "boringssl_revision": revisions["boringssl_revision"],
        "quiche_revision": revisions["quiche_revision"],
        "source_files": source_files,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="append", required=True,
                        help="release tag, e.g. 152.0.7977.83; repeatable")
    parser.add_argument("--out")
    args = parser.parse_args()

    collected = {}
    for tag in args.version:
        print(f"{tag}:", file=sys.stderr, flush=True)
        try:
            collected[tag] = collect(tag)
        except (urllib.error.HTTPError, urllib.error.URLError) as error:
            raise SystemExit(f"{tag}: fetch failed: {error}")

    report = {"schema": "chrome_client.profile_evidence.v1", "releases": collected}

    if len(collected) > 1:
        tags = list(collected)
        identical, differing = [], []
        for name in FILES:
            digests = {collected[t]["source_files"][name]["sha256"] for t in tags}
            (identical if len(digests) == 1 else differing).append(name)
        report["patch_release_comparison"] = {
            "tags": tags,
            "identical_files": sorted(identical),
            "differing_files": sorted(differing),
            "audited_surface_is_branch_stable": not differing,
        }
        print(f"\nacross {len(tags)} patch releases: {len(identical)} of "
              f"{len(FILES)} audited files identical", file=sys.stderr)
        if differing:
            print(f"  differing: {', '.join(sorted(differing))}", file=sys.stderr)

    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
        print(f"\nwrote {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
