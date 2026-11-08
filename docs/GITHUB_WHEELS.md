# GitHub Actions wheel 构建

`.github/workflows/build-wheels.yml` 只构建 Rust/PyO3 绑定，不重新编译 Chromium。构建前必须让 GitHub runner 获得已经审计过的 Core 文件：

```text
core/binaries/<target>/
```

这些 Core 二进制已直接提交进仓库（`git ls-files core/binaries/` 可见，`.gitattributes` 把它们标记为 binary），不是 Git LFS 指针；`.gitignore` 里针对 `core/binaries/*/*.so` 之类的规则只作用于未跟踪的本地产物，不影响已提交的发布产物。工作流用 `actions/checkout@v4` 的 `lfs: true` 检出；Linux 与 macOS 的 job 在编译前跑 `Verify Core artifact` 步骤、Windows 的 job 跑 `tools/stage-windows-wheel.ps1`，两者都按 `manifest.json` 校验 SHA-256 和字节数，缺文件或对不上就中止。`tools/audit-core-binaries.sh` 只在 `ci.yml` 里运行，wheel 工作流不调用它。

Linux 使用 `manylinux2014` 容器。该容器以 glibc 2.17 为基线，因此生成的 wheel 兼容 glibc 2.18 及以上版本。`auditwheel` 会收集 `libminicronet.so` 及 NSS/NSPR 私有依赖；不能从 Ubuntu runner 直接收集依赖。

Windows 构建前由 `tools/stage-windows-wheel.ps1` 把匹配架构的 `minicronet.dll` 放到 `chrome_client` 包目录中。ABI v8 起 ICU 数据已编入库中，不再携带 `icudtl.dat`（每个 Windows wheel 因此少 10.8 MB），Windows 系统 DLL 同样不随 wheel 携带。

macOS 发布物是 **universal2** wheel，由 `macos-universal` job 生成。Chromium 是按单一
架构交叉构建的，所以 `core/binaries/` 里只有 `macos-x86_64/` 与 `macos-arm64/` 两个
dylib，没有 universal 产物，而 `rustc --print target-list` 里也没有 `universal2-apple-darwin`
（1.98.0 实测，`cargo build --target universal2-apple-darwin` 直接失败）。因此流程是：

1. `tools/lipo-dylib.py` 先把两个 Core dylib 合成 `universal-core/libminicronet.dylib`；
2. 两个 Rust triple 各出一个 wheel（`--target x86_64-apple-darwin` / `aarch64-apple-darwin`）；
3. `tools/fuse-macos-wheels.py` 把两个 wheel 的原生扩展 lipo 成 universal2，并把
   `WHEEL` 的 `Tag:` 与文件名一起改到 `macosx_<floor>_universal2`；
4. `delocate-wheel` 在 `DYLD_LIBRARY_PATH=universal-core` 下收依赖，确保打进 wheel 的
   是合成后的那份 dylib，而不是先找到的分架构那一个。

`macos` job 仍然按架构各构建一次并 delocate，这是证明两半各自可加载的那一步；它的
artifact 名字是 `per-arch-*`，`publish` 的 `pattern: wheel-*` 不会收集，避免同一个
平台同时有分架构与 universal2 两个候选让 pip 去挑。

两个 dylib 的 macOS 部署目标不同（当前 x86_64 是 10.12，arm64 是 11.0），融合后的 tag
取两者中更新的那个，并在日志里打印这条改写。

主绑定使用 `cp39-abi3`，Python 3.6–3.8 使用独立的 `bindings/python36`、PyO3 0.15.1 和 `cp36-abi3`。

现代 wheel 的构建解释器必须 **>= 3.9**，这是 pyo3 0.30.1 的硬约束：`abi3-py39` 配上 3.7 解释器会直接报 "cannot set a minimum Python version 3.9 higher than the interpreter version 3.7"（`pyo3-build-config-0.30.1/src/impl_.rs:82-89`）。manylinux2014 镜像现在也只提供 CPython 3.9 及以上的 `/opt/python/cp3XX-cp3XX` 目录，原来的 `cp37-cp37m` 已经不存在，所以 Linux 全部改用 `/opt/python/cp39-cp39/bin/python3.9`。Python 3.6 的 ABI3 wheel 同样用 CPython 3.9 作为构建解释器（maturin 0.14.17 要求 >= 3.7），最低兼容版本仍由 `abi3-py36` feature 和 wheel 标签决定；实际导入和运行需在独立 Python 3.6 环境中验证。Linux ARM64 在原生 `ubuntu-24.04-arm` runner 上构建，macOS 使用 `macos-15`（ARM64）和 `macos-15-intel`（x86_64）。Windows ARM64 暂不生成 Python 3.6 wheel：官方没有可用的 Python 3.6 ARM64 构建和对应导入库；准备好该工具链后再加入矩阵。

Alpine 不在本工作流中伪装成 glibc wheel。`musllinux` wheel 只有在准备好 musl 版 `libminicronet` 后才能增加对应 job；现有 glibc Core 不能放进 Alpine wheel。
