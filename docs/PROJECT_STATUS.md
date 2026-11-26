# chrome_client 项目状态

记录时间：2026-10-22。本文覆盖已完成、进行中、已知缺陷和后续计划。数字都来自本机
实测，可用 `tools/` 下的脚本复现。

## 项目定位

用真实 Chromium 网络栈实现的 HTTP/WebSocket 客户端，提供 requests / curl-cffi 风格
的 Python API，同时让 TLS 指纹、HTTP/2 SETTINGS、QUIC 参数与真实 Chrome 一致。

分层：`libminicronet`（Chromium C++，唯一协议实现）→ C ABI v16（35 个导出符号）→
`minicronet-sys`（手写 FFI 声明）→ `minicronet`（Rust 安全层）→ 各语言薄绑定。

- Chromium revision：`58bcc148f76b8975bbe59f1a6012d4ae409d62a9`（Chrome 156.0.8078.31 stable）
- Python 发布版本与 crate 版本同号（不再有四段版本号映射）；具体版本号只在根
  `Cargo.toml` 的 `workspace.package.version` 里，本文件不再复述，以免再次漂移
- Chrome profile：`chrome_86` — `chrome_158`，73 个

## 已完成

### 阶段 0：可复现构建与基线

- 从 `new/` 迁入 12 个 Chromium 补丁（`core/patches/`，现为 14 个）、8 个平台构建脚本
  （2026-11 增至 9 个，见「第九个目标」）、
  3 个平台审计脚本、FeatureList 审计、profile 表生成器
- `sync-core.sh` 重写为本仓库布局，逐个校验补丁已应用，半应用状态直接失败
- **可复现性成立**：用 08-23 版源码重建得到与已发布 wheel 逐字节一致的产物；
  连续两次强制重编译哈希相同。增量重建约 23 秒
- 新增 `tools/bench-core-baseline.py`，其中 `stalled_consumer_isolation` 是背压
  工作的验收探针
- 基线写入 `docs/BASELINE_LINUX_X86_64.md`

### 阶段 1：ABI v8 pause/resume 背压

修掉了一个线上故障：v7 在 Rust 侧 `on_body` 里用 Condvar 阻塞 Core 回调线程，而
发布的二进制又早于「每请求独立 callback runner」的源码改动，用的是进程级唯一
sequenced runner。**一个消费者停在 1 MiB 水位就让同进程后续请求永久不返回，连
timeout 都失效**——超时的完成回调排在同一条被堵住的队列里。

ABI v7 → v8 只增加一个符号（19 → 20）：

```c
typedef mn_read_disposition_t (MN_CALL *mn_request_body_fn)(...);  /* PAUSE / CONTINUE */
mn_result_t mn_request_resume_read(mn_request_t *request);
```

用回调返回值而非独立 `pause_read` 是为了消除竞态。Core 侧三态 `read_state_`
（reading / paused / resume_requested）吸收「resume 比 pause 先到」的交错。

验收：慢消费者隔离在两个 Engine 上均 20/20 完成，而 v7 是永久挂死。吞吐反而优于
v7：同步顺序 738.8 → 804.6 req/s，流式 148.5 → 150.9 MiB/s。

### 阶段 2：IDN 崩溃修复与 ICU 数据内嵌

排查 Windows wheel 那份 10.8 MB `icudtl.dat` 时发现 Core 从不调用
`base::i18n::InitializeICU()`，但 ICU 仍被链进依赖图。后果是
`chrome_client.get("http://例え.テスト/")` 触发 SIGTRAP 打掉宿主进程。

三种数据集实测：完整 10,876,560 / IDNA+全部转换器 6,276,640 / **仅 IDNA 191,056**。
（后续发现「仅 IDNA」会切断 Basic/Digest 的 realm 解码，当前数据集为 194,064 —— 见
本文「代理认证修复」一节。）
字符集转换器占 97%，而 Core 从不调用它们，所以只保留 IDNA 并用
`icu_use_data_file = false` 编入库中。

- 各平台库增加 190–287 KB
- **Windows wheel 净减 10.69 MB**（不再携带外挂数据文件）
- 仓库删除 3 份共 32 MB 的死数据
- IDN 现在与其 punycode 形式行为一致

### 阶段 2 续：`is_cfi = false`

Chromium 只对 `is_official_build && is_clang && linux-x64` 默认开 CFI
（`sanitizers.gni:58`），`use_cfi_icall` 的 cflags 也嵌在 `if (is_cfi)` 里，所以这个
开关只对 linux-x86_64 有效。

- **linux-x86_64 减 147,456 字节**（9,320,288 → 9,172,832，−1.58%），其中 123,737
  字节是 `.text` 里的 `cfi-vcall` 跳转表
- 吞吐无可测量变化：同会话交替三轮 A/B，四项指标区间全部重叠
- 其余 7 个目标零改动，已在 linux-arm64、windows-x86_64、macos-arm64 上验证加上
  这一行后 `gn gen` 生成的 ninja 文件逐字节相同，因此不必重建
- `tools/audit-core-linux.sh` 上限从 9,400,000 收到 9,250,000

这是安全性换体积：没有 `cfi-vcall`/`cfi-icall`，网络栈一旦出现内存破坏漏洞，劫持
虚表或函数指针的门槛就降低。用户已确认接受。

### 阶段 3 首项：删除 `Engine::callback_runner()` 死代码

Request 与 WebSocket 在 ABI v8 里各自建独立 sequenced runner，`Engine` 上那条通往
`Runtime` 的进程级 runner 已无调用方。删掉两个访问器、一个成员和 `Runtime` 构造里
那次 `CreateSequencedTaskRunner`，8 个平台全部重建。

- 体积基本不变：只有 linux-x86 少 64 字节，其余 7 个目标各段大小一字不差；被删代码
  不在导出表里，剩下的指令差被 `.text` 对齐填充吃掉
- 8 份产物的 SHA-256 全部变化，manifest 已刷新
- 收益是每进程少一个没人用的 sequenced task runner，以及少一条误导性线索
- 顺带发现：`FROM_HERE`/`CHECK` 把 `__LINE__` 编进产物，纯加一个空行就让哈希变化
  6 个字节（每处 +1）。做 A/B 实验前必须先对齐源码行号

同一轮还给 `net/features.gni` 的 `enable_websockets` 加了意图注释。原先记为「逻辑
写歪了」，重核后不成立：两个 arg 相互独立，四种组合结果都正确，第二个操作数是
防御性的（C ABI 导出 `mn_websocket_*`，WebSocket 必须编进来）。它在 8 个发布配置下
恒真冗余，不是错误。注释零影响已验证：1,674 个 `.ninja` 文件逐字节相同。

### 阶段 3 第二项：并发倒挂已定位，是基准的缺陷

「并发 128 比并发 32 慢一倍」挂了三个阶段，原先怀疑网络线程和 `poll_event`。都不是。

用安全 Rust 层写一个无 Python 的多线程压测，倒挂照样出现（并发 8 → 128 是
1,360 → 510 req/s），排除绑定层。网络线程确实稳定占单核 88–93%、限高约
2,250 req/s，但加 Engine 也不加吞吐（1/2/4 个 Engine 都是约 2,250），而无缓存竞争时
并发 128 并不掉，所以它只是限高，不产生倒挂。

真正的原因是 **`HttpCache` 按 cache key 串行**。固定并发 128 只改不同 URL 的数量，
吞吐从 492（1 个）线性升到 1,604 req/s（8 个）后撞上限高。让响应带
`Cache-Control: no-store` 仍然倒挂，说明不是写缓存的开销，而是同 key 事务在
`ActiveEntry` 上排队。并发超过不同 URL 数之后只增加队列深度。这是 Chromium 的缓存
语义，真实 Chrome 一样，不是缺陷。

缺陷在基准：`tools/bench-core-baseline.py` 让 200 个并发请求全打同一个 URL。改成
每请求带 `n=` 序号后，并发 32 从 737 升到 1,294 req/s、并发 128 从 377 升到
1,227 req/s。`--single-url-concurrency` 保留用于故意复现竞争。这是「先做对照再下
结论」那条教训的第二次出现，第一次是 40 ms 延迟 ACK。

顺手试过让 asyncio 的 `notify` 每次唤醒排空至多 64 个事件，插桩显示实际每次唤醒
96% 只有 1 个事件可取，零收益，已丢弃。

### 156 内核重建的一轮体积精简（8 平台 −376,896 字节）

升级完成后 Windows x86_64 距上限只剩 3,328 字节（0.03%），任何新增 profile 都会顶破
门禁。定位到 `//third_party/simdutf` 显式用 `optimize_speed` 覆盖了本项目的
`optimize_for_size`（与先前 Windows 那次 `-O2` 是同一类缺陷：不是漏配，而是某个 target
把尺寸档换成速度档，只看总字节数的门禁发现不了）。simdutf 在 Core 里只经
`net::Base64Decode` 到达，不在吞吐热路径，因此让它在该构建下改走 `-Oz`。

实测 8 平台合计 **−376,896（−0.53%）**，linux-x86_64 −102,400、macos-x86_64 −106,496、
windows-x86_64 −97,280。三档上限按既有规则收敛到 Linux 9,050,000 / Windows 10,510,000 /
macOS 8,710,000，**最小余量从 0.03% 回到约 2%**（规则是「最大产物 × 1.02 向上取整到 1 万」，
换算成占上限的百分比会因取整略低于 2%）。（这一轮之后的三次重建——BoringSSL 尺寸档、
156 内核、chrome_86..chrome_98——又按同一条规则把三档收到 Linux 8,540,000 /
Windows 9,610,000 / macOS 8,120,000，见下文「当前默认上限」。）

验收：smoke、125 个 Python 测试、base64 专项（本地服务端逐字节比对 Basic 凭据）、
指纹（JA4 / peetprint / Akamai 与改动前逐字节相同）、以及 base64 路径的 A/B
（356.3/326.0 → 344.8/338.0 req/s，区间重叠）。同批次实测后**否决**了 zstd 的同类改动
（`-Oz` 反而 +69,632，解压是真实热路径）与关闭 libc++ hardening（省 73,728 但属安全
取舍）。详见 `docs/CORE_BINARY_SIZE_PLAN.md` 第七节。

### chrome_86 – chrome_98 已加入（72 个 profile）

原有范围是 `chrome_99`–`chrome_157`。本次把基线往前推到 86，**每个版本用自己的 tag
源码与自己的抓包推导**，没有从 99 继承常量：

- `tools/collect-profile-evidence.py` 增加 `PATH_ERAS`：86–92 的 BoringSSL 扩展实现在
  `ssl/t1_lib.cc`（当时还没有 `extensions.cc`），QUICHE 在 152 之前用 `quic/` 前缀。
  没有这张表时这些文件 404，采集器会把「该文件当时不存在」和「抓取失败」混为一谈。
- 新增 `tools/derive-profile-from-capture.py`：只读该版本自己的抓包与源码行，写出前做
  双向交叉校验（wire↔source、wire↔wire）。用同一工具重推 `chrome_99` 与仓库已提交条目
  逐字段一致，作为方法校准。
- 实测边界（全部双向确认）：ALPS 从 91 起（86–90 的 `features.cc` 没有该 feature，
  `http_network_session.cc` 也没有 `application_settings_` 赋值）；padding 扩展长度
  在线缆上稳定为 h2 86–90=418、91–97=400、98–118=408（h1 分别 424/424/432）。
  **这组数字是 BoringSSL 那条 512-total 规则的输出，不是配置值**：profile 表的
  `client_hello_padding_length` 对全部 72 个 profile 都是 0，由
  `tools/audit-padding-rule.py` 的两半断言（表侧必须为 0，线缆侧必须是上述宽度）。
  86–97 比 98–118 宽 10 字节，来源是 `supported_versions` 里多出的 TLS 1.0/1.1 两个字。
- 验收证据见 [`CHROME_86_157_SUPPORT_MATRIX.md`](CHROME_86_157_SUPPORT_MATRIX.md)。

