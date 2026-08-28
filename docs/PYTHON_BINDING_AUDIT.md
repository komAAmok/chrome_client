# Python binding audit

审计日期：2026-08-26

## 结论

主版本和 Python 3.6 版本都能编译并链接到同一 Rust/Core 层。主版本的
PyO3 0.28.3 使用 `abi3-py37`，3.6 版本固定使用 PyO3 0.15.1 和
`abi3-py36`。两者的同步网络等待都会释放 GIL；异步路径不创建线程池，
由 Core 回调通过 `loop.call_soon_threadsafe` 唤醒事件循环。

Python 公开包名为 `chrome_client`；facade 的内部实现目录为
`bindings/python/chrome_client/_python_impl`，不再作为顶层 `_python_impl`
包暴露，也不使用容易混淆的 `minicronet` Python 目录名。

主版本已达到可发布的 API/ABI 基线，但 Python 3.13 free-threaded 运行时、
Windows/macOS wheel 和真实 WSS 服务端仍需要对应构建机/目标机验收。Python
3.6 native 扩展已经补齐请求和 WebSocket 符号；3.6 的语法和运行时矩阵仍需
真实 Python 3.6 runner 验证。

## 本轮旧接口清理

- 删除顶层 `_python_impl` 源码包入口，实现在 `chrome_client._python_impl`
  私有子包内；公开导入仍只有 `chrome_client`。
- 删除旧 `minicronet` Python 包名、旧 native 别名和 `_LEGACY_NATIVE` 兼容变量；
  native 扩展只按 Python 版本选择 `chrome_client_native` 或
  `chrome_client_native36`。
- `Client`、`AsyncClient` 和 WebSocket 构造/请求不再静默吞掉未知关键字，旧字段
  会直接得到 `TypeError`，避免拼写错误被忽略。
- `stream` 保留为 Requests 兼容参数；现在已提供同步/异步 body iterator。
  `stream=False` 仍聚合到 `content`，`stream=True` 使用有界消费路径。

## 主版本（Python 3.7–3.13）

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| PyO3 版本 | 通过 | `pyo3 = =0.28.3` |
| 最低 Python | 通过 | `abi3-py37`、`requires-python >=3.7` |
| 扩展入口 | 通过 | 导出 `PyInit_chrome_client_native` |
| 公开导入 | 通过 | `import chrome_client` |
| GIL | 通过 | `Python::detach` 包住同步等待；回调只 `Python::attach` 调度 loop |
| asyncio | 通过 | `call_soon_threadsafe`，无 per-request 线程 |
| 取消/超时 | 通过 | `mn_request_cancel`，映射 `Timeout`/`CancelledError` |
| 高并发 | 通过 | 32/128/1000 请求测试 |
| 大响应 | 通过 | 4 MiB smoke；Rust 每请求 1 MiB 有界 body 队列 |
| 资源生命周期 | 已接入 | `Client.close()`/`AsyncClient.aclose()` 幂等；请求取消/超时会清理 callback |
| WebSocket 队列 | 已接入初版 | Rust 按 1024 条/4 MiB 限制；异步层使用 Future 唤醒，超限取消并报错 |
| free-threaded 3.13t | 明确不承诺 | module 声明 `gil_used = true`，导入时保留 GIL；必须完成独立 3.13t 审计后才能改为 `false` |
| 流式 Response | 已接入 | `stream=True` 返回 body iterator；`max_response_bytes` 超限会取消并抛出 `ResponseTooLarge` |

主版本实际使用了 0.28.3 的新 GIL API（`detach/attach`）。同步 Core I/O
期间调用 `detach`，因此不会阻塞其他 Python 线程；回调只短暂 `attach` 调度
事件循环。模块目前明确 `gil_used = true`，因此不会误报已经支持 free-threaded
3.13t；普通 `cp37-abi3` wheel 也不应和 free-threaded wheel 混为一谈。

## Python 3.6 版本

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| PyO3 版本 | 通过 | `pyo3 = =0.15.1` |
| 最低 Python | 通过 | `abi3-py36`、`requires-python >=3.6,<3.7` |
| 扩展入口 | 通过 | 导出 `PyInit_chrome_client_native36` |
| 类导出 | 通过 | `PyEngine`、`PyRequest`、`PyResponse`、`PyWebSocket` |
| 同步 GIL | 通过 | `Python::allow_threads` 包住 Core 等待 |
| asyncio bridge | 代码通过 | 使用 3.6 可用的 `Python::with_gil` 和 loop callback |
| 请求参数 | 通过 | timeout、redirect、impersonate、proxy、verify |
| WebSocket | 代码通过 | `PyWebSocket` 已导出，同步/异步事件接口与主版本对齐 |
| 资源生命周期 | 已接入初版 | close/cancel/detach callback 路径幂等 |
| 真实 Python 3.6 运行 | 待验收 | 当前环境没有 `python3.6` 可执行文件 |

3.6 版本不能使用 PyO3 0.28 的 `Python::attach/detach`、free-threaded slot
或 Python 3.7+ 语法；它使用 0.15.1 对应的 `allow_threads/with_gil`，这是
正确的版本化实现，而不是把现代扩展强行加载到 3.6。

## 可重复命令

```sh
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace --all-targets
cargo check -p chrome-client-python
cargo check --manifest-path bindings/python36/Cargo.toml
python3 -m unittest bindings/python/tests/test_stability.py
readelf -Ws target/debug/libchrome_client_native.so | grep PyInit
readelf -Ws bindings/python36/target/debug/libchrome_client_native36.so | grep PyInit
```

当前 Python 3.12 环境中的稳定性测试为 `6 passed, 1 skipped`；跳过项仅在
未设置 `MINICRONET_WS_URL`/`MINICRONET_WSS_URL` 时发生。真正发布前必须在
Python 3.6、3.7、3.13（含 3.13t）及各目标平台 wheel 上重复同一矩阵。
