# profiles/

One directory per Chromium major, 86 through 157, each holding the reference
wire captures for that release under all three protocols. Nothing here is
hand-written: every file is written by `tools/import-reference-captures.py`
from the reference capture set.

## Layout

```text
profiles/
  chrome_<major>/                     one per release, 72 directories
    meta.json                         version, targets, per-protocol status
    source-evidence.json              audited Chromium source lines for this tag
    README.md                         what this release's captures are
    h1/  captures.json  samples.json
    h2/  captures.json  samples.json
    h3/  captures.json  samples.json     (86..92: unsupported.json instead)
  normalized_profiles.json            runtime input for the table generator
  effective_protocol_params.json      runtime input
  network_feature_snapshots.json      runtime input
  wire_expectations.json              legacy browserleaks gate input
  RAW_EVIDENCE.md                     legacy: describes the pre-migration tree
  README.md                           this file
```

Every release has the same shape. That is deliberate: the previous layout mixed
two directory spellings, three file sets and a `raw/` subdirectory that only
some releases had, so a tool that assumed one shape silently skipped the
releases that did not match.

## What the captures are

Each protocol directory holds six independent connections, captured from a real
Chrome build of that exact release:

| File | Contents |
| --- | --- |
| `captures.json` | the endpoint's own response objects, unmodified, one per connection, concatenated (not a JSON array) |
| `samples.json` | the same six samples plus their `checks` metadata: ja3/ja4/peetprint hashes, session id, client random, header count |
| `unsupported.json` | only for h3 on 86..92: why no QUIC handshake completed |

`captures.json` is concatenated JSON objects because that is the format the
wire gate parses -- one object per connection. A JSON array would parse as a
single record and every stochastic check would pass vacuously.

The endpoints differ by protocol and the difference is load-bearing:

| Protocol | Endpoint | How Chrome was pinned |
| --- | --- | --- |
| h2 | `tls.peet.ws/api/all` | free to negotiate (native) |
| h1 | `tls.peet.ws/api/all` | forced HTTP/1.1 |
| h3 | `quic.tools.scrapfly.io/api/fp/quic` | forced HTTP/3 |

The h2 file is a *native* capture: its ClientHello offers ALPN `[h2, http/1.1]`.
Reproducing it needs the native protocol mode, not a forced-h2 pin -- forcing h2
narrows ALPN to `[h2]` and, because the ClientHello then loses the
`http/1.1` token, moves the padding total as well (418 -> 436 on chrome_86).
`tools/align-live-fingerprint.py` encodes that mapping in `PROBE_PROTOCOL`.

## Coverage

| Protocol | Releases with captures | Note |
| --- | --- | --- |
| h1 | 72 (86..157) | 6 connections each |
| h2 | 72 (86..157) | 6 connections each |
| h3 | 65 (93..157) | 86..92 could not complete a QUIC handshake against the reference endpoint; the failure is recorded per release |

209 protocol/release pairs in total. The h3 gap is a property of those Chrome
builds, not a missing capture: they predate Chrome's QUIC fingerprint reaching
the shape the endpoint requires, and the Core rejects a forced-H3 engine on
those profiles with `MN_ERROR_PROFILE_UNSUPPORTED` rather than pretending
otherwise.

## Gates that read this directory

| Gate | What it asserts |
| --- | --- |
| `tools/audit-padding-rule.py` | the ClientHello padding width each era reports, and that the compiled table pins no override |
| `tools/audit-tls-version-range.py` | the `supported_versions` list per release, and that the compiled table carries the matching floor |
| `tools/audit-h1-alps-absence.py` | h1 never offers ALPS while h2 does from 91, and both protocols agree on the protocol-independent extensions |
| `tools/audit-h3-profile-support.py` | which releases can do HTTP/3 at all |
| `tools/align-live-fingerprint.py` | our built Core against these captures, field by field, live or `--offline` |

All five read `captures.json` directly through `tools/profile_captures.py`.
Nothing reads a pre-recorded alignment report any more: those recorded our
Core's output from one run on one machine, could not be re-derived, and omitted
`supported_versions` entirely -- which is why three separate conclusions in the
docs used to be argued indirectly from the ClientHello being ten bytes longer.

## Runtime inputs are separate

`normalized_profiles.json`, `effective_protocol_params.json` and
`network_feature_snapshots.json` are read by exactly one tool
(`tools/generate-profile-table.py`), which emits
`core/source/minicronet/profile_table_generated.h`. They are not captures and
the importer does not touch them.

Regenerating the table from them reproduces the committed header byte for byte;
that is verified by running the generator, and the four content gates above
assert what the header says against these captures.

## Known gap

`normalized_profiles.json` records `"generated_by": "tools/build-normalized-profiles.py"`,
which exists only in the pre-migration `new/` tree. The runtime table therefore
cannot be rebuilt end to end from a clean checkout; only its consumers are gated.
`tools/build-profile-entry.py` covers *adding* one profile by rebuilding the
derivable half of that entry from the release's own capture and source lines.

`wire_expectations.json` and `RAW_EVIDENCE.md` are the legacy browserleaks
pipeline's inputs. `tools/validate-wire-profile.py` still reads the former; the
captures it was written for are no longer in this directory, so it is only
reachable by pointing `--expectations` and the response files at an external
copy. The current gates do not use it.
