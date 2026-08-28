# minicronet

Safe Rust layer for Core handles, immutable Engine configuration, callbacks,
streams, cancellation, timeout mapping, and error conversion. TLS, HTTP,
QUIC, WebSocket, and proxy behavior remain in `libminicronet`.

`Engine::request` creates a Core-owned request. `Request::start` returns a
standard `Future`; once headers arrive, `Response::body` implements the
lightweight `futures_core::Stream` trait and also exposes `blocking_next`.
`Request::cancel`, `upload_write`, and `follow_redirect` map directly to the
ABI operations.

`Engine::websocket` follows the same ownership rule. `WebSocket::start`
returns `WebSocketEvents`, whose events are copied from Core callbacks. No
Rust callback may unwind into C; terminal native callbacks release the shared
callback state exactly once.

The only external Rust dependency is `futures-core`, used solely for the
standard `Stream` trait. It provides no executor or protocol implementation;
an application may use any executor or poll the streams itself.
