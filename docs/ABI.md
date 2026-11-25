# C ABI boundary

`core/abi/minicronet.h` is the single source of truth. No Chromium Cronet
headers are exposed. Opaque `mn_engine_t`, `mn_request_t`, and
`mn_websocket_t` handles are reference counted by the Core; Rust is the only
high-level owner exposed to language adapters.

## v14（当前 ABI）

`MN_ABI_VERSION` 是 14，导出符号 **32** 个：v13 追加了 `mn_engine_capabilities`，
v14 追加了 `mn_platform_user_agent_group` 与 `mn_platform_hint`。

### 为什么需要能力位

同一个 ABI 被构建九次，每次的 `args.gn` 不同。有两个编译期开关会改变调用方
**观察得到**的行为，而它们的值来自 `is_debug`（`net/features.gni:69,74`）：

| 开关 | 作用 | 关掉时的表现 |
| --- | --- | --- |
| `ENABLE_QUIC_PROXY_SUPPORT` | `quic://` 能否变成 `SCHEME_QUIC` | 解析器拒绝该 scheme |
| `ENABLE_BRACKETED_PROXY_URIS` | `[a,b]` 多跳链是否合法 | 链校验失败；两者互斥时 `CHECK` |

没有能力位时，绑定层只能「发个请求看错误码」，而 `ERR_NO_SUPPORTED_PROXIES`
无法区分「本构建不支持这个 scheme」与「支持但代理拒绝了连接」——两件完全不同的事，
同一个错误码。

```c
uint32_t flags = 0;
mn_engine_capabilities(&flags);   /* 不需要 engine，描述的是这个库 */
```

| 位 | 值 | 含义 |
| --- | --- | --- |
| `MN_CAPABILITY_QUIC_PROXY` | `1 << 0` | 可以拨 `quic://` 代理 |

本仓库发布的九个产物该位为 1：`tools/build-core-*.sh` 在 `is_debug = false` 的官方
构建上显式覆盖了 `enable_quic_proxy_support`，理由写在脚本里——`quic://` 代理不是
调试特性。这是本项目对 Chromium 默认值的**有意偏离**，位为 1 就是它可见的凭据。

### 位 1 为什么是保留的

`enable_bracketed_proxy_uris` 也被打开了（`proxy_config.cc:105-110` 用 `CHECK` 把它和
`enable_quic_proxy_support` 绑在一起，只开一个会让带括号的链崩溃而非失败），但它
**没有**对应的能力位，因为 ABI 表达不了这个能力：

- `proxy_chain` 按 `,` 切分，每个 entry 交给 `ProxyServer::FromSchemeHostAndPort`，
  再 `ProxyList::AddProxyServer`——一个 entry 就是一跳，存不出多跳链。
- 真正能构造多跳的是 `ProxyList::AddProxyChain(ProxyChain)`，只有
  `MultiProxyUrisToProxyChain` 会调它，而那个函数用**空格**分隔各跳
  （`proxy_string_util.cc:283`），且只从 `ProxyRules::ParseFromString` 进入。

实测：`mn_engine_create` 对 `"[https://a:443 https://b:443]"` 与
`"[https://a:443,https://b:443]"` 都返回 `MN_ERROR_INVALID_ARGUMENT`。报告一个 ABI
无法兑现的位，会让这个位失去「探针」的作用——所以它不出现，而不是永远返回 0。

### 未定义位

本修订之外的位必须被忽略。fd 后续只追加，不重编号。

---

## v13（历史）

`MN_ABI_VERSION` 是 13，导出符号 **30** 个：v13 只追加了 `mn_engine_capabilities`
与 `mn_capability_t`。这一版引入的两个能力位（QUIC 代理、bracketed 链）在 v14 中
继续有效，其中位 1 已在 v14 改为保留位，理由见上面「位 1 为什么是保留的」。

---

## v12（历史）

`MN_ABI_VERSION` 是 12，导出符号 **29** 个：v12 新增了 `mn_request_release_buffer`。

三件事，两件是正确性修复，一件是如实标注收益的零拷贝选项。

### 1. `mn_request_redirect_fn` 带结构化头字段

回调签名新增 `const mn_header_t *header_fields, size_t header_field_count`（在
`status_code` 之后、原始块之前）。重定向跳转的 `headers` 原始块此前是绑定层唯一还得
自己 split 的地方——`on_response` 早就拿到了 `mn_header_t` 数组。现在两者一致，Python
侧不再重解析。重复的 `Set-Cookie` 与混合大小写头名实测正确：`["a=1", "b=2"]` 两条
都在，`X-MiXeD-Case` 归一。