### chrome_156 与 chrome_157 已加入（59 个 profile）

抓包各 5 条完整握手连接，且**带精确版本**：156 是 `156.0.8078.80`，157 是
`157.0.8103.39`（此前 152–155 的抓包只暴露主版本号）。源码证据取自各自同名 tag。

**必须写明的一处不一致**：Core 仍编译自 `58bcc148f76b`（`156.0.8078.31`），而 156/157
的 profile 审计于 `fb096c0011f9`（`156.0.8078.80`）与 `9a31f0acec18`（`157.0.8103.39`）。
`source-evidence.json` 里同时记录 `chromium_commit`（审计用）与
`compiled_core_chromium_commit`（编译用）两个字段，使这处差异可被机器读出。
它不改变线缆行为：156 的 12 份审计源码在 `.31` 与 `.80` 两个 tag 上**逐字节相同**
（已实测），所以 `.31` 编译的 Core 产出的就是 `.80` 抓到的字节。

wire gate：各 5 条连接、5 个源端口，全部稳定字段一致，`wire_verified: true`。
与 chrome_155 相比，**de-GREASEd cipher、supported groups、signature_algorithms、
扩展集合、HTTP/2 指纹与 28 个 trust-anchor ID（186 字节 wire contents）全部不变**，
唯一 wire 差异是 UA 主版本。生成条目的 `tls`/`http2`/`http3`/`alpn`/`websocket`
五组与 155 逐字节相同；不同的是 BoringSSL/QUICHE revision、`features.cc` 尺寸与审计行号。

157 的审计源码有 6 份与 156 不同（`boringssl_extensions`、`features`、`http2`、`http3`、
`quiche_h3` 及其他），说明上游确实在演进；但**这些变化都不落在 wire 可见的稳定字段上**，
这正是 `tools/build-profile-entry.py` 会重新推导并比对、而不是直接继承的原因。

一处限制：两个 profile 各自只有一个构建可审计，做不到 155 那样的首尾 tag 比对，因此
`patch_release_comparison.identical_across_patch_releases` 记 `null` 而非编造结果。

证据与复现命令见 `profiles/chrome_156/README.md` 与 `profiles/chrome_157/README.md`。

### chrome_158 已加入（73 个 profile）

抓包来自 `158.0.8127.37`，h1/h2/h3 **各 6 条**完整连接（不是 156/157 的 5 条）。
源码基准按 AGENTS §9 取该大版本**最新 tag** `158.0.8127.99`（commit `a67074ee`），
而不是抓包所在的那个 patch tag；这两个事实都记录在 `profiles/chrome_158/source-evidence.json`。

**必须写明的一处不一致**：抓包是 `.37`，源码基准是 `.99`。实测两个 tag 的差异是局部的——
`boringssl_revision`（`ea9f3a34`）与 `quiche_revision`（`132bfd90`）**完全相同**；
九份审计源码里八份逐字节相同，只有 `base/rand_util.cc` 变化。该文件承载的是 QUIC
随机源信号（`quic_random`），不是线缆可观测字段，因此 `.99` 的审计结论适用于 `.37` 的抓包。

wire gate：三协议各 6 条、6 个不同 session，稳定字段六条全等，随机字段（client_random、
session_id、扩展顺序、GREASE）六条互不相同；trust-anchor ID 与表 28/28 一致。

与 chrome_157 相比，**de-GREASEd cipher 列表、扩展集合、HTTP/2 指纹
（`1:65536;2:0;4:6291456;6:262144|15663105|0|m,a,s,p`）、supported_versions、
trust-anchor ID 全部不变**，A 级差异只有 UA 主版本与 BoringSSL/QUICHE revision。

三级证据链全部通过：`--levels 1`（tag/DEPS/12 份源码哈希）、`--levels 2`（三协议各 6 条）、
`--levels 3`（真实产物 6 次真实请求，`live_connections: 6`）。
验收记录见 [`CHROME_158_SUPPORT.md`](CHROME_158_SUPPORT.md)，证据与复现命令见
`profiles/chrome_158/README.md`。

### chrome_154 与 chrome_155 已加入（57 个 profile）

两条官方 stable 分支各取首尾 tag 审核：154 是 `154.0.8037.17` 与 `154.0.8037.100`，
155 是 `155.0.8059.12` 与 `155.0.8059.40`。155 的 12 份审计源码逐字节一致；154 只有
`net/quic/quic_chromium_client_session.cc` 差 2 字节，且**所有审计信号行完全一致**，
因此审计面是分支稳定的。

wire gate 都通过：154 用 6 条独立连接、155 用 5 条，全部稳定字段一致，stochastic
字段每条连接都不同。与 chrome_153 相比，**de-GREASEd cipher 列表、supported groups、
signature_algorithms、扩展集合、HTTP/2 指纹与 28 个 trust-anchor ID 集合都不变**，
唯一差异是 UA 主版本；28 条 ID 的顺序不同，那是 absl::flat_hash_set 的进程内属性，
不是版本差异（机制见本节「trust_anchors 载荷」一段）。因此两个 profile 复用
chrome_153 的 `tls` 语义常量，只 pin 各自的版本与 BoringSSL/QUICHE revision。

154 与 155 的 `net/base/features.cc` 里 `kAlpsForHttp2`、`kTryQuicByDefault`、
`kTLSTrustAnchorIDs`、`kNonMtcTrustAnchorIDs`、`kTlsGreaseSigalgs` 仍为
`FEATURE_ENABLED_BY_DEFAULT`，`kEnableTLS13EarlyData` 仍为 disabled，网络特性快照
区间（123–155）与 chrome_153 相同。证据与复现命令见
`profiles/chrome_154/README.md` 与 `profiles/chrome_155/README.md`。

### chrome_153 已加入（历史记录）

官方 stable 分支首尾 tag `153.0.8010.12` 与 `153.0.8010.37` 的 12 份审计源码
逐字节一致。5 条独立 Chrome 153 连接通过 wire gate：5 个源端口，所有稳定字段一致，
Client random、session ID、扩展顺序、GREASE、key share 与 ECH 均至少 5 个不同值。
与 chrome_152 相比，TLS/H2 wire 指纹不变，trust-anchor ID 集合从 32 个收到 28 个，变化是 UA 主版本
与对应的 Chromium/BoringSSL/QUICHE source pin。证据与复现命令见
`profiles/chrome_153/README.md`。

### chrome_152 已加入（历史记录）

与 chrome_151 相比，wire 上只差三处，全部可从源码证明：

1. UA 版本号 `151.0.0.0` → `152.0.0.0`，其余 token 一字不差。`minicronet.cc` 对
   major > 104 本来就生成 `<major>.0.0.0`，无需改代码
2. 多一个扩展 **0xca34 `TLSEXT_TYPE_trust_anchors`**。151 里
   `kTLSTrustAnchorIDs` 是 `FEATURE_DISABLED_BY_DEFAULT`、`kNonMtcTrustAnchorIDs`
   不存在，且只发与服务端通告的交集；152 两个 feature 都默认开，改用
   `SelectAllTrustAnchorIDs()` 无条件发全部。151 的开关是编译期关闭的，组件再新也
   不会发，所以这是真正的版本级差异
3. **`signature_algorithms` 里多一个 GREASE 值**。`kTlsGreaseSigalgs` 是 152 新增
   且默认开（`features.cc:965`），`ssl_client_socket_impl.cc:211` 调用
   `SSL_CTX_set_grease_sigalgs_enabled`。这一项不需要新机制：
   `grease_signature_algorithms` 是已有字段，Core 本来就按
   `has_profile ? profile 字段 : feature 默认值` 逐 socket 解析

JA4 从 `t13d1516h2_8daaf6152771_806a8c22fdea` 变成
`t13d1517h2_8daaf6152771_cb7bf5808d99`（扩展 16→17，cipher 哈希不变），Akamai H2
指纹逐字节相同。为稳妥起见还把两个 tag 的 140 个 net feature 默认值全量对比了一遍：
两个翻转、五个新增且默认开、七个移除，其中只有
`kTcpSocketPoolLimitRandomizationForProxy` 涉及 profile 字段，而它 gate 的行为在 152
里变成无条件开启，所以 `randomize_proxy_socket_pool_limit` 仍为 true。

**trust_anchors 载荷：集合是 profile 数据，顺序不是。** ID 集合按观测冻结。

**2026-11 修正：152 的集合是 32 个，不是 28 个。** 早先那一轮只采到 28，据此判断
「编译期根库是 32、抓包 28 是子集，抓包机的根库被 PKI Metadata 组件更新过」。新的参考
抓包集里 chrome_152 的**六条连接全部是 32 个 ID**（`common=32 union=32`），且严格包含
旧的 28 个，多出的正好是旧 README 点名缺失的 `d6790902`、`d6790903`、`d6790909`、
`d679090e`（11129.9.x）。也就是说：旧抓包那台机器的根库确实被动过，但 28 是**被污染
后的观测**，不是 Chrome 152 的行为。153–157 仍是 28 个，且与表逐字节一致。

这个偏差真实存在过：Core 发出的 152 指纹比真 Chrome 少 4 个 trust anchor ID。现在
`profiles/normalized_profiles.json` 的 `trust_anchor_ids_encoded` 是 204 字节 / 32 个 ID，
`tools/audit-profile-addition.py --levels 2` 会按参考抓包核对这个集合。

顺序**故意不冻结**：
`SSLContextConfig::trust_anchor_ids` 是 `absl::flat_hash_set`，absl 给每个表实例单独
撒种（`raw_hash_set.h`：*"Per table hash salt … randomize iteration order
per-table"*），所以顺序是表实例的属性而非版本的属性。Chrome 每个浏览器进程建一次表，
这就是它 4 次抓包顺序一致、重启后又变的原因。Core 复现机制而不是复现结果：把 28 个 ID
插进同一种容器，让 absl 排序。实测一个 Engine 内 4 次请求 1 种顺序、同进程 4 个 Engine
4 种顺序、集合恒定——Engine 扮演浏览器进程的角色。冻结顺序反而会让每个实例发出同一个
排列，那是真实 Chrome 不会有的行为。

**不影响其它版本，已用 wire 验证。** 新增 `tools/inspect-client-hello.py`
从本地 socket 把 ClientHello 读回来解析，不靠表自证。采样次数很关键：BoringSSL 的
RFC 7685 padding 依赖长度，而 ECH GREASE 载荷长度逐连接变化，所以同一个 profile 的
`padding` 会时有时无，单次采样会把它误读成指纹变化。用 `--repeat 4` 对横跨 99–151 的
11 个 profile 在改动前后各采一轮：11 个的稳定集合与浮动集合完全一致，且没有任何
152 之前的 profile 发出 `trust_anchors`——空 span 让
`ShouldAdvertiseTrustAnchorIDs()` 返回 false，这是结构上的保证。

profile 表重新生成的可信度也先证明过：迁入 `profiles/` 三份输入后重新生成，与已提交的
表**逐字节一致**；加入 chrome_152 后 diff 是纯增量——**删除 0 行**，53 个旧 profile 各
多一个空 `{}` 字段。

证据与复现命令见 `profiles/chrome_152/README.md`。

### 其它已修复

- **GIL 与 Rust Mutex 加锁顺序反转导致的死锁**（`schedule_event` 在 edition 2021 下
  跨调用持有 MutexGuard 再抢 GIL）。原先约 1/3 概率挂死，修复后 25/25 通过
- **`session.cookies.get_dict()`**：新增 `CookieJar`
- **PyPI 项目介绍与 GitHub 不一致**：`bindings/python36/pyproject.toml` 缺 `readme`，
  maturin 回退到 crate 本地 README，导致 0.2.1 在 PyPI 上把「Python 3.6 native
  compatibility」当成项目介绍。已修并加 `tools/audit-readme.sh` 做门禁
- **畸形 URL 处理**：`not-a-url`、`http://` 现在在创建期 fail-closed
- Python facade 的 `_engine.request()` 移进 try 块，创建期错误也映射为
  `RequestException`

