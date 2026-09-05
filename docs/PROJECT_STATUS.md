# chrome_client 项目状态

记录时间：2026-09-05。本文覆盖已完成、进行中、已知缺陷和后续计划。数字都来自本机
实测，可用 `tools/` 下的脚本复现。

## 项目定位

用真实 Chromium 网络栈实现的 HTTP/WebSocket 客户端，提供 requests / curl-cffi 风格
的 Python API，同时让 TLS 指纹、HTTP/2 SETTINGS、QUIC 参数与真实 Chrome 一致。

分层：`libminicronet`（Chromium C++，唯一协议实现）→ C ABI v8（20 个导出符号）→
`minicronet-sys`（手写 FFI 声明）→ `minicronet`（Rust 安全层）→ 各语言薄绑定。

- Chromium revision：`010786339149198c8c24d58c30cf5a41fcf60c14`（MAJOR=153，2026-08-04）
- Python 发布版本 0.2.2，crate 版本 0.2.2（两者同号，不再有四段版本号映射）
- Chrome profile：`chrome_99` — `chrome_152`，54 个

## 已完成

### 阶段 0：可复现构建与基线

- 从 `new/` 迁入 12 个 Chromium 补丁（`core/patches/`）、8 个平台构建脚本、
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

### chrome_152 已加入（54 个 profile）

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

**trust_anchors 载荷：集合是 profile 数据，顺序不是。** 28 个 ID 按观测冻结——编译期
根库在 152 tag 和固定树里都是 32 个，抓包那 28 个是子集，机器的根库被 PKI Metadata
组件更新过，任何源码树里都没有这个状态。但顺序**故意不冻结**：
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

证据与复现命令见 [`profiles/chrome-152/`](../profiles/chrome-152/README.md)。

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
`curl_options`、`referer` 等）显式抛 `UnsupportedFeature` 而不是静默忽略。

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
macOS 12,000,000 → 8,750,000。macOS 那一档原来虚高 3 MB 以上，等于没有门禁。

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
8/8 通过，ABI 仍是 v8）：

| 目标 | 大小（字节） | SHA-256 前缀 |
| --- | --- | --- |
| linux-x86 | 8,555,652 | `462635f0e53cfeac` |
| linux-x86_64 | 8,955,744 | `e2cb4c7b8956c492` |
| linux-arm64 | 8,490,832 | `cdd52ff2e640add0` |
| windows-x86 | 8,971,264 | `cb9bbb7a738cc905` |
| windows-x86_64 | 11,291,648 | `504868b04c106c21` |
| windows-arm64 | 9,221,120 | `776ca5d97becf441` |
| macos-x86_64 | 8,618,588 | `60bf3a174c3d0163` |
| macos-arm64 | 7,806,416 | `9a9b5f58b4b5e7f8` |

（表中是体积精简后的值；证书修复那一轮的 SHA 已被这一轮覆盖，两处改动都在里面。）

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
  上游那几个 audit 生成器（`extract-*`、`build-normalized-profiles.py`）和 4 个
  `profile_verification` 探针；新增 profile 目前靠 `tools/collect-profile-evidence.py`
  加 `tools/verify-wire-capture.py` 走通，全流水线迁移还没做
- Linux x86/ARM64 交叉编译目前还依赖 `new/.tools/pkgconf` 的 deb（通过
  `MINICRONET_PKGCONF_DIR` 指向解包目录），因为 gn 为 NSS 调用 `pkg-config`；
  仓库里没有这一层，换机器需要先解包，见上文「证书错误码修复的跨平台验收状态」

### 阶段 5：profile v2 剩余专题

`new/PROFILE_V2_TODO.md` 的 7 个专题里只有第 3 项（FeatureList 快照）标了完成，
其余 6 项需逐项核对：TLS 扩展集合与顺序、完整 H2 数值 SETTINGS、WebSocket 握手
差异、代理连接池、session resumption/0-RTT/Alt-Svc、cache/cookie/连接池策略。
需要抓包证据支撑，建议单独排期。

### WebSocket 背压

现在 `pending_data_` 计数不阻塞线程，但队列超限时 fail-closed 关闭连接。改成消费者
驱动的 pause/resume 需要能端到端验证的 WS/WSS 环境（`test_websocket_sync_and_async`
目前因缺少 `MINICRONET_WS_URL` 而 skip）。

### 发布

PyPI 上的 0.2.1.1 同时存在背压挂死和 IDN 崩溃，两者都已在本地修好但**未发布**。
发布需要打 `v*` tag 触发 `pypa/gh-action-pypi-publish`，属于不可撤销操作。

## 门禁现状

| 检查 | 状态 | 在 CI |
| --- | --- | --- |
| `tools/audit-abi.sh` | 20 个符号，header / FFI / Core / 三份导出表一致 | 是 |
| `tools/audit-core-binaries.sh` | 8 个平台通过 | 是 |
| `tools/audit-readme.sh` | 通过 | 是 |
| `cargo fmt` / `clippy -D warnings` | 通过 | 是 |
| `cargo test --workspace` | 8 个单元测试通过（6 个回调/背压 + 2 个 ABI 布局） | 是 |
| Python 套件 | 103 个用例通过，1 个 skip（真实 WSS 端点，握手已由本地服务器覆盖） | 是 |
| `tools/audit-core-linux.sh` | 通过（体积上限 9,250,000） | 否，需 Chromium 树 |
| 8 个平台构建 | 全部通过 | 否，需 Chromium 树与交叉工具链 |
| `tools/generate-profile-table.py` | 重新生成与已提交表逐字节一致 | 否，未接入 |
| `tools/verify-wire-capture.py` | chrome_152 `wire_verified: true` | 否，需抓包 |
| `tools/inspect-client-hello.py` | 11 个 profile 改动前后指纹一致 | 否，需已构建扩展 |

Python 套件能进 CI 是因为 linux-x86_64 Core 已提交在仓库里；已用一份浅克隆验证过
从零检出可以跑通（装 `libnss3`、`cargo build --release -p chrome-client-python`）。
套件现在分两个文件：`test_stability.py`（生命周期、并发、背压）和 `test_compat.py`
（requests / curl-cffi 兼容面、cookie 会话、代理路由、泄漏、并发）。

仍在 CI 之外的是需要 Chromium 源码树的两项：`audit-core-linux.sh` 的源码级审计
（529 个 net 源文件、166 处 FeatureList 读取、体积上限）和 8 个平台的构建。它们依赖
一份 70 GB 的固定 revision 检出加 xwin/osxcross 工具链，免费 runner 承担不了。