**顺序是有意的**：新参数插在 `status_code` 之后而不是末尾。这是一次 break change，
`MN_ABI_VERSION` 因此升到 12——旧调用方按旧签名调用新库会在版本校验处被拒绝
（`callbacks.version != MN_ABI_VERSION` 返回 `MN_ERROR_UNSUPPORTED_ABI`），而不是被
按 11 个参数调用一个 9 参数的函数。

### 2. 跨域重定向的 `Authorization` 剥离（opt-in）

`mn_request_config_t` 追加一个字段：

```c
uint8_t strip_authorization_on_cross_origin_redirect;  /* 0 = 保留（默认） */
```

`//net` 本身不做这件事：剥离在 `services/network/url_loader.cc:1025`，而 Core 直接驱动
`URLRequest` 并调 `FollowDeferredRedirect(std::nullopt, std::nullopt)`，所以什么也没删。
v12 让 Core 在 `OnReceivedRedirect` 里比 origin 并调 `RemoveRequestHeaderByName`——
`is_redirecting_` 是 `URLRequest` 允许该调用的唯一窗口
（`net/url_request/url_request.cc:258-261`）。

**默认 0**，因为 ABI 11 调用方从未得到这个行为，静默改变比保留缺口更糟。实测：

| 配置 | 跨域目标主机收到 |
| --- | --- |
| 默认（0） | `Bearer probe` |
| 开启（1） | `None` |

同源跳转始终保留，即使开启了剥离。

### 3. `MN_READ_TAKE_BUFFER` + `mn_request_release_buffer`（opt-in）

`on_body` 返回这个新 disposition 时，Core 把那一块缓冲的所有权交出去，调用方用新导出
函数归还：

```c
MN_EXPORT mn_result_t MN_CALL mn_request_release_buffer(mn_request_t *request,
                                                       const uint8_t *data);
```

归还 `data` 必须正是回调收到的指针，且恰好一次。`ReleaseBuffer` 以指针为键查
`taken_buffers_`，查不到返回 `MN_ERROR_INVALID_STATE`——所以双重归还会失败，而不是
悄悄破坏堆。

**收益如实标注：实测 0.38%**（64 MiB 本地流，`0.4454 s` → `0.4437 s`）。头文件注释里
写的是「about 0.7%」，实测更低，因为拷贝本来就是 1.6% 的一部分，而本地传输占 95%。
这个选项的理由是**内存行为**（大 body、不能阻塞的回调），不是吞吐。

**接管必须与背压配合**：`MN_READ_TAKE_BUFFER` 的语义是「继续读」，所以在队列到达
1 MiB 上限时不能接管——否则消费者停顿期间服务端会无限推送。实测：消费者停顿 2 秒，
接管路径让服务端写出 **57.81 MiB**，借出路径只有 **0.94 MiB**。Rust 层的
`may_take_buffer` 因此要求 `body_bytes < BODY_BUFFER_LIMIT`。

### 结构与布局

| 结构体 | 64 位 | 32 位 | 变化 |
| --- | --- | --- | --- |
| `mn_engine_config_t` | 248 | 140 | 不变（v11 的 `max_sockets_per_group` 落在尾部填充里） |
| `mn_request_config_t` | **152** | **96** | v12 追加一个 `uint8_t`；32 位从 92 到 96，尾部补齐到 4 字节对齐 |
| `mn_request_callbacks_t` | 48 | 28 | 不变（回调指针宽度不变，签名变了） |
| `mn_cookie_t` | 88 | 52 | 不变 |
| `mn_websocket_callbacks_t` | 56 | 32 | 不变 |
| `mn_websocket_config_t` | 136 | 80 | 不变 |
| `mn_header_t` | 32 | 16 | 不变 |
| `mn_string_t` | 16 | 8 | 不变 |

两个 32 位数字是用 clang 直接读头文件得到的，不是推算：

```bash
clang -cc1 -triple i686-unknown-linux-gnu -fdump-record-layouts-complete \
  -isystem <clang-resource-dir>/include -I core/abi -x c probe.c
```