### 已排除的精简候选

`optional_trace_events_enabled = false` **零收益**：`args.gn` 带上了开关、buildflag
头翻转为 `(0)`、产物 SHA-256 逐字节一致。`--gc-sections`、`--icf=all` 和 ThinLTO
已经把那些宏清干净了。

### 阶段 4：Python 兼容层重写（2026-09-04）

Python 绑定从「能跑通」提升到「requests / curl_cffi 可直接迁移」。四个缺陷用测量
确认后修掉，完整证据见 `docs/PYTHON_BINDING_AUDIT.md`：

| 缺陷 | 证据 | 修复 |
| --- | --- | --- |
| 异步请求泄漏 ≈12 KiB/请求 | 每 500 请求 RSS +5.9 MiB，线性增长 | 终止事件清理 Core 事件回调，打断穿过 Rust 的引用环；修复后 2000 请求 +68 KiB |
| 每请求重建 Chromium Engine | 5 次带 override 的请求构造 5–6 个 Engine | 按配置缓存 Engine（每 session 有界、带锁、跨 fork 重建）；同样场景降到 1 个 |
| override 丢会话状态 | `session.get(..., verify=False)` 拿不到已有 cookie | jar 镜像 `Set-Cookie` 并在换 Engine 时重发 |
| 流式失败死锁 / `allow_redirects=False` 挂住 | 两者都能稳定复现（timeout 124） | 失败路径改为发信号而非 resolve 无人等待的 future；新增 `wait_manual` 同时等 redirect 与 response |
| WebSocket 整条路径不可用 | `origin` 默认 `""`，Core 拒绝空 origin，所有 `websocket()` 直接失败；旧 WS 测试默认 skip 所以从未覆盖 | origin 默认取 URL 自身；构造函数等握手 open；错误改为具名 net error；`WebSocket(url=...)` 不再泄漏 Session |
| 证书错误丢失原始错误码 | 所有证书失败都报 `ERR_ABORTED (-3)`，与主动取消无法区分；`error_mapping.h` 里的 cert 分支永远走不到 | 覆盖 `OnSSLCertificateError`，用 `CancelWithSSLError`（Chromium 自己在 `services/network/url_loader.cc` 的做法）；已重建 linux-x86_64 Core，无 ABI 改动 |

新增能力：requests 全异常层次与 `Response` 属性、`RequestsCookieJar`、可变
`session.proxies`、`Request`/`PreparedRequest`、adapter 挂载点、`codes`、
`CurlMime`、`http_version`、`RetryStrategy`、分块上传、真实子模块
`chrome_client.requests`。无法忠实实现的选项（`ja3`、`akamai`、`cert`、
`curl_options` 等）显式抛 `UnsupportedFeature` 而不是静默忽略。`referer=` 与
`Referer` 头原也在其列，后改为真实发出，见下文的「Referer 修复」。

Chromium net error 码现在驱动异常类型与消息（`ERR_CERT_DATE_INVALID (net error
-201)`）。测试从 16 个增加到 103 个：13 个 WebSocket 用例跑在本地握手服务器上，8 个
证书用例用本地生成的 CA 与各带一个缺陷的证书，都不需要外部端点。

证书错误码修复是本轮唯一改动 C++ 的地方，根因链见
`docs/PYTHON_BINDING_AUDIT.md`：Chromium 把真实错误码交给 delegate 后就依赖 delegate
结束请求，而基类默认实现调 `URLRequest::Cancel()`，即
`DoCancel(ERR_ABORTED, SSLInfo())`——写死 ERR_ABORTED 并丢掉 SSLInfo。同一个 Core 的
WebSocket 路径一直做得对，这也印证了它是疏漏而非设计选择。

明确否掉的一个改动：不接受顶层传入的 WebSocket UA。Core 把 `User-Agent` 列为禁止的
握手额外头，且实测 UA 在握手中的位置由 Chromium 决定、本身属于指纹；正确入口是
Engine 级 `Session(user_agent=...)` 或换 profile。传 header 会抛 `UnsupportedFeature`
而不是静默丢弃。

### Referer 修复（2026-09-07）

此前 `referer=` 与 `Referer` 头显式抛 `UnsupportedFeature`，因为实测 Chromium 会剥掉
extra headers 里的 `Referer`。问题在于那条路径本身就不通：`URLRequestHttpJob` 只认
`URLRequest::SetReferrer` 设的字段，Core 从不调用它，所以「接受它等于让调用方以为设置
成功」这个判断在旧 ABI 下成立。

现在的做法是把 `Referer` 路由到 `SetReferrer`（`core/source/request.cc`），让值从
Chromium 自己那条路径上 wire。途中发现并处理了第二个坑：默认 referrer policy 是
`CLEAR_ON_TRANSITION_FROM_SECURE_TO_INSECURE`，一个 `https://` referrer 打到
`http://` 目标会被 `url_request.cc` 在 job 读取前清掉——这正是「忠实于 Chrome」与
requests/curl 语义冲突的地方。选择后者：显式提供 `Referer` 时把 policy 设为
`NEVER_CLEAR`，调用方给什么就发什么。不带 `Referer` 的请求走默认 policy，指纹路径不变。

验收：`Session(headers={"Referer": ...})`、`Session(referer=...)`、`request(..., referer=)`
三种方式都在本地 echo 服务器上看到请求头；`test_referer_reaches_the_wire` 覆盖四种
组合；103 个 Python 用例全过（2 个跳过）。

### 阶段 4 续：体积精简（2026-09-05）

先在未 strip 镜像上按符号量清体积去向，再只动可证明不可达的部分。完整测量见
`docs/BASELINE_LINUX_X86_64.md`。

**已采纳：磁盘缓存后端不进链接产物。** Core 只请求 `HttpCacheParams::IN_MEMORY`，
ABI v8 也没有缓存目录参数，所以 blockfile 与 simple 两个后端不可达；它们之所以还在
产物里，是因为 `CreateCacheBackendImpl` 在内存分支之后构造的 `CacheCreator` 引用了
它们。新补丁在内存分支后加一个 `BUILDFLAG(MINICRONET_BUILD)` 早返回，不删源码清单，
让 `--gc-sections` 自己丢——将来若真有别处引用，构建仍然通过而不是链接失败。

八个平台合计 **73,798,444 → 71,911,264 字节，−1,887,180（−2.56%）**，单平台
−1.47% 到 −3.08%。符号层面确认 `disk_cache::Simple*` 从 100,051 降到 1,134 字节、
两个 blockfile 类归零，而 `MemBackendImpl`/`MemEntryImpl` 一字节未动。

验收：内存缓存的命中 / `bypass` / `only_if_cached` / `cache=False` 四种行为逐项确认；
Core smoke、三个平台审计、103 个 Python 用例全过；**TLS 指纹未变**——
`inspect-client-hello.py` 对 chrome_152/151/120/99 的 cipher 数、稳定扩展集合和 profile
间集合差异与修改前逐字节一致（逐次 order 不同是 Chrome 自身的扩展乱序）。

三个体积上限随之收紧：Linux 9,250,000 → 9,050,000、Windows 12,000,000 → 11,400,000、
macOS 12,000,000 → 8,750,000。macOS 那一档原来虚高 3 MB 以上，等于没有门禁。`-Oz`
那轮之后三档又收到 Linux 8,940,000、Windows 10,400,000、macOS 8,620,000。
156.0.8078.31 升级后按同一条规则重算两次（上游增长，随后 simdutf 尺寸档修复）。
chrome_86..chrome_98 加入 profile 表后又重算一次（8 平台同批重建，合计 +28,624 字节）。
**当前默认上限是 Linux 8,540,000、Windows 9,610,000、macOS 8,120,000**，对应最大的产物
8,367,664 / 9,416,192 / 7,958,968 各加 2% 向上取整到 1 万。Windows 那一档在加入新 profile 后
曾只剩 1.91%，低于这条规则本身要求的 2%，所以从 9,600,000 提到 9,610,000。

**已测量但未采纳：HSTS 预载表（−786,432 字节，−8.57%）。** 这是剩下唯一的大头，与上面
那项叠加可再省到 −10.98%。没做，因为它用保真度换体积：Chrome 会在发出任何字节之前把
预载域名的 `http://` 升级为 `https://`，去掉这张表之后明文请求会真的以明文发出——既是
安全回退，也是可被检测方观察到的与真实 Chrome 的差异。要不要接受是产品决定：接受的话
只需在 8 个构建脚本的 args.gn 各加一行，并同步下调上限与兼容边界文档。

## 进行中

没有正在执行的改动。下一步的候选见「后续计划」，其中阶段 3 剩下的两项（进程级唯一
网络线程、body 三次拷贝）都需要先量化再动手。

## 已知缺陷与阻塞

### 证书错误码修复的跨平台验收状态

8 个平台的 Core 全部已重建、安装并刷新 manifest（`tools/audit-core-binaries.sh`
8/8 通过，ABI 仍是 v8），且**全部包含本轮 C++ 修复**。

8 个平台在 2026-11 的 BoringSSL 尺寸档精简之后**同批重建**（见
[CORE_BINARY_SIZE_PLAN.md](CORE_BINARY_SIZE_PLAN.md) 第八节），合计
68,170,984 → **64,720,064 字节（−3,450,920，−5.06%）**。

chrome_86..chrome_98 加入 profile 表之后，8 个平台**再次同批重建**，合计
**64,748,688 字节**（+28,624，+0.04%：多出的 13 个 profile 条目与 `HistoricalUserAgent`
的版本串）。下表是这次重建后的产物值；`tools/audit-core-binaries.sh` 会逐个断言
产物里含 `chrome_86` 与 `chrome_158`，所以「只重建了一个平台」不会再通过门禁：

| 目标 | 大小（字节） | SHA-256 前缀 |
| --- | --- | --- |
| linux-x86_64 | 8,367,664 | `38e49c9e3e065539` |
| linux-x86 | 8,062,952 | `b977a206714b445b` |
| linux-arm64 | 8,012,448 | `348c66c08ab69c8e` |
| windows-x86 | 7,501,312 | `220749d5ac507d7d` |
| windows-x86_64 | 9,416,192 | `7e07526a8efe53e3` |
| windows-arm64 | 8,118,272 | `d2e52049fcb30914` |
| macos-x86_64 | 7,958,968 | `9d4e872f0b4940bb` |
| macos-arm64 | 7,310,880 | `6e69ad70bf355923` |

`linux-armv7` 不在上表里：它是在那次同批重建之后加入的第九个目标，合计口径仍是
那份 8 平台记录。它自己的产物是 6,081,560 字节 / `a4cf93ff913b868e`，见下文
「第九个目标：Linux ARMv7 (hard-float)」。

### 打 tag 前全量 review 的约束（AGENTS §13）

`AGENTS.md` §13 规定每次打 tag 前跑 `tools/review-before-tag.py`，把静态门禁、指纹、
并发、泄漏、四个绑定放在同一份 commit 上一次跑完，产出 `docs/review-<version>/` 下的
`summary.json` / `bench.json` / `leak.json` 与该 tag 一起提交。

本次实测：28 步全部 pass，233 秒；`async_concurrency_128` 733.9 req/s，
`sync_sequential_1kib` 455.5 req/s，5 种请求形态的泄漏测量全部平坦（fd ≤ +1，
RSS ≤ +396 KiB）。

同批重建全部 9 个平台产物。版本串随 `MINICRONET_VERSION` 进产物，
`audit-core-binaries.sh` 逐个校验 `minicronet/<version>` 标记与 `tools/version.py`
的输出相等。具体版本号只在根 `Cargo.toml`，此处不复述。

### 第九个目标：Linux ARMv7 (hard-float)

2026-11 新增 `linux-armv7`，Rust triple `armv7-unknown-linux-gnueabihf`。与另外 8 个
目标不同，它的前置条件不是 `gclient runhooks` 会满足的：`debian_bullseye_armhf-sysroot`
只在 `--arch=arm` 时下载，缺失时 `gn gen` 直接断言在 `build/config/sysroot.gni:60`。

