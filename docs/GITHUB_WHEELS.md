# GitHub Actions wheel 构建

`.github/workflows/build-wheels.yml` 只构建 Rust/PyO3 绑定，不重新编译 Chromium。构建前必须让 GitHub runner 获得已经审计过的 Core 文件：

```text
core/binaries/<target>/
core/dependencies/windows-*/icudtl.dat
```

这些大文件当前被 `.gitignore` 排除，发布时应使用 Git LFS 或在 workflow 中增加从 GitHub Release 下载并校验 SHA-256 的步骤。工作流中的 `tools/audit-core-binaries.sh` 会在编译前阻止缺失或错误架构的 Core 继续构建。

Linux 使用 `manylinux2014` 容器。该容器以 glibc 2.17 为基线，因此生成的 wheel 兼容 glibc 2.18 及以上版本。`auditwheel` 会收集 `libminicronet.so` 及 NSS/NSPR 私有依赖；不能从 Ubuntu runner 直接收集依赖。

Windows 构建前由 `tools/stage-windows-wheel.ps1` 把匹配架构的 `minicronet.dll` 和 `icudtl.dat` 放到 `chrome_client` 包目录中。Windows 系统 DLL 不随 wheel 携带。

macOS 构建后使用 `delocate-wheel` 收集并修复匹配架构的 `libminicronet.dylib`。

主绑定使用 `cp37-abi3`，Python 3.6 使用独立的 `bindings/python36`、PyO3 0.15.1 和 `cp36-abi3`。当前 maturin 自身只支持 Python 3.7 及以上，因此构建 `cp36-abi3` wheel 时使用受 PyO3 0.15.1 支持的较新解释器；最低兼容版本由 `abi3-py36` feature 和 wheel 的 `cp36-abi3` 标签决定。Python 3.6 的实际导入和运行仍需在独立 Python 3.6 环境中验证。Windows ARM64 暂不生成 Python 3.6 wheel：官方没有可用的 Python 3.6 ARM64 构建和对应导入库；准备好该工具链后再加入矩阵。

Alpine 不在本工作流中伪装成 glibc wheel。`musllinux` wheel 只有在准备好 musl 版 `libminicronet` 后才能增加对应 job；现有 glibc Core 不能放进 Alpine wheel。