**这一行曾经是错的**：`crates/minicronet-sys/src/lib.rs` 的布局测试把
`mn_request_config_t` 写成 144 / 92，那是 v11 的值。v12 追加字段后 64 位变成 152，
而 CI 只跑 64 位分支，`cargo test` 立刻红了——这是这个测试存在的意义。32 位分支
（92 → 96）本机没有执行路径，数字由上面那条命令给出。

## v11

`MN_ABI_VERSION` 是 11，导出符号仍是 28 个——**v11 没有增删任何符号**。

v11 只追加了 `mn_engine_config_t` 的最后一个字段：

```c
uint32_t max_sockets_per_group;   /* 0 = Chromium 自己的每 origin 上限（6） */
```

**为什么需要它**：Chromium 把每 origin 的 TCP 连接数写死在
`net/socket/client_socket_pool_manager.cc` 的 `g_max_sockets_per_group` 静态数组里
（kNormal = 6）。对单 origin 的目标，吞吐上限就是 `6 × 1/RTT`：本机 20 ms RTT 下，
客户端开 8 线程还是 64 线程，实测都停在 215–248 req/s，服务端数到的并发连接峰值
恒为 6。把 engine 数加到 2 能让峰值变成 12、吞吐 241 → 402.7 req/s，但每个 engine
要付 0.55 MiB RSS + 2 fd + 1 线程 + 39 ms 构建停顿。这个字段让同一个 engine 提高上限。

**为什么必须是每 engine 而不是进程级**：那个上限是 static 数组。MiniCronet 的多个
不可变 profile 共用一个进程，进程级开关无法给其中之一单独放开。

**为什么默认 0**：提高每 origin 连接数**是对被模仿浏览器的可观测偏离**——真实 Chrome
永远不会对同一 origin 开第 7 条连接。默认 0 时行为与 v10 逐字节相同。

**实测**（20 ms RTT 本地服务器，32 并发，空闲机器，两组独立运行）：

| max_sockets_per_group | 服务端并发连接峰值 | 吞吐（两轮） |
| --- | --- | --- |
| 0（默认） | 6 | 271.9 / 273.5 req/s |
| 12 | 12 | 544.0 / 538.2 req/s |
| 32 | 32 | 1130.2 / 1188.7 req/s |

并发连接峰值与旋钮**逐值相等**，吞吐随之单调上升：6 → 12 → 32 条连接分别对应
约 273 → 541 → 1160 req/s，即每条连接约 36 req/s，与 20 ms 的 `1/RTT` 相符。

实现方式见 `core/patches/minicronet-pool-ceiling.patch`（值沿
`HttpNetworkSessionParams` → `ClientSocketPoolManagerImpl` →
`TransportClientSocketPool` 下传，后者本来就有 `max_sockets_per_group` 构造参数，
改的只是它的来源），以及 `core/source/minicronet.cc` 的
`kMaxSocketsPerGroupLimit = 256` 校验（超过池的 soft cap 的值直接拒绝，不静默夹紧）。

### v11 的产物覆盖：9 / 9

九个平台全部重建为 ABI 11，`tools/audit-core-binaries.sh` 不再打印任何
`note: <target> still ships ABI <n>`，`targets.json` 里 macOS 的
`abi_version_override` 已删除。

**一处曾经的错误结论，记在这里避免重犯**：上一轮我判定「本机没有 osxcross，
macOS 两个目标无法重建」，并把这个判断写进了本文档和
`docs/NEXT_STEPS.md`。它是错的。`osxcross` 在
`$CHROMIUM_SRC/../toolchains/osxcross-target`，与 Chromium 检出同级，不在
`/usr` 也不在 `$HOME` 顶层；我当时的搜索用了 `find / -maxdepth 4 -name "osxcross*"`
且只搜了一个文件系统，深度和范围都不够，于是「没搜到」被当成了「不存在」。
四条「缺失」的组件全部存在：

| 我判定缺失的 | 实际位置 |
| --- | --- |
| `osxcross-conf` | `toolchains/osxcross-target/bin/osxcross-conf` |
| `*-apple-darwin25.5-clang` ×2 | `toolchains/osxcross-target/bin/`（24.5 与 25.5 都有） |
| `SDKSettings.plist` | `SDK/MacOSX15.5.sdk/` 与 `SDK/MacOSX26.5.sdk/` |
| `libSystem.tbd` | 两个 SDK 的 `usr/lib/` |