三条与别的目标不同的硬事实：

1. **必须显式写 `arm_float_abi = "hard"`。** `build/config/arm.gni:51` 在
   `current_os == "linux" && target_cpu != v8_target_cpu` 时默认 `softfp`，而本构建
   正是这个条件；armhf sysroot 是 hard-float，用默认值会把两种 ABI 链进同一份产物。
2. **NEON 是硬依赖，且无法在 GN 里关。** `build/config/arm.gni:9-11` 无条件
   `arm_use_neon = true`。无 NEON 的 ARMv7 核会 SIGILL，不会报告缺特性。
3. **`tools/audit-abi-layout.py` 对它不构成证明。** 该脚本用宿主 `cc` 取
   `sizeof/offsetof`，与 32 位 ARM 无关。Rust 侧 72 处 `offset_of!` 断言按
   `target_pointer_width` 分支（`crates/minicronet-sys/src/lib.rs:330+`），所以 32 位
   能过，但这与 `linux-x86` 同属「交叉未验证」，不能写成已验证。

产物：`core/binaries/linux-armv7/libminicronet.so`，**6,081,560 字节**，
SHA-256 `a4cf93ff913b868e`，ELF32 / `ARM` / `EABI5`，DT_NEEDED 与另外两个 Linux
目标同集合，loader 为 `ld-linux-armhf.so.3`。比 `linux-x86` 小 1,981,392 字节：
`.text` 少 1,970,246（Thumb-2 比 i386 编码密），`.data` 只多 88 字节。

连续性验证：

- `tools/build-core-linux-armv7.sh` 一次跑通 6339 条 ninja 边，产出通过
  `tools/audit-core-linux.sh`（20 个 ABI 符号、loader 名、依赖集、尺寸上限、
  随机源标记、profile 载荷）。
- **可复现**：强制重编同一份源码后哈希不变（`a4cf93ff...` 两次相同）。
- `tools/audit-core-payloads.py`：27 个 artifact/载荷对全部命中，572 字节载荷逐字节相同。
- 变异测试两条：把 x86_64 产物配上自洽 manifest 放进 `linux-armv7/` → 门禁报
  `wrong architecture (ELF 64-bit ... x86-64)`；把 relinked loader 名改成
  `ld-linux-armhf.so.9` → `audit-core-linux.sh` 退出码 1 并打印期望值与实际值的 diff。
- **未运行**：本机无 `qemu-user`，也没有 ARMv7 硬件。按 AGENTS §12 记为
  「已构建、静态审计通过、未加载、未发请求」，与另外 7 个目标同一档。

### 跨平台一致性：能证明到哪一步

只有 linux-x86_64 能在构建机上执行（无 qemu、无 wine、无 32 位运行时）。另外 7 个平台
**没有一次真实运行**，所以「行为一致」不能声称。能证明的是下面这些，逐条都有命令：

| 结论 | 怎么证明 | 强度 |
| --- | --- | --- |
| 8 个产物都由同一份 `core/source/` 编译 | 六个源文件与 Chromium 树里的安装副本 `cmp` 逐字节相同 | 强 |
| 产物都含全部 73 个 profile id，缺失 0、多余 0、无重复 | `tools/audit-core-binaries.sh` 逐个 grep 首尾 id | 强 |
| 8 个产物嵌入的 profile 载荷**逐字节相同** | `tools/audit-core-payloads.py`：从生成头取 `trust_anchor_ids_*` 数组，要求每个产物都含这段字节 | 强 |
| 载荷与 `profiles/normalized_profiles.json` 一致 | 同一个门禁先比对头与输入，头陈旧即失败 | 强 |
| trust anchor 代码路径没有平台分支 | 所有相关文件里 `trust_anchor` 与 `BUILDFLAG(IS_*)`/`ARCH_CPU_*` 同窗口出现 0 次 | 强 |
| `SelectAllTrustAnchorIDs()` 不读系统根库 | 上游 `net/ssl/ssl_config_service.cc:78` 直接返回 `trust_anchor_ids`，唯一赋值点是 `profile_ssl_config_service.cc:63` | 强 |
| 7 个平台**能加载** | — | **无证据** |
| 7 个平台**能发请求** | — | **无证据** |
| 7 个平台发出的 trust anchor 是 32 个 | — | **无证据**（只能由上面几条推断） |

最后三行是这条链的边界。按 AGENTS §12，构建成功 ≠ 能加载 ≠ 能发请求，所以这 7 个平台的
状态记为「已构建、载荷静态一致、未运行」，不能写成「已验证」。

`tools/audit-core-payloads.py` 的存在理由就是这条边界：既然跑不了，就把「产物里装的是不是
同一份数据」变成机器可判的。变异测试两条：换入旧表编译的 linux-x86 产物 → 报
`does not embed trust_anchor_ids_0 (204 bytes)`；把生成头改一个字节 → 报 `the generated
table is stale`。

### 本轮 C++ 修复的平台覆盖

`core/source/` 的四处修改（长度字段上限、close code 1014、`protocol_mode`
移出 profile 块、WebSocket 握手定时器）已进入全部 8 个平台。

证据分三层，因为本机只能执行 x86_64 Linux 二进制（无 qemu、无 wine）：

1. **单一源码来源**：`tools/sync-core.sh` 把 `core/source/*` 安装到唯一位置
   `$CHROMIUM_SRC/minicronet/`，8 个 out 目录全部从这里编译。该副本与仓库
   `core/source/` 逐字节一致（`cmp` 验证 `minicronet.cc`/`engine.cc`/`websocket.cc`），
   四个补丁标记齐备（`kMaxHeaderCount` 3 处、`timeout_timer_.Stop` 3 处、
   `force_quic_everywhere` 3 处、close code 白名单内 1014 为 0 处）。
2. **逐平台重建**：8 个 `build-core-*.sh` 各自退出 0，并各自跑通该平台的
   `audit-core-*.sh`（架构、导出符号、体积上限、运行时依赖）。产物与 HEAD 逐
   字节不同，证明确实重新编译。
3. **可复现性**：linux-x86_64 连续两次强制重建得到逐字节相同的产物
   （`512231db…`），说明产物确实由当前源码确定性构建。

**仍未做到的**：7 个非本机平台没有一次真实运行（AGENTS.md §11
「构建成功 ≠ 能加载 ≠ 能发请求」）。行为验证只在 linux-x86_64 上完成——
`http_version=v3` 报 `ERR_QUIC_HANDSHAKE_FAILED`(-358)、close code 1014 被拒绝、
四个 P0 长度输入返回 `INVALID_ARGUMENT`。其余平台的同三项行为待有对应运行环境时补测。

（表中尺寸与 SHA 是当前提交的产物值。
`audit-core-binaries.sh` 会从产物字节里读 `minicronet/<version>` 标记并与
工作区版本比对，所以版本提升必须重建，否则门禁直接失败——本轮先改版本号时
8 个平台全部报「artifact reports the old version but the workspace version
is the new one」。同一条检查也覆盖了历史上 `core_version()` 长期谎报
`0.4.0` 的那个缺陷。）

三层证据，因为本机只能执行 x86_64 Linux 二进制（无 qemu、无 wine）：

1. **行为**：linux-x86_64 实测过期 -201、主机名不匹配 -200、CA 不受信 -202，`verify=False`
   与 `verify=<CA>` 不回退。
2. **每个架构的编译产物**：8 个 target 的 `obj/minicronet/minicronet/request.o(bj)` 里都能
   用 `llvm-nm` 看到
   `minicronet::Request::OnSSLCertificateError(net::URLRequest*, int, net::SSLInfo const&, bool)`
   的定义（`T`）加 vtable 引用（`U`），Windows 是对应的 MSVC 修饰名。虚函数在已发射的
   vtable 里不会被链接器丢弃，所以链接产物必然含它。
3. **可复现**：重建 linux-arm64 两次得到同一 SHA-256（`3e52609d7449b468`），说明安装的
   二进制就对应当前源码。

未做的是在真实 Windows / macOS / ARM64 机器上跑一次
`test_compat.CertificateErrorTests`。那 8 个用例只需要本地生成证书，不依赖外网，可以直接在
目标机上跑；断言里的 `assertNotIn("ERR_ABORTED", message)` 就是为此写的。

Linux x86/ARM64 交叉编译需要 `pkg-config`（gn 为 NSS 调用它）。仓库外的 deb 在
`new/.tools/pkgconf/`，展开后用 `MINICRONET_PKGCONF_DIR` 指向解包目录：

```sh
cd new/.tools/pkgconf && mkdir -p root && for deb in *.deb; do dpkg-deb -x "$deb" root/; done
MINICRONET_PKGCONF_DIR=$PWD/root tools/build-core-linux-arm64.sh
```

### NSS 保留（已决策，不再是阻塞）

曾考虑用 `use_nss_certs = false` 去掉 3 个私有依赖（`libnss3`/`libnspr4`/`libnssutil3`）。
**决策：不移除。** 两个理由：

1. 上游不支持该配置。`use_nss_certs = false` 在 Linux 上编译不过：
   `net::TestRootCerts::Init()` 只在 `use_nss_certs` 分支里进 Linux 源码集（一行 GN
   补丁可解），但 `net::CreateSslSystemTrustStoreChromeRoot()` 在
   `system_trust_store.cc` 的平台 `#if/#elif` 链里 **Linux 只有 `USE_NSS_CERTS`
   一个分支**，必须自己实现一个「只用 Chrome Root Store、不读系统信任库」的版本
2. 那会改变证书校验语义——系统与企业安装的根证书不再被承认，而真实 Chrome 在 Linux
   上是会读的。本项目的契约是与真实 Chrome 一致，所以这是功能行为变更，不可接受

收益本来也只在 manylinux 打包健壮性，不在体积。构建脚本里没有这个开关，不需要改动。

收益主要在 manylinux 打包健壮性（少 3 个私有依赖 `libnss3`/`libnspr4`/`libnssutil3`），
不在体积。

### 其它待确认

- Windows manifest 的 `runtime_dependencies` 曾声明 `libplc4.so`/`libplds4.so`，
  但它们不在 `DT_NEEDED` 里（是 `libnss3` 的传递依赖），属于过度声明
- Python 3.6 wheel 从未在真实 3.6 环境验证过（本机只有 3.12）
- Windows/macOS 的审计脚本和 7 个非 x86_64 构建脚本只做过同构适配和语法检查，
  执行验证是随 ABI v8 那轮才第一次完成

## 后续计划

### 阶段 2 剩余

阶段 2 已收尾，两个候选都有结论、都不再执行：

1. NSS 依赖移除——**已决策不做**，见「已知缺陷与阻塞」里的说明
2. `.rela.dyn` 432 KB 用 DT_RELR 可压到约 50 KB，但 manylinux2014 是 glibc 2.17，
   加载器不支持，**已排除**

### 阶段 3：设计优化

- **进程级唯一网络线程**：`Runtime` 单例只有一个 `base::Thread`，所有 Engine 共享，
  已量化为单核 88–93%、约 2,250 req/s 的限高。加 Engine 不加吞吐。要突破得给每个
  Engine 一条网络线程，或让 `Runtime` 持有线程池。这会改变 profile 隔离的推理方式
  （连接池、会话缓存都挂在 context 上），需要单独设计
- **每块 body 拷三次**：Core 侧 `std::vector`、Rust `copy_bytes`、Python `bytes()`。
  ABI 契约要求 Rust 必须拷一次，能省的是 Core 侧那次（直接传 `IOBuffer`）

### 阶段 4：补测试与迁移收尾

- `tests/` 下 core/rust/python/node/go 五个目录在本机存在但**从未被 git 跟踪**，仓库里
  没有这一层。真实测试在 `bindings/python/tests/test_stability.py` 和各 crate 的
  `#[cfg(test)]` 里。要么补内容，要么删掉这个空壳，不要让它继续冒充测试布局
