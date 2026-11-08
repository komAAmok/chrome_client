# 后续工作清单

本文件是 `chrome_client` 在 `main` 分支上的执行顺序。它描述
“已存在的文件”与“已验证的能力”之间的区别；没有通过对应命令或真实目标机
测试的项目不能标记为完成。

## 约束

- `libminicronet` 是唯一的 TLS、HTTP/1.1、HTTP/2、HTTP/3/QUIC、
  WebSocket/WSS 和 HTTP/HTTPS/SOCKS4/SOCKS5 实现。
- Rust 只负责稳定 C ABI、所有权、回调、Future/Stream、取消、超时和错误映射。
- Python、Go、Node.js 只能做薄绑定，不得复制任何网络协议实现。
- Android、Java、公共 JNI、Chrome UI、Mojo、Variations 和无关 Chromium
  target 不进入发布物。
- 随机值、会话 ID、GREASE、ClientHello/QUIC padding、WebSocket key 等必须
  继续由 Chromium/BoringSSL/QUICHE 的 CSPRNG 生成，发布配置不得注入固定种子。
- 版本 profile 只覆盖已审计的 Chrome 99--155 差异；无法证明的行为必须
  `fail-closed`，不能用当前 Chromium 行为冒充历史版本。

## 阶段 1：多平台 Rust target 与 linker（当前首项）

- [ ] 添加本地可覆盖的 `.cargo/config.toml`；绝对路径只能由环境变量或 wrapper
  提供，不能写成唯一的机器路径。
- [ ] 为每个 target 建立独立 linker wrapper，并在 wrapper 中固定目标三元组、
  sysroot/SDK 和 Core import library：

  | 目标 | Rust target | 工具链边界 |
  | --- | --- | --- |
  | Linux x86 | `i686-unknown-linux-gnu` | Chromium i386 sysroot |
  | Linux x86_64 | `x86_64-unknown-linux-gnu` | 本机 glibc/NSS 或匹配发布 sysroot |
  | Linux ARM64 | `aarch64-unknown-linux-gnu` | Chromium ARM64 sysroot |
  | Windows x86 | `i686-pc-windows-msvc` | xwin Windows SDK/CRT + `lld-link` |
  | Windows x86_64 | `x86_64-pc-windows-msvc` | xwin Windows SDK/CRT + `lld-link` |
  | Windows ARM64 | `aarch64-pc-windows-msvc` | xwin Windows SDK/CRT + `lld-link` |
  | macOS x86_64 | `x86_64-apple-darwin` | osxcross + macOS SDK |
  | macOS ARM64 | `aarch64-apple-darwin` | osxcross + macOS SDK |

- [ ] 运行 `cargo build --workspace --target <target>`，不能只以安装了 Rust
  std target 或 `cargo check` 作为完成依据。
- [ ] Linux x86/ARM64 做 ELF 架构、解释器、sysroot 和动态依赖审计；不能用
  x86_64 glibc 冒充其他架构。
- [ ] Windows 做 PE 架构、导出符号、import library、`icudtl.dat` 配对审计。
- [ ] macOS 做 Mach-O 架构、最低系统版本、install name 和系统框架审计。
- [ ] 目标机或对应 GitHub runner 完成真实 H1/H2/H3、WS/WSS、代理、取消和
  TLS 验证；交叉编译成功不等于运行时成功。

## 阶段 2：Core 与 ABI 发布闭合

- [ ] 以 `core/abi/minicronet.h` 和 `core/abi/ABI_VERSION` 为唯一 ABI 来源，
  对照 `crates/minicronet-sys/src/lib.rs` 的 FFI 声明和 `core/exports/` 下的
  `minicronet.def`/`minicronet.exports`/`minicronet.lds` 三份导出表，做逐字段、
  尺寸、对齐、导出符号审计。
- [ ] 对 8 个 `core/binaries/<target>/manifest.json` 重新计算 SHA-256、大小、
  Chromium revision、ABI 和运行时依赖。
