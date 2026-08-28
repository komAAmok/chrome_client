# Architecture

```text
Python (PyO3) ─┐
Rust ──────────┼─> minicronet ─> minicronet-sys ─> libminicronet
Go (cgo) ──────┤
Node (N-API) ───┘
```

`libminicronet` is the only protocol implementation. `minicronet-sys` is a
mechanical declaration of the stable C ABI. `minicronet` owns handles,
configuration freezing, callbacks, cancellation, timeout mapping, and safe
Rust lifetimes. Language bindings only translate their host-language types.
