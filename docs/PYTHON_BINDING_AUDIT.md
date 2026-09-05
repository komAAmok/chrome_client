# Python binding audit

审计日期：2026-09-04（上一轮：2026-08-26）

本轮把 Python 绑定从"能跑通"提升到"requests / curl_cffi 可直接迁移"，并修掉了
四个用测量而非推理确认的缺陷。所有结论都附可复现的证据。

## 修复的缺陷

### 1. 异步请求泄漏（每请求约 12 KiB）

asyncio 桥会在 Core 上安装一个事件回调，该回调持有事件循环和 Python `notify`
可调用对象，而 `notify` 又持有 `PyRequest`。这个环穿过 Rust，Python 的 GC 看不
见它。旧代码只在 `cancel()` / `close()` / `detach_callback()` 时清理回调，**正常
完成的路径从不清理**。

测量（Linux x86_64，同一进程，`/proc/self/statm`，每轮 500 次请求后 `gc.collect()`）：

| 轮次 | 修复前 RSS 增量 | 修复后 RSS 增量 |
| --- | --- | --- |
| 1 | +5904 KiB | +48 KiB |
| 2 | +11856 KiB | +64 KiB |
| 3 | +17764 KiB | +68 KiB |
| 4 | +23704 KiB | +68 KiB |

修复前每 500 次请求稳定增长约 5.9 MiB（≈12 KiB/请求）；修复后 4 轮共 2000 次请求
的总增量停在 +68 KiB，不再随请求数线性增长。同步路径修复前后都是 +100 KiB 量级。

修复：`PyRequest::finish()` 在每个终止事件（`done` / `error`）上清理事件回调并丢
弃 response future，`poll_event` / `poll_events` 的所有终止分支都调用它；WebSocket
的 `closed` / `error` 事件同样处理。两个绑定都已修改。

回归测试：`test_compat.LifecycleTests.test_async_requests_do_not_leak`，断言 800
次请求的 RSS 增长小于 2 MiB（按旧速率应为约 9 MiB）。

### 2. 每次请求重建一个 Chromium Engine

旧 facade 在任何 per-request override（`verify=`、`proxy=`、`impersonate=`）或任何
带构造参数的模块级调用时新建 `Client`，也就是新建一个完整的 Chromium
`URLRequestContext`：独立线程、socket 池、DNS 缓存、TLS session 缓存、HTTP 缓存和
cookie store。

测量（统计 Engine 构造次数）：

| 场景 | 修复前 | 修复后 |
| --- | --- | --- |
| 5 × `requests.get(url, timeout=5)` | 5 | 1 |
| 5 × `session.get(url, verify=False)` | 6 | 1 |
| 5 × `session.get(url, impersonate="chrome_151")` | — | 1 |

副作用比开销更严重：新 Engine 带来空 cookie store，所以**任何 per-request override
都会丢掉会话状态**。实测修复前 `session.get("/echo", verify=False)` 返回
`<none>`，同一 session 不带 override 时返回完整 cookie。

修复：`engine.EngineCache` 按配置缓存 Engine——每个 session 独立、有界
（`max_engines`，默认 8）、带锁、跨 fork 自动重建。

### 3. 流式请求失败时死锁

`stream=True` 的消费者等待的是一个 `asyncio.Event`，而失败路径只 resolve 了
response future。future 在流式模式下没有任何人 await，于是消费者永远睡着。

复现：`await session.get(url, stream=True, max_response_bytes=1024)` 之后迭代
body，进程挂住（`timeout` 退出码 124）。这也是 `test_stability` 挂在
`test_streaming_and_response_limit` 的原因。

修复：`_AsyncState._fail()` 总是发信号，并且只在非流式模式下 resolve future——流式
模式下 resolve 还会留下无人取走的异常。

回归测试：`test_compat.CurlCffiSurfaceTests.test_async_stream_failure_surfaces`。

### 4. `allow_redirects=False` 挂住

Chromium 在 manual redirect 模式下会 defer 这一跳并等待 `follow_redirect`，所以
只等 response 永远等不到。旧代码就是只等 response。

修复：Rust 侧新增 `wait_manual()`（同时等 redirect 和 response，取先到者），异步侧
新增 `redirect` 事件类型。同步与异步都已验证。

### 5. WebSocket 整条路径不可用

`websocket()` 的 `origin` 默认是 `""`，而 Core 拒绝空 origin
（`mn_websocket_create` 要求 `origin_length != 0` 且 origin 非 opaque），所以**任何
没有显式传 origin 的 WebSocket 调用都直接失败**。旧的 WS 测试只在设置了
`MINICRONET_WS_URL` 时运行，所以这条路径从未被覆盖。

同一次排查在这条路径上找到 5 个问题：

