# chrome_client

`chrome_client` is a multi-language client built around the minimal Chromium
network Core `libminicronet`.

```text
Python / Go / Node.js / Rust
              |
          Rust API
              |
       minicronet-sys
              |
        libminicronet
```

The Core is the only implementation of TLS, HTTP/1.1, HTTP/2, HTTP/3/QUIC,
WebSocket/WSS, and proxy protocols. Rust owns the safe lifecycle and policy
surface; language bindings only translate types and callbacks.

## Repository boundaries

- `core/abi/`: the stable C ABI contract.
- `core/binaries/`: audited platform Core binaries, selected by target.
- `core/dependencies/`: runtime dependencies declared per target.
- `crates/minicronet-sys/`: raw ABI declarations and native linker selection.
- `crates/minicronet/`: safe Rust ownership/configuration layer.
- `bindings/`: thin Python, Go, and Node.js adapters.
- `tools/`: one explicit build/audit entry point per platform architecture.

Supported targets are Windows x86/x86_64/ARM64, Linux x86/x86_64/ARM64, and
macOS x86_64/ARM64. Android, Java, public JNI, UI, and unrelated Chromium
targets are excluded.

## Local Rust check

The structure can be checked without a native binary:

```sh
cargo check --workspace --all-targets
```

To require a real Core for a target:

```sh
MINICRONET_CORE_DIR=/path/to/core/binaries/linux-x86_64 \
MINICRONET_REQUIRE_NATIVE=1 \
cargo check -p minicronet
```

The Core binary must be accompanied by a manifest containing its ABI version,
Chromium revision, target triple, SHA-256, and runtime dependencies.

Migration and compatibility rules are documented in
[`docs/MIGRATION_FROM_NEW.md`](docs/MIGRATION_FROM_NEW.md), with the profile
boundary in [`docs/PROFILE_CONTEXT_DESIGN.md`](docs/PROFILE_CONTEXT_DESIGN.md)
and the frozen Rust contract in [`docs/RUST_API_FREEZE.md`](docs/RUST_API_FREEZE.md).
