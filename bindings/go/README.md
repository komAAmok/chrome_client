# Go binding

The Go package uses cgo only against the Rust facade C ABI. Protocol logic
remains in the MiniCronet Core and Rust ownership layer.

The platform package must load the target's Rust facade/Core artifacts:
`libminicronet.so` on Linux, `minicronet.dll` plus `minicronet.lib` on
Windows, and `libminicronet.dylib` on macOS. It must not call Chromium or
implement a second HTTP/TLS stack.

The planned surface is `Engine`, `Request`, and `Response`, with byte/string
conversion and callback/error translation only. A Go toolchain is not present
in the current build environment, so the cgo package remains a follow-up.
