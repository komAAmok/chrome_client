# chrome_client

基于 Chromium 网络栈的 HTTP/WebSocket 客户端。Core 负责 TLS、HTTP、HTTP/2、
HTTP/3/QUIC、代理和 WebSocket/WSS；Python/Rust 绑定只负责参数、类型、错误和
生命周期转换，不另实现一套网络协议。兼容requests和curl-cffi。

[English README](https://github.com/komAAmok/chrome_client/blob/main/README.en.md) · [构建说明](https://github.com/komAAmok/chrome_client/blob/main/docs/BUILD.md) · [兼容边界](https://github.com/komAAmok/chrome_client/blob/main/docs/COMPATIBILITY_BOUNDARY.md)

## 支持范围

### 操作系统、架构与绑定

| 操作系统 | Core 目标 | Python 3.7–3.13 | Python 3.6 | Rust | 备注 |
| --- | --- | --- | --- | --- | --- |
| Linux | x86 (`i686`)、x86_64、ARM64 | ✓ | ✓（独立 abi3 扩展） | ✓ | manylinux/glibc 运行时依赖见 manifest |
| Windows | x86、x86_64、ARM64 | ✓ | x86/x86_64 ✓ | ✓ | DLL 随 wheel，包含 `icudtl.dat` |
| macOS | x86_64、ARM64 | ✓ | ✓（独立 abi3 扩展） | ✓ | dylib 按架构匹配 |


每个 Core 产物位于 `core/binaries/<target>/`，带 ABI 版本、Chromium revision、
SHA-256 和依赖清单。ABI 当前为 v8。Go 和 Node.js 目录目前是绑定设计说明，
不是已发布的可安装包。

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
`chrome<major>` 别名）。当前支持 Chrome 99–151，共 53 个 profile；没有列出的
版本会被拒绝，不会静默降级到当前版本。

| Chrome 主版本 | 可用 profile |
| --- | --- |
| 99–105 | `chrome_99` … `chrome_105` |
| 106–112 | `chrome_106` … `chrome_112` |
| 113–119 | `chrome_113` … `chrome_119` |
| 120–126 | `chrome_120` … `chrome_126` |
| 127–133 | `chrome_127` … `chrome_133` |
| 134–140 | `chrome_134` … `chrome_140` |
| 141–147 | `chrome_141` … `chrome_147` |
| 148–151 | `chrome_148`、`chrome_149`、`chrome_150`、`chrome_151` |

Profile 影响 TLS ClientHello、ALPN、HTTP/2 设置、QUIC/H3 和相关 Chromium 网络
参数；它不是完整 Chrome 浏览器，也不包含 Blink、扩展、Service Worker 或持久化
浏览器 Profile。

### 功能矩阵

| 功能 | Python | Rust/Core | 说明 |
| --- | --- | --- | --- |
| HTTP/1.1、HTTP/2、HTTP/3/QUIC | ✓ | ✓ | 默认由 Chromium 协商；Rust 可强制 H1/H2/H3 |
| HTTPS/TLS、证书校验 | ✓ | ✓ | `verify=False` 仅在明确需要时使用 |
| HTTP/HTTPS/SOCKS 代理 | ✓ | ✓ | Python 支持 `proxy` 和 Requests 风格 `proxies` |
| 同步请求 | ✓ | ✓ | 同步等待释放 GIL |
| asyncio 请求 | ✓ | — | Core 回调唤醒事件循环，不创建请求线程池 |
| 流式响应 | `iter_content` / `aiter_bytes` | `ResponseStream` | 每请求 body 队列上限 1MiB |
| 取消、超时、大小限制 | ✓ | ✓ | `Timeout`、`ResponseTooLarge`、`RequestException` |
| WebSocket / WSS | ✓ | ✓ | 同步 `recv` 与异步 `recv` |
| Cookie、内存缓存、重定向、上传 | ✓ | ✓ | Cookie/cache 随 Engine 生命周期存在 |


## Python 使用示例

### 同步 GET、参数、请求头和 JSON

```python
import chrome_client

response = chrome_client.get(
    "https://example.com/api",
    params={"page": 1},
    headers={"Accept": "application/json"},
    impersonate="chrome_151",
    timeout=15,
)
response.raise_for_status()
print(response.status_code, response.json())
```

其他 Requests 风格方法使用同一组参数：

```python
with chrome_client.Client() as client:
    client.head("https://example.com/resource")
    client.options("https://example.com/resource")
    client.post("https://example.com/items", json={"name": "demo"})
    client.put("https://example.com/items/1", data=b"raw body")
    client.patch("https://example.com/items/1", json={"name": "new"})
    client.delete("https://example.com/items/1")
    client.get("https://example.com/redirect", allow_redirects=False)
```

### Client/Session、Cookie、代理和证书校验

```python
from chrome_client import Client

with Client(cookies={"session": "abc"}, timeout=10) as client:
    response = client.get(
        "https://example.com/private",
        proxies={"https": "http://127.0.0.1:8080"},
        verify=True,
    )
    print(response.text)
```

显式 `proxy="http://..."` 优先于 `proxies`；`proxies` 按 `http`、`https`、`all`
键选择。

`client.cookies` 是 `CookieJar`（`dict` 子类），支持 Requests 风格的
`get_dict()`：

```python
with Client(cookies={"session": "abc"}) as client:
    client.cookies["extra"] = "1"
    print(client.cookies.get_dict())   # {'session': 'abc', 'extra': '1'}
```

它只包含调用方为出站请求配置的 cookie。响应返回的 cookie 由 Core 内的 Chromium
CookieStore 拥有，ABI v8 不导出该存储，因此不会出现在这个 jar 里，也不需要在
Python 侧重复附加。jar 没有 domain/path 元数据，`get_dict(domain=...)` 或
`get_dict(path=...)` 会抛 `ValueError` 而不是返回未过滤的数据。

### asyncio 并发

```python
import asyncio
from chrome_client import AsyncClient

async def main():
    async with AsyncClient(impersonate="chrome_151") as client:
        responses = await asyncio.gather(*[
            client.get("https://example.com") for _ in range(32)
        ])
        print([r.status_code for r in responses])

asyncio.run(main())
```

### 流式读取、大小限制和取消

```python
from chrome_client import Client, ResponseTooLarge

with Client() as client:
    response = client.get("https://example.com/large", stream=True,
                          max_response_bytes=16 * 1024 * 1024)
    try:
        total = 0
        for chunk in response.iter_content(64 * 1024):
            total += len(chunk)
        print("bytes:", total)
    finally:
        response.close()       # 未读完时取消 native 请求
```

异步流使用 `async for chunk in response.aiter_bytes()`，使用完毕调用
`await response.aclose()`。超过 `max_response_bytes` 会取消请求并抛出
`ResponseTooLarge`。

### WebSocket / WSS

```python
from chrome_client import WebSocket

with WebSocket("wss://echo.example.com", impersonate="chrome_151") as socket:
    socket.send("ping")
    print(socket.recv())
```

```python
import asyncio
from chrome_client import AsyncClient

async def main():
    client = AsyncClient(impersonate="chrome_151")
    socket = await client.websocket("wss://echo.example.com")
    try:
        await socket.send("ping")
        print(await socket.recv())
    finally:
        await socket.close()
        await client.aclose()

asyncio.run(main())
```

### 超时与错误处理

```python
import chrome_client

try:
    chrome_client.get("https://example.com", timeout=0.5)
except chrome_client.Timeout:
    print("request timed out")
except chrome_client.RequestException as error:
    print("request failed:", error)
```

## Requests 与 curl-cffi 语法兼容

常用调用参数与 Requests/curl-cffi 对齐，可直接迁移大多数简单 GET/POST 代码：

| 语法/参数 | 支持情况 |
| --- | --- |
| `get/post/put/delete`、`Client`、`Session` | ✓；`Session` 等价于 `Client` |
| `params`、`headers`、`cookies`、`data`、`json` | ✓ |
| `timeout`、`verify`、`proxies`、`proxy` | ✓ |
| `impersonate="chrome_151"` | ✓，使用本项目 Chromium profile |
| `stream=True`、`iter_content` | ✓；异步使用 `aiter_bytes` |
| `session.cookies.get_dict()` | ✓；只返回已配置的出站 cookie，不含响应 cookie |
| `curl_options`、`ja3`、`akamai`、libcurl 句柄 | ✗ |
| Requests 的全部插件/适配器/持久化 Cookie 功能 | ✗ |

这不是 `curl_cffi.requests` 的替代导入：请将 `from curl_cffi import requests`
改为 `from chrome_client import requests`，并确认响应流、异常类型和 WebSocket
接口按本 README 使用。两者都可接受常见的 `impersonate`、代理和超时参数，但
profile 覆盖范围、TLS 行为和底层连接池由各自实现决定。

## 目录与验证

| 路径 | 内容 |
| --- | --- |
| `core/abi/` | 稳定 C ABI v8 |
| `core/binaries/` | 8 个已审计平台 Core |
| `crates/minicronet/` | Rust 安全层、流和生命周期 |
| `bindings/python/` | Python 3.7–3.13 绑定与 facade |
| `bindings/python36/` | Python 3.6 独立 abi3 绑定 |
| `docs/` | 构建、平台、ABI、兼容性和审计说明 |
