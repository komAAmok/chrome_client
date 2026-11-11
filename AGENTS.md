# AGENTS.md — 在这个仓库里工作的约束

本文件是**强制规则**，不是建议。任何自动化代理（coding agent）与人类贡献者都适用。
开始改动前先读本文件与 [docs/LESSONS.md](docs/LESSONS.md)；后者解释了每条规则
是被哪个真实缺陷换来的。

---

## 0. 先确认自己在改什么

1. `git status --porcelain` 必须是干净的，或有你明确知道的改动。
2. 读 `docs/PROJECT_STATUS.md`（当前状态与门禁表）与 `docs/NEXT_STEPS.md`（未完成项）。
   文档与代码冲突时，**以代码和门禁为准**，并把文档改对。
3. 涉及版本、ABI、profile 的改动，先读 `docs/LESSONS.md` 对应条目。

---

## 1. 架构边界（不得跨越）

```
Python (PyO3) ─┐
Rust ──────────┼─> minicronet ─> minicronet-sys ─> libminicronet (Chromium C++)
Go (cgo) ──────┤
Node (N-API) ──┘
```

- `libminicronet` 是**唯一**的 TLS / HTTP/1.1 / HTTP/2 / HTTP/3 / QUIC / WebSocket /
  HTTP·SOCKS 代理实现。不得引入第二套网络栈。
- 绑定层（Python / Go / Node）**只做**类型、参数、错误、生命周期转换。
  禁止在绑定里实现协议、重试语义、TLS 行为。
- Rust 层只负责 C ABI 声明、所有权、回调、取消、超时、错误映射。
- 禁止引入 Tokio / async-std / 其他执行器。

## 2. ABI 规则

- `core/abi/minicronet.h` 是**唯一** ABI 来源。当前 ABI v9，**28** 个导出符号。
- 结构体变更必须同时更新：头文件、`crates/minicronet-sys/src/lib.rs` 的 `#[repr(C)]`
  声明与布局测试、`core/exports/minicronet.{def,exports,lds}`。
- 每个 config 结构体以 `size` + `version` 开头，两者都要校验。
- 任何 create 函数的 out-param 必须在**所有**返回路径上先置空。
- 回调指针只在调用期间有效，不得保留。
- **Engine 必须比它的 request / WebSocket 活得久**：释放最后一个 engine 引用会拆除
  `URLRequestContext`，此时若仍有 `URLRequest` 注册，Chromium 会 abort 宿主进程。
- ABI 不携带的信息（例如协商到的协议版本）**不得由绑定层编造**。

## 3. 版本与常量

- 发布版本只存在于根 `Cargo.toml` 的 `workspace.package.version`。
  需要版本号时调用 `tools/version.py`，**禁止**写第二份字面量。
- Core 的版本串由 `tools/sync-core.sh` 生成
  （`core/source/minicronet/minicronet_version_generated.h`）；提交的是
  `0.0.0-generated` 占位符，构建时覆盖。不要手改那个文件。
- 平台相关分支用 `BUILDFLAG(IS_WIN/IS_MAC)` 与 `ARCH_CPU_*`，禁止硬编码平台令牌。
- 随机数只能来自 Chromium/BoringSSL/QUICHE 的 CSPRNG。禁止固定种子；
  profile 只能固定**概率分布常数**，不能固定种子。

## 4. 绝对路径

- 禁止把开发者本机绝对路径写进任何提交的文件。
- Chromium 检出位置通过 `CHROMIUM_SRC` 覆盖；默认值由 `tools/core-paths.sh` 解析
  （`source` 它，不要执行）。
- 工具链位置用 `CHROMIUM_TOOLCHAIN` / `OSXCROSS_TARGET` / `XWIN_ROOT`。

## 5. 门禁（提交前必须全绿）