- [ ] Linux 保持架构匹配的 NSS/NSPR 与 glibc 动态依赖；不静态链接 glibc/NSS，
  不把其他架构的库打包进来。
- [ ] Windows 保证 DLL、`.lib` 与 `icudtl.dat` 同架构同版本。
- [ ] 运行 `tools/audit-core-binaries.sh`，并保存每个平台的审计结果。
- [ ] 发布包不得包含 Chromium 源码、GN/Ninja 输出、`.o`、静态归档、Siso
  缓存、smoke 可执行文件、Android/Java/JNI 文件或本地证书私钥。

## 阶段 3：Rust 安全层审计与冻结

- [ ] 核对 `minicronet-sys` 与 ABI v8：结构体、枚举、回调签名、`size/version`
  前置字段和所有导出函数一一对应。
- [ ] 验证 Engine、Request、ResponseStream、WebSocket 的创建、重复 release、
  异步回调、Engine 关闭、跨线程 `Send/Sync` 和 panic 边界。
- [ ] 验证固定上传、分块上传、自动/手动/错误重定向、缓存模式、Cookie、请求头、
  超时、取消、网络错误、32 并发。
- [ ] 验证直连、HTTP/HTTPS 代理、SOCKS4/5、WS/WSS 以及 H2/H3 Extended CONNECT。
- [ ] 只保留 `futures-core` 这类最小接口依赖；Rust 不引入第二套网络栈。
- [ ] 完成 `cargo fmt --check`、`cargo check --workspace --all-targets`、
  `cargo clippy --workspace --all-targets -- -D warnings` 和离线单元测试后冻结
  公共 Rust API。

## 阶段 4：Chrome profile 与隔离

- 历史证据和规范化参数已迁入 `profiles/`（`normalized_profiles.json`、
  `effective_protocol_params.json`、`network_feature_snapshots.json`，以及每个发布
  版本一个证据目录 `profiles/chrome_152/`、`profiles/chrome_153/`）；运行时只编译
  精简只读表，不把源码证据文本编入二进制。
- [ ] 验证 `chrome_86`--`chrome_157` 的 TLS/ALPN、H2 SETTINGS、QUIC/H3、WS
  和关键 FeatureList 差异；未验证版本保持拒绝或明确降级状态。
- [ ] 确认 ProfileContext 在 Engine 创建时冻结，profile namespace 进入连接池、
  TLS/QUIC session、Alt-Svc、HTTP cache、Cookie 和代理复用键。
- [ ] 同一进程创建多个 profile Engine，确认无进程级可变 FeatureList 或缓存污染。
- [ ] 三次独立 ClientHello/QUIC/WS 连接检查随机性，不比较需要随机变化的字段的
  单次哈希相等。

## 阶段 5：语言绑定

- [ ] 先冻结 Rust API，再实现 Python、Go、Node.js 薄绑定。
- [ ] 绑定只做字符串/字节/回调/错误类型转换，不实现 TLS、HTTP、QUIC、WS 或代理。
- [ ] 每个绑定至少有一次真实 Linux x86_64 smoke，并检查 Engine/Request 生命周期。
- [ ] 为其他平台提供加载路径、Core 选择和运行时依赖说明；不在绑定层复制 Core。

## 阶段 6：CI、打包与发布

- [ ] CI 分离 Rust 结构检查、Core 二进制审计、各平台交叉编译和目标机运行测试。
- [ ] 免费 GitHub runner 只承担可复现构建/静态审计；需要真实运行的架构使用对应
  runner 或目标机，不把交叉编译当作运行时验收。
- [ ] 生成每个 target 的可追溯 manifest、符号表摘要、依赖清单和测试报告。
- [ ] 只发布 `core/abi`、对应 Core/依赖、Rust crate 和薄绑定；排除证据抓包、缓存、
  私钥、临时 CA、构建目录和开发工具缓存。