- profile 表的三份输入（`normalized_profiles.json`、`effective_protocol_params.json`、
  `network_feature_snapshots.json`）已随 chrome_152 迁入 `profiles/`，
  `tools/generate-profile-table.py` 现在能在本仓库直接重新生成。**仍在 `new/` 的**是
  上游那几个 audit 生成器（`extract-*`、`build-normalized-profiles.py`）；新增 profile
  目前靠 `tools/collect-profile-evidence.py` 加 `tools/verify-wire-capture.py` 走通，
  全流水线迁移还没做。
  4 个 `profile_verification` 探针（`profile_isolation_probe.c`、
  `profile_feature_probe.cc`、`profile_state_isolation_probe.cc`、
  `websocket_extended_connect_probe.c`）曾只存在于本机 Chromium 检出里，
  `tools/sync-core.sh` 因此只在这台机器上通过、在任何干净克隆上都会 `die`；现已迁入
  `core/source/`，并由 `tools/audit-core-inputs.sh` 与 `sync-core.sh` 的安装后置校验
  双向守住（见下）
- Linux x86/ARM64 交叉编译目前还依赖 `new/.tools/pkgconf` 的 deb（通过
  `MINICRONET_PKGCONF_DIR` 指向解包目录），因为 gn 为 NSS 调用 `pkg-config`；
  仓库里没有这一层，换机器需要先解包，见上文「证书错误码修复的跨平台验收状态」

### 阶段 5：profile v2 剩余专题

`new/PROFILE_V2_TODO.md` 的 7 个专题里只有第 3 项（FeatureList 快照）标了完成，
其余 6 项需逐项核对：TLS 扩展集合与顺序、完整 H2 数值 SETTINGS、WebSocket 握手
差异、代理连接池、session resumption/0-RTT/Alt-Svc、cache/cookie/连接池策略。
需要抓包证据支撑，建议单独排期。

### WebSocket 背压

WebSocket 已经是消费者驱动的 pause/resume：`pending_data_` 计数配合
`HasPendingDataFrames()` 让 channel 在还有未消费帧时暂停读取，`ResumeReading()`
在消费完后恢复，同一时刻最多一条消息在飞行，不存在「超限断连」。剩余缺口是端到端
验证——`test_websocket_sync_and_async` 目前因缺少 `MINICRONET_WS_URL`（本地
WS/WSS 服务器）而 skip。

### 发布

PyPI 上的 0.2.1.1 存在背压挂死和 IDN 崩溃，两者都已修复并随 **0.2.2** 发布
（0.2.2 起 crate 与 Python 版本同号，不再有四段版本号）。版本号见根 `Cargo.toml`。
发布路径：打与 `Cargo.toml` 一致的 `v<版本>`
tag 触发 `pypa/gh-action-pypi-publish`（OIDC 可信发布，需要 PyPI 侧把
`komAAmok/chrome_client` 的 `build-wheels.yml` 加进 trusted publishers），属于
不可撤销操作；重复上传同名文件会被 PyPI 以 400 拒绝。

## PyO3 0.30.1 迁移与 Python 支持矩阵（2026-10-22）

把现代绑定从 PyO3 0.28.3 升到 **0.30.1**，并把「哪个解释器加载哪个扩展」的下界从
3.7 抬到 **3.9**。这不是一次纯粹的依赖升级：新读到的两个上游事实改变了打包矩阵。

| 事实 | 复核位置 |
| --- | --- |
| PyO3 0.30.1 的最低支持版本是 **3.9** | `pyo3-build-config-0.30.1/src/impl_.rs:26` `MINIMUM_SUPPORTED_VERSION = { major: 3, minor: 9 }` |
| 它的 abi3 上限是 **3.15** | 同文件 `:40` `STABLE_ABI_MAX_MINOR: u8 = 15` |
| feature 表只有 `abi3-py39`..`abi3-py315`，**没有** `abi3-py37`/`abi3-py38` | crates.io 索引 0.30.1 的 features；写上不存在的 feature 是编译期错误 |
| 因此现代轮子标签是 `cp39-abi3` | maturin 1.15.0 实跑产物 `tagprobe-0.0.0-cp39-abi3-manylinux_2_38_x86_64.whl` |
| PyO3 0.15.1 的 abi3 上限是 **3.9**（`ABI3_MAX_MINOR = 9`） | `pyo3-build-config-0.15.2/src/impl_.rs:23` |

于是矩阵变成：**3.6–3.8 → `bindings/python36`（0.15.1 + `abi3-py36`，cp36-abi3）**，
**3.9–3.15 → `bindings/python`（0.30.1 + `abi3-py39`，cp39-abi3）**。
free-threaded（3.13t/3.14t/3.15t）维持**明确不承诺**：abi3 在 free-threaded 上不生效
（`applicable_stable_abi` 在该情况下返回 `None`），模块也仍声明 `gil_used = true`。

### 破坏性更新对本 crate 的影响是零

0.30.1 的变更清单里踩不到本 crate 的有：移除 0.28 弃用项、`#[pyclass]` 不再默认派生
`FromPyObject`、`pyo3_build_config` 若干 API 改动、`PyBuffer` 若干方法弃用。
两个 `src/lib.rs` **一行未改**即可编译，且 `cargo clippy -- -D warnings` 干净。
证据：两处 `git diff` 为空；`cargo tree -p chrome-client-python -i pyo3` → 0.30.1。

真正需要动的是**版本与打包**两处，不是源码：

```
bindings/python/Cargo.toml: pyo3 = { version = "=0.30.1", features = ["extension-module", "abi3-py39"] }
bindings/python36/Cargo.toml: pyo3 = { version = "=0.15.1", features = ["extension-module", "abi3-py36"] }   # 未动
```

`generate-import-lib` 被删除：0.29 起它是 no-op（Windows 改走 `raw-dylib`）且已弃用。
删除后 `cargo check --target x86_64-pc-windows-msvc` 仍通过，并连带移除了 4 个依赖
（`python3-dll-a` → `cc` → `shlex`/`find-msvc-tools`）。锁文件里 `syn 2.0.119 → 3.0.9`
看着像大升级，但 `cargo tree -i syn` 表明只有 pyo3 的两个 proc-macro crate 使用它，
属强制升级而非顺带漂移。

### 修掉的那个真实缺陷：facade 阈值与 abi3 下界是两个数字

`bindings/python/chrome_client/_python_impl/_native.py` 用
`sys.version_info < (3, M)` 决定加载哪个原生模块，而 `abi3-py3N` 决定那个模块的轮子
能在哪一版上装。**两者互不知情**，PyO3 只校验 N 与*构建解释器*的关系，从不读 facade。
迁移后若 M 仍停在 7，3.7/3.8 的解释器会装 cp36-abi3 轮子（其中只有
`chrome_client_native36`）却去要 `chrome_client_native`，**在一个 README 打着 ✓ 的
版本上 ImportError**。修法是 M 改为 9，并把这条等式变成门禁（见下）。

### 新增的门禁与它们的变异测试

- `tools/audit-pypi-metadata.sh` 末尾新增一致性断言：从 `bindings/python/Cargo.toml`
  解析 `abi3-py3N` 得 N，从 `_native.py` 的 `MODULE_NAME` 行解析 `(3, M)` 得 M，
  断言 **N == M**，不等就报出两个数字与两处 file:line。附带断言 py36 crate 仍是
  `=0.15.1` + `abi3-py36`、现代 crate 的 abi3 不是 `abi3-py37/38`（防回退）、pyo3 必须用
  `=` 精确 pin（否则 feature 表不可知）。
- 该门禁自己先有一个缺陷：首版正则 `abi3-py(\d+)` 把 `abi3-py36` 读成 `36`，
  报出「abi3 floor is 3.36」。改为 `abi3-py3(\d+)` 后 `36→6`、`39→9`、`315→15`。
- 变异测试（AGENTS.md §6）：`_native.py` 改回 `(3, 7)` → exit 1，报错数字是
  `3.9`/`3.7` 而非 `3.39`；`abi3-py39` 改 `abi3-py38` → 命中「does not exist in
  pyo3 0.30.1」。两次都恢复并用 diff 证明字节一致。
- `.github/workflows/build-wheels.yml` 新增 `python39-smoke` job：在**真 Python 3.9**
  上装 Linux x86_64 现代轮子，断言 `MODULE_NAME == 'chrome_client_native'` 并真发一次
  `https://example.com` 请求断言 200，且已加入 `publish.needs`——否则装不上的轮子照样能
  发布，门禁形同虚设。这是 LESSONS 37「下界必须真在那一版解释器上执行」的落点。

该 job 上线前修掉三个缺陷：artifact 名少一层 `linux-`（实际上传名是
`wheel-linux-linux-x86_64-python`，`matrix.core-target` 本身已含前缀）；断言用了
`chrome_client._python_impl._native` 这个**被覆盖的包属性**
（`_python_impl/__init__.py:17` 的 `from ._native import native as _native` 把它重绑成
扩展模块本身，无 `MODULE_NAME`），改用 `importlib.import_module`；`publish.needs`
不含该 job。

### 版本提升与 Core 重建必须同时落地

升版本号会让 `tools/audit-core-binaries.sh` 全红：它从每个产物的**字节**里读
`minicronet/<version>`（`minicronet.cc` 的默认 User-Agent 字面量）与工作区版本比对，
它读产物不读 manifest，所以陈旧 manifest 骗不过它。本轮 8 个平台已全部重建并刷新
manifest（`--abi-version 8`）：

| 目标 | 字节 | SHA-256 前 16 位 |
| --- | --- | --- |
| linux-x86_64 | 8,863,392 | c5806d82473c6229 |
| linux-x86 | 8,776,588 | 8e7d378b7d71f954 |
| linux-arm64 | 8,692,184 | 652687b39eac573b |
| windows-x86_64 | 10,299,392 | 78d519fb43d95eb4 |
| windows-x86 | 8,134,656 | 57b7c1a59ea80840 |
| windows-arm64 | 8,960,000 | 7dabbd24677ed50a |
| macos-x86_64 | 8,536,776 | 9cae8c52ff777c89 |
| macos-arm64 | 7,872,560 | 2ae93a3341fb1f08 |

重建是增量的（各 `out/` 已有 obj，只重编 Core 相关目标并重链接），
`sync-core.sh` 先从根 `Cargo.toml` 注入版本头（版本号只在那里），`install-core-binaries.py` 再刷新 manifest。
三者现已一致：`core_version()` = `__version__` = 根 `Cargo.toml` 的 `workspace.package.version`。

### 变更（main，未发版）：收缩 API 表面，删掉 14 个 curl_cffi 关键字

本项目是 Chromium 后端的指纹客户端，不是 libcurl 的 drop-in。那些「接受后拒绝」或
「接受后忽略」的关键字现在**不存在**：传了就是 `TypeError`。

| 删除 | 替代 |
| --- | --- |
| `ja3` `akamai` `perk` | `impersonate="chrome_<major>"` |
| `interface` `curl_options` `max_recv_speed` `thread` `debug` `quote` | 无（Core 没有对应能力） |
| `doh_url` | `doh_template=` + `dns_secure_mode=2` |
| `max_redirects` | `allow_redirects=False`（单跳）；上限固定 20（`CHROMIUM_MAX_REDIRECTS`） |
| `dns_over_tls` `dns_padding` | 无（Chrome 桌面版没有 DoT / EDNS0 padding） |
| `referer` | `headers={"Referer": ...}`（Core 路由到 `SetReferrer`） |

`ExtraFingerprints` 从 15 个字段缩到 2 个（`header_order`、`form_boundary`，两项都真生效）；
`_NEUTRAL_EXTRA_FP` 整表删除，手写 mapping 里的未知键由 `validate_extra_fp` 点名拒绝。

**连带清掉的死代码**：`_validate_max_redirects`、`self.max_redirects`、`effective_max`、
`options["max_redirects"]`、`reject_fingerprint_overrides`、`PreparedRequest.quote`、
`build_url(..., quote=)` 分支、`_NEUTRAL_EXTRA_FP`。