| 问题 | 表现 | 修复 |
| --- | --- | --- |
| `origin=""` | 全部 `websocket()` 返回 `InvalidArgument` | 默认取 URL 自身的 origin（`ws://h:p` → `http://h:p`），即同源页面连接的样子 |
| 错误未映射 | 泄漏裸 `RuntimeError` | 走 `map_native_error`；WS 失败消息改为具名 net error（`ERR_UNSAFE_PORT (net error -312)`） |
| 构造函数在握手完成前返回 | 立即 `close()`/`send()` 得到 `InvalidState` | 同步与异步构造都等 open 事件；抢先到达的数据帧排队而不是丢弃 |
| `UnsupportedFeature` 被重新映射 | 它经 `NotImplementedError` 继承 `RuntimeError`，被 `except RuntimeError` 捕获后变成 `RequestException` | 先 `except RequestException: raise` |
| `WebSocket(url=...)` 泄漏 Session | 每个 socket 留下一个 Chromium Engine | 记录自有 Session，`close()` 时释放 |

新增 13 个 WS 回归测试，用本地握手服务器，不再依赖外部端点。

WS 路径没有内存泄漏。第一次测量看到每连接约 3.5 KiB 的线性增长，按对象计数定位后
发现增长来自测试服务器自己记录每次握手（每连接一个 dict 加一个 list），换成不记录的
服务器后 800 次同步连接 +28 KiB、800 次异步连接 +52 KiB。同一结论用纯 Rust 探针
（`crates/minicronet/examples/websocket_churn.rs`，不经过 Python）独立确认：800 次连接
+16 KiB。

### 关于「WebSocket 的 UA 是否应接受顶层传入」

不应该，这是本轮明确否掉的一个改动。Core 的
`IsForbiddenWebSocketHeader`（`core/source/minicronet.cc:60`）把
`Connection`、`Host`、`Origin`、`User-Agent`、`Upgrade`、`Sec-WebSocket-*` 列为禁止的
额外头，传入直接 `MN_ERROR_INVALID_ARGUMENT`。即使 Core 允许，也不该这么做：实测
握手头顺序为

```
Host, Connection, Pragma, Cache-Control, User-Agent, Upgrade, Origin,
Sec-WebSocket-Version, Accept-Encoding, Sec-WebSocket-Key, Sec-WebSocket-Extensions
```

UA 的**位置**由 Chromium 决定，本身就是指纹的一部分；从 Python 侧插一个 UA 只会
破坏它，并且让 WS 握手的 UA 与同一 session 其它请求的 UA 不一致，而调用方看不到这
个差异。

正确的入口是 Engine 级的 `Session(user_agent=...)`，它对 HTTP 与 WS 一致生效（实测
WS 握手 UA 变为 `Engine/1.0`）；或者换 `impersonate` profile（实测 `chrome_152` 与
`chrome_100` 的 WS 握手 UA 分别是各自的 Chrome UA）。`Session(user_agent=...)` 与
profile 同时给出时 Core 报 `ProfileConflict`，这也是对的——profile 拥有自己的 UA。

因此实现选择是：per-call 或 session 级的 `User-Agent` 头在 WS 路径上抛
`UnsupportedFeature` 并说明替代入口，而不是静默丢弃。静默丢弃会产生一个调用方无法
察觉的指纹不一致。

### 6. 证书错误丢失原始错误码（已修，改了 C++ 并重建 Core）

**症状。** 任何证书校验失败都报成 `ERR_ABORTED (net error -3)`，与调用方主动取消
完全无法区分。`CertificateVerifyError` 早就实现并映射好了，但永远不会触发。

**根因，逐层确认。**

1. TLS 校验失败时，Chromium 调用
   `URLRequest::NotifySSLCertificateError(net_error, ssl_info, fatal)`
   （`net/url_request/url_request.cc:1246`）。它先把 `status_` 置为 `OK`，再把**真实
   错误码**（如 `ERR_CERT_DATE_INVALID` = -201）交给 delegate。
2. 请求在这里不会自己结束——Chromium 把决定权交给 delegate：要么
   `Cancel()`，要么 `ContinueDespiteLastError()`（`url_request.h:201-210` 的注释）。
3. Core 没有覆盖这个方法，所以跑的是基类默认实现
   （`url_request.cc:193-198`）：`request->Cancel();`。
4. `Cancel()` 就是 `DoCancel(ERR_ABORTED, SSLInfo())`（`url_request.cc:792`）——
   **写死** `ERR_ABORTED`，并且传入一个空的 `SSLInfo`。于是 `status_ = -3`，证书错误码
   和 SSLInfo 一起被丢弃。
5. Core 的 `OnResponseStarted` 收到 -3，按 `net_error == ERR_ABORTED ?
   MN_ERROR_CANCELED : MapNetError(net_error)` 判成"取消"。