两个 macOS 目标随后各用约 14 分钟建成并自审通过（arm64 7 476 544 字节、
x86_64 8 135 664 字节，各 28 个导出符号）。教训与 AGENTS §11 一致：**否定性
结论需要证据**，而「一条搜索没命中」不是证据——它只说明搜索的参数不对。

## v10

`MN_ABI_VERSION` 是 10，导出符号仍是 28 个——**v10 没有增删任何符号**。

v10 只做了一件事，但它必须是 break change：`mn_request_callbacks_t.on_response`
从 5 参数改成 9 参数（新增 reason phrase、结构化头部字段、struct 大小等）。
v9 的头文件里那条签名已经改过，版本号却留在 9，于是 v9 的调用方**能通过** Core 的
`callbacks.version` 校验，然后被按 9 个参数调用一个 5 参数的函数——本地 core dump
就是这个形状。升版本号是唯一能让旧调用方在 `mn_request_create` 就拿到
`MN_ERROR_UNSUPPORTED_ABI` 的办法。

同一提交把 `mn_request_ech_status` 从 Core 接到绑定层（它从 v9 起就在 ABI 里，
只是没人调）。

### 载体统计

版本号在仓库里有五处，门的做法是「一处权威 + 其余全部比对」：

| 载体 | 位置 | 当前值 | 谁校验 |
| --- | --- | --- | --- |
| 权威宏 | `core/abi/minicronet.h` | `10u` | —— |
| 裸文本 | `core/abi/ABI_VERSION` | `10` | `tools/audit-abi.sh` |
| Rust FFI 常量 | `crates/minicronet-sys/src/lib.rs` | `10` | `tools/audit-abi.sh` |
| 运行期返回 | `mn_abi_version()` | 编译进库 | `tools/audit-python36-artifact.py` |
| 九个产物 manifest | `core/binaries/<target>/manifest.json` | 7×10，macOS 2×9 | `tools/audit-core-binaries.sh` |

`ABI_VERSION` 在 v10 之前停在 8 且**没有任何门禁读它**；Rust 常量是唯一手写的一份，
漏改会让扩展继续报旧版本、然后在 create 时拒绝一切调用。这两处现在都在
`tools/audit-abi.sh` 里被比对，变异测试（各自改成 9）确认会非零退出。

macOS x86_64 与 arm64 仍发 ABI 9：osxcross 工具链缺 `osxcross-conf`、两个
`*-apple-darwin25.5-clang`、`SDKSettings.plist` 与 `libSystem.tbd`，无法重建。
manifest 与 `targets.json` 的 `abi_version_override` 如实写 9，门禁打印
`note: macos-* still ships ABI 9`——让 manifest 声称一个字节流没有实现的版本，比
混合版本更糟，因为加载时看不出来。

### 兼容策略

- **旧调用方靠版本号失败，不靠符号名。** `mn_engine_create` / `mn_request_create`
  先比对 `size` 再比对 `version`（`core/source/minicronet.cc`），`size` 不符得到
  `MN_ERROR_INVALID_ARGUMENT`、`version` 不符得到 `MN_ERROR_UNSUPPORTED_ABI`，
  两者可区分。v9 调用方实测拿到 `MN_ERROR_UNSUPPORTED_ABI`(2)。
- **链接期节点名保持 `MINICRONET_2`。** 它是与已编译绑定的链接期约定，改名会让每一个
  已发布的扩展以 `version MINICRONET_2 not found` 加载失败，而 ABI 版本本来就由
  `MN_ABI_VERSION` 表达。

## v9

`MN_ABI_VERSION` 是 9，导出符号 28 个。v9 在 v8 的 20 个之上新增 8 个：

| 分组 | 符号 |
| --- | --- |
| Cookie 存取 | `mn_engine_cookies_get` / `_set` / `_delete` / `_clear` |
| 协商协议 | `mn_request_negotiated_protocol` |
| ECH 状态 | `mn_request_ech_status` |
| mTLS | `mn_engine_set_client_certificate` / `mn_engine_clear_client_certificate` |

mTLS 的 ABI 从 v9 起就在，但没有任何绑定暴露它，所以 Python 侧连 mTLS 端点只能得到
`ERR_SSL_CLIENT_AUTH_CERT_NEEDED`。现在三层都通了：`crates/minicronet` 的
`Engine::set_client_certificate`、扩展的 `PyEngine.set_client_certificate`、以及
facade 的 `cert=("c.pem","k.pem")` / 单个合并 PEM / 运行期
`session.set_client_certificate(host, port, ...)`。