**新增门禁** `tools/audit-compat-surface.py`：四个入口（两个构造器 + 两个 `request()`）的
每个关键字都必须有判决（ENGINE / REQUEST / FACADE），已删除的 14 个必须缺席。三个方向都
做过变异测试（新关键字、已删关键字回来、判决没有对应关键字）。

`multipart=` 与 `CurlMime` 也一起删掉：实测同一组 part 用 `files=`（含 `(None, data)` 无文件名
字段、3 元组 content-type、4 元组 per-part headers）与 `CurlMime` 产出的 body **逐字节相同**，
两者只差 part 顺序。渲染器保留为私有 `_MultipartBody`（`encode_multipart` 仍在用），
`chrome_client.CurlMime` 不再导出。

`accept_encoding=` 也删了：六次探针（默认、`identity`、`gzip`、`br`，以及两种 `headers=` 写法）
到线全部是 profile 值 `gzip, deflate, br, zstd`——Chromium 自己决定这个字段，关键字承诺了一个
Core 给不了的控制。`Session.request` 31 → 30。

**ABI 未变**，九个平台产物不需要重建；`audit-pypi-metadata.sh` 的重定向载体随契约改写
（从「只能是 None」改成「说不上是参数」），同样做过变异测试。

规模：构造参数 51 → 41（`AsyncSession` 52 → 42），`Session.request` 43 → 31；
测试 612 → 608 条（合并/删除针对已删关键字的断言，并新增「它们确实不在了」的断言）。
### 变更（main，最近一个 tag 之后）：`header_policy` 默认 `compat`

`Connection: keep-alive` 这类从 devtools 复制出来的头出现在大量旧脚本里，逐个报错的迁移
成本换不到任何安全性——这些字段在 Chromium 里根本写不进去。因此 facade 的默认改成三档：

| 值 | 六个 hop-by-hop 字段 | 五个「Chromium 决定值」的字段 |
| --- | --- | --- |
| `compat`（默认） | 移除，计入 `dropped_headers` | 照旧发送 |
| `strict` | 抛 `UnsupportedFeature` | 照旧发送 |
| `drop` | 移除并计数 | 也移除（profile 抓到的值上线） |

**为什么不是把 `drop` 设为默认**：那会连 `Cookie`、`Host`、`Content-Length`、
`Transfer-Encoding`、`Accept-Encoding` 一起静默丢弃。实测过一次：11 条既有测试红，
其中两条正是「调用方给的 `Cookie` 头必须原样上线」——对这种字段，「发出去」和「被丢掉」
是两种不同结果，而调用方常常依赖前者。所以默认只收窄到「根本写不进去」的六个。

ABI 侧不变：`MN_HEADER_POLICY_REJECT = 0` 仍是零值，`drop` 才传 `MN_HEADER_POLICY_DROP`；
`compat` 由 facade 先滤掉那六个名字、再按 REJECT 调用，所以九个平台产物无需重建。

进程级开关仍是 `CHROME_CLIENT_HEADER_POLICY` / `set_default_header_policy()`，只是默认值
从 `strict` 变成 `compat`；`Session(header_policy=...)` 显式值优先级最高。
### 验收（2026-11-26，ABI v15）

这一轮把「哪些头名不可写」从三个绑定层 frozenset 下沉到 Core：ABI 14 → 15，
新增 `mn_header_disposition` 一个符号（32 → 33），并在 `mn_request_config_t` 末尾追加
`header_policy`。结构体大小不变——该字段落进
`strip_authorization_on_cross_origin_redirect` 之后的既有 padding，x86_64 仍是 152
字节、i686 仍是 100 字节，`audit-abi-layout.py` 核对 8 个结构体 105 个字段。

**规则只有一份**：`mn_request_create` 按 `header_policy` 拒绝（REJECT）或移除（DROP）
表里的名字，`mn_websocket_create` 用同一张表的 WS 作用域，绑定层通过
`mn_header_disposition` 只问不抄。`audit-layer-ownership.py` 的 KNOWN 从 12 条降到 9 条。

**九平台产物**（版本串取自根 `Cargo.toml` 的 workspace 版本；本文件不复制那个数字，33 个导出符号，`audit-core-binaries.sh` 9/9）：

| target | ABI 14 尺寸 | 当前尺寸 | Δ | sha256 前缀 |
| --- | --- | --- | --- | --- |
| linux-x86_64 | 8,662,576 | 8,662,576 | 0 | `17f9b9a4` |
| linux-x86 | 8,264,304 | 8,264,848 | +544 | `86c89564` |
| linux-armv7 | 6,250,672 | 6,251,112 | +440 | `c6211cdf` |
| linux-arm64 | 8,307,744 | 8,308,640 | +896 | `5f60491e` |
| macos-x86_64 | 8,140,016 | 8,140,080 | +64 | `a5fedf42` |
| macos-arm64 | 7,476,800 | 7,476,864 | +64 | `2e6d43fa` |
| windows-x86_64 | 9,629,184 | 9,630,208 | +1,024 | `2b1f9a17` |
| windows-x86 | 7,665,664 | 7,666,176 | +512 | `df1b19fb` |
| windows-arm64 | 8,325,632 | 8,326,656 | +1,024 | `e634cb53` |

尺寸余量：linux 2.01%、windows 2.03%、macos 2.04%（`audit-core-size-ceilings.py`）。
ABI 15 的产物先在前一个版本下建成、随后随本次版本推进重新链接；只有版本串变化，九个目标
**尺寸逐字节不变**，只有 sha256 变了。

**门禁**：静态 27 项全绿（含 `audit-profile-probes.sh`：feature boundary 与 profile
isolation 通过；`profile_state_isolation_probe` 因缺 `HTTP_BASE` / `HTTPS_BASE` 按文档
跳过）、`cargo fmt/check/clippy/test`、`python36` 的 `clippy --locked`、
`audit-python36-artifact.py`（本次构建的 manylinux wheel）、`audit-doctests.py`、
`mutation-fingerprint-gates.sh` 7/7 全部通过，Python 套件 607 条 163 秒 OK（2 skip）。

**本轮同时修掉的门禁缺陷**（两条都先红过）：

1. `tools/audit-core-binaries.sh` 里有**第四份**手写导出表。ABI 15 加符号后九个产物
   全部正确，门禁却对 linux-x86 报 `set differs`——它自己还停在 32 个。改为读
   `core/exports/minicronet.exports`（`audit-abi.sh` 已证明四份载体一致）。
2. `tools/mutation-fingerprint-gates.sh` 两条 session-parity 变异自 P0 加入
   `header_policy=` 之后就锚不上代码了，`assert` 失败被计成
   `MUTATION NOT DETECTED`——**这条门禁从那个提交起一直是红的**。改为按选项名定位。

**体积**：复测 `optional_trace_events_enabled = false`（「已排除的精简候选」里已有的
结论），linux-x86_64 全量重编后产物 sha256 与改动前完全一致、8,662,576 字节——
结论被复现，不是新发现。args.gn 已还原，`record-core-build-args.py --check` 9/9。

**仍然做不到的**：只有 linux-x86_64 被真实加载并发请求（`run-http-smoke.py`、
`minicronet_smoke`、`minicronet_abi_cpp_smoke` 与 607 条 Python 用例），其余八个平台
仍只有 payload 字节一致与静态审计。`audit-v9-profile-fields.py` 需要 19 MiB 的独立抓包
证据集（按设计不进仓库），本机没有，因此该步在本机不可运行——不是通过。

---

### 验收（2026-11，ABI v14）

这一轮把能力查询与平台身份下沉到 Core，ABI 从 12 升到 14（新增
`mn_engine_capabilities`、`mn_platform_user_agent_group`、`mn_platform_hint` 三个
符号，29 → 32）。

**静态门禁 27/27 全绿**：`audit-abi`（v14，32 符号）、`audit-abi-layout`（8 结构体
104 字段）、`audit-layer-ownership`、`audit-readme`、`audit-pypi-metadata`、
`audit-python-stubs`、`audit-doctests`、`audit-extra-fp`、`audit-session-parity`、
`audit-frozen-headers`、`audit-discard-fingerprint`、`mutation-fingerprint-gates`、
`audit-client-hints`、`audit-python-typing`、`audit-core-binaries`（**9/9**）、
`audit-core-inputs`、`audit-core-source-fingerprint`、`audit-core-size-ceilings`、
`audit-core-payloads`、`record-core-build-args --check`、`audit-padding-rule`、
`audit-tls-version-range`、`audit-h1-alps-absence`、`audit-h3-profile-support`、
`audit-h2-framing`、`audit-v9-profile-fields`、`audit-python36-artifact`；
Rust 四项（`fmt` / `check` / `clippy -D warnings` / `test`）全过。

**九平台产物全部为 ABI 14**，每个 32 个导出符号；`python36` wheel 同步重建。

**这一轮修掉的问题**（细节见 `docs/LESSONS.md` §86-88）：

1. `quic://` 代理从「硬编码不支持」改为由 Core 报告能力位决定。同时发现
   `mn_engine_capabilities` 的位 1（bracketed 链）**无法兑现**——ABI 存不出多跳链，
   已改为保留位而不是谎报。
2. `proxy=` 与 `proxy_chain=` 走两条不同解析路径，同一个代理换个选项名就一个能用一个
   报 `InitializationFailed`；`socks://` 在 `engine.cc` 里根本没有分支。
3. 全无效的 `proxy_rules`（如 `http=garbage://x;https=garbage://y`）被 `ACCEPTED`
   之后**静默直连**，代理端口零连接而请求返回 200。已按 `type` 分两种形态检查。
4. 模块级 `chrome_client.get(url, impersonate=...)` 只发 4 个头——`impersonate` 与
   `platform` 不在 `api._SESSION_ONLY` 里，抓包头一个都没注入。
5. 平台身份有两份独立派生（Python 的 `sys.platform` 与 Core 的 `ARCH_CPU_*`），
   在同一台机器上加载 32 位构建时会分叉。已下沉到 ABI v14。

**`linux-armv7` 仍未加载、未发请求**，本轮尝试用 `qemu-arm` 补上但段错误，原因未查明
（详见 `docs/NEXT_STEPS.md`）。因此不发布 ARMv7 wheel。

---

### 验收（2026-11，ABI v12）

（发布日期与版本号只在根 `Cargo.toml` 的 `workspace.package.version`，本文不复述——复述过，且因此漂了两版。）

**静态门禁全绿**：`audit-abi`（v12，29 符号）、`audit-abi-layout`（8 结构体
104 字段）、`audit-layer-ownership`、`audit-readme`、`audit-pypi-metadata`、
`audit-python-stubs`、`audit-doctests`、`audit-python-typing`、`audit-extra-fp`、
`audit-client-hints`、`audit-core-binaries`（**9/9**）、`audit-core-inputs`、
`audit-core-source-fingerprint`、`audit-core-size-ceilings`、`audit-core-payloads`、
`record-core-build-args --check`、`audit-session-parity`（含「构造选项是否真的到达
Core」这一项）、`audit-python36-artifact`；Rust 四项
（`cargo fmt` / `check` / `clippy -D warnings` / `test`）全过。

Python 套件 **561 个用例通过、2 个 skip**。

**九平台产物全部为 ABI 12**：linux-x86/x86_64/arm64/armv7、
windows-x86/x86_64/arm64、macos-x86_64/arm64。

**这一轮修掉的两个缺陷，都是「门禁声称验证了、实际没有」**：

1. `bindings/python36` 无法发出任何请求。共享 facade 无条件传 11 个位置参数与 11 个
   关键字给 `PyEngine`，而 3.6 扩展的构造签名只有 11 个参数、`request` 只有 10 个。
   第一个请求就 `TypeError`。`audit-python36-artifact.py` 当时在 `native = load()`
   **之后**才改 `MODULE_NAME`，所以它测的是现代扩展，永远发现不了这个。