```bash
tools/audit-abi.sh
python3 tools/audit-abi-layout.py
tools/audit-readme.sh
# It also runs tools/audit-readme-tables.py, which compares the capability
# tables between README.md and README.en.md: row and column counts plus the
# architecture names, not the prose. A row added to one language only is how
# ARMv7 ended up documented as having a wheel in Chinese and not existing in
# English.
tools/audit-pypi-metadata.sh
python3 tools/audit-python-stubs.py
python3 tools/audit-extra-fp.py
# The per-profile request header set is generated from the h1 captures, so a
# stale table or a capture that stopped agreeing with its five siblings has to
# fail here rather than show up as a fingerprint mismatch on the wire.
python3 tools/audit-client-hints.py
# Every curl_cffi extra_fp knob must carry an explicit verdict: curl_cffi
# simulates Firefox and Safari too, so some knobs have no counterpart in a
# Chrome-only, Chromium-backed client. A new knob without a verdict fails.
python3 tools/audit-python-typing.py
tools/audit-core-binaries.sh
tools/audit-core-inputs.sh
python3 tools/audit-core-size-ceilings.py
python3 tools/audit-core-payloads.py
# Stage 0 (docs/NEXT_STEPS.md) records that the committed args.gn cannot pass
# gn gen, so an artifact cannot be rebuilt from the repository. This gate keeps
# the per-target configuration snapshot in step with the build output that
# actually produced the artifact: a rebuild that changes a flag without
# re-recording it fails here.
python3 tools/record-core-build-args.py --check
# The line above is the only cross-platform behavioural claim this
# repository can make without running anything: every artifact must
# embed the profile payloads from the generated header byte for byte.
# Seven of the eight targets cannot be loaded on a build host, so a
# stale or divergent build is otherwise invisible.
python3 tools/audit-padding-rule.py
python3 tools/audit-tls-version-range.py
python3 tools/audit-h1-alps-absence.py
python3 tools/audit-h3-profile-support.py
# The Core writes the three HTTP/2 framing knobs from profile data instead of
# inheriting Chromium's C++ default, because a browser build's default and the
# captures are different claims. This gate checks both directions: no captured
# connection may carry a greased SETTINGS id or a reserved frame type, and the
# compiled table must say so for every profile.
python3 tools/audit-h2-framing.py
# Every profile gate above compares the compiled table with the captures this
# repository imported, so an importer that dropped a field would leave both
# agreeing and both wrong. This cross-checks the nine v9 profile fields against
# a second, independently collected run of the same protocol: the boundaries it
# can witness are ECH (119), the ALPS codepoint (135), H3 datagram (120) and the
# absence of HTTP/2 grease everywhere. The second set is 19 MiB of raw captures
# and stays outside the repository, so the gate reports 'unavailable' and exits 0
# without --evidence-dir rather than passing silently.
python3 tools/audit-v9-profile-fields.py --evidence-dir $PROFILE_EVIDENCE_DIR
# A new profile is only accepted when all three evidence levels pass: the
# release's own source at its latest tag, six captured connections per
# protocol, and the built artifact reproducing them. See section 9.
# Level 3 needs the network and a built Core, so CI runs 1,2 and a
# developer runs the whole chain before claiming support.
python3 tools/audit-profile-addition.py --levels 1,2 --profile chrome_157
# The four profile gates above read profiles/chrome_<major>/<proto>/captures.json,
# so profiles/ must stay tracked. Without it they fail on a clean clone instead
# of skipping: evidence that is absent must not read as evidence that agreed.
tools/audit-profile-probes.sh # needs a Chromium checkout + gn/ninja
python3 tools/audit-profile-evidence.py # needs network (googlesource); --offline for a local-only check
python3 tools/audit-net-error-names.py   # needs a Chromium checkout (CHROMIUM_SRC)
cargo fmt --all -- --check
cargo check --workspace --all-targets
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace --all-targets
# bindings/python36 contains no #[test] function, so a cargo test on it compiles
# and passes unconditionally. The artifact is what needs checking: it must still
# load, still report the ABI version the header declares, still be a real abi3
# build, and still drive the shared facade for one live request.
python3 tools/audit-python36-artifact.py
python3 -m unittest discover -s bindings/python/tests -p "test_*.py"
```

## 6. 修改门禁时的硬性要求

- **变异测试**：新增或修改 `tools/audit-*.sh` / `audit-*.py` 后，必须故意破坏被检查
  的对象，确认脚本**非零退出**，然后恢复。没做变异测试的检查不算检查。
- **扩展扫描范围**：改门禁前先问「还有哪些同类载体没被扫到」。
  例：版本号门禁曾扫 README 却不扫 `docs/PROJECT_STATUS.md`，于是那里漂移了两版。
- 审计脚本不得使用永远为真的判断；不得用 `|| true` 吞掉失败。

## 7. 文档与断言的纪律

- 文档里的可验证事实必须与代码一致。改代码时同步改文档。
- 不得写「看起来在验证」的注释。注释描述的是**现在**的行为。
- 不得把缺陷写成断言：如果某个值目前是错的（例如 `http_version` 曾恒为
  `HTTP/1.1`），测试应当断言**正确**行为或明确标记为已知缺陷，
  而不是把错误值冻结成期望。
- 不可得的数据要如实声明不可得，并说明推导方式——参见 `reason` 与 `http_version`。

## 8. fail-closed 承诺（对外契约）

无法用 Chromium 忠实实现的选项必须**抛错**（`UnsupportedFeature`），不得静默忽略。
这条承诺覆盖两层实现：Python facade 与 Core 存储。
（`discard_cookies` 曾只作用于 Python 侧，导致 Core 仍然发出 cookie。）

## 9. 新增 Chrome profile 的三级证据链（最高优先级约束）