- [ ] 在打 tag 前重新执行全矩阵并记录 Chromium revision；升级 revision 必须重新
  生成 profile、重新构建和重新抓包审计。

## 当前可执行顺序

```text
1. target/linker wrapper + sysroot/SDK 检查
2. 8 个 target 的 Rust build 与产物审计
3. ABI/Core manifest 重新核对
4. Rust 生命周期与网络回归
5. profile/隔离回归
6. Python/Go/Node 薄绑定
7. CI 和发布包验收
```

## 待办：profile_state_isolation_probe 缺测试服务器

`core/source/profile_state_isolation_probe.cc` 断言两个 Engine 之间的 cache、cookie、
连接池与 TLS session cache 完全隔离，但它驱动 `/cache`、`/etag`、`/cookie-redirect`、
`/connection/*`、`/tls-close/*` 这些路径，需要一个专门的 HTTP + HTTPS 服务器。那个
服务器不在本仓库里，所以 `tools/audit-profile-probes.sh` 把它记为**显式跳过**
（打印 `SKIP ... needs HTTP_BASE and HTTPS_BASE`），不是静默跳过。

补法：把那个 harness 加进 `tools/`（一个自签 CA 加固定响应体的小服务即可），然后让
`audit-profile-probes.sh` 自己起它并把两个 base 传进去。在补上之前，这一格行为验证是
**未执行**状态，不要在任何文档里把它记成通过。

Python 资源生命周期、流式响应、WebSocket 有界队列、Core 回调隔离以及 ABI v8
pause/resume 的详细设计和验收门槛见
[`PYTHON_RESOURCE_BACKPRESSURE_PLAN.md`](PYTHON_RESOURCE_BACKPRESSURE_PLAN.md)。
当前阶段 1--3 已完成 ABI v8 实现；阶段 4 的每请求/每 WebSocket 独立顺序 callback
runner 已随 8 个平台 Core 重建上线；阶段 5 的 HTTP pause/resume 已落地为
`mn_request_resume_read` 加 `on_body` 返回值；WebSocket 侧则通过
`HasPendingDataFrames()` 加 `ResumeReading()` 实现消费者驱动 pause/resume
（未消费帧让 channel 暂停读取，消费完再恢复）。

Linux x86_64 的可复现构建、体积构成和吞吐/并发基线见
[`BASELINE_LINUX_X86_64.md`](BASELINE_LINUX_X86_64.md)。

## 待办：把协商协议暴露到 ABI（v9）

`http_version` 目前只在状态行能证明时返回协议，其余返回 `None`。原因是 ABI v8 的
response 回调只有 `(status_code, headers)`，而 Chromium 会把 HTTP/2、HTTP/3 的响应
规范化成 `HTTP/1.1 200` 的状态行（`net/spdy/spdy_http_utils.cc`），绑定层无法从
现有数据推出真实协议。

真正的修法是加 ABI 字段而不是继续猜：在 `mn_request_callbacks` 增加
`on_response_info`，或新增 `mn_request_negotiated_protocol(mn_request_t*,`
`mn_http_version_t*)`，由 Core 从 `net::HttpResponseInfo`（`WasFetchedViaSpdy()` /
QUIC 标记 / ALPN）填写。这需要 ABI v8 → v9：头文件、`crates/minicronet-sys` 的布局
测试、三份导出表、8 个平台产物与 manifest 全部同步，并走一次完整发布验收。
在此之前绑定层必须继续返回 `None`——错报的指纹比缺失的指纹更糟。

## 已完成（facade 层）：默认请求头 = profile 抓包；Core 注入仍属 ABI v9

**现状（2026-11 落地）**：`impersonate=` 现在发出该 profile 自己的 h1 抓包头集合，
逐字节且顺序一致，72 个 profile 全部通过。表由 `tools/generate-client-hints.py` 从
`profiles/chrome_<major>/h1/captures.json` 生成，`tools/audit-client-hints.py` 每次
提交前重新推导；门禁见 AGENTS.md 第 5 节。平台跟随宿主（与 Core 的 UA 推导一致），
`Sec-Fetch-*` 取导航语义，两者都可被调用方按字段覆盖，`default_headers=False` 可整体关闭。

