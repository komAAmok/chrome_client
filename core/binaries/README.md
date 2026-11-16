# MiniCronet Core binaries

Place one audited `libminicronet` binary under the directory matching its Rust
target triple. The repository does not accept the old `cronet` ABI or library
names.

Supported directories:

```text
linux-x86       linux-x86_64       linux-armv7       linux-arm64
windows-x86     windows-x86_64     windows-arm64
macos-x86_64    macos-arm64
```

Each directory must contain a `manifest.json` with the Chromium revision, ABI
version, target triple, library filename, SHA-256, runtime dependencies, and
`core_sources_sha256` — a hash over every Core build input, so
`python3 tools/audit-core-source-fingerprint.py` can refuse an artifact that
predates an edit to `core/source/`. Without that field an edited `.cc` and its
stale `.so` look identical to every other gate: measured, a +11 diff sat in
`core/source/engine.cc` for a full round while the whole suite stayed green.

### What the manifest `sha256` means

It means: **this exact artifact was built from this commit and then audited** — its
size, Chromium revision, ABI version, version marker and runtime dependency list all
agree with the other eight, and `tools/audit-core-payloads.py` finds every profile
payload byte-for-byte inside it.

It is also a reproducibility claim. `0.2.14` was rebuilt on all nine
platforms from the same commit; `docs/BUILD.md` records the host tools that rebuild
needs (pkgconf, ripgrep, xwin) and the exact `gn gen` invocation.

`core/build-args/<target>.snapshot.txt` records the configuration each artifact was
built with, recovered from its generated buildflag headers rather than copied from
`args.gn`, so it reflects what reached the compiler. `python3
tools/record-core-build-args.py --check` fails if a rebuild changes that
configuration without re-recording it.

Run the local integrity and ABI audit with:

```sh
tools/audit-core-binaries.sh
```

The audit checks the manifest hashes, target architecture, ABI exports,
platform-specific library pairing, SONAME, and staged runtime dependencies.