**一个 Chrome 大版本只有走完下面三级才算「新增通过」。任何一级没有证据，这个版本就
不存在——不能写进 profile 表、不能出现在 README 的支持列表、不能对外声称支持。**

### 优先级顺序是强制的，不是建议

1. **官方源码是最终裁判。** 行为必须能在该大版本自己的 tag 源码里找到，而不是从相邻
   版本继承或推断。源码与抓包冲突时，以源码为准并解释抓包为何不同。
2. **抓包必须做到字节级对齐。** 六个独立连接的 h1、h2、h3 各一份，稳定字段逐字段
   相等，随机字段必须互不相同。
3. **编译产物必须复现抓包。** 用真实构建的 Core 发真实请求，与第 2 级的抓包比对。

第 1 级不是「参考资料」，它是判定标准：只做到第 2 级的 profile 是拟合了一次抓包；
只做到第 3 级的 profile 是碰巧对上了。

### 每一级的硬性要求

| 级别 | 要求 | 判定工具 |
| --- | --- | --- |
| 1 源码 | 该大版本**最新的**官方 tag（`refs/tags/<major>.*` 里版本号最大的那个）；该 tag 的 `DEPS` 所指的 BoringSSL 与 QUICHE revision；`net/http/http_network_session.cc`、`net/base/features.cc`、`net/spdy/spdy_session.cc`、`net/quic/quic_chromium_client_session.cc`、`net/socket/ssl_client_socket_impl.cc`、`net/websockets/websocket_basic_handshake_stream.cc`、`ssl/extensions.cc`、`ssl/handshake_client.cc`、`quiche/quic/core/quic_config.cc` 九个文件逐个取回并记录 sha256 | `tools/audit-profile-addition.py --levels 1` |
| 2 抓包 | h1、h2、h3 **各 6 条**独立连接，存成 `profiles/chrome_<major>/<proto>/captures.json`；稳定字段（cipher 去掉 GREASE、扩展集合、supported_versions、HTTP/2 SETTINGS、UA 版本串）六条全等；随机字段（client_random、session_id、扩展顺序；h3 另加 dcid/scid）六条互不相同 | `tools/audit-profile-addition.py --levels 2` |
| 3 产物 | 用 `core/binaries/<target>/` 里的真实产物发 6 次真实请求，逐字段与第 2 级抓包比对 | `tools/audit-profile-addition.py --levels 3` |

### 不可协商的细则

- **第 1 级必须走网络取官方源码**，不得用本地 Chromium 检出代替：检出只有一个 commit，
  而每个大版本要各自 tag 的源码。取不到就是没通过，不是「跳过」。
- **该大版本最新的 tag 是默认基准。** 若选择更早的 patch tag，必须在 profile 的
  `chromium_tag` 里写明，并在该 profile 的 `README.md` 说明为什么不取最新——工具会把
  `recorded_tag_is_latest: false` 和新 tag 列表打出来，这是要签字的事实，不是错误。
- **BoringSSL / QUICHE 的 revision 必须来自该 tag 自己的 `DEPS`**，不得抄相邻版本。
  工具会比对 profile 记录的 revision 与 `DEPS` 的差异并失败。
- **抓包不足 6 条即为未通过。** 3 条、5 条都不算；缺一条就是 `N connections, 6 required`。
- **随机字段重复即视为固定种子**，直接失败——这是本项目的红线（见 §3）。
- **h3 在某个大版本上确实不可用时**，写 `unsupported.json` 记录当时的失败原因，并在
  第 3 级断言引擎**拒绝**该组合（`MN_ERROR_PROFILE_UNSUPPORTED`）。缺抓包且无
  `unsupported.json` 是失败，不是跳过。
- **三级全绿才允许**把 id 加进 `profiles/normalized_profiles.json`、重新生成表、重建 8 个
  平台产物。顺序不能颠倒：先加进表再补证据，等于先把结论写下来再去找支撑。

### 提交时的证据要求

新增 profile 的提交必须附上三级的原始输出（`--json` 的完整报告），并在提交信息里写明
该大版本最新 tag 与所选 tag 的关系。没有这份输出的提交按未验证处理。

### 为什么这条规则存在

这批 86–157 里有 7 个版本（86–92）的 h3 根本跑不通，而当时的表把它们标成「支持 H3」；
强制 H3 会真的发出 QUIC 并拿到 HTTP 200，等于报告了一个那些版本从未有过的指纹。
只做第 2 级（抓包）发现不了这件事，因为缺的那一级正是源码。

## 10. 产物与仓库体积

- `core/binaries/<target>/` 的**二进制与 manifest 都提交**（CI 需要离线可用）。
  不要给它们加回 `.gitignore`：曾经忽略过，结果是重建的 Core 不出现在
  `git status`、`git add` 静默失效。
