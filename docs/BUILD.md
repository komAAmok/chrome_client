# Build layout

Chromium is built outside this repository. This repository stores only the
audited Core ABI header, platform binaries, manifests, and language bindings.

For local Core selection:

```sh
MINICRONET_CORE_DIR=/path/to/core/binaries/linux-x86_64 \
MINICRONET_REQUIRE_NATIVE=1 \
cargo check -p minicronet
```

The eight platform directories are intentionally explicit; no script silently
switches architecture.

After migrating a Core build, verify all targets and manifests:

```sh
tools/audit-core-binaries.sh
```

The ABI header, Rust declarations, Core definitions, and all three platform
export tables can be checked without a Chromium checkout:

```sh
tools/audit-abi.sh
```

Rust checks are independent of the native Core, while real Linux x86_64
smoke tests require the staged Core and its runtime loader path:

```sh
cargo check --workspace --all-targets
LD_LIBRARY_PATH="$PWD/core/binaries/linux-x86_64" \
  MINICRONET_REQUIRE_NATIVE=1 cargo test --workspace --all-targets
```
