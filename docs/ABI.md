# C ABI boundary

`core/abi/minicronet.h` is the single source of truth. No Chromium Cronet
headers are exposed. Opaque `mn_engine_t`, `mn_request_t`, and
`mn_websocket_t` handles are reference counted by the Core; Rust is the only
high-level owner exposed to language adapters.

## v11（当前 ABI）

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