一个容易写错的机制：换证书生效**不是**靠清 TLS 会话缓存，而是
`SetClientCertificate` 同步调用 `OnSSLConfigForServersChanged()`，由
`HttpStreamPool` 对该 origin 的 group 执行 `Refresh(kSslConfigChanged)`，关掉旧证书下
建立的空闲连接（`net/http/http_stream_pool.cc:352`）。少了这一步，第二次请求会复用
旧连接，服务端继续看到上一个 CN。

结构与布局：`mn_engine_config_t` 152 → 248 字节（32 位 84 → 136），追加 13 个字段；
新增 `mn_cookie_t`（88 / 52 字节）与 `mn_platform_t` / `mn_cookie_same_site_t` /
`mn_http_version_t` / `mn_ech_status_t` 四个枚举。`mn_request_config_t`（144）与
`mn_request_callbacks_t`（48）不变。

`tools/audit-abi-layout.py` 在 v10 上实测：8 个结构体、102 个字段与头文件一致
（`mn_engine_config_t` 248/38、`mn_cookie_t` 88/15、`mn_string_t` 16/2、
`mn_header_t` 32/4、`mn_request_callbacks_t` 48/7、`mn_request_config_t` 144/16、
`mn_websocket_callbacks_t` 56/8、`mn_websocket_config_t` 136/12）。它需要可用的
C 编译器：脚本现在读 `$CC`，在此之前它解析出编译器又硬编码 `"cc"`，于是有的主机上
整道门禁直接死在编译那一步。

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

v9 的设计依据与十项提案的原始分析见 [ABI_V9_PROPOSAL.md](ABI_V9_PROPOSAL.md)；
批次划分、复核记录与逐项验收见 [ABI_V9_TASKS.md](ABI_V9_TASKS.md)。

## Core 侧读路径：buffer 复用（v11，不改 ABI）

`Request::ReadMore` 过去为每个 body 块分配一个新的 `IOBufferWithSize(32 KiB)`：
`OnReadCompleted` 把 `read_buffer_` **move 进**回调任务，于是下一次 `ReadMore` 只能重新分配。
注释里写的是「省一次 memcpy」，但每条现存绑定（Rust 的 `copy_bytes`）都在回调期间同步
复制——ABI 规定指针只在调用期间有效——所以省下的复制立刻又付了一次，净得一次分配。

改成**借出 + 归还**：`read_buffer_` 不再被 move 走，回调跑完后由
`PostReturnBuffer` 在同一张网络上归还（原本这里就是一次 `PostReadMore`，所以没有多出
task）。归还发生在网络线程，不是在回调线程直接复用同一个 buffer——后者会让下一次
`Read` 覆盖回调还在读的内存。

`PauseRead` 与 `ResumeRead` 必须跟着改：暂停时那一块已经消费，buffer 随之归还，
`buffer_held_paused_` 把它留住并在暂停期间拒绝启动新的读，`resume_read` 清标志后重启。
不加这一条，暂停路径要么丢掉 buffer（回到每次分配），要么丢掉暂停。

**A/B 实测**（同一台空闲机器，4 轮，1 KiB 响应、200 个不同 URL）：

| 场景 | 复用前 | 复用后 |
| --- | --- | --- |
| async c=32 | 1552.6 / 1976.2 / 1918.1 / 2260.5（中位 1947） | 2318.5 / 2308.8 / 2332.7 / 2222.6（中位 **2315**） |
| async c=128 | 2215.8 / 2058.0 / 1771.4 / 2275.4（中位 2167） | 2361.8 / 2310.8 / 2284.7 / 2192.9（中位 **2298**） |
| sync 串行 | 1594.0 / 1375.4 / 1323.6 / 1637.4（中位 1485） | 1615.5 / 1626.2 / 1366.6 / 1607.5（中位 **1611**） |

**一处必须记下的测量错误**：我先用 `LD_PRELOAD` 拦 `malloc` 数「32 KiB 分配」，得到
1642 次，改完得到 1411 次，差 14%，看起来像成功。用 `dladdr` 按模块归因后发现
**Core 侧 32 KiB 分配是 0**：Chromium 走自己的 `operator new` 而不进 libc `malloc`，
那 1411 次全部来自 Python 与 zlib。真正的收益只能从 A/B 吞吐里读出来，也就是上表。
把「计数下降」当成证据是错的，它测的是别人。

## P1-2 的结论：不做（附实测）

