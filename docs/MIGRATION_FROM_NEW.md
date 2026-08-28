# `new` 目录迁移说明

## 目的

`/home/sj/桌面/new` 是历史工作目录，包含 Core 胶水代码、Chromium 补丁、
profile 资料、抓包证据、旧 Rust workspace 和交叉编译脚本。目标仓库是
`/home/sj/桌面/chrome_client`，以后以目标仓库的目录和 ABI 为准。

本文件只定义迁移；未完成审计前不执行覆盖、删除或批量复制。

## 迁移规则

1. 先计算源文件 SHA-256、检查许可证和 Chromium revision，再迁移。
2. 目标已有文件不直接覆盖。特别是 `core/abi/minicronet.h`、Rust FFI 和
   manifest 必须逐字段/逐符号比较。
3. Chromium 源码仍在 `CHROMIUM_SRC` 外部维护；不把 Chromium 源码、GN/Ninja
   输出或中间文件放进 `chrome_client`。
4. 迁移后的脚本必须使用仓库相对路径和环境变量，不能依赖 `/home/sj/桌面/new`
   或单台机器的绝对路径。
5. 抓包、测试 CA、私钥、Cookie、代理凭据和临时日志不进入发布仓库。

## 源到目标映射

| `new` 源路径 | `chrome_client` 目标 | 处理 |
| --- | --- | --- |
| `include/minicronet.h` | `core/abi/minicronet.h` | 逐字段审计；以 ABI v7 唯一来源为准，禁止盲目覆盖 |
| `core/*.cc`, `core/*.h` | `core/source/`（待建立） | 只迁移当前 Core 胶水和生命周期代码；先过最小依赖审计 |
| `core/BUILD.gn` | `core/source/BUILD.gn` 或外部 Chromium patch | 只保留 `//minicronet` 闭包，剔除 Android/UI/JNI |
| `core/minicronet.{def,exports,lds}` | `core/exports/`（待建立） | 与 8 个产物的导出表逐一核对 |
| `patches/*.patch` | `core/patches/`（待建立） | 记录适用 revision、目的、依赖和回滚方式；不把证据文本编译进库 |
| `profiles/*.json`, profile 审计 md | `profiles/`（待建立） | 只迁移规范化、可重生成的 profile；源码证据留在审计资料，不进二进制 |
| `tools/build-core-*.sh` | `tools/build-core-*.sh` | 保留每架构独立入口，改为 `CHROMIUM_SRC`/`OUT_DIR` 环境变量 |
| `tools/audit-*.sh`, `tools/validate-*.py` | `tools/` | 逐个检查绝对路径、依赖和隐私数据后迁移 |
| `rust/minicronet-sys` | `crates/minicronet-sys` | 以目标仓库 ABI 和 linker 选择为准，补齐 target 测试 |
| `rust/minicronet` | `crates/minicronet` | 以当前安全封装为准；只迁移缺失测试/文档，不回退到旧 API |
| `rust/minicronet/tests` | `crates/minicronet/tests` | 先核对是否引用旧 ABI；通过后迁移并纳入 CI |
| `CHROMIUM_REVISION` | `CHROMIUM_REVISION`（仓库根目录） | 迁移 revision 记录，构建时强制校验 |
| `README.md`, `PROJECT_PROMPT.md` 等 | `docs/` | 合并约束和构建说明，删除过时路径/旧 API 描述 |
| `config.json` | 本地构建配置或 `docs/BUILD.md` | 不把本机路径和凭据提交为默认配置 |
| `.github/workflows/build-cronet.yml` | `.github/workflows/` | 与现有 CI 合并前审计权限、缓存和 secret，不能直接覆盖 |

## 明确不迁移

以下内容是生成物、证据或本机缓存，不属于 `chrome_client` 的源代码或发布包：

- `new/captures/` 中的 PCAP、TLS key log、响应和代理日志；只保留脱敏后的摘要
  或报告链接。
- `new/target/`、`new/rust/target/`、GN/Ninja `out/`、`.o`、`.a`、Siso/Ninja
  缓存和 smoke 可执行文件。
- `new/.tools/`、`new/.xwin-cache/`、Python `__pycache__` 和下载压缩包。
- `artifacts/` 中来源、revision、SHA-256 未确认的旧二进制。
- 私钥、临时 CA、Cookie、代理用户名/密码、浏览器用户目录和任何抓包凭据。
- `curl_cffi.md`、`requests.md` 等研究材料不进入运行时；若保留，只放入单独的
  `docs/research/`，不得作为构建输入。

## 实际迁移顺序

### 1. 建立清单和校验

```sh
cd /home/sj/桌面/new
find core include patches profiles rust tools -type f -print0 \
  | sort -z | xargs -0 sha256sum > /tmp/chrome-client-new.sha256
```

审计清单至少包括 ABI 版本、Chromium revision、导出符号、profile 表生成器、
每个 target 的构建参数和脚本中的绝对路径。

### 2. 迁移 ABI 和 Core 胶水

先比较 `new/include/minicronet.h` 与 `chrome_client/core/abi/minicronet.h`，再迁移
缺失的 C++ 胶水/patch。构建只允许使用 `//minicronet` 根 target，并执行 Android、
Java、JNI、UI 和无关 target 泄漏检查。

### 3. 迁移 profile 和验证工具

迁移后重新生成只读 profile 表，确保输入 JSON 的顺序稳定、输出可复现、随机字段
不被固定。抓包证据只作为本地或独立归档，不复制进最小发布包。

### 4. 迁移 Rust workspace

目标结构固定为：

```text
chrome_client/
  Cargo.toml
  crates/minicronet-sys/
  crates/minicronet/
```

删除旧 workspace 路径依赖和旧 API；先运行 ABI 尺寸、生命周期、并发、cancel/timeout
测试，再执行 8 个 target 的 `cargo build`。

### 5. 迁移构建与 CI

每个架构保留独立构建脚本，linker/sysroot/SDK 通过环境变量或 wrapper 注入。CI
先做离线 Rust 检查和静态 Core 审计，再在对应 runner/目标机执行真实网络测试。

## 验收门槛

- [ ] 目标仓库不再依赖 `/home/sj/桌面/new` 才能构建或运行。
- [ ] ABI v7 字段、尺寸、符号和 manifest 全部一致。
- [ ] 8 个 target 均完成 Rust 编译；无法运行的架构有明确的目标机/runner 记录。
- [ ] Linux x86_64 完成真实 H1/H2/H3、WS/WSS、代理、缓存/Cookie、取消/超时和
  并发回归。
- [ ] 其他平台至少完成对应格式、导出、依赖和加载验证；运行时报告单独记录。
- [ ] profile 99--151 的可证明差异已生成精简只读表，多 Engine 隔离测试通过。
- [ ] 发布包不含证据、私钥、缓存、构建中间物和 Android/Java/JNI。

迁移完成后，`new` 只能作为历史工作目录或只读证据源；后续修改必须提交到
`chrome_client`，避免两套实现再次分叉。
