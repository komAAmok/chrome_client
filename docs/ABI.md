# C ABI boundary

`core/abi/minicronet.h` is the single source of truth. No Chromium Cronet
headers are exposed. Opaque `mn_engine_t`, `mn_request_t`, and
`mn_websocket_t` handles are reference counted by the Core; Rust is the only
high-level owner exposed to language adapters.

## v9 提案（未实现）

v9 的十项改动（cookie 存取、协商协议 + ECH 状态、client hints 注入、DNS 配置、
host mapping、mTLS、四项原生能力补齐、平台身份、非幂等重发修复、hop-by-hop 头）见
[ABI_V9_PROPOSAL.md](ABI_V9_PROPOSAL.md)。该文件是提案，不是契约；v8 仍是当前 ABI，
`MN_ABI_VERSION` 仍是 8，导出符号仍是 20 个。

提案里会改动结构体布局的是 DNS / host mapping / HSTS / cookie 语义 / 代理链 / platform
六组 engine 字段；协商协议、ECH 状态与 mTLS 是独立符号（3 个 getter + 2 个 setter）；第 9 项是 Chromium 补丁、第 10 项是纯绑定层改动，都不动 ABI。
以上都不改结构体，
`mn_request_config_t` 与 `mn_request_callbacks_t` 因此保持 144 / 48 字节不变。
