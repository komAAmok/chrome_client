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
version, target triple, library filename, SHA-256, and runtime dependencies.

### What the manifest `sha256` does and does not mean

It means: **this exact artifact was built and then audited** — its size, Chromium
revision, ABI version, version marker and runtime dependency list all agree with
the other eight, and `tools/audit-core-payloads.py` finds every profile payload
byte-for-byte inside it.

It does **not** mean the artifact can be reproduced from this commit. The
committed `args.gn` cannot pass `gn gen` (see `docs/BUILD.md`, "The committed
args.gn cannot be regenerated", and stage 0 of `docs/NEXT_STEPS.md`), so a clean
rebuild is not possible. `core/build-args/<target>.snapshot.txt` records what each
artifact was actually built with, recovered from its generated buildflag headers;
`python3 tools/record-core-build-args.py --check` fails if a rebuild changes that
configuration without re-recording it.

Treat the `sha256` as an identity for review and for detecting accidental
replacement, not as a reproducibility claim.
Large release binaries should be distributed through GitHub Releases or Git
LFS; `build.rs` can also use `MINICRONET_CORE_DIR` for local development.

Run the local integrity and ABI audit with:

```sh
tools/audit-core-binaries.sh
```

The audit checks the manifest hashes, target architecture, ABI exports,
platform-specific library pairing, SONAME, and staged runtime dependencies.