**仍未做的部分（ABI v9）**：让 Core 自己注入，而不是 facade 拼 extra headers。
facade 注入的代价是这些头不经过 `net` 层的 URLLoader 工厂，因此 Chromium 自己的
`Sec-Fetch` 策略与 client-hint 协商（响应头 `Accept-CH` 决定服务器想要哪些 hint）
都不作用于它们——一个只靠 `Accept-CH` 才该发 hint 的服务器，这里仍然每次都发。
下面的原始分析保留，作为 v9 的输入。

### 原始分析（v9 的背景）

> **以下为 2026-11 之前的观测，保留作历史记录。** 当时 `Session(impersonate="chrome_155")`
> 只发 `Host`、`Connection`、`User-Agent`、`Accept-Encoding` 四个头。facade 层已修，
> 见上一节。

**当时的现状（实测，非推断）**：`Session(impersonate="chrome_155")` 发出的请求头只有
`Host`、`Connection`、`User-Agent`、`Accept-Encoding` 四个；`impersonate` 只把
`User-Agent` 的版本串换成 `Chrome/155.0.0.0`，**默认头集合一个都不变**。

真实 Chrome 在同样的请求上还会发 `sec-ch-ua`、`sec-ch-ua-mobile`、
`sec-ch-ua-platform`、`Upgrade-Insecure-Requests`、`Accept`、`Sec-Fetch-Site`、
`Sec-Fetch-Mode`、`Sec-Fetch-User`、`Sec-Fetch-Dest`、`Priority` 和
`Accept-Language`。因此任何同时检查 `sec-ch-ua` 与 `User-Agent` 的检测方都能把本
客户端判定为非 Chrome。这是**未闭合的保真度缺口**，README 已如实声明。

**为什么现在拿不到**：那些头由 Chrome 的 `services/network` URLLoader 工厂层注入
（client hints 与 Sec-Fetch 源自 renderer 的请求头），不在 `net` 层。
`net::URLRequestContextBuilder` 只有 `set_user_agent` 与 `set_accept_language`
（`net/url_request/url_request_context_builder.h:244`），二者都落到
`StaticHttpUserAgentSettings`，而该接口只提供 `GetAcceptLanguage()` /
`GetUserAgent()`——没有产出 client hints 或 Sec-Fetch 的入口。仓库内零处
`user_agent_metadata` / `UserAgentMetadata`。

**当时的修法设想（ABI v9，仍是 Core 侧注入的正确做法）**：

1. Core 侧为每个 profile 携带 `UserAgentMetadata`（品牌列表、full version、
   platform、mobile 标志）与 UA 品牌 GREASE 顺序。Chrome 的 client hints 由
   `net::HttpUtil`/`ClientHints` 序列化，取值随版本变化，必须逐版本从官方源码取。
2. 在 `Request::StartOnNetworkThread`（`core/source/request.cc`）用
   `SetExtraRequestHeaders` 注入 `sec-ch-ua*`；`Sec-Fetch-*` 与 `Priority` 需要
   在 ABI 上新增字段表达（导航型 vs 子资源型请求的取值不同），不能硬编码一组。
3. `Accept` 与 `Accept-Language` 可先用现有 Engine 字段补齐，代价最小。
4. 必须先用 `tools/inspect-client-hello.py` 的同款 `--repeat` 采样法对**真实
   Chrome** 抓包，逐版本确认取值与**顺序**后再冻结。按 AGENTS.md §7，不得把
   当前猜测值写成期望值。

**验收（已由 facade 层实现）**：新增一个本地 echo 服务器用例，断言 `impersonate="chrome_155"` 时请求头
集合与真实 Chrome 155 抓包一致（含顺序），且 `impersonate=None` 时**不**注入任何
client hint——默认路径不能因为补 profile 而被污染。

