# MiniCronet Core binaries

Place one audited `libminicronet` binary under the directory matching its Rust
target triple. The repository does not accept the old `cronet` ABI or library
names.

Supported directories:

```text
linux-x86       linux-x86_64       linux-arm64
windows-x86     windows-x86_64     windows-arm64
macos-x86_64    macos-arm64
```

Each directory must contain a `manifest.json` with the Chromium revision, ABI
version, target triple, library filename, SHA-256, and runtime dependencies.
Large release binaries should be distributed through GitHub Releases or Git
LFS; `build.rs` can also use `MINICRONET_CORE_DIR` for local development.

Run the local integrity and ABI audit with:

```sh
tools/audit-core-binaries.sh
```

The audit checks the manifest hashes, target architecture, ABI exports,
platform-specific library pairing, SONAME, and staged runtime dependencies.