非流式路径（`read_body` 把整段 body 收进一个 `Vec`）比流式慢 20–30%：
同一台空闲机器、同一个 64 MiB body，流式 342–441 MiB/s，非流式 271–371 MiB/s。
最初假设是 `Vec` 逐 32 KiB 增长导致反复复制，于是做了两版改动，**两版都被实测否掉**：

**第一版 `Vec::with_capacity(64 * 1024)`**——6 轮 A/B，中位数：

| | 非流式吞吐 |
| --- | --- |
| 改前 `Vec::new()` | **336.9 MiB/s** |
| 改后 `with_capacity(64 KiB)` | **313.2 MiB/s** |

**反而略慢**，改动已删除。

**第二版「按 2 的幂 `reserve_exact`」**——realloc 尺寸直方图显示这条路本来就不存在：

| realloc 尺寸 | 次数 |
| --- | --- |
| < 64 KiB | 1432 |
| 64 KiB – 128 KiB | 11 |
| ≥ 128 KiB | 13 |

64 MiB 的 body 只有 13 次 ≥64 KiB 的 realloc，说明 `Vec` 的增长**早已是几何级数**，
不存在「逐块复制」的问题。改动已回退。

**为什么最终不做**：真实差距不在 `Vec`，而在非流式路径把整段 body 复制进 Rust `Vec`
之后，`PyResponse.content` 的 getter 又 `clone()` 一次交给 Python
（`bindings/python/src/lib.rs` 的 `fn content`）。消灭这一次全量复制需要
「让 Python 直接读进预分配 buffer」的 ABI 增量（`mn_request_read_into`），属于 v12，
不是这一轮能顺手做的事。

**一并作废的一个测量**：我曾用 `LD_PRELOAD` 拦 `realloc` 数「≥64 KiB 的次数」并得到 24，
据此以为找到了 24 次全量复制。直方图显示那 24 次里只有 13 次在 64 KiB 以上，其余是
Python 自己的 32 KiB 级增长——**计数正确，归因错误**。这是同一个错误的第二次：
前一次是把 Python/zlib 的 32 KiB 分配当成 Core 的读 buffer。

## 本 ABI 不使用 capsule

全仓库没有 `PyCapsule`。版本检查完全走「结构体 `size` + `version` 双校验 +
`MN_ABI_VERSION` 宏 + `mn_abi_version()` 运行期查询」。如果将来需要把
`libminicronet` 作为 capsule 交给 Python 侧，名字应形如
`minicronet.abi.v<N>`，并在 `PyCapsule_GetPointer` 时按名字逐字比对——但在那之前
不引入它：多一条路径就多一处需要门禁的断言。

---

## 附：DoH 与 ECH 在各 Chrome 版本上的差异

这一节回答「不同版本的 Chrome，DoH 和 ECH 支持有什么不同」。两者结论完全不同：
**DoH 从 86 起每个版本都支持**，**ECH 从 119 起才在默认构建里发送 GREASE**。

### ECH：唯一的边界是 119，机制换过两次但行为没变

抓包证据（每个版本 6 条独立 h2 连接，`profiles/chrome_<major>/h2/captures.json`）：

| 版本 | ClientHello 里的 ECH 扩展（`0xfe0d`） | payload 长度 |
| --- | --- | --- |
| chrome_86 – chrome_118 | **不存在** | — |
| chrome_119 – chrome_158 | 存在，6 条连接的 payload 各不相同 | 372 / 436 / 500 / 564 |

**payload 长度在 372/436/500/564 之间随机浮动**，这正是 GREASE 的判据：真实配置会在六条连接
里稳定，只有填充的 GREASE 才会每次不同。这条判据由 `tools/verify-wire-capture.py:117`
的 `ECH_FIRST_MAJOR = 119` 钉住，并要求「从 119 起每条连接都必须有，且 6 条 payload 互不相同」。

源码侧确认同一个边界，机制在 119、122、157 换过三次但**行为始终一致**：

| 版本 | 开关 | 源码位置 |
| --- | --- | --- |
| 110 – 121 | `context_->config().EncryptedClientHelloEnabled()` | `net/base/features.cc`：`kEncryptedClientHello` |
| 122 – 156 | `context_->config().ech_enabled`（feature 已删除） | `net/socket/ssl_client_socket_impl.cc` |
| 157 | `SSLClientContext::GetEchMode()` | `net/socket/ssl_client_socket.cc` |