## 待办：逐请求 / 会话级 DNS 配置（ABI v9）

**现状（实测，非推断）**：`dns_options` 在仓库内**零命中**——`grep -rn "dns_options"`
扫过 `*.py` / `*.pyi` / `*.rs` / `*.cc` / `*.h` / `*.md` 无任何结果，Python 绑定里连
`dns` 这个词都不存在。实测：

```
>>> chrome_client.get("http://example.com/", dns_options={"timeout": 1})
TypeError: Session.request() got an unexpected keyword argument 'dns_options'
>>> chrome_client.get("http://example.com/", doh_url="https://dns.example/dns-query")
UnsupportedFeature: doh_url= is not supported: ABI v8 exposes no Core setting for it
```

即最接近的 DNS 开关 `doh_url` 是被**主动拒绝**的（`sessions.py:995` 与 `:1444` 的
fail-closed 分支），不是已实现。根因在 ABI：`mn_engine_config_t` 的全部字段为
`user_agent` / `proxy_rules` / `proxy_username` / `proxy_password` /
`http_cache_mode` / `accept_language` / `profile_id` / `profile_namespace` /
`protocol_mode` / `tls_verify_mode` / `custom_ca_pem`，**没有任何 DNS / resolver /
DoH / nameserver 字段**，DNS 行为完全由 Chromium 内部默认值决定。

**Chromium 侧注入点已核实齐全**（这是可行性依据）：
`net/dns/public/dns_config_overrides.h` 提供 `DnsConfigOverrides`，字段为
`nameservers` / `dns_over_tls_active` / `dns_over_tls_hostname` / `search` /
`append_to_multi_label_name` / `ndots` / `fallback_period` / `attempts` /
`doh_attempts` / `rotate` / `use_local_ipv6` / `dns_over_https_config` /
`secure_dns_mode` / `allow_dns_over_https_upgrade` / `fallback_doh_nameservers`。
注入路径两条：`HostResolverManager::SetDnsConfigOverrides()`
（`net/dns/host_resolver_manager.h:201`，public）与
`HostResolver::ManagerOptions::dns_config_overrides`（正式构造路径，
`host_resolver_manager.cc:495` 消费）。`net/BUILD.gn:97` 定义了
`ENABLE_BUILT_IN_DNS`，说明默认 resolver 走 `DnsClient`，overrides 会真正生效——
否则改了也是空转。

**修法（ABI v9）**：

1. `mn_engine_config_t` 增加 DNS 字段（指针 + 长度成对，沿用现有结构体风格），
   `MN_ABI_VERSION` 8 → 9；三份导出表版本符号 `MINICRONET_2` → `MINICRONET_3`。
2. `crates/minicronet-sys/src/lib.rs` 同步 `#[repr(C)]` 与布局测试——注意 32 位
   分支要重算（`mn_engine_config_t` 加字段后 68/72 那类断言必须跟着改，
   且见下文「32 位 ABI 布局没有执行路径」，该分支目前无 CI 执行证明）。
3. Core 侧在 `Engine::InitializeOnNetworkThread`（`core/source/engine.cc`）
   构造 `DnsConfigOverrides` 并注入；参数校验沿用 `core/source/minicronet.cc`
   既有的 `kMax*` 上界风格，调用方传入的长度不可信。
4. 绑定层：`EngineConfig` 加字段（进缓存键）、`Session.__init__` 加参数、
   解除 `doh_url` 的拒绝分支；`.pyi` 由 `tools/expand-verb-signatures.py`
   重生成（该脚本幂等，`--check` 可断言），不要手改 24 处 verb 签名。

**必须先定的两个取舍**（不是实现细节，是语义决定）：

