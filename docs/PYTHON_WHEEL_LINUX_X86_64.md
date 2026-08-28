# Linux x86_64 自包含 wheel 原型

构建使用仓库中已经编译好的 `core/binaries/linux-x86_64/libminicronet.so`，不会编译 Chromium。先安装 `maturin`、`auditwheel` 和 `patchelf`，然后运行：

```bash
tools/build-linux-x86_64-wheel.sh
```

脚本先生成未修复的 wheel，再由 `auditwheel repair` 收集并重命名 Core 及 NSS/NSPR 依赖。最终 wheel 内应包含：

```text
chrome_client.libs/
├── libminicronet-<hash>.so
├── libnspr4-<hash>.so
├── libnss3-<hash>.so
├── libnssutil3-<hash>.so
├── libplc4-<hash>.so
└── libplds4-<hash>.so
```

扩展和 Core 使用相对 ELF `RPATH`，因此安装后不需要设置 `MINICRONET_CORE_DIR` 或 `LD_LIBRARY_PATH`。glibc、系统 loader、`libc.so.6` 等仍由目标 Linux 提供，不能随 wheel 私自捆绑。

本次原型在 Ubuntu glibc 2.39 环境修复，并从该环境收集 NSS/NSPR，最终实际标签为 `manylinux_2_38_x86_64`；这不是 Core 构建环境的判断，也不是旧发行版兼容承诺。发布前应在目标 manylinux 基线环境重新构建 Rust 扩展并收集该基线的 NSS/NSPR，再在没有系统 NSS/NSPR 的干净容器中测试 HTTPS、证书校验和并发请求。这个过程仍然使用已有 Core，不需要编译 Chromium。

验证示例：

```bash
python3 -m venv /tmp/chrome-client-wheel-test
/tmp/chrome-client-wheel-test/bin/pip install dist/linux-x86_64-prototype/*.whl
env -u LD_LIBRARY_PATH -u MINICRONET_CORE_DIR \
  /tmp/chrome-client-wheel-test/bin/python -c \
  'import chrome_client; print(chrome_client.get("https://example.com").status_code)'
```
