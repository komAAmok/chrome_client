# 后续工作清单

本文件是 `chrome_client` 在 `dev/0.2.0` 分支上的执行顺序。它描述
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
- 版本 profile 只覆盖已审计的 Chrome 99--151 差异；无法证明的行为必须
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
  对照 `new/include/minicronet.h` 做逐字段、尺寸、对齐、导出符号审计。
- [ ] 对 8 个 `core/binaries/<target>/manifest.json` 重新计算 SHA-256、大小、
  Chromium revision、ABI 和运行时依赖。
- [ ] Linux 保持架构匹配的 NSS/NSPR 与 glibc 动态依赖；不静态链接 glibc/NSS，
  不把其他架构的库打包进来。
- [ ] Windows 保证 DLL、`.lib` 与 `icudtl.dat` 同架构同版本。
- [ ] 运行 `tools/audit-core-binaries.sh`，并保存每个平台的审计结果。
- [ ] 发布包不得包含 Chromium 源码、GN/Ninja 输出、`.o`、静态归档、Siso
  缓存、smoke 可执行文件、Android/Java/JNI 文件或本地证书私钥。

## 阶段 3：Rust 安全层审计与冻结

- [ ] 核对 `minicronet-sys` 与 ABI v7：结构体、枚举、回调签名、`size/version`
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

- [ ] 把 `new/profiles` 中的历史证据和规范化参数迁入版本化 profile 目录；运行时
  只编译精简只读表，不把源码证据文本编入二进制。
- [ ] 验证 `chrome_99`--`chrome_151` 的 TLS/ALPN、H2 SETTINGS、QUIC/H3、WS
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

Python 资源生命周期、流式响应、WebSocket 有界队列、Core 回调隔离以及 ABI v8
pause/resume 的详细设计和验收门槛见
[`PYTHON_RESOURCE_BACKPRESSURE_PLAN.md`](PYTHON_RESOURCE_BACKPRESSURE_PLAN.md)。
当前阶段 1--3 已完成 ABI v7 初版实现；阶段 4 的 Core 回调隔离和阶段 5 的 ABI v8
pause/resume 仍未实施，不能将其视为已完成能力。

完成条件是每项都有命令输出或目标机报告；“文件存在”不等于“平台支持完成”。