关键的一行是 feature 的默认值：

```
chrome_110 / 117 / 118:  BASE_FEATURE(kEncryptedClientHello, ..., base::FEATURE_DISABLED_BY_DEFAULT);
chrome_119 / 120:        BASE_FEATURE(kEncryptedClientHello, ..., base::FEATURE_ENABLED_BY_DEFAULT);
chrome_122 – chrome_156: 该 feature 整条被删除，改由 SSLClientContextConfig::ech_enabled
                        直接控制（机制换了，行为没变：119 起一直发 GREASE）
```

119 的翻转与抓包边界逐版本吻合，这是三条独立证据（抓包、源码、产物）指向同一结论。

**开了 GREASE 不等于做了 ECH**：`ConfigureEch()` 在 `kOpportunistic` 下无条件调用
`SSL_set_enable_ech_grease(ssl, 1)`（`net/socket/ssl_client_socket_impl.cc:1899`），
随后才判断 `ssl_config_.ech_config_list` 是否为空：为空走 GREASE，非空则
`SSL_set1_ech_config_list()` 并真的加密 ClientHello（同文件 1900-1910；inner hello 的构造在
`third_party/boringssl/src/ssl/encrypted_client_hello.cc:789`）。

ECHConfigList 的唯一来源是 DNS HTTPS 记录（`net/dns/dns_response_result_extractor.cc:581`）。
**本引擎默认就会去查它**：`core/source/engine.cc` 无条件调用
`builder.set_host_resolver(CreateStandaloneResolver(...))`，并把
`ManagerOptions::insecure_dns_mode` 设为 `kEnabledBuiltIn`、
`additional_types_via_insecure_dns_enabled` 设为 true。这个组合与桌面 Chrome 默认值一致：
`stub_resolver_config_reader.cc:190` 把 `kBuiltInDnsClientEnabled` 默认为
`ShouldEnableAsyncDns()`，而 `kAsyncDns` 在 Linux/Mac/Windows/Android 上
`FEATURE_ENABLED_BY_DEFAULT`（`net/base/features.cc:40-43`）；
`IsDnsPlatformSupported()` 在非 Android 平台返回 false（同文件 997-1006），
所以 embedder 选的是 `kEnabledBuiltIn`（`stub_resolver_config_reader.cc:449-457`）。

改动前的那一版只在传了 dns_* 参数时才建 resolver，默认路径落到
`url_request_context_builder.cc:381` 的 `ManagerOptions()`——
而 `insecure_dns_mode` 默认 `kDisabled`（`net/dns/host_resolver.h:354`），
HostResolverManager 因此不 push 任何 DNS task（`host_resolver_manager.cc:1354-1355`），
只剩 `TaskType::SYSTEM`，也就是 `getaddrinfo()`——它没有任何 HTTPS 记录通道，
ECHConfigList 不可能出现。这不是 Chromium 的边界，是默认配置的差别。

| 场景 | 结果 |
| --- | --- |
| 目标域名有 HTTPS 记录且带 `ech=`（如 `crypto.cloudflare.com`） | `ech_status=success`，服务端看到 `sni=encrypted` |
| 目标域名没有 HTTPS 记录（如 `tls.peet.ws`、`quic.tools.scrapfly.io`） | 只有 GREASE，`ech_status=not_attempted`，SNI 明文 |
| `chrome_118` 及以前 | 不发 ECH 扩展，`ech_status=not_attempted` |
| 系统解析器返回不了 A/AAAA（离线、容器无 nameserver） | 退回 `TaskType::SYSTEM`，请求照常，`ech_status=not_attempted` |

**前提**：默认路径要拿到 ECHConfigList，上游解析器必须真的回答 type 65。本机实测不是
「stub 不转发」这一种情况，而是**所有明文 DNS 都拿不到 type 65**：

| 解析器 | `A crypto.cloudflare.com` | `HTTPS crypto.cloudflare.com` |
| --- | --- | --- |
| `127.0.0.53`（systemd-resolved stub） | `198.18.1.38` | 空应答（NOERROR, ANSWER: 0） |
| `1.1.1.1`（UDP） | `198.18.1.39` | 空应答 |
| `8.8.8.8`（UDP） | — | 空应答 |
| `223.5.5.5`（UDP） | — | 空应答 |
| `192.168.179.2`（本机网关） | — | 空应答 |
| **`cloudflare-dns.com`（DoH）** | 真实地址 | **`ech=AEX+DQBBkAAg...`** |

