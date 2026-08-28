# Node.js binding

The Node package uses a minimal napi-rs adapter over the Rust facade. It must
not duplicate networking or TLS implementations.

The platform loader selects the matching Core artifact: `libminicronet.so`,
`minicronet.dll`, or `libminicronet.dylib`. The binding translates JavaScript
strings, byte arrays, promises, and errors to the Rust request API; protocol
behavior remains in `libminicronet`.

The Node toolchain is available, but `napi-rs` is not yet vendored or declared
in the workspace. The binding scaffold remains a follow-up after the Rust API
surface is frozen.