- **逐请求 vs 会话级**。DNS 是 engine 级设置（`set_*` 只在建 context 时生效）。
  若要做 `get(..., dns_options=...)`，每个不同配置都会重建 engine 并占用一个
  缓存槽——项目里已有同类教训（`_resolve_cookies` 注释记载「8 次调用方改动填满
  8 个槽并挤掉可复用那个」）。复用 `discard_cookies` 的 `_one_shot_slot` 可以规避
  缓存问题，但一次性 engine **会丢掉连接池与 TLS 会话缓存**，即逐请求 DNS 的请求
  享受不到 keep-alive。建议先只做**会话级**（进 `EngineConfig` 缓存键），语义清晰、
  无一次性 engine 代价。
- **`doh_url` 的语义映射**。curl_cffi 的 `doh_url` 是单个 URL，Chromium 是
  `DnsOverHttpsConfig`（多服务器 + 模板）。要么 1:1 映射，要么继续 fail-closed
  拒绝——按 AGENTS.md §8 不得静默忽略。

**验收**：

- 每个暴露的 overrides 字段都要有**可观测证据**证明它真的改变了 Chromium 行为
  （例如指向自定义 nameserver 后解析一个只有该服务器知道的域名）。按 AGENTS.md §7，
  没有实测的字段不能声明支持——`use_local_ipv6` 这类依赖系统栈行为的字段若无法
  证明，就 fail-closed 拒绝而不是暴露。
- 8 个平台产物与 manifest 全部重建并同步（`tools/install-core-binaries.py`）。
- 回归用例覆盖：默认路径（不传 DNS 参数）行为不变；非法 nameserver 报
  `InvalidArgument` 而非崩溃；DNS 配置不同的两个 Session 不串用解析结果。

## 待办：H3 全量强制测试会被端点限流

`tools/audit-profile-addition.py --levels 3 --protocol h3` 会为每个版本发 6 条真实 QUIC
请求到 `quic.tools.scrapfly.io`。对 93–157 全部 65 个版本连跑（390 次请求）时，端点会
开始拒绝部分连接：

```
response: RequestError { error: Protocol, net_error: -356 }
  即 ERR_QUIC_PROTOCOL_ERROR
live_connections: 5 / 1 / 4   （凑不满 6 条，无法比对）
```

**实测 2026-11**：全量 65 个版本跑下来 `passed 62, failed 3 [105, 106, 109]`，3 个失败
**全部**是这个 -356，且 `grep 'but the captures say'` 为 0 条——即**没有任何一条是字段
不符**。这 3 个版本单独重跑全部 PASS。同一版本连跑 6 次也全 PASS，说明触发条件是
**跨版本连续请求的总量**，不是单版本重复。

这不是指纹缺陷，但会让「全量 H3 验收」这条命令的结果不可信：一次全量扫描的 PASS/FAIL
里混着端点限流，而 FAIL 看起来和真缺陷一模一样。

**当前做法**：抽样跑。跨 era 边界取样（93/100/110/119/120/124/130/140/150/157），
覆盖 h3_text 的两个形态（93–119 无 SETTINGS_H3_DATAGRAM、120+ 有）与 ja4 的三个值。

**已做（2026-11）**：

- `--delay SECONDS`：每条连接之间等待，默认 0。
- `--retry N`：对 `-356` 重试 N 次（只重试这一个码；其他退出码不重试，因为那更可能是
  真的构建问题）。
- FAIL 分类：连不上 6 条时 `facts.verdict_kind = COULD_NOT_CONNECT`，文本输出显示为
  `3_binary: FAIL [COULD_NOT_CONNECT]`，与「字段不符」明确区分。连不上意味着这次运行
  **没有证明任何关于指纹的事**，不该和「这个字段错了」混在同一个失败列表里。

实测：之前失败的 `chrome_105 / 106 / 109` 在 `--delay 1 --retry 3` 下全部 PASS。

**仍待补**：全量 65 个版本在带延时的情况下跑一遍并记录结果（本次只验证了修复本身）。
判定条件：全量 65 个版本 0 个字段不符，且任何 COULD_NOT_CONNECT 都被如实标出而不是
当成 PASS。

