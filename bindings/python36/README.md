# chrome_client

当前版本：`0.2.2`

基于 Chromium 网络栈的 HTTP/WebSocket 客户端。Core 负责 TLS、HTTP、HTTP/2、
HTTP/3/QUIC、代理和 WebSocket/WSS；Python/Rust 绑定只负责参数、类型、错误和
生命周期转换，不另实现一套网络协议。

Python API 同时对齐两套习惯：`requests` 的 `Session`/`Response`/异常层次，以及
`curl_cffi` 的 `impersonate`、`http_version`、`AsyncSession`、`CurlMime` 和
WebSocket。无法用 Chromium 忠实实现的选项会显式报错，而不是静默忽略——详见
[兼容边界](https://github.com/komAAmok/chrome_client/blob/main/docs/COMPATIBILITY_BOUNDARY.md)。

[English README](https://github.com/komAAmok/chrome_client/blob/main/README.en.md) · [构建说明](https://github.com/komAAmok/chrome_client/blob/main/docs/BUILD.md) · [兼容边界](https://github.com/komAAmok/chrome_client/blob/main/docs/COMPATIBILITY_BOUNDARY.md)

## 支持范围

### 操作系统、架构与绑定

| 操作系统 | Core 目标 | Python 3.7–3.13 | Python 3.6 | Rust | 备注 |
| --- | --- | --- | --- | --- | --- |
| Linux | x86 (`i686`)、x86_64、ARM64 | ✓ | ✓（独立 abi3 扩展） | ✓ | manylinux/glibc 运行时依赖见 manifest |
| Windows | x86、x86_64、ARM64 | ✓ | x86/x86_64 ✓ | ✓ | DLL 随 wheel；ICU 数据已编入库中 |
| macOS | x86_64、ARM64 | ✓ | ✓（独立 abi3 扩展） | ✓ | dylib 按架构匹配 |


每个 Core 产物位于 `core/binaries/<target>/`，带 ABI 版本、Chromium revision、
SHA-256 和依赖清单。ABI 当前为 v8。Go 和 Node.js 目录目前是绑定设计说明，
不是已发布的可安装包。

Core 体积在 7.8–11.3 MB 之间（macOS ARM64 最小，Windows x86_64 最大，静态 MSVC/UCRT
多出约 2 MB）。IDNA-only 的 ICU 数据已编入库中，所以不需要外挂 `icudtl.dat`；用不到的
磁盘缓存后端不进链接产物。各平台的体积上限由 `tools/audit-core-*.sh` 把关，超了直接
构建失败。

| Core 目录 | Rust target | Core 文件 |
| --- | --- | --- |
| `linux-x86` | `i686-unknown-linux-gnu` | `libminicronet.so` |
| `linux-x86_64` | `x86_64-unknown-linux-gnu` | `libminicronet.so` |
| `linux-arm64` | `aarch64-unknown-linux-gnu` | `libminicronet.so` |
| `windows-x86` | `i686-pc-windows-msvc` | `minicronet.dll` + `minicronet.lib` |
| `windows-x86_64` | `x86_64-pc-windows-msvc` | `minicronet.dll` + `minicronet.lib` |
| `windows-arm64` | `aarch64-pc-windows-msvc` | `minicronet.dll` + `minicronet.lib` |
| `macos-x86_64` | `x86_64-apple-darwin` | `libminicronet.dylib` |
| `macos-arm64` | `aarch64-apple-darwin` | `libminicronet.dylib` |

### Chrome profile

`impersonate` 推荐使用精确的 `chrome_<major>` 名称（同时接受 curl-cffi 风格的
`chrome<major>` 别名，以及解析到最新 pinned 版本的 `chrome`）。当前支持
Chrome 99–152，共 54 个 profile；范围外的版本抛 `ImpersonateError`，不会静默降级。
`available_profiles()` 返回完整列表。Edge、Safari、Firefox、Tor 等非 Chromium 目标
同样显式报错，而不是当作 Chrome 处理。

| Chrome 主版本 | 可用 profile |
| --- | --- |
| 99–105 | `chrome_99` … `chrome_105` |
| 106–112 | `chrome_106` … `chrome_112` |
| 113–119 | `chrome_113` … `chrome_119` |
| 120–126 | `chrome_120` … `chrome_126` |
| 127–133 | `chrome_127` … `chrome_133` |
| 134–140 | `chrome_134` … `chrome_140` |
| 141–147 | `chrome_141` … `chrome_147` |
| 148–152 | `chrome_148`、`chrome_149`、`chrome_150`、`chrome_151`、`chrome_152` |

Profile 影响 TLS ClientHello、ALPN、HTTP/2 设置、QUIC/H3 和相关 Chromium 网络
参数；它不是完整 Chrome 浏览器，也不包含 Blink、扩展、Service Worker 或持久化
浏览器 Profile。

### 功能矩阵

| 功能 | Python | Rust/Core | 说明 |
| --- | --- | --- | --- |
| HTTP/1.1、HTTP/2、HTTP/3/QUIC | ✓ | ✓ | 默认由 Chromium 协商；`http_version="v1"/"v2"/"v3"` 可强制 |
| HTTPS/TLS、证书校验 | ✓ | ✓ | `verify=False` 或 `verify="/path/ca.pem"` 自定义 CA；证书失败按具体检查项报错 |
| HTTP/HTTPS/SOCKS 代理 | ✓ | ✓ | `proxy`、Requests 风格 `proxies`，运行期可改 |
| 同步请求 | ✓ | ✓ | 同步等待释放 GIL，可直接放进线程池 |
| asyncio 请求 | ✓ | — | Core 回调唤醒事件循环，无请求线程池；每次唤醒批量取事件 |
| 流式响应 | `iter_content` / `aiter_content` / `raw` | `ResponseStream` | 每请求 body 队列上限 1 MiB，超限由 ABI v8 暂停读取 |
| 分块上传 | `data=` 传文件对象或迭代器 | `Upload::Chunked` | 同步与异步都走 `upload_write` |
| 取消、超时、大小限制 | ✓ | ✓ | `Timeout`、`ResponseTooLarge`、`RequestException` |
| WebSocket / WSS | ✓ | ✓ | 同步与异步，curl-cffi 方法名齐全 |
| Cookie | ✓ | ✓ | `session.cookies` 为 `RequestsCookieJar`，读写都生效 |
| 重定向 | ✓ | ✓ | `history`、最终 URL、`max_redirects`、`allow_redirects=False` |
| 内存缓存 | ✓ | ✓ | 随 Engine 生命周期存在，`cache=False` 可关闭 |


## 安装与加载 Core

```bash
python -m pip install chrome-client
```

已发布的 wheel 自带对应平台的 native 扩展。从源码使用时，先针对某个 Core 目录构建
扩展（`MINICRONET_CORE_DIR` 在构建期读取），运行时把同一目录放到加载路径上：

```bash
MINICRONET_CORE_DIR=$PWD/core/binaries/linux-x86_64 cargo build --release -p chrome-client-python

LD_LIBRARY_PATH=$PWD/core/binaries/linux-x86_64 PYTHONPATH=bindings/python python -c \
  'import chrome_client; print(chrome_client.get("https://example.com").status_code)'
```

## Python 使用示例

### requests 风格

```python
import chrome_client as requests          # 或 from chrome_client import requests

response = requests.get(
    "https://example.com/api",
    params={"page": 1},
    headers={"Accept": "application/json"},
    impersonate="chrome_152",
    timeout=15,
)
response.raise_for_status()
print(response.status_code, response.reason, response.json())
```

`Session` 与 `requests.Session` 同形：`headers`、`params`、`cookies`、`proxies`、
`auth`、`hooks`、`stream`、`verify`、`max_redirects`、`trust_env`、`adapters`、
`mount()`、`prepare_request()`、`send()`、`resolve_redirects()`、`close()` 和
上下文管理器都可用。

```python
from chrome_client import Session

with Session() as session:
    session.headers.update({"Accept": "application/json"})
    session.get("https://example.com/login")          # 服务端 Set-Cookie
    print(session.cookies.get_dict())                  # 会话内可见
    session.cookies.set("consent", "1", domain="example.com", path="/")
    session.get("https://example.com/private")         # 自动带上两类 cookie
```

### 会话保持：cookie 与代理

`session.cookies` 是 `RequestsCookieJar`（`http.cookiejar.CookieJar` 子类），
带 domain/path/secure 元数据，读写都会生效：

```python
session.cookies.get_dict()
session.cookies.get_dict(domain="example.com")
session.cookies.set("sid", "abc", domain="example.com", path="/")
session.cookies.update({"a": "1"})
del session.cookies["sid"]
session.cookies.clear()
```

响应侧 cookie 由 Core 内的 Chromium `CookieMonster` 拥有并自动附加；facade 会把每
一跳（含重定向）的 `Set-Cookie` 同步进 `session.cookies` 和 `response.cookies`。
当调用方改动 jar 与 Core 已存的 cookie 冲突时，Core 会覆盖请求头，因此 facade 会
换一个空 cookie store 的同配置 Engine，让改动真正生效——连接池仍按配置复用。

`session.proxies` 是普通可变映射，运行期改动立即生效：

```python
session.proxies.update({"https": "http://user:pass@127.0.0.1:8080"})
session.get("https://example.com")                     # 走代理
session.proxies.clear()
session.get("https://example.com")                     # 直连
session.get("https://example.com", proxy="socks5://127.0.0.1:1080")   # 单次覆盖
```

`proxies` 按 `scheme://host`、`scheme`、`all://host`、`all` 顺序匹配；显式
`proxy=` 优先。`trust_env=True`（默认）时读取 `HTTP_PROXY`/`HTTPS_PROXY`/
`NO_PROXY`。代理是 Engine 级设置，切换代理会换一个 Engine，但 jar 里的 cookie 会
继续随请求发出。

### 重定向、异常与响应

```python
response = session.get("https://example.com/r")
print(response.url, response.history, response.redirect_count)
response = session.get("https://example.com/r", allow_redirects=False)
print(response.status_code, response.headers["Location"], response.next.url)
session.get("https://example.com/r", max_redirects=5)   # 超限抛 TooManyRedirects
```

异常层次与 `requests.exceptions` 一致（`RequestException` 继承 `IOError`），
并补上 curl-cffi 的叶子类型。Chromium 的 net error 会映射成具体类型和可读名字：

```python
try:
    session.get("https://expired.example.com")
except chrome_client.CertificateVerifyError as error:
    print(error)          # ERR_CERT_DATE_INVALID (net error -201)
except chrome_client.ConnectionError:
    ...
except chrome_client.Timeout:
    ...
```

证书失败按具体检查项区分：过期 `ERR_CERT_DATE_INVALID (-201)`、主机名不匹配
`ERR_CERT_COMMON_NAME_INVALID (-200)`、CA 不受信 `ERR_CERT_AUTHORITY_INVALID (-202)`，
类型均为 `CertificateVerifyError`（继承 `SSLError` → `ConnectionError`）。要接受私有
CA 用 `verify="/path/ca.pem"`，完全跳过校验用 `verify=False`（Engine 级设置）。

`Response` 提供 `status_code`、`reason`、`headers`、`cookies`、`history`、
`elapsed`、`request`、`url`、`encoding`、`apparent_encoding`、`text`、`content`、
`json()`、`raw`、`links`、`is_redirect`、`is_permanent_redirect`、`next`、`ok`、
`raise_for_status()`、`iter_content()`、`iter_lines()`、`close()`，以及 curl-cffi
的 `http_version`、`charset`、`redirect_count`、`redirect_url`。

### curl-cffi 风格：指纹与协议

```python
from chrome_client import Session, AsyncSession, CurlMime

with Session(impersonate="chrome152", http_version="v2") as session:
    session.get("https://example.com")
```

`impersonate` 接受 `chrome_152`、`chrome152`，以及解析到最新 pinned 版本的
`chrome`。TLS ClientHello、ALPN、HTTP/2 设置与优先级、HTTP/3 传输参数和默认请求头
顺序全部来自该 profile：这也是 `ja3=`、`akamai=`、`perk=` 和大部分 `extra_fp` 字段
会抛 `UnsupportedFeature` 的原因——接受一个 JA3 字符串却仍然发送 Chromium 自己的
ClientHello，等于谎报保真度。`extra_fp` 中 facade 能真正实现的两项会被采纳：
`header_order` 和 `form_boundary`。

不受支持而显式报错的还有：`cert=`（客户端证书）、`interface=`、`doh_url=`、
`curl_options=`、`max_recv_speed=`、`referer=`。`referer` 尤其容易误判：Chromium
自己拥有 referrer 并会剥掉调用方设置的 `Referer` 头，ABI v8 也没有 referrer 字段，
所以设置它在任何路径下都不会到达网络。

multipart 两种写法都支持：

```python
session.post(url, data={"title": "t"}, files={"f": ("a.txt", b"...", "text/plain")})

mime = CurlMime()
mime.addpart(name="title", data="hello")
mime.addpart(name="photo", filename="p.jpg", content_type="image/jpeg",
             local_path="/tmp/p.jpg")
session.post(url, multipart=mime)
```

### 流式读写与分块上传

```python
with session.stream("GET", "https://example.com/large") as response:
    for chunk in response.iter_content(64 * 1024):
        ...

response = session.get(url, stream=True, max_response_bytes=16 * 1024 * 1024)
try:
    for line in response.iter_lines():
        ...
finally:
    response.close()        # 未读完时取消 native 请求

with open("big.bin", "rb") as handle:
    session.post(url, data=handle)      # 文件对象或迭代器走分块上传
```

`max_response_bytes` 超限会取消请求并抛 `ResponseTooLarge`。每个请求的 body 队列
上限 1 MiB，超过时 ABI v8 用 `MN_READ_PAUSE` 暂停读取，不会占住 Core 线程。

### 并发

同步 `Session` 可直接跨线程共享：所有阻塞调用都释放 GIL，Engine 缓存有锁。

```python
from concurrent.futures import ThreadPoolExecutor

with Session() as session, ThreadPoolExecutor(max_workers=32) as pool:
    codes = list(pool.map(lambda url: session.get(url).status_code, urls))
```

asyncio 路径不创建线程池：Core 回调用 `call_soon_threadsafe` 唤醒事件循环，每次
唤醒批量取事件，因此一个几 MB 的响应不会按 chunk 逐次往返事件循环。

```python
import asyncio
from chrome_client import AsyncSession

async def main():
    async with AsyncSession(impersonate="chrome_152", max_clients=64) as session:
        responses = await asyncio.gather(*[session.get(u) for u in urls])
        async with session.stream("GET", big_url) as response:
            async for chunk in response.aiter_content(65536):
                ...

asyncio.run(main())
```

`max_clients` 限制同时在途的请求数。进程池请使用 `spawn` 或 `forkserver`：Engine
存在后进程已是多线程且 Chromium 线程持锁，`fork()` 可能在子进程执行任何 Python
代码之前就死锁。

Chromium 对同一 host 组默认最多 6 条 HTTP/1.1 连接（实测同一 host 并发上限确为 6，
换用多个 host 后吞吐随之上升）。这是浏览器语义的一部分，不是绑定的瓶颈：需要更高
单 host 并发时应使用 HTTP/2 或 HTTP/3 端点。

### WebSocket / WSS

```python
from chrome_client import WebSocket

with WebSocket(url="wss://echo.example.com", impersonate="chrome_152") as socket:
    socket.send_str("ping")
    print(socket.recv_str())
```

```python
async def main():
    async with AsyncSession() as session:
        socket = await session.websocket("wss://echo.example.com")
        async with socket:
            await socket.send_json({"op": "ping"})
            print(await socket.recv_json())
```

同步与异步都提供 `send`/`send_str`/`send_bytes`/`send_json`/`ping`、
`recv`/`recv_str`/`recv_bytes`/`recv_json`/`recv_fragment`、`close`、`terminate`，
同步侧还有 `run_forever(on_message=..., on_error=..., on_open=..., on_close=...)`。
构造函数返回时握手已完成——Core 在 open 之前会拒绝 `send()` 和 `close()`。

握手头有两条 Chromium 规则：

- **UA 不能按次传。** Core 明确拒绝把 `User-Agent`、`Host`、`Origin`、
  `Connection`、`Upgrade`、`Sec-WebSocket-*` 作为额外头（`IsForbiddenWebSocketHeader`），
  因为 Chromium 自己决定这些头的取值和在握手里的位置，而 UA 的位置本身就是指纹的
  一部分。要改 WebSocket 的 UA，用 `Session(user_agent=...)`（Engine 级设置，HTTP 与
  WS 一致）或换 `impersonate` profile；传 header 会得到 `UnsupportedFeature`，而不是
  静默生成一个与其它请求 UA 不一致的握手。
- **`Origin` 默认取自 URL 自身。** Core 拒绝空 origin，而这里没有页面可继承，所以
  默认用 `ws://host:port` 对应的 `http://host:port`——即同源页面发起连接的样子。需要
  别的值就显式传 `origin=`。

Cookie 会按 URL 匹配后附加到握手上。

## 与 requests / curl-cffi 的对照

`from curl_cffi import requests` 改成 `from chrome_client import requests`，或
`import requests` 改成 `import chrome_client as requests`，多数代码不需要其他改动。

| 用法 | 支持情况 |
| --- | --- |
| `get/post/put/patch/delete/head/options/trace/query` | ✓ |
| `Session`、`AsyncSession`、`Client`、`AsyncClient` | ✓（`Client` 是 `Session` 别名） |
| `params`、`data`、`json`、`content`、`files`、`multipart` | ✓ |
| `headers`、`cookies`、`auth`、`timeout`（含 `(connect, read)`） | ✓ |
| `proxies`、`proxy`、`proxy_auth`、`trust_env`、`NO_PROXY` | ✓ |
| `verify=True/False/"/path/ca.pem"` | ✓ |
| `allow_redirects`、`max_redirects`、`history`、最终 URL | ✓ |
| `stream=True`、`iter_content`、`iter_lines`、`raw` | ✓ |
| `aiter_content`、`aiter_lines`、`atext`、`acontent`、`session.stream(...)` | ✓ |
| `hooks={"response": ...}`、`prepare_request`、`send`、`mount`、自定义 adapter | ✓ |
| `Request`、`PreparedRequest`、`codes`、`CaseInsensitiveDict`、`Headers`、`Cookies` | ✓ |
| `HTTPBasicAuth`、`HTTPProxyAuth`、`AuthBase` | ✓ |
| `requests.exceptions` 全层次 + curl-cffi 叶子类型 | ✓ |
| `impersonate`、`http_version`、`retry`/`RetryStrategy`、`raise_for_status=True` | ✓ |
| `base_url`、`discard_cookies`、`default_encoding`、`content_callback` | ✓ |
| `CurlMime`、`ExtraFingerprints(header_order=..., form_boundary=...)` | ✓ |
| `HTTPDigestAuth` | 部分：需要用 `parse_challenge()`/`handle_401()` 显式驱动 |
| `HTTPAdapter(pool_connections=..., pool_maxsize=...)` | 接受但不生效：连接池由 Chromium 拥有 |
| `ja3`、`akamai`、`perk`、`extra_fp` 的 TLS/HTTP2 字段 | ✗ 显式抛 `UnsupportedFeature` |
| `cert`（客户端证书）、`interface`、`doh_url`、`curl_options`、`max_recv_speed` | ✗ 显式抛 `UnsupportedFeature` |
| `referer=` / `Referer` 头 | ✗ 显式抛 `UnsupportedFeature`（Chromium 会剥掉） |
| 持久化 cookie/cache 文件、`Curl` 低层句柄、`CurlOpt`/`CurlInfo` | ✗ |

行为差异（有意为之，不是缺陷）：

- 模块级 `chrome_client.get(...)` 共用一个进程内 `Session`，因此也共用连接池和
  cookie store。requests 每次新建 `Session`；这里一个 Session 等于一整个 Chromium
  `URLRequestContext`，按次新建会付出线程与内存代价。需要隔离时显式建 `Session`，
  或调用 `close_shared_session()`。
- `session.headers` 默认为空。requests 会预置 `User-Agent`/`Accept`/
  `Accept-Encoding`/`Connection`，而这里那些头由 profile 和 Chromium 决定，注入
  `Accept: */*` 会被指纹检测看见。
- `Response.raw` 是覆盖 `read`/`stream`/`close` 的文件对象，不是 urllib3
  `HTTPResponse`。
- `Response.ok` 用 requests 语义（`status_code < 400`），不是 curl-cffi 的
  200–399。

## 目录与验证

| 路径 | 内容 |
| --- | --- |
| `core/abi/` | 稳定 C ABI v8 |
| `core/binaries/` | 8 个已审计平台 Core |
| `crates/minicronet/` | Rust 安全层、流和生命周期 |
| `bindings/python/` | Python 3.7–3.13 绑定与共享 facade |
| `bindings/python36/` | Python 3.6 独立 abi3 绑定（共用同一 facade） |
| `bindings/python/tests/` | `test_stability.py` 生命周期与并发；`test_compat.py` requests/curl-cffi 兼容面 |
| `docs/` | 构建、平台、ABI、兼容性和审计说明 |

回归测试需要已审计的 Core：

```bash
LD_LIBRARY_PATH=core/binaries/linux-x86_64 PYTHONPATH=bindings/python \
  python -m unittest discover -s bindings/python/tests -p "test_*.py"
```
