# C ABI boundary

`core/abi/minicronet.h` is the single source of truth. No Chromium Cronet
headers are exposed. Opaque `mn_engine_t`, `mn_request_t`, and
`mn_websocket_t` handles are reference counted by the Core; Rust is the only
high-level owner exposed to language adapters.

## v9 提案（未实现）

v9 的四项改动（cookie 存取、协商协议、client hints 注入、DNS 配置）见
[ABI_V9_PROPOSAL.md](ABI_V9_PROPOSAL.md)。该文件是提案，不是契约；v8 仍是当前 ABI，
`MN_ABI_VERSION` 仍是 8，导出符号仍是 20 个。

提案里唯一会改动结构体布局的是 DNS 字段；协商协议是一个独立 getter，
`mn_request_config_t` 与 `mn_request_callbacks_t` 因此保持 144 / 48 字节不变。
