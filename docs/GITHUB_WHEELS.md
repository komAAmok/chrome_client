# GitHub Actions wheel 构建

`.github/workflows/build-wheels.yml` 只构建 Rust/PyO3 绑定，不重新编译 Chromium。构建前必须让 GitHub runner 获得已经审计过的 Core 文件：

```text
core/binaries/<target>/
```

这些 Core 二进制已直接提交进仓库（`git ls-files core/binaries/` 可见，`.gitattributes` 把它们标记为 binary），不是 Git LFS 指针；`.gitignore` 里针对 `core/binaries/*/*.so` 之类的规则只作用于未跟踪的本地产物，不影响已提交的发布产物。工作流用 `actions/checkout@v4` 的 `lfs: true` 检出；Linux 与 macOS 的 job 在编译前跑 `Verify Core artifact` 步骤、Windows 的 job 跑 `tools/stage-windows-wheel.ps1`，两者都按 `manifest.json` 校验 SHA-256 和字节数，缺文件或对不上就中止。`tools/audit-core-binaries.sh` 只在 `ci.yml` 里运行，wheel 工作流不调用它。

Linux 使用 `manylinux2014` 容器。该容器以 glibc 2.17 为基线，因此生成的 wheel 兼容 glibc 2.18 及以上版本。`auditwheel` 会收集 `libminicronet.so` 及 NSS/NSPR 私有依赖；不能从 Ubuntu runner 直接收集依赖。

Windows 构建前由 `tools/stage-windows-wheel.ps1` 把匹配架构的 `minicronet.dll` 放到 `chrome_client` 包目录中。ABI v8 起 ICU 数据已编入库中，不再携带 `icudtl.dat`（每个 Windows wheel 因此少 10.8 MB），Windows 系统 DLL 同样不随 wheel 携带。

macOS 构建后使用 `delocate-wheel` 收集并修复匹配架构的 `libminicronet.dylib`。

主绑定使用 `cp37-abi3`，Python 3.6 使用独立的 `bindings/python36`、PyO3 0.15.1 和 `cp36-abi3`。为满足 maturin 的最低运行时要求，Linux 和 macOS 的 Python 3.6 ABI3 wheel 使用 CPython 3.7/3.10 作为构建解释器，最低兼容版本仍由 `abi3-py36` feature 和 wheel 标签决定；实际导入和运行需在独立 Python 3.6 环境中验证。Linux ARM64 在原生 `ubuntu-24.04-arm` runner 上构建，macOS 使用 `macos-15`（ARM64）和 `macos-15-intel`（x86_64）。Windows ARM64 暂不生成 Python 3.6 wheel：官方没有可用的 Python 3.6 ARM64 构建和对应导入库；准备好该工具链后再加入矩阵。

Alpine 不在本工作流中伪装成 glibc wheel。`musllinux` wheel 只有在准备好 musl 版 `libminicronet` 后才能增加对应 job；现有 glibc Core 不能放进 Alpine wheel。