2. `take_body_buffers=True` 是空选项。它被存进 session 然后从未传给
   `engine.request()`，零拷贝一次都没发生过；两种行为返回的字节完全一样，没有任何
   门禁能看出来。

两条的回归测试都在 `bindings/python/tests/test_session_defects.py`
（`Py36BindingShapeTests`、`EngineOptionForwardingTests`），并且都做过变异测试：
删掉转发 / 删掉参数后测试变红。

**仍未验证的一项**：真实 Python 3.9 解释器上运行现代轮子只能由 CI 的 `python39-smoke`
证明，本机只有 3.12；同样，3.7/3.8 上安装 cp36 轮子这条路本机也无法执行。

## 兼容层审计（2026-10-14）

**本轮含一个破坏性变更**：`impersonate="chrome"` / `"chromium"` 从「解析到最新
pinned profile」改为**抛 `ImpersonateError`**，必须给出精确的 `chrome_<major>`。

理由是防漂移：裸家族名会解析到本构建恰好最后收录的 profile，一旦新增 profile，
调用方的指纹就在**无人要求**的情况下改变。对一个以指纹保真为卖点的库，静默漂移
比缺失更糟，所以按 fail-closed 处理（AGENTS.md §8）。`chrome<major>` 与
`chrome-<major>` 等拼写不受影响。版本号因此提升一个 patch 位（按维护者选择，
未进 minor 位）；具体数字只在根 `Cargo.toml`，本文件不复述。

同一轮修掉的缺陷：

| 缺陷 | 根因 | 证据 | 修复 |
| --- | --- | --- | --- |
| 调用方编辑 cookie jar 时 engine 缓存被死 generation 填满 | `_resolve_cookies` 的两条换 generation 路径不对称：`discard=True` 分支有 `discard(slot.config)`，调用方编辑分支没有。被取代的 slot 已不可达（mirror 不再匹配 jar），却仍占着有界缓存槽位 | 8 次调用方编辑 → `len(_engines)` 从 1 涨到 **8**（`DEFAULT_MAX_ENGINES` 正是 8），即全部槽位被尸体占满 | 补上 `discard`，两分支对齐；新增 `test_editing_the_cookie_jar_does_not_fill_the_engine_cache` |
| stub prose 里的 profile 版本区间漂移 | Literal 块由 `generate-python-stubs.py` 生成，但**周围散文是手写的**，没有任何东西重新生成它 | `chrome_99 .. chrome_153` 在 3 个 stub 的 **9 处**存活，跨越 154 与 155 两次新增；`available_profiles()` 的 docstring 示例返回值直接是错的 | 全部修正为 155；新增 `check_prose_version_ranges()` 门禁 |
| stub prose 交叉引用指向不存在的类型 | Sphinx 风格的 `:class:`/`:data:` 引用失效时只渲染成纯文本，既不报错也不链接 | 全库扫描 57 处裸引用，全部可解析；`_types.pyi` 的 `__all__` 里有一个**从未定义**的 `ChromeFamilyAlias` | 新增 `check_prose_type_references()` 门禁；删除悬空条目 |

**被证伪的一个假设**（未改代码）：我原本判定 `_AsyncState.notify()` 的节流提前返回
会因 Rust 侧 `notify_scheduled` 单飞闸门保持置位而永久挂死消费者。插桩三轮后
**否定**：`buffered >= STREAM_BUFFER_LIMIT` ⟹ `chunks` 非空（`buffered` 是 `chunks`
元素大小之和，只由 `__anext__` 的弹出递减，弹出后必然调 `resume()`），而消费者只在
`chunks` 为空时才停泊。实测节流返回次数与 `resume()` 重排次数**逐次相等**（37/37、
18/18），且从不在消费者停泊时发生。直接合入那个「防御性重排」补丁会引入每 1 ms 的
忙轮询并掩盖真实观察。**插桩的价值在这里：先证明可达性再决定合入。**

同时量出一个**尚未处理**的精度问题：`STREAM_BUFFER_LIMIT` 是 1 MiB，但控制只在批次
边界生效，实测 `buffered` 可达 **2,162,160 字节（2.06 倍）**，批次上限
`ASYNC_POLL_BATCH = 64` 个 chunk。行为有界且正确（`resume()` 会收敛），属于水位精度
而非泄漏；是否收紧留给后续，因为它触及 ABI v8 的 pause/resume 时序——那是历史上出过
线上故障的地方。

**新门禁都做了变异测试**（AGENTS.md §6）：破坏版本区间 → exit 1；删光全部区间 →
触发空转守卫 exit 1；注入裸的 `ChromeFamilyAlias` 引用 → exit 1；删光全部裸引用 →
触发空转守卫 exit 1。两个守卫都必需：一个「找不到任何东西就通过」的检查等于没有检查。

**测试诚实性**：我先写的第二个 engine 回归测试（断言普通请求不重建）**在修复前也
通过**，即不具备区分度，已删除而不是留下充数。Python 套件从 128 增至 131 个用例。

## 本轮审计修复（2026-09-23）

对全仓库做了一次审计与复核，修掉的问题、每条的经验和新增的门禁都记在
`docs/LESSONS.md`；面向后续改动的强制规则记在 `AGENTS.md`。摘要：

- `core_version()` 返回了一个早已过期的 `0.4.0`：Core 的版本串改为构建期从根
  `Cargo.toml` 注入（`tools/version.py` + `tools/sync-core.sh`），并把
  `docs/PROJECT_STATUS.md` 纳入版本门禁的扫描范围。
- `http_version` 对 h2/h3 谎报 `HTTP/1.1`：先改为只在能证明时返回 `None`，ABI v9 后
  改由 `mn_request_negotiated_protocol` 报告真值——状态行不再参与，`UNKNOWN` 仍如实
  报 `None`。四层（Core/Rust/pyo3/facade）已全部接通。
- 异步流式响应在 `allow_redirects=False` 或较紧的 `max_redirects` 下静默丢 body：
  流式响应现在始终挂载 reader，与同步路径一致。
- `discard_cookies=True` 仍会发出 Core 存储里的 cookie：改为切换到空 cookie store 的
  同配置 Engine，并在请求后丢弃被取代的 slot（同时修掉 engine 缓存抖动）。
- `send()` 忽略 session 默认值（含 `max_response_bytes`）：现在补齐会话级默认。
- `priority=` 的 stub 写 `int` 而运行时只接受字符串：`_types.pyi` 早已定义
  `RequestPriority: TypeAlias = Optional[Literal["default", "highest", "medium",
  "low", "lowest", "idle"]]`，但 `api.pyi` 的 10 处与 `sessions.pyi` 的 24 处签名
  仍在写 `Optional[int]`，该别名被引用 0 次、也不在 `_types.pyi` 的 `__all__` 里。
  stub 门禁只校验参数名存在、不校验注解类型，所以这 34 处一直「通过」。现已全部
  换成 `RequestPriority` 并补进 `__all__`（变异测试：把其中一处改回 `Optional[int]`
  → `audit-python-stubs.py` 报 verb 签名不一致）。
- 默认请求在 chrome_124+ 上发出 `priority: i` 而非真实 Chrome 的 `u=0, i`：ABI 的
  `MN_REQUEST_PRIORITY_DEFAULT` 与 `LOWEST` 同落 `net::LOWEST`（`request.cc:198-201`），
  urgency 算成 3（`quic_http_utils.cc:16`），而 3 是 RFC 9218 的默认值、序列化时被
  省略（`quic_stream_priority.cc:19-27`），只剩一个 `i`。真实 Chrome 导航走
  `kVeryHigh`→`net::HIGHEST`（`document_loader.cc:2142`、`web_url_request.cc:61-62`），
  urgency 0 → `u=0, i`。修法在 facade：`impersonate.default_priority()` 让 124+ 默认
  `highest`，99–123 保持不发该头（它们本来就不发，动它会改指纹）。实测 124/140/155
  现发 `u=0, i`+weight 256，99/123 仍为 absent+147；显式 `priority=` 依旧优先。
- `AsyncResponse.close()` 对异步响应是空操作并泄漏原生请求：现在会取消并摘除。
- 32 位 ABI 布局测试的三个结构体尺寸写错（88/28/76 → 84/32/80）：按头文件实测值修正。
- `[workspace.lints]` 因为成员没有 `[lints] workspace = true` 而从未生效：四个
  manifest 都已加入，并用一次故意违规确认 `E0133` 会失败。
- 历史 User-Agent 的平台令牌硬编码为 Linux：改为按 `BUILDFLAG(IS_WIN/IS_MAC)` 与架构分支。
- 仓库无 `LICENSE`、无第三方归属：新增 `LICENSE` 与 `THIRD_PARTY_NOTICES.md`（10 个
  组件的许可全文取自固定 Chromium 检出）。
- 工具脚本里的开发者绝对路径：改为 `tools/core-paths.sh` 统一解析（兄弟目录 → `$HOME/chromium`）。
- 8 个平台 Core 已按上述 Core 源码改动全部重建并刷新 manifest。

仍未闭合、已记入 `NEXT_STEPS.md` 的：ABI v9 协商协议、ABI v9 逐请求/会话级 DNS 配置、32 位布局的 CI 执行路径、
产物来源证明（重建比对/签名）、Linux 交叉构建的 pkgconf 迁移、git 历史瘦身。

## 代理认证修复（2026-09-24）

**带 realm 的 407/401 挑战永远不会认证**——这是本轮唯一一个「静默失效」级别的缺陷：
凭据从未发出，调用者拿到一个裸的 407，没有异常也没有日志。

根因在 ICU 数据集而不在代理代码：`core/icu/filter.json` 排除了 `conversion_mappings`，
而 HTTP Basic/Digest 的 realm 解码要经
`ConvertToUtf8AndNormalize(..., kCharsetLatin1, ...)` → `ucnv_open("ISO-8859-1")`，该名字是
`windows-1252-html` 转换器的别名。转换器打不开 → `ParseRealm` 失败 →
`CreateAuthHandler` 返回 `ERR_INVALID_RESPONSE` → `ChooseBestChallenge` 静默丢弃挑战 →
`GetAuthChallengeInfo()` 返回 nullptr → `NotifyAuthRequired()` 从不发生 →
Core 的 `Request::OnAuthRequired` 一次都不执行。逐层证据与调用点行号见
`docs/LESSONS.md` 第 22 条与 `core/icu/README.md`。

- `filter.json` 改为 `includelist: ["windows-1252-html"]`，数据集 191,056 → **194,064**
  字节（+3,008），每个平台的库相应 +4,096 字节
- `_NET_ERRORS` 补上 `-375 ERR_TOO_MANY_RETRIES`：凭据被拒时 Chromium 在同一个
  `URLRequest` 内重试到 `kMaxRestarts`(32)，最终报的是这个码，此前调用者只看到裸的
  `net error -375`
- 新增 `test_compat.ProxyAuthenticationTests`：夹具**真的**回 407 并断言响应体，覆盖
  带 realm 的挑战、URL 内嵌凭据、以及错密码必须失败三种情况
- 8 个平台 Core 全部重建并刷新 manifest

原先「没有任何现有功能依赖 ICU 转换器」的断言已被删除：判断数据可否裁剪时只看了本仓库的
调用点，漏掉了 **Chromium net 层自己**的消费者。

## Session 重构与 profile 请求头（2026-11）

两件事，第二件依赖第一件的结构。

### 传输上下文变成值：`SessionBinding`

`Session` 的传输配置（profile / 代理 / 凭据 / TLS 校验 / CA / UA / Accept-Language /
协议钉 / HTTP cache）原本散在十几个可变属性里，由 `_engine_config()` 每次请求现场拼。
现在它是一个不可变值 `SessionBinding`，同时充当引擎缓存键（`EngineConfig` = binding +
`generation`），所以「两条请求是否共享连接池、TLS 会话缓存、H2/H3 会话池」从推断变成
`==` 比较。`Session.binding(url)` 把它暴露出来。