两条证据说明这是疏漏而不是设计选择：`error_mapping.h` 的兜底分支写着
`net::IsCertificateError(net_error) ? MN_ERROR_TLS : MN_ERROR_NETWORK`——映射一直在等一个
永远不会到达的错误码；同一个 Core 的 WebSocket 路径（`core/source/websocket.cc:147`）
用 `ssl_error_callbacks->CancelSSLRequest(net_error, &ssl_info)` 做对了。

对比 `OnCertificateRequested`（`url_request.cc:188`）用的是
`CancelWithError(ERR_SSL_CLIENT_AUTH_CERT_NEEDED)`——Chromium 自己在客户端证书那条路径
上保留了错误码，只有服务器证书这条路径的默认实现不保留。原因是真实浏览器会弹拦截页并
可能让用户继续，所以基类默认只做"拒绝"，不承诺错误码。

**修复。** 覆盖该方法并用 `CancelWithSSLError(net_error, ssl_info)`
（`core/source/request.cc` 与 `core/source/minicronet/request.h`），这正是
`services/network/url_loader.cc:2326` 在决定不继续时的调用，同时也把 SSLInfo 保留进
`response_info_`。8 个平台的 Core 全部已重建、安装并刷新 manifest；**不涉及 ABI 改动**，
仍是 v8，因为终止回调本来就带 `net_error` 字段。跨平台验收证据见
`docs/PROJECT_STATUS.md`：本机只能执行 x86_64 Linux 二进制，所以行为验证在
linux-x86_64，另外用 `llvm-nm` 确认 8 个 target 的 `request.o(bj)` 都定义了该虚函数，
并用重复构建确认 SHA-256 可复现。

先确认过 `verify=False` 不走这条路（它由 `InsecureCertVerifier` 加
`session_params.ignore_certificate_errors` 实现，见 `core/source/engine.cc:320` 与
`:420`），所以覆盖 delegate 不会把 `verify=False` 变成失败。

**实测结果**（本地私有 CA + 各带一个缺陷的证书，以及 badssl.com）：

| 场景 | 修复前 | 修复后 |
| --- | --- | --- |
| 过期证书 | `ERR_ABORTED (-3)` | `ERR_CERT_DATE_INVALID (-201)` |
| 主机名不匹配 | `ERR_ABORTED (-3)` | `ERR_CERT_COMMON_NAME_INVALID (-200)` |
| 自签 / 未知 CA | `ERR_ABORTED (-3)` | `ERR_CERT_AUTHORITY_INVALID (-202)` |
| 异常类型 | `RequestException` | `CertificateVerifyError`（继承 `SSLError` → `ConnectionError`） |

未回退的行为：`verify=False` 仍放行（三种缺陷都是 200）；`verify=<CA>` 对合法证书返回
200；主动取消仍是 `CancelledError`；超时仍是 `Timeout`；WSS 路径不受影响。8 个回归测试
用本地生成的 CA 与证书，不依赖外网。

## 本轮新增的 API 面

Python 包重写为多个模块，公开导入仍只有 `chrome_client`，另加真实子模块
`chrome_client.requests`。

| 模块 | 内容 |
| --- | --- |
| `exceptions` | requests 全层次 + curl_cffi 叶子类型；Chromium net error 码映射 |
| `structures` | `CaseInsensitiveDict`（保留原始大小写）、`Headers`（保留重复字段）、`LookupDict` |
| `cookies` | `RequestsCookieJar`（`http.cookiejar.CookieJar` 子类）与 Core cookie store 桥 |
| `models` | `Request`、`PreparedRequest`、`Response`、`AsyncResponse`、`RawStream` |
| `status_codes` | `codes` 查找对象与 reason 短语表 |
| `utils` | `default_headers`、`requote_uri`、`parse_header_links`、代理与 netrc 解析等 |
| `auth` | `AuthBase`、`HTTPBasicAuth`、`HTTPProxyAuth`、`HTTPDigestAuth` |
| `adapters` | `BaseAdapter`、`HTTPAdapter`（挂载点保留，池参数不生效） |
| `multipart` | `CurlMime`、`files=` 编码、分块上传辅助 |
| `impersonate` | profile 归一化、`ExtraFingerprints`、`CurlHttpVersion` |
| `engine` | `EngineConfig`、`EngineCache` |
| `sessions` | `Session`、`AsyncSession`、`RetryStrategy` |
| `websockets` | `WebSocket`、`AsyncWebSocket`、`WsCloseCode` |
| `api` | 模块级 `get`/`post`/… 与共享 session |

关键行为决定：

- **facade 不再注入任何默认请求头。** profile 与 Chromium 拥有默认头集合、取值和
  顺序；注入 `Accept: */*` 会被指纹检测看到。`utils.default_headers()` 返回空。
