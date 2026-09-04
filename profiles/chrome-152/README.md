# chrome_152 profile inputs

Evidence collected for a `chrome_152` profile. **The profile is not activated
yet**; one blocker remains, described at the end.

## Files

| File | What it is |
| --- | --- |
| `captures.json` | 4 independent Chrome 152 connections to `tls.peet.ws/api/all` |
| `validation.json` | wire gate result, produced by `tools/verify-wire-capture.py` |
| `source-evidence.json` | the 12 audited source files at the 152 release tags, produced by `tools/collect-profile-evidence.py` |
| `trust-anchor-ids.json` | the 28 trust anchor IDs the captures advertise, in wire order |

Reproduce both:

```sh
tools/verify-wire-capture.py --profile chrome_152 \
  --captures profiles/chrome-152/captures.json \
  --out profiles/chrome-152/validation.json

tools/collect-profile-evidence.py --version 152.0.7977.83 --version 152.0.7977.42 \
  --out profiles/chrome-152/source-evidence.json
```

## Pinned inputs

| Input | Value |
| --- | --- |
| chrome_version / chromium_tag | `152.0.7977.83` |
| chromium_commit | `79460ebecaa5625e57a5fb679a735659e73dc687` |
| boringssl_revision | `572a4c68475d284b34675f45ddbb9c158ef3c2ae` |
| quiche_revision | `1ba0d99a5c2fec4f4dbb7f98f251b05dcf4e2968` |

Chrome's reduced User-Agent reports `152.0.0.0`, so the captures cannot be
attributed to a patch build. Instead all twelve audited files were compared
between the first (`152.0.7977.42`, 2026-08-12) and last (`152.0.7977.83`,
2026-09-03) stable releases of the branch: **all twelve are identical**, so the
audited surface is branch-stable and the exact build does not change the
fingerprint.

## Wire gate

`wire_verified: true`. 4 connections from 4 distinct source ports.

Byte-identical on every connection: de-GREASEd cipher list, supported groups,
signature algorithms, HTTP/2 fingerprint, trust_anchors payload, User-Agent.
Different on every connection (4 distinct values each, 3 required): client
random, session id, extension order, GREASE values, key share, ECH payload.

The extension set differs by exactly `pre_shared_key (41)`, present only on the
3 connections that resumed a session. That is expected and is why
`verify-wire-capture.py` treats resumption extensions separately instead of
demanding an identical set.

## What differs from chrome_151

Only two things, and both are provable from source:

**1. User-Agent version.** `151.0.0.0` → `152.0.0.0`, every other token
identical. `core/source/minicronet.cc` already emits `<major>.0.0.0` for any
major above 104, so no code change is needed.

**2. One new TLS extension: `0xca34` / 51764 = `TLSEXT_TYPE_trust_anchors`.**
Everything else matches: JA3's cipher and curve segments are identical, JA4 goes
from `t13d1516h2_8daaf6152771_806a8c22fdea` to
`t13d1517h2_8daaf6152771_cb7bf5808d99` — the `1516` → `1517` is the extension
count, and the cipher hash `8daaf6152771` is unchanged. The Akamai HTTP/2
fingerprint is byte-identical, including the 15,663,105 window increment.

This is a real version-level change, not a component-update artifact:

| | Chrome 151 | Chrome 152 |
| --- | --- | --- |
| `kTLSTrustAnchorIDs` | `FEATURE_DISABLED_BY_DEFAULT` | `FEATURE_ENABLED_BY_DEFAULT` |
| `kNonMtcTrustAnchorIDs` | does not exist | `FEATURE_ENABLED_BY_DEFAULT` |
| selection | `SelectTrustAnchorIDs()`, only the intersection with server-advertised IDs | `SelectAllTrustAnchorIDs()`, all trusted anchors unconditionally |

So a Chrome 151 never sends the extension no matter how current its components
are: the flag is compiled off.

## The remaining blocker: the payload is not derivable from source

The extension is well-defined in shape — `AddTrustAnchorIdToEncodedList` emits a
sequence of one-byte-length-prefixed IDs, and that function plus
`ShouldAdvertiseTrustAnchorIDs` and `SelectAllTrustAnchorIDs` are **byte-identical
between the 152 tag and this repository's pinned Chromium tree**. So a Core built
here will produce Chrome 152's exact encoding, in whatever order it is fed.

What is not derivable is *which* IDs. The captures consistently advertise 28
(186-byte payload, identical on all 4 connections). The compiled-in Chrome Root
Store has 32 — in both the 152 tag and the pinned tree — and the 28 are a strict
subset, missing `d6790902`, `d6790903`, `d6790909`, `d679090e` (all 11129.9.x).
Order differs too: the wire starts with `82df130206`, the root store with
`839a648c9b2d010a`. The capture machine had a PKI-metadata-component-updated root
store, and that state is not in any source tree.

Two options, and the evidence rules one out:

- **Derive from the compiled-in root store.** Reproducible from source, but
  produces 32 IDs in a different order and a different payload length. It matches
  no observed Chrome, so it fails the fidelity requirement.
- **Freeze the 28 observed IDs in their observed order.** Matches the capture
  byte for byte, and is consistent with how every other profile field already
  works — cipher lists, curve lists and extension lists are frozen constants, not
  values read from the build. The cost is that it is a snapshot: real Chrome 152
  installs will advertise whatever their component shipped.

Freezing is the only option that satisfies the fidelity requirement, but it needs
sign-off because it puts a component-updated value into a version-keyed table.
Landing it also needs a Core change: `core/source/profile_ssl_config_service.cc`
must populate `SSLContextConfig::trust_anchor_ids` from the profile, a new field
must be added to the profile table, and 8 platforms rebuilt. Profiles up to 151
keep an empty list, so `ShouldAdvertiseTrustAnchorIDs()` returns false for them
and they stay byte-identical — the change is fail-closed by construction.
