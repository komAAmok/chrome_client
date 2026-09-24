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

- `core/abi/minicronet.h` 是**唯一** ABI 来源。当前 ABI v8，**20** 个导出符号。
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
tools/audit-readme.sh
tools/audit-pypi-metadata.sh
python3 tools/audit-python-stubs.py
python3 tools/audit-python-typing.py
tools/audit-core-binaries.sh
cargo fmt --all -- --check
cargo check --workspace --all-targets
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace --all-targets
cargo test --manifest-path bindings/python36/Cargo.toml --locked
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

## 9. 产物与仓库体积

- `core/binaries/<target>/` 的**二进制与 manifest 都提交**（CI 需要离线可用）。
  不要给它们加回 `.gitignore`：曾经忽略过，结果是重建的 Core 不出现在
  `git status`、`git add` 静默失效。
- `bindings/*/target/`、`target/` 永不提交。
- 不要提交超过 1 MB 的新二进制，除非它是发布产物的一部分；提交前先问
  「旧版本需要留在历史里吗」。

## 10. 结论的验证责任

- 任何外部结论（子代理、审查报告、issue、本文件的某条）在写进代码或文档前，
  **必须回到源码验证**，并在提交信息或注释里给出复核位置（文件:行）或复现命令。
- **否定性结论同样需要证据**：判定「不是缺陷」也要指出是哪几行使其不成立。
- 不要因为某个结论听起来严重就照做，也不要因为听起来轻微就跳过。

## 11. 测试的诚实性

- 构建成功 ≠ 能加载 ≠ 能发请求。平台矩阵的每一格都需要一次真实运行，
  否则不要在 README 打 ✓。
- 需要外部依赖的测试要么在 CI 里满足依赖，要么在文档里明确标注为「长期跳过」——
  不允许悄悄 skip 掉一个曾经有效的门禁。