- **`referer=` 与 `Referer` 头显式报错。** 实测 Chromium 会剥掉调用方设置的
  `Referer`（18 个探测头中唯一被丢弃的一个），ABI v8 也没有 referrer 字段，所以
  接受它等于让调用方以为设置成功了。
- **net error 码驱动异常类型和消息。** `ERR_CERT_DATE_INVALID (net error -201)`
  比 `Tls` 有用得多。
- `session.stream` 同时是 requests 的布尔标志和 curl_cffi 的上下文管理器方法
  （`int` 子类且 callable）。

## 主版本（Python 3.7–3.13）

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| PyO3 版本 | 通过 | `pyo3 = =0.28.3` |
| 最低 Python | 通过 | `abi3-py37`、`requires-python >=3.7` |
| 扩展入口 | 通过 | 导出 `PyInit_chrome_client_native` |
| 公开导入 | 通过 | `import chrome_client`、`from chrome_client import requests` |
| GIL | 通过 | `Python::detach` 包住同步等待；回调只 `Python::attach` 调度 loop |
| asyncio | 通过 | `call_soon_threadsafe`，无 per-request 线程；每次唤醒批量取 64 个事件 |
| 取消/超时 | 通过 | 单个 `call_later` 定时器，不再为每请求加一个 `wait_for` Task |
| 高并发 | 通过 | `asyncio.gather` 64/512/2000；线程池 8/32/64 workers 全 200 |
| 大响应 | 通过 | 4 MiB 单请求；批量轮询避免逐 chunk 往返事件循环 |
| 资源生命周期 | 通过 | 终止事件清理回调；被遗弃的 stream 由 `__del__` 兜底取消 |
| 泄漏 | 通过 | 见上表；2000 次异步请求后 +68 KiB |
| free-threaded 3.13t | 明确不承诺 | module 声明 `gil_used = true` |
| 分块上传 | 通过 | 文件对象 / 迭代器 / 异步迭代器均走 `upload_write` |
| 重定向 | 通过 | history、最终 URL、`max_redirects`、`allow_redirects=False` |

## Python 3.6 版本

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| PyO3 版本 | 通过 | `pyo3 = =0.15.1` |
| 最低 Python | 通过 | `abi3-py36`、`requires-python >=3.6,<3.7` |
| 扩展入口 | 通过 | 导出 `PyInit_chrome_client_native36` |
| native 方法对齐 | 通过 | 脚本比对两个扩展的方法集合；facade 使用的 25 个方法在 3.6 侧齐全 |
| 共享 facade 语法 | 通过 | 全部 `.py` 用 `ast.parse(feature_version=(3, 6))` 通过；无 f-string、无 walrus |
| 实际运行 | 通过（代理验证） | `abi3-py36` 扩展可被新版 CPython 导入，用它跑同一 facade：GET、cookie 会话、JSON、流式、重定向、override、50 并发异步、`ResponseTooLarge` 全部正确 |
| 真实 Python 3.6 解释器 | 待验收 | 当前环境没有 `python3.6` 可执行文件 |

3.6 版本不能使用 PyO3 0.28 的 `attach/detach` 或 3.7+ 语法；它使用 0.15.1 的
`allow_threads/with_gil`。两个扩展的方法集合必须保持一致，否则共享 facade 会在
3.6 上退化成 `AttributeError`——本轮新增的 `wait_manual`、`poll_events`、
`take_redirects`、`upload_write`、`upload_finish`、`read_body`、`attach_async`、
`await_response`、`start`、`resume_read`、`follow_redirect`、`is_finished` 都已同步。

## 可重复命令

```sh
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo build --release -p chrome-client-python
cargo build --release --manifest-path bindings/python36/Cargo.toml

LD_LIBRARY_PATH=core/binaries/linux-x86_64 PYTHONPATH=bindings/python \
  python3 -m unittest discover -s bindings/python/tests -p "test_*.py"

readelf -Ws target/release/libchrome_client_native.so | grep PyInit
readelf -Ws bindings/python36/target/release/libchrome_client_native36.so | grep PyInit
```

当前结果：103 个测试通过、1 个跳过（跳过项仅在未设置 `MINICRONET_WS_URL` /
`MINICRONET_WSS_URL` 时发生）。发布前仍需在真实 Python 3.6、3.13（含 3.13t）以及
各目标平台 wheel 上重复同一矩阵。

## 已知的 Core 侧限制（需要 ABI/Core 改动，本轮未修）

- **cookie store 不可读写。** ABI v8 的 21 个导出符号里没有任何 cookie 函数，
  facade 只能镜像 `Set-Cookie` 并在冲突时换 Engine。
- **无 per-request 代理 / TLS / 客户端证书。** 这些都是 Engine 级设置。
- **无 reason 短语、无 timing 指标。** `reason` 取自标准状态码表；`elapsed` 由
  facade 自己计时，不是 Core 上报的传输耗时。