`generation` 不在 binding 里：它只表示 cookie store 换代，混进传输身份正是
`discard_cookies` 历史缺陷（LESSONS §43）的根因。

`EngineSlot` 记录 in-flight 计数，`EngineCache.acquire()` 返回租约，cookie 解析换引擎时
`lease.rebind()` 把租约挪到新 slot；淘汰一个仍在使用的 slot 记为 `deferred`。
`Session.stats` 暴露 `builds`/`hits`/`evictions`/`deferred`/`inflight`——LESSONS §29 的
「每请求重建上下文」不再只能靠 TIME_WAIT 发现。

### 同步与异步各自都改，共享同一实现

| 能力 | 同步 | 异步 |
| --- | --- | --- |
| `SessionBinding` / `binding()` | ✅ | ✅ |
| `stats` 计数器 | ✅ | ✅ |
| 租约 pin/release + `rebind` | ✅ | ✅ |
| cookie 解析串行化（`_cookie_lock`） | ✅ | ✅ |
| 零复制流式重切块 | `_stream_chunks` | `_astream_blocks`，共用 `_take` |
| 单次聚合复制 | — | `b"".join`，替代 `bytearray` + `bytes()` 两次复制 |
| `close()` 后仍在飞的流可读完 | ✅ | ✅ |

重切块实测（4 MiB / 64 KiB buffer）：512B 块 602 → 1877 MiB/s（3.1×），
4 KiB 块 4042 → 7568 MiB/s（1.9×）。对齐的 buffer 原样交出，由对象 identity 断言锁定。

### 默认请求头 = profile 自己的抓包

`impersonate=` 原本只换 `User-Agent` 版本串，一个 `chrome_155` 会话发 4 个头，而那个版本
发 14 个。现在表由 `tools/generate-client-hints.py` 从六条独立抓包生成（六条必须全等），
`browser_headers.py` 把它变成有序头列表，`BaseSession._browser_headers` 在唯一的
`prepare_request` 入口合并——同步与异步共用。**72/72 profile 逐字节且顺序一致。**

两处刻意偏离抓包，理由写在 README 与 `docs/COMPATIBILITY_BOUNDARY.md`：平台跟随宿主
（与 Core 的 UA 推导一致，`"Windows"` hint 配 `X11; Linux` UA 更容易被检出）、
`Sec-Fetch-*` 取导航语义（`session.get(url)` 就是导航）。两者都可被调用方按字段覆盖，
`default_headers=False` 可整体关闭，`impersonate=None` 一个都不注入。

顺带修掉一个既有缺陷：`headers={"X": None}` 之前发的是字面量 `X: None`——`Headers.add`
把 `None` 字符串化了，而 requests 的语义是删除。`Headers` 现在记录删除（`deleted_names`），
`merge_setting` 转发到合并结果。

仍未闭合（ABI v9）：让 Core 自己注入。facade 注入的头不经过 `net` 层的 URLLoader 工厂，
所以 Chromium 的 `Sec-Fetch` 策略与 `Accept-CH` 协商不作用于它们。见 `docs/NEXT_STEPS.md`。

---

## 门禁现状

| 检查 | 状态 | 在 CI |
| --- | --- | --- |
| `tools/audit-abi.sh` | 20 个符号，header / FFI / Core / 三份导出表一致；注释掉的声明不算声明 | 是 |
| `tools/audit-abi-layout.py` | 7 个结构体、70 个字段：用 C 编译器取头文件真实的 `offsetof`/`sizeof`，与 Rust `#[repr(C)]` 声明逐字段比对 | 是 |
| `tools/audit-core-binaries.sh` | 9 个平台通过；比对 manifest 的 `chromium_revision` 与 `CHROMIUM_REVISION`，并**从产物字节里断言含 `chrome_86` 与 `chrome_158`**，所以「只重建了一个平台」不再通过 | 是 |
| `tools/audit-core-payloads.py` | 8 个平台产物必须逐字节嵌入生成头里的 profile 载荷 |
| `tools/audit-core-size-ceilings.py` | 三档 `MAX_BYTES` 必须等于「该平台最大产物 × 1.02，向上取整到 1 万」。实测 Linux 8,540,000 / Windows 9,610,000 / macOS 8,120,000 全部满足；**新增 profile 使产物变大时，上限必须同批重算**，否则这里失败 | 是 |
| `tools/audit-net-error-names.py` | `_NET_ERRORS` 的 69 行逐条与 `net_error_list.h` 比对：0 处错标、0 处陈旧、**0 个未映射的代理类错误**。注释掉的 `// NET_ERROR(...)` 不算定义（17 个已移除码因此不会被误认为当前码） | 否，需 Chromium 树 |
| `tools/audit-readme.sh` | 通过 | 是 |
| `cargo fmt` / `clippy -D warnings` | 通过 | 是 |
| `cargo test --workspace` | 18 个单元测试通过（`request.rs` 16、`minicronet-sys/lib.rs` 2）；含 panic 不穿越 FFI 边界、丢弃 body stream 不搁死请求、`Some(Duration::ZERO)` 仍是真实 deadline 三项 | 是 |
| Python 套件 | 320 个用例通过，2 个 skip（未设 `MINICRONET_WS_URL`/`MINICRONET_WSS_URL`，无真实 WS/WSS 端点） | 是 |
| `tools/audit-client-hints.py` | 72 个 profile 的请求头表从 `profiles/chrome_<major>/h1/captures.json` 重新推导；另断言每条抓包的 UA 版本串等于 Core 的 `kFullVersions`、`sec-ch-ua-platform` 只出现在抓包有的版本上。变异测试四条（改值/换序/删 profile/凭空加 hint）全部非零退出 | 是 |
| `tools/audit-python-stubs.py` | 17 个 stub 模块，0 错；含 prose 版本区间与裸交叉引用两项扫描（都有空转守卫） | 是 |
| `tools/audit-python-typing.py` | mypy 检查存根与消费者，两者都干净；**缺 mypy 时非零退出**（`--optional` 是显式逃生门，CI 与门禁清单都不传），不再静默跳过 | 是 |
| Core manifest 与 `CHROMIUM_REVISION` 一致 | 8 个平台通过 | 是 |
| `tools/audit-pypi-metadata.sh` 的扩展分界断言 | `abi3-py3N`（N=9）== facade 的 `(3, M)`（M=9），另断言 py36 仍 `=0.15.1`+`abi3-py36`、现代 crate 不得用 `abi3-py37/38`、pyo3 必须 `=` pin | 是 |
| 真 Python 3.9 上安装并实发一次请求 | `build-wheels.yml` 的 `python39-smoke`；已进 `publish.needs`，装不上的轮子无法发布 | 是（需要构建产物） |
| 扩展可 import 且能发一次真实请求 | 通过 | 是 |
| `tools/audit-core-linux.sh` | 通过（体积上限 8,540,000；Linux 三目标最大 8,367,664） | 否，需 Chromium 树 |
| 8 个平台构建 | 全部通过 | 否，需 Chromium 树与交叉工具链 |
| `tools/generate-profile-table.py` | 重新生成与已提交表逐字节一致 | 否，未接入 |
| `tools/audit-padding-rule.py` | 144 组 profile/协议：86–118 固定宽度、119–123 条件、124+ 永不；**并断言编译进 Core 的表里 `client_hello_padding_length` 全为 0**（宽度是规则的输出，不是配置值） | 是 |
| `tools/audit-tls-version-range.py` | 86–97 下限 TLS1、98+ TLS1_2，读生成表而非 JSON 输入 | 是 |
| `tools/audit-h1-alps-absence.py` | 72 个 profile：h1 从不发 ALPS、h2 从 91 起发；参考抓包集存在时额外做 wire 对比，缺失时打印 SKIP | 是 |
| `tools/audit-h3-profile-support.py` | 72 个 profile，86–92 标为不支持 H3，其余支持 | 是 |
| `tools/audit-profile-probes.sh` | `profile_feature_probe`（含 chrome_92/93 的 H3 边界）与 `profile_isolation_probe`（86–157 全区间）真实运行通过；`profile_state_isolation_probe` 因缺 HTTP/HTTPS harness 显式 SKIP | 否，需 Chromium 树 |
| `tools/verify-wire-capture.py` | chrome_152 `wire_verified: true` | 否，需抓包 |
| `tools/inspect-client-hello.py` | 11 个 profile 改动前后指纹一致 | 否，需已构建扩展 |

Python 套件能进 CI 是因为 linux-x86_64 Core 已提交在仓库里；已用一份浅克隆验证过
从零检出可以跑通（装 `libnss3`、`cargo build --release -p chrome-client-python`）。
套件按关注点分文件：`test_stability.py`（生命周期、并发、背压）、`test_compat.py`
（requests / curl-cffi 兼容面、cookie 会话、代理路由、泄漏、并发）、
`test_session_binding.py`（传输绑定、租约、cookie 串行化、零复制重切块，同步与异步各自覆盖）、
`test_client_hints.py`（72 个 profile 的请求头逐字节复现、覆盖语义、两条删除路径）。

仍在 CI 之外的是需要 Chromium 源码树的两项：`audit-core-linux.sh` 的源码级审计
（529 个 net 源文件、166 处 FeatureList 读取、体积上限）和 8 个平台的构建。它们依赖
一份 70 GB 的固定 revision 检出加 xwin/osxcross 工具链，免费 runner 承担不了。

## 本轮（2026-11-26）：并发/内存/体积的实测与判定

代码未变（一次尺寸档试验已完整回退，`core/binaries/linux-x86_64/libminicronet.so` 重建后
strip 与提交产物逐字节相同）。门禁与泄漏全部复跑：

| 项 | 结果 |
| --- | --- |
| 15 项静态门禁（abi / layout / layer-ownership / ceilings / source-fingerprint / binaries / payloads / compat-surface / session-parity / frozen-headers / discard-fingerprint / client-hints / binding-surface / extra-fp / stubs） | 全绿 |
| Python 套件 | 609 tests, OK (skipped=2) |
| 六形态泄漏 `leak-probe.py` | 6/6 flat（fd +0；RSS −48…+280 KiB） |
| 基准（三次，本机） | 同步 485–555 req/s、异步 32 823–905、异步 128 832–953、流式 106–130 MiB/s |

并发侧的归因（临时 `ThreadTicks` 插桩，已删除）与三条被排除的路线、以及 `//base`
`optimize_max` 的否决数据，分别记在 `docs/CORE_BINARY_SIZE_PLAN.md` 与
`docs/NEXT_STEPS.md` 的同名小节。结论一句话：每请求 0.949 ms 网络线程 CPU 里 0.564 ms
是 Chromium 的 URLRequest 机器，剩下能动的只有唤醒次数，也就是已获准的 ABI v16 批量唤醒。

## 验收（2026-11-26，ABI v16）

- **九个平台产物重建并安装**：35 个导出符号，`audit-core-binaries.sh` 9/9，`targets.json abi_version=16`，manifest 的 SHA-256 与 `core_sources_sha256` 均已刷新。
- **批量唤醒实测**（同一二进制，只切 `CHROME_CLIENT_WAKEUP_BATCH`）：128 并发 792 → 1,104 req/s、p99 186 → 130 ms；512 并发 866 → 1,090 req/s、p99 550 → 421 ms。
- **本地门禁 31/31**；`bindings/python/tests` 609 用例 OK（skip 2）；`cargo test --workspace` 24 用例；六形态泄漏 flat（fd +0，RSS 最差 +24 KiB）。
- **体积**：linux-x86_64 8,666,672（较 v15 +4,096），九平台合计 72,792,155；linux 上限按「最大产物 × 1.02」重导为 8,850,000。
- **同期修掉的缺陷**：`__version__` 未随版本 bump、cp36 扩展的 `start_async`/`attach_async` 缺 `batch` 形参、装包内扩展的加载器自递归、`cargo fmt`/clippy 各一处、maturin 复用被 auditwheel 改写过的 `.so`。
