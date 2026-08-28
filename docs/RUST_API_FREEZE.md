# Rust API freeze

## Version and crates

- Workspace API version: `0.2.0`.
- The native boundary consumed by `minicronet-sys` is ABI v7.
- No compatibility aliases or deprecated entry points are provided.

`minicronet-sys` contains only the ABI constants, opaque handles, C enums,
`#[repr(C)]` structures, callback types, and the 19 exported C functions. It
contains no networking implementation or async runtime.

`minicronet` owns `Engine`, `Request`, response streams, WebSocket streams,
callbacks, cancellation, timeout mapping, and error conversion. No Rust type
implements TLS, HTTP, QUIC, WebSocket, proxy, cookie, cache, or certificate
behavior.

## Frozen rules

- Engine configuration is copied at construction and cannot be changed.
- Request and WebSocket creation copies borrowed strings, headers, and payload
  metadata before returning.
- `Request::start` and `WebSocket::start` are one-shot operations.
- Core owns timeout, cancellation, redirects, protocol selection, TLS,
  proxies, and cache behavior.
- Request completion has one terminal result; WebSocket completion has exactly
  one `Closed` or `Failure` event.
- Dropping Engine does not cancel active Core operations. Dropping an active
  request or WebSocket releases only the caller handle; Core retains the
  operation until its terminal callback.
- Callback buffers are copied before entering Rust-owned state.
- Callback panics are caught and never unwind through C.
- `Send`/`Sync` are implemented only for handles whose ABI contract is
  thread-safe; mutable callback state is protected by synchronization.

## Deliberate exclusions

The workspace does not add Tokio, async-std, another executor, a Rust network
stack, ABI compatibility layers for older versions, or direct protocol logic.
Language bindings must call this safe Rust surface rather than the Core
directly.

The freeze gate requires formatting, workspace tests, C/C++ ABI smoke tests,
HTTP and WebSocket smoke tests, the minimal-Core audit, and the release symbol
audit. Cross-platform compilation remains a separate release-matrix task.