- `bindings/*/target/`、`target/` 永不提交。
- 不要提交超过 1 MB 的新二进制，除非它是发布产物的一部分；提交前先问
  「旧版本需要留在历史里吗」。

## 11. 结论的验证责任

- 任何外部结论（子代理、审查报告、issue、本文件的某条）在写进代码或文档前，
  **必须回到源码验证**，并在提交信息或注释里给出复核位置（文件:行）或复现命令。
- **否定性结论同样需要证据**：判定「不是缺陷」也要指出是哪几行使其不成立。
- 不要因为某个结论听起来严重就照做，也不要因为听起来轻微就跳过。

## 12. 测试的诚实性

- 构建成功 ≠ 能加载 ≠ 能发请求。平台矩阵的每一格都需要一次真实运行，
  否则不要在 README 打 ✓。
- 需要外部依赖的测试要么在 CI 里满足依赖，要么在文档里明确标注为「长期跳过」——
  不允许悄悄 skip 掉一个曾经有效的门禁。

## 13. 打 tag 前必须全量 review

一个 tag 是「这一刻仓库里每一条断言都成立」的唯一凭据，而这些断言由不同的工具
把守、从来没有在同一份 commit 上一起跑过：静态门禁、真实请求、并发基准、泄漏测量、
四个语言的绑定，各自在某个时刻通过过。

所以打 tag 前执行一次完整 review，产出一份 JSON 记录：

```bash
LD_LIBRARY_PATH=core/binaries/linux-x86_64 \
  PYTHONPATH=bindings/python \
  CHROMIUM_SRC=$CHROMIUM_SRC \
  python3 tools/review-before-tag.py --out docs/review-<version>/summary.json
```

它不重新实现任何检查，每一步都 shell 出去交给拥有它的工具；跑不了的步骤记为
`unavailable` 并写明原因，绝不记成 pass。退出码只在**能跑的步骤全部通过**时为 0。

### 四组必查项，对应四类不能回归的性质

| 组 | 查什么 | 工具 |
| --- | --- | --- |
| `gates` | ABI、README、stub、Core 产物、profile 证据 | `audit-abi.sh` 等 12 项 |
| `fingerprint` | h1/h2/h3 指纹是否漂移 | `audit-padding-rule.py`、`audit-tls-version-range.py`、`audit-h1-alps-absence.py`、`audit-h3-profile-support.py`、`audit-profile-addition.py`、`audit-profile-probes.sh`、`run-http-smoke.py` |
| `concurrency` | 同步/异步吞吐、`stalled_consumer_isolation` | `bench-core-baseline.py` |
| `leaks` | 五种请求形态的 fd / RSS 增长 | `leak-probe.py` |

`fingerprint` 组里前四个是静态的：它们比对 profile 表、抓包与源码，能发现改了表
却没重新抓包。`wire-smoke` 是唯一真的连服务器的指纹检查——静态检查看不到协商结果。

`concurrency` 与 `leaks` 是源码里读不出来的：只能测，且只在跑绑定的那台机器上有意义。
两者都写 JSON，让一次发布有数字可比，而不只是一个退出码。

### 泄漏必须覆盖五种形态

`test_async_requests_do_not_leak` 在测试套件里比较两批等量请求，但它只覆盖异步回调
一条路径。泄漏是分路径的，被测到的那条正好是唯一干净的那条就是最坏情况。所以
`leak-probe.py` 每条形态各起一个子进程（崩溃或 finalizer 挂住不会被误判为干净），
跑「预热 / 第 1 批 / 第 2 批」三个采样点：

| 形态 | 覆盖的路径 |
| --- | --- |
| `sync-get` | `get()`，丢弃 body |
| `sync-stream` | `stream=True` + `iter_content()`，完整消费 |
| `sync-abandon` | `stream=True` 后 `close()` 但不读 |
| `async-gather` | `AsyncSession` + `asyncio.gather` |
| `error-path` | 每个请求都打到拒绝端口，测错误路径的分配 |

判据是**第二批相对第一批的增量**（fd ≤ 8 个、RSS ≤ 24 MiB）：泄漏随请求数线性增长，
固定容差吸收 Chromium 的缓存与线程池。

### 报告要能数出来「哪些没跑」

`unavailable` 不是一个可以通过的状态，但也不是失败：本机没有 Chromium 检出、没有网络、
没有目标机时，这些步骤本来就不该跑。区别在于**必须写明原因**，因为沉默正是让 ARMv7
那一行宣称了一个它从未有过的 wheel 的原因。

每次打 tag 后，`docs/review-<version>/` 下的 `summary.json`、`bench.json`、
`leak.json` 与 tag 一起提交，构成该版本的验收证据。

```bash
# 只要某一个子集（调试用）
python3 tools/review-before-tag.py --only padding-rule leak-probe \
  --out /tmp/partial.json
```
