# Legacy: the browserleaks capture pipeline (pre-migration)

> **This file describes a pipeline this repository no longer runs.** It
> documents the `raw/` directory layout of the browserleaks captures that used
> to live under `profiles/chrome-<major>/raw/`, and how
> `tools/validate-wire-profile.py` verified them. Those directories are gone:
> `profiles/` now holds the reference captures imported by
> `tools/import-reference-captures.py`, one directory per release, three
> protocols each -- see [`README.md`](README.md).
>
> Kept because `profiles/wire_expectations.json` and
> `tools/validate-wire-profile.py` still exist and this explains what they
> were for. Nothing below is a description of the current tree.

---

# profiles/chrome-<major>/raw/

The original wire captures behind each profile's `wire_verified`, committed so a
clean clone can reproduce the claim instead of taking it on trust.

## Why the raw captures are committed

The derived summaries (`captures.json` from 146 up, and the inline evidence in
`normalized_profiles.json` for the rest) are not enough on their own:

- **Key material exists nowhere else.** BrowserLeaks reports only the *length* of
  each key share. `key_share_digest` — one of the six stochastic fields the gate
  requires to differ on every connection — can only be computed from the
  ClientHello in a pcap.
- **Connection independence is a pcap property.** Whether three connections came
  from three source ports cannot be shown from the JSON summary alone; for
  chrome_146/147 the endpoint did not even fill in `tcpip`, so the pcap is the
  only source.
- **The H3 half of the status needs the key logs.** Profiles up to 151 are
  `tls_h2_and_h3_request_verified`, and their `*-h3-local/` directories hold QUIC
  pcaps plus the `*.keys` TLS traffic secrets those pcaps are decrypted with.
  Those are per-session secrets for captured traffic, i.e. evidence, not
  credentials.

## Layout

Each directory keeps the name it had in the pre-migration tree, so it can be
matched against the original pipeline's records:

```text
profiles/chrome-<major>/raw/
  chrome-<major>-browserleaks-validation[-<timestamp>]/   TLS ClientHello captures
  chrome-<major>-release-validation/                      endpoint summary, no pcaps
  chrome-<major>-h3-local/                                QUIC captures + *.keys
```

A profile may have several of these, and not all of them are equally useful:
some `*-release-validation/` directories are summary copies whose pcaps live
elsewhere (or were never kept). They are committed for completeness; the
`capture`/`key_log` fields inside the validation JSON still name the paths those
files had in the pre-migration tree, which is a record of where each capture was
taken. No in-repo tool resolves those paths.

## Reproducing the verification

```sh
tools/verify-browserleaks-captures.py              # every profile with captures
tools/verify-browserleaks-captures.py --major 120  # one release
```

The wrapper walks `profiles/chrome-<major>/raw/`, pairs each `response-N.json`
with its `session-N.pcap`/`run-N.pcap`, and runs
`tools/validate-wire-profile.py` (the original browserleaks gate, migrated from
the pre-migration tree) against
`profiles/wire_expectations.json`. For every major it can see, it exits non-zero
unless at least one capture directory reports `stable_match: true` with the
randomness checks passing.

**Coverage, stated honestly**: it can see 53 of the 72 profiles -- `chrome_99`
through `chrome-151`. The other 19 have no `raw/` directory holding
`response-*.json`, so this wrapper cannot verify them at all: `chrome_86`..
`chrome_98` keep their captures as a single `captures.json` (verified by
`tools/verify-wire-capture.py`, not by this wrapper), and `chrome-152`..`chrome-157`
likewise ship `captures.json` without `raw/`. A major with no capture directory
is counted as skipped, not as verified -- check the summary line for the split.

`tools/validate-wire-profile.py` and `profiles/wire_expectations.json` are
committed for exactly this reason: without them the captures would be an archive
rather than evidence.

## chrome_148 (was a gap, now closed)

`chrome_148` was the one profile whose `wire_verified: true` could not be
reproduced, and the reason was a packaging accident rather than missing
measurements: the three responses that carry the evidence were filed under
`chrome-148-validation/` next to nine records from a different experiment, and
nothing paired them into a directory the gate could read. Its
`chrome-148-release-validation/` held only one response, and its pcaps are all
QUIC/H3, so the directory that looked authoritative could never pass.

They are now collected as `chrome-148-browserleaks-boundary-20261014/`:

- three independent connections, all 19 stable fields matching
  `wire_expectations.json`, and all six stochastic fields distinct;
- **no pcaps**, because the tls.peet.ws schema carries the raw `key_share` and
  ECH bytes inline, so `key_share_digest` and `ech_digest` are computed from the
  responses. That is why `tools/verify-browserleaks-captures.py` only demands a
  pcap for the browserleaks schema, which reports key-share *lengths* only.

Three defects in the wrapper were hiding this, and all three are fixed:
it required a pcap unconditionally, it never passed `--url` (so every tls.peet.ws
directory failed the `:authority`/`:path` rewrite while the whole TLS fingerprint
matched), and a directory whose records are named by phase rather than
`response-<n>` was reported as a failure instead of being left unverifiable.

## What was deliberately not committed

The raw directories were captured by running a real browser, so they also
contained run state that is not wire evidence. Those files were dropped rather
than archived:

- `chrome-profile/` and `chrome-capture-profile/` — throwaway Chrome user-data
  directories (Code Cache, LevelDB logs, `Cookies`, `Login Data`). The credential
  databases were schema-only (0 rows in `logins` and `cookies`) and the keys DB
  held 0 private keys, so nothing sensitive was discarded, but 9.3 MB of browser
  state is not evidence for a TLS fingerprint.
- `nss-home/` — the fake HOME the captures ran under: fontconfig caches, a Chrome
  crash-report settings file, and an NSS DB containing the local **Test Root CA**
  (0 private keys).
- `chrome-force-h3.strace` — a 17.9 MB syscall trace of the *browser* process. It
  records IPC (`AUTH EXTERNAL`, socket FDs 5/14/36/38), no `sendto` to UDP :443,
  and no `socket()`/`connect()` calls, so it cannot corroborate the QUIC sessions
  it sits beside. Nothing in the pre-migration tree references it; the file the
  records do reference, `chrome-force-h3.pcap`, is kept.

Nothing referenced by a validation JSON was removed: every path those files name
still resolves. The result is 13 MB instead of 36 MB of which the extra 23 MB was
browser cache, a test HOME and one unreferenced trace.