## 待办：32 位 ABI 布局没有执行路径

`crates/minicronet-sys` 的布局测试已有 32 位分支（数值已按头文件修正为
84/28/92/32/80/16/8），但 CI 只跑 x86_64，所以 `linux-x86`、`windows-x86` 两个已
发布平台没有执行证明。需要在 CI 加一次
`cargo test -p minicronet-sys --target i686-unknown-linux-gnu`（或等价目标机运行）。

## 待办：net error 名称审计没有 CI 入口

`tools/audit-net-error-names.py` 逐行核对 `exceptions.py` 的 `_NET_ERRORS` 与 Chromium
的 `net/base/net_error_list.h`，但它需要一份 Chromium 检出，而两个 workflow 里都没有
带检出或缓存的 job。`ci.yml` 曾经写着「它由 Chromium-tree job 调用」，但那个 job 从来
不存在（`jobs:` 只有 `check` 和 `python-regression`，两者都不设 `CHROMIUM_SRC`）——
这正是 LESSONS 第 23 条「本地跑不到的门禁等于没有门禁」的同一形态：一条注释声称有远程
入口，实际只有开发机能跑。

现状：`NetErrorTableShapeTests` 在没有检出时校验表的**结构**（约 60 行、名字以 `ERR_`
开头、叶子类型正确、传输错误不得报成 TLS 或 chunked），这能在 CI 跑；但**名字与码的
对应关系**只能在有检出的机器上核对。

要么给 CI 加一个只拉 `net/base/net_error_list.h`（而非整棵 Chromium）的 job，要么把这份
头文件按 revision 缓存进仓库并加一条「缓存与 CHROMIUM_REVISION 一致」的门禁。

## 待办：产物来源证明

`core/binaries/*/manifest.json` 的 SHA-256 由写它的同一个工具生成，所以同时替换
二进制与 manifest 可以通过所有门禁。已加的检查只能保证 manifest 的
`chromium_revision` 与 `CHROMIUM_REVISION` 一致。要真正证明二进制来自这份源码，
需要一条能跑 Chromium 构建的 CI 路径：重建 → 比对 → 记录，或对产物签名/attestation。

## 待办：Linux 交叉构建的 pkgconf 未进仓库

`linux-x86` / `linux-arm64` 的交叉构建需要 `pkg-config`（gn 为 NSS 调用它）。
**获取方式已文档化**，见 [`BUILD.md`](BUILD.md) 的「Linux cross-builds need
pkg-config」一节：从发行版取三个 deb、解包到仓库外、用
`MINICRONET_PKGCONF_DIR` 指过去即可。仓库里不放这份工具（它是发行版包，
不属于本项目的产物）。

```bash
MINICRONET_PKGCONF_DIR=/tmp/pkgconf/root tools/build-core-linux-arm64.sh
```

两条**实测**结论，都与旧版本文档不同：

- `PKG_CONFIG_LIBDIR` **不需要设置**。旧文档要求显式指向
  `.../debian_bullseye_<arch>-sysroot/usr/lib/pkgconfig`，但
  `build/config/linux/pkg-config.py` 会自己从 `--sysroot` 推导并覆盖这个变量。
  用 `/nonexistent/bogus/path` 覆盖它仍然构建成功，且产物与不设置时逐字节相同
  （`069276f884924c4f`）。
- 不要设置 `PKG_CONFIG_SYSROOT_DIR`：gn 自己会传 `--sysroot`，再设一次会拼出双
  前缀（`.../sysroot/home/.../sysroot/usr/include/nss`），构建死在
  `pk11pub.h: file not found`。这一条已复现确认。

剩余项只是「要不要把 deb 也纳入仓库以支持离线构建」，不是「命令未知」。

完成条件是每项都有命令输出或目标机报告；“文件存在”不等于“平台支持完成”。
