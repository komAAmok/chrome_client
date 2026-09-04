# chrome_client 项目状态

记录时间：2026-09-03。本文覆盖已完成、进行中、已知缺陷和后续计划。数字都来自本机
实测，可用 `tools/` 下的脚本复现。

## 项目定位

用真实 Chromium 网络栈实现的 HTTP/WebSocket 客户端，提供 requests / curl-cffi 风格
的 Python API，同时让 TLS 指纹、HTTP/2 SETTINGS、QUIC 参数与真实 Chrome 一致。

分层：`libminicronet`（Chromium C++，唯一协议实现）→ C ABI v8（20 个导出符号）→
`minicronet-sys`（手写 FFI 声明）→ `minicronet`（Rust 安全层）→ 各语言薄绑定。

- Chromium revision：`010786339149198c8c24d58c30cf5a41fcf60c14`（MAJOR=153，2026-08-04）
- Python 发布版本 0.2.1.1，crate 版本 0.2.2（Cargo 不接受四段版本号）
- Chrome profile：`chrome_99` — `chrome_151`，53 个

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

## 进行中

没有正在执行的改动。下一步的候选见「后续计划」，其中阶段 3 剩下的两项（进程级唯一
网络线程、body 三次拷贝）都需要先量化再动手。

## 已知缺陷与阻塞

### 不链接 NSS：上游不支持该配置

`use_nss_certs = false` 在 Linux 上**编译不过**，两处上游缺口：

1. `net::TestRootCerts::Init()` —— `test_root_certs_builtin.cc` 只在
   `use_nss_certs` 分支里被加入 Linux 源码集（已用一行 GN 补丁解决）
2. `net::CreateSslSystemTrustStoreChromeRoot()` —— `system_trust_store.cc` 的平台
   `#if/#elif` 链里，**Linux 只有 `USE_NSS_CERTS` 一个分支**
   （`SystemTrustStoreChrome(chrome_root, TrustStoreNSS(...))`）

第 2 点无法只靠 GN 解决，必须新增一个「只用 Chrome Root Store、不读系统信任库」的
实现。**这会改变证书校验语义**：系统/企业安装的根证书不再被承认。真实 Chrome 在
Linux 上是会读的，所以这是一个功能行为变更，不只是去掉依赖。

缓解手段存在：ABI 的 `MN_TLS_VERIFY_CUSTOM_CA` 允许调用方自带 PEM，而
`COMPATIBILITY_BOUNDARY.md` 描述的契约本来就是「Chrome Root Store」。

当前状态：已撤回 `use_nss_certs = false`，保留 `is_cfi = false`，等决策。
收益主要在 manylinux 打包健壮性（少 3 个私有依赖 `libnss3`/`libnspr4`/`libnssutil3`），
不在体积。

### chrome_152：证据缺失

无法添加。每个 profile 的 `evidence` 要求匹配该 Chrome 版本源码树的逐文件
SHA-256、字节数和 CSPRNG / GREASE / 扩展置换 / key_share 的行号级定位，加上
`wire_verified` 的真实抓包。本机情况：

| 需要的输入 | 状态 |
| --- | --- |
| `profiles/` 里的 152 数据 | 0 条 |
| `captures/` 里的 152 抓包 | 0 组 |
| Chromium 树里的 152 tag | 0 个（单 revision 浅克隆） |
| 本机 Chrome 152 浏览器 | 无 |

复制 chrome_151 的参数改标签会让调用方以为在模拟 152 而实际发出 151 的指纹，
版本号与指纹不匹配本身就是检测信号。项目规则也明确要求 fail-closed。

解锁需要：Chrome 152 的 wire 抓包，或一个带 tag 的完整 Chromium 克隆。

### 其它待确认

- Windows manifest 的 `runtime_dependencies` 曾声明 `libplc4.so`/`libplds4.so`，
  但它们不在 `DT_NEEDED` 里（是 `libnss3` 的传递依赖），属于过度声明
- Python 3.6 wheel 从未在真实 3.6 环境验证过（本机只有 3.12）
- Windows/macOS 的审计脚本和 7 个非 x86_64 构建脚本只做过同构适配和语法检查，
  执行验证是随 ABI v8 那轮才第一次完成

## 后续计划

### 阶段 2 剩余

1. NSS 依赖移除——等证书校验语义的决策
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
- 迁入 4 个 `profile_verification` 探针与 profile 证据流水线，让
  `MIGRATION_FROM_NEW.md` 的「不再依赖 new/」对全部路径成立
- Linux x86/ARM64 交叉编译目前还依赖 `new/.tools/pkgconf`（通过
  `MINICRONET_PKGCONF_DIR` 指定）

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
| Python 套件 | 16 个用例通过，1 个 skip（需 WS 端点） | 是 |
| `tools/audit-core-linux.sh` | 通过（体积上限 9,250,000） | 否，需 Chromium 树 |
| 8 个平台构建 | 全部通过 | 否，需 Chromium 树与交叉工具链 |

Python 套件能进 CI 是因为 linux-x86_64 Core 已提交在仓库里；已用一份浅克隆验证过
从零检出可以跑通（装 `libnss3`、`cargo build --release -p chrome-client-python`、
16 个用例 15 通过 1 skip）。

仍在 CI 之外的是需要 Chromium 源码树的两项：`audit-core-linux.sh` 的源码级审计
（529 个 net 源文件、166 处 FeatureList 读取、体积上限）和 8 个平台的构建。它们依赖
一份 70 GB 的固定 revision 检出加 xwin/osxcross 工具链，免费 runner 承担不了。
