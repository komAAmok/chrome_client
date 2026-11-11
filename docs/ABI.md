# C ABI boundary

`core/abi/minicronet.h` is the single source of truth. No Chromium Cronet
headers are exposed. Opaque `mn_engine_t`, `mn_request_t`, and
`mn_websocket_t` handles are reference counted by the Core; Rust is the only
high-level owner exposed to language adapters.

## v9（当前 ABI）

`MN_ABI_VERSION` 是 9，导出符号 28 个。v9 在 v8 的 20 个之上新增 8 个：

| 分组 | 符号 |
| --- | --- |
| Cookie 存取 | `mn_engine_cookies_get` / `_set` / `_delete` / `_clear` |
| 协商协议 | `mn_request_negotiated_protocol` |
| ECH 状态 | `mn_request_ech_status` |
| mTLS | `mn_engine_set_client_certificate` / `mn_engine_clear_client_certificate` |

结构与布局：`mn_engine_config_t` 152 → 248 字节（32 位 84 → 136），追加 13 个字段；
新增 `mn_cookie_t`（88 / 52 字节）与 `mn_platform_t` / `mn_cookie_same_site_t` /
`mn_http_version_t` / `mn_ech_status_t` 四个枚举。`mn_request_config_t`（144）与
`mn_request_callbacks_t`（48）不变，所以 32 位分支的 92 / 28 两条断言不用动。

三条必须知道的事实，都是实现时被源码钉住的：

- **`mn_ech_status` 的 `FAILED` 不可达。** `net/ssl/ssl_info.h` 只有一个
  `encrypted_client_hello` 布尔值，「尝试后被拒」与「没尝试」都是 false。桌面端实际
  返回 `NOT_ATTEMPTED`，含义是「没有 ECHConfigList」，不是「平台关闭」。
- **`mn_request_negotiated_protocol` 会返回 `UNKNOWN`。** `connection_info` 的默认值
  就是 `kUNKNOWN`（命中 HTTP 缓存时如此）。把它报成 HTTP/1.1 是本 ABI 明确要根除的谎报。
- **`mn_engine_config_t` 没有 `hsts_enabled`。** Chromium 在
  `ssl_client_socket.cc:230` 对 `TransportSecurityState` 做非空 CHECK，并在
  `ssl_client_socket_impl.cc:1278,1300` 无判空解引用，所以「关掉 HSTS」不是这个构建
  能到达的状态；唯一可配置的是 `hsts_bypass_list`，且每项必须是单标签（多标签会被
  `transport_security_state.cc:246-248` 的 DCHECK 约束并在 release 下静默忽略，因此
  Core 直接拒绝）。

版本脚本节点名保持 `MINICRONET_2`：它是与已编译绑定的链接期约定，改名会让每一个已发布
的扩展以 `version MINICRONET_2 not found` 加载失败，而 ABI 版本本来就由
`MN_ABI_VERSION` 表达。

v9 的设计依据与十项提案的原始分析见 [ABI_V9_PROPOSAL.md](ABI_V9_PROPOSAL.md)；
批次划分、复核记录与逐项验收见 [ABI_V9_TASKS.md](ABI_V9_TASKS.md)。