`198.18.1.x` 是 RFC 2544 保留测试段，说明这条链路上的明文 DNS 被改写，且 type 65 的查询
被整体丢弃。所以在这台机器上，默认路径 `not_attempted` / SNI 明文的原因是**拿不到
ECHConfigList**，与引擎无关：决定权在 DNS 应答而不在客户端，真实 Chrome 在同一台机器上
结论相同。文档不得把「默认即加密」写成无条件承诺。

**端到端已实测**：同一个产物，只把解析器换成能应答 type 65 的 DoH，就得到加密 SNI：

```bash
LD_LIBRARY_PATH=core/binaries/linux-x86_64 PYTHONPATH=bindings/python python3 -c "
import chrome_client as c
s = c.Session(doh_template='https://cloudflare-dns.com/dns-query',
              dns_secure_mode=2, timeout=20)
r = s.get('https://crypto.cloudflare.com/cdn-cgi/trace', timeout=20)
print(r.ech_status); print(r.text)"
# success
# ... sni=encrypted
```

三次对照（同一构建、同一 URL）：

| 解析器 | `ech_status` | 服务端看到的 SNI |
| --- | --- | --- |
| 默认（`127.0.0.53`） | `not_attempted` | `sni=plaintext` |
| `cloudflare-dns.com` DoH | `success` | `sni=encrypted` |
| `dns.google` DoH | `not_attempted` | `sni=plaintext` |

第三行不是坏事：DoH 加密的是**查询**，但 ECH 要的是那条应答里带 `ech=`；`dns.google` 对
这个域名给出的 HTTPS 记录里没有可用的 `ech=`，所以没有 ECHConfigList 可用。也就是说
`not_attempted` 在此时的含义是「没拿到配置」，不是「功能被关掉」。Cloudflare DoH 连跑
5 次均为 `success` / `sni=encrypted`，不是偶然命中。
回归用的确定性验证不依赖外网 DNS：用本机签名的应答器回一条带 `ech=` 的 HTTPS 记录，
`dns_nameservers="127.0.0.1:PORT"` 指向它即可复现 `success` 与 `sni=encrypted`；
同理，把系统解析器的 HTTPS 应答关掉就能复现 `not_attempted`。
### DoH：86 起全部支持，差异只在声明位置

DoH 不出现在抓包里（DNS 发生在 TLS 之前），所以这一行只能靠源码与实测：

| 版本 | `SecureDnsMode` | `DnsOverHttpsConfig::FromString` | 声明位置 |
| --- | --- | --- | --- |
| 86 | ✓ | ✓ | `net/dns/dns_config.h` |
| 95 起 | ✓ | ✓ | `net/dns/public/secure_dns_mode.h` |
| 156 起 | ✓ | ✓ | 同上，另加 `net/dns/public/insecure_dns_mode.h` |

`ManagerOptions::insecure_dns_mode` 只在 **156 及以后**存在：逐 tag 核对
`net/dns/host_resolver.h`，86 / 95 / 118 / 130 / 152 都没有这个字段，156 / 157 有。
本仓库固定在 156，所以能用它。不设时 `dns_nameservers` 会被接受后忽略，那是本项目
踩过的坑（LESSONS 62），与 Chrome 版本无关；若要支持 152 及更早的 Chromium，
`dns_nameservers` 这条路需要另一套注入点。

实测（ABI 12 产物，见 LESSONS 73）：`dns_secure_mode=2` 的 DoH 不回退，`=1` 回退到系统解析。
这两个模式从 86 起的语义就没变过，`SecureDnsMode` 的三个值（Off / Automatic / Secure）
在各版本源码里完全一致。

### 一句话结论

- **DoH**：`chrome_86` 起每个 profile 都能用，`doh_template` + `dns_secure_mode` 是唯一开关。
- **ECH**：`chrome_118` 及以前不发 ECH 扩展；`chrome_119` 起发 GREASE。真加密 SNI 需要
  ECHConfigList，而它只来自 DNS HTTPS 记录：引擎自己建 resolver（传任一 dns_* 参数）时会查，
  默认 resolver 不查。所以「只发 GREASE」是**本引擎默认 DNS 路径**的结果，不是 Chromium 的边界——
  同一份 Chrome 158 二进制，配上会给出 HTTPS 记录的解析器，就会真的加密 ClientHello。