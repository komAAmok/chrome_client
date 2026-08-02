# chrome_client

[![PyPI version](https://img.shields.io/pypi/v/chrome-client)](https://pypi.org/project/chrome-client)
[![Python](https://img.shields.io/pypi/pyversions/chrome-client)](https://pypi.org/project/chrome-client)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

`chrome_client` 是一个基于 Chromium Cronet 的 Python HTTP 客户端，提供参数提示、同步、异步、流式请求、Cookie、代理、 WebSocket 、与chrome完全一致的http(s) 请求指纹/行为和TLS 指纹/行为，兼容requests、curl_cffi的API。

## 目录

- [主要能力](#主要能力)
- [安装与平台](#安装与平台)
- [接口层级与迁移](#接口层级与迁移)
- [快速开始](#快速开始)
- [Client 和 Session](#client-和-session)
- [请求接口](#请求接口)
- [Response](#response)
- [异步接口](#异步接口)
- [流式响应](#流式响应)
- [Cookie](#cookie)
- [PreparedRequest](#preparedrequest)
- [上传与下载](#上传与下载)
- [WebSocket](#websocket)
- [代理、超时与证书](#代理超时与证书)
- [TLS Profile 与 impersonate](#tls-profile-与-impersonate)
- [异常处理](#异常处理)
- [兼容性边界](#兼容性边界)
- [原生库排查](#原生库排查)

## 主要能力

- Requests 风格模块 API：`get()`、`post()`、`request()` 等。
- Requests 风格命名空间：`from chrome_client import requests`。
- 同步 `Client` / `Session` 和异步 `AsyncClient` / `AsyncSession`。
- HTTP、HTTPS、SOCKS5、SOCKS5H 代理。
- 自动重定向、跨域认证保护和 `response.history`。
- 同步与异步流式响应。
- RFC 6265 风格 Cookie Domain、Path、Expires、Max-Age 和 Secure 处理。
- 可配置 Chrome TLS Profile，公开参数名为 `impersonate`。
- 回调式 WebSocket。
- Rust 原生扩展，使用 PyO3 `abi3-py36`。

## 安装与平台

安装：

```bash
pip install chrome_client
```

升级：

```bash
pip install --upgrade chrome_client
```

要求 Python `>= 3.6`。

| 平台 | 架构 | 状态 |
|---|---|---|
| Windows | x86_64 | 支持 |
| Windows | x86 / 32 位 | 支持 |
| Linux | x86_64，glibc >= 2.18（包含 2.23） | 支持 |
| macOS | Apple Silicon / arm64 | 支持 |

当前不提供 Linux ARM64、macOS Intel 和 Alpine Linux / musl Wheel。

## 接口层级与迁移

### 推荐导入方式

```python
import chrome_client

# Requests 风格命名空间，仅提供同步模块 API
from chrome_client import requests

# 可复用会话
from chrome_client import Session, AsyncSession

# 底层客户端
from chrome_client import Client, AsyncClient
```

### 类关系

- `Client`：底层同步客户端，持有一个原生 Cronet Session。
- `AsyncClient`：底层异步客户端。
- `Session`：继承 `Client`，作为 Requests 风格同步入口。
- `AsyncSession`：继承 `AsyncClient`，作为异步入口。

`Client` 与 `AsyncClient` 具有相同的公共方法集合；区别是网络方法是否需要 `await`。

### 从 requests 迁移

原代码：

```python
import requests

with requests.Session() as session:
    response = session.get("https://example.com/api", params={"page": 1})
```

迁移后：

```python
from chrome_client import requests

with requests.Session(impersonate="chrome_150") as session:
    response = session.get("https://example.com/api", params={"page": 1})
```

也可以直接替换为：

```python
from chrome_client import Session

with Session() as session:
    response = session.get("https://example.com/api", params={"page": 1})
```

TLS Profile 参数统一使用 `impersonate`。

## 快速开始

### 单次同步请求

```python
import chrome_client

response = chrome_client.get(
    "https://example.com/api",
    params={"page": 1},
    headers={"accept": "application/json"},
)
response.raise_for_status()

print(response.status_code)
print(response.headers)
print(response.json())
```

### Requests 命名空间

```python
from chrome_client import requests

response = requests.post(
    "https://example.com/api",
    json={"name": "chrome_client"},
    impersonate="chrome_150",
)

print(response.status_code)
print(response.json())
```

### 可复用 Session

```python
from chrome_client import Session

with Session(
    base_url="https://example.com/api/",
    headers={"accept": "application/json"},
    params={"language": "zh-CN"},
    auth=("username", "password"),
    timeout=30,
    impersonate="chrome_150",
) as session:
    response = session.get("users", params={"page": 1})
    response.raise_for_status()
    print(response.json())
```

### POST JSON、表单与原始内容

```python
import chrome_client

# JSON 支持字典、列表、字符串、数字、布尔值和 None
json_response = chrome_client.post(
    "https://example.com/json",
    json=[{"id": 1}, {"id": 2}],
)

# data 字典编码为 application/x-www-form-urlencoded
form_response = chrome_client.post(
    "https://example.com/form",
    data={"username": "alice", "enabled": "1"},
)

# content 用于发送原始请求体，不能与 data/json 同时使用
raw_response = chrome_client.post(
    "https://example.com/raw",
    content=b"raw body",
    headers={"content-type": "application/octet-stream"},
)
```

## Client 和 Session

同步和异步客户端支持相同的构造参数：

```python
client = chrome_client.Client(
    verify=True,
    proxies=None,
    timeout=30,
    impersonate="chrome_150",
    headers=None,
    cookies=None,
    auth=None,
    proxy=None,
    base_url=None,
    params=None,
    allow_redirects=True,
    max_redirects=30,
    default_headers=True,
    timeout_ms=None,
    default_domain=None,
)
```

| 参数 | 含义 |
|---|---|
| `verify` | 是否验证 TLS 证书，仅支持布尔值。 |
| `proxies` | 代理字符串或 Requests 风格代理字典。 |
| `proxy` | `curl_cffi` 风格单代理参数，不能和 `proxies` 同时使用。 |
| `timeout` | 超时秒数。 |
| `timeout_ms` | Cronet 毫秒超时，不能和非默认 `timeout` 同时使用。 |
| `impersonate` | TLS Profile 名称；传 `None` 不加载自定义 Profile。 |
| `headers` | Session 默认请求头。 |
| `cookies` | Cookie 映射或 `CookieJar`。 |
| `auth` | `(username, password)` HTTP Basic Auth。 |
| `base_url` | 相对 URL 的基础地址。 |
| `params` | 每次请求自动合并的查询参数。 |
| `allow_redirects` | 默认是否跟随重定向。 |
| `max_redirects` | 最大重定向次数，默认 `30`。 |
| `default_headers` | 为 `False` 时忽略构造时传入的默认 `headers`。 |
| `default_domain` | 手动写入 Cookie 时使用的默认域名。 |

建议总是使用上下文管理器，确保原生 Session 被释放：

```python
with chrome_client.Client() as client:
    response = client.get("https://example.com")
```

不使用上下文管理器时必须手动关闭：

```python
client = chrome_client.Client()
try:
    response = client.get("https://example.com")
finally:
    client.close()
```

## 请求接口

模块和客户端均支持：

- `request(method, url, **kwargs)`
- `get(url, params=None, **kwargs)`
- `options(url, **kwargs)`
- `head(url, **kwargs)`
- `post(url, data=None, json=None, **kwargs)`
- `put(url, data=None, **kwargs)`
- `patch(url, data=None, **kwargs)`
- `delete(url, **kwargs)`
- `trace(url, **kwargs)`
- `query(url, **kwargs)`

常用请求参数：

| 参数 | 含义 |
|---|---|
| `params` | 字典或键值序列，使用 `doseq=True` 编码。 |
| `headers` | 字典或有序 `(name, value)` 序列；值为 `None` 可删除同名默认头。 |
| `cookies` | 仅本次请求使用的 Cookie 映射或 `CookieJar`。 |
| `data` | 表单、字符串或字节请求体。 |
| `content` | 原始请求体，不能和 `data/json` 同时使用。 |
| `json` | 任意可被 `json.dumps()` 序列化的值。 |
| `auth` | 本次请求的 Basic Auth。 |
| `timeout` | 本次请求超时秒数。 |
| `verify` | 本次请求证书验证设置。 |
| `allow_redirects` | 是否自动跟随重定向。 |
| `max_redirects` | 本次请求的重定向上限。 |
| `proxies` / `proxy` | 本次请求代理。 |
| `impersonate` | 本次请求 TLS Profile。 |
| `hooks` | Requests 风格 `{"response": callback}` 响应钩子。 |
| `stream` | 返回 `StreamResponse`。 |

当请求级 `timeout`、`verify`、代理或 `impersonate` 与当前 Client 不同时，`Client` 会创建兼容的临时原生 Session，并在响应关闭后释放。

### 有序请求头

```python
headers = [
    ("user-agent", "Mozilla/5.0"),
    ("accept", "text/html,application/xhtml+xml"),
    ("accept-language", "zh-CN,zh;q=0.9"),
]

response = chrome_client.get(
    "https://example.com",
    headers=headers,
)
```

Header 名称和值必须是字符串。包含 CR、LF、NUL 或非法名称字符时会在进入原生层前抛出异常。

### 重定向

```python
response = chrome_client.get(
    "https://example.com/redirect",
    allow_redirects=True,
    max_redirects=10,
)

for previous in response.history:
    print(previous.status_code, previous.url)
```

- 跨域重定向不会携带 `Authorization`。
- 301、302、303 会按浏览器语义切换请求方法。
- 307、308 保留原方法和请求体。
- Session 默认查询参数不会在每次重定向中重复追加。

## Response

普通请求返回 `Response`，`stream=True` 返回 `StreamResponse`。

常用属性：

```python
response.status_code
response.headers       # 大小写不敏感
response.cookies       # CookieJar
response.content       # bytes
response.text          # str
response.url
response.encoding
response.ok
response.is_redirect
response.history
response.request       # PreparedRequest
response.raw
```

常用方法：

```python
response.json()
response.raise_for_status()
response.iter_content(chunk_size=8192)
response.iter_lines(chunk_size=512, decode_unicode=False)
response.close()
```

`Response.iter_content()`、`Response.iter_lines()` 和 `StreamResponse.iter_lines()` 默认产生字节；后两类行迭代以及非流式 `iter_content()` 可通过 `decode_unicode=True` 产生字符串。流式 `StreamResponse.iter_content()` 始终产生字节。

## 异步接口

### AsyncClient

以下写法兼容 Python 3.6：

```python
import asyncio
import chrome_client


async def main():
    async with chrome_client.AsyncClient(
        impersonate="chrome_150",
        timeout=30,
    ) as client:
        responses = await asyncio.gather(
            client.get("https://example.com/1"),
            client.get("https://example.com/2"),
        )
        for response in responses:
            print(response.status_code)


loop = asyncio.get_event_loop()
loop.run_until_complete(main())
```

Python 3.7 及以上可使用：

```python
asyncio.run(main())
```

### 模块级异步函数

```python
async def module_api_example():
    response = await chrome_client.async_get("https://example.com")

    response = await chrome_client.async_post(
        "https://example.com/api",
        json={"name": "chrome_client"},
    )
```

可用函数：

- `async_request()`
- `async_get()`
- `async_options()`
- `async_head()`
- `async_post()`
- `async_put()`
- `async_patch()`
- `async_delete()`
- `async_upload_file()`
- `async_download_file()`

## 流式响应

### 同步流

```python
import chrome_client

response = chrome_client.get("https://example.com/file", stream=True)
try:
    with open("download.bin", "wb") as output:
        for chunk in response.iter_content(64 * 1024):
            output.write(chunk)
finally:
    response.close()
```

也可以让响应上下文自动关闭：

```python
with chrome_client.get("https://example.com/file", stream=True) as response:
    for chunk in response.iter_content(8192):
        process(chunk)
```

### 异步流

```python
import asyncio
import chrome_client


async def download():
    async with chrome_client.AsyncClient() as client:
        response = await client.get("https://example.com/file", stream=True)
        try:
            with open("download.bin", "wb") as output:
                async for chunk in response.aiter_content(64 * 1024):
                    output.write(chunk)
        finally:
            await response.aclose()


loop = asyncio.get_event_loop()
loop.run_until_complete(download())
```

异步流还支持：

```python
async def read_stream(response):
    body = await response.acontent()
    text = await response.atext()

    async for line in response.aiter_lines(chunk_size=512):
        print(line)
```

## Cookie

Session 会自动解析响应中的 `Set-Cookie`，并在后续请求中发送匹配的 Cookie。

### 自动行为

- 按 `(domain, path, name)` 保存，同名不同 Path 可以共存。
- Host-only Cookie 只发送到原主机。
- 带 `Domain` 的 Cookie 可发送到匹配子域。
- 请求路径必须符合 RFC 6265 Path 匹配规则。
- 同名 Cookie 发送时，较长 Path 排在前面。
- 未提供 `Path` 时，根据设置 Cookie 的请求 URL 计算默认 Path。
- 支持 `Expires` 与 `Max-Age`，`Max-Age` 优先。
- `Max-Age=0` 或已过期的 `Expires` 会删除对应 `(domain, path, name)` Cookie。
- 过期 Cookie 会在查询和发送前自动清理。
- `Secure` Cookie 只通过 HTTPS/WSS 发送。
- 支持 Host-only IPv4、IPv6 和普通域名。

### Session Cookie

```python
from chrome_client import Session

with Session(default_domain="example.com") as session:
    session.cookies.set("language", "zh-CN")
    session.cookies["theme"] = "dark"

    response = session.get("https://example.com/account")

    print(session.cookies["language"])
    print(session.cookies.get_dict(domain="example.com", path="/account"))
```

### Path 和持久时间

```python
session.cookies.set(
    "token",
    "value",
    domain="example.com",
    path="/account",
    max_age=3600,
    secure=True,
)

# expires 使用 Unix 时间戳
session.cookies.set(
    "persistent",
    "value",
    domain="example.com",
    path="/",
    expires=4102444800,
)
```

### 查询和删除

```python
value = session.cookies.get(
    "token",
    domain="example.com",
    path="/account/settings",
)

cookies = session.cookies.cookies_for_request(
    "https://example.com/account/settings"
)

session.cookies.delete(
    name="token",
    domain="example.com",
    path="/account",
)

session.cookies.clear_expired_cookies()
session.cookies.clear_session_cookies()
session.cookies.clear(domain="example.com", path="/account")
```

### 单次请求 Cookie

单次请求的 Cookie 不会写回 Session，并会覆盖同名 Session Cookie：

```python
response = session.get(
    "https://example.com/account",
    cookies={"token": "request-only"},
)
```

## PreparedRequest

同步和异步客户端均支持 `Request`、`prepare_request()` 和 `send()`。

### 同步

```python
from chrome_client import Request, Session

with Session(base_url="https://example.com/") as session:
    request = Request(
        method="POST",
        url="api/items",
        params={"page": 1},
        headers={"accept": "application/json"},
        json={"name": "item"},
    )
    prepared = session.prepare_request(request)
    response = session.send(prepared)
```

### 异步

```python
from chrome_client import AsyncSession, Request


async def send_prepared():
    async with AsyncSession(base_url="https://example.com/") as session:
        prepared = session.prepare_request(Request(
            "POST",
            "api/items",
            json=[1, 2, 3],
        ))
        response = await session.send(prepared)
```

## 上传与下载

### 上传文件

```python
result = chrome_client.upload_file(
    "https://example.com/upload",
    "./image.png",
    field_name="file",
    additional_fields={"category": "avatar"},
)
```

客户端方法：

```python
with chrome_client.Client() as client:
    response = client.upload_file(
        "https://example.com/upload",
        "./image.png",
        field_name="file",
        additional_fields={"category": "avatar"},
    )
```

### 下载文件

```python
result = chrome_client.download_file(
    "https://example.com/file",
    "./download.bin",
    chunk_size=64 * 1024,
)

print(result["file_path"])
print(result["size"])
print(result["status_code"])
print(result["headers"])
```

异步版本：

```python
async def async_download():
    result = await chrome_client.async_download_file(
        "https://example.com/file",
        "./download.bin",
        chunk_size=64 * 1024,
    )
```

`files=` 当前不作为通用请求参数实现；请使用 `upload_file()`。

## WebSocket

```python
from chrome_client import Client


def on_open(ws):
    print("connected")
    ws.send("hello")


def on_message(ws, message):
    print("message:", message)


def on_close(ws, code, reason):
    print("closed:", code, reason)


def on_error(ws, error):
    print("error:", error)


with Client() as client:
    ws = client.websocket(
        "wss://example.com/ws",
        on_open=on_open,
        on_message=on_message,
        on_close=on_close,
        on_error=on_error,
        sub_protocols=["chat", "json"],
        origin="https://example.com",
        headers={"X-Client": "chrome_client"},
    )
    ws.run_forever()
```

后台线程：

```python
import threading


opened = threading.Event()

with Client() as client:
    ws = client.websocket(
        "wss://example.com/ws",
        on_open=lambda ws: opened.set(),
    )
    thread = ws.run_in_background()

    try:
        if not opened.wait(10):
            raise TimeoutError("WebSocket connection timed out")
        ws.send("text")
        ws.send_bytes(b"binary")
    finally:
        ws.close(code=1000, reason="done")
        thread.join()
```

`AsyncClient.websocket()` 返回同一个回调式 `WebSocketApp`；事件循环运行在其独立线程中，不需要 `await ws.run_forever()`。

Windows Cronet 原生 ABI 支持 `origin` 和 `sub_protocols`，但当前不支持任意额外 WebSocket Header；Linux/macOS 支持 `headers`。

## 代理、超时与证书

### 代理

```python
client = chrome_client.Client(
    proxies="http://127.0.0.1:8080"
)

client = chrome_client.Client(
    proxies={
        "http": "http://127.0.0.1:8080",
        "https": "http://user:password@127.0.0.1:8080",
    }
)

client = chrome_client.Client(
    proxy="socks5h://user:password@127.0.0.1:1080"
)
```

支持协议：

- `http://`
- `https://`
- `socks5://`
- `socks5h://`

Cronet Session 最终使用一个代理地址；代理字典按 `https`、`http`、`all`、`all://` 顺序选择。

### 超时

秒：

```python
client = chrome_client.Client(timeout=15)
response = chrome_client.get("https://example.com", timeout=15)
```

毫秒：

```python
client = chrome_client.Client(timeout_ms=15000)
```

原生 Cronet Session 当前不能表达无限超时，因此 `timeout=None` 使用安全默认值 30 秒。

### 证书验证

```python
client = chrome_client.Client(verify=True)
```

测试环境或自签名服务：

```python
client = chrome_client.Client(verify=False)
```

当前 `verify` 仅支持布尔值，不支持 CA Bundle 路径；`cert=` 客户端证书尚未实现。

## TLS Profile 与 impersonate

默认 Profile 为 `chrome_150`：

```python
with chrome_client.Client(impersonate="chrome_150") as client:
    response = client.get("https://example.com")
```

当前支持 Chrome 99 到 Chrome 151，`impersonate` 可选值如下：

```text
chrome_99   chrome_100  chrome_101  chrome_102  chrome_103
chrome_104  chrome_105  chrome_106  chrome_107  chrome_108
chrome_109  chrome_110  chrome_111  chrome_112  chrome_113
chrome_114  chrome_115  chrome_116  chrome_117  chrome_118
chrome_119  chrome_120  chrome_121  chrome_122  chrome_123
chrome_124  chrome_125  chrome_126  chrome_127  chrome_128
chrome_129  chrome_130  chrome_131  chrome_132  chrome_133
chrome_134  chrome_135  chrome_136  chrome_137  chrome_138
chrome_139  chrome_140  chrome_141  chrome_142  chrome_143
chrome_144  chrome_145  chrome_146  chrome_147  chrome_148
chrome_149  chrome_150  chrome_151
```

IDE 会像 `curl_cffi` 一样为 `impersonate` 显示上述可选值，并为
`chrome_client.requests.get/post/request/...` 显示可用请求参数。

不加载 Profile：

```python
client = chrome_client.Client(impersonate=None)
```

查看可用 Profile：

```python
profiles = chrome_client.get_tls_profiles()
print(sorted(profiles))
```

新增或修改 Profile：

```python
profile = chrome_client.get_tls_profiles()["chrome_150"].copy()
profile["tls_curves"] = [
    "X25519MLKEM768",
    "X25519",
    "P-256",
    "P-384",
]

chrome_client.add_tls_profile("chrome_custom", profile)

with chrome_client.Client(impersonate="chrome_custom") as client:
    response = client.get("https://example.com")
```

替换全部 Profile：

```python
chrome_client.set_tls_profiles({
    "chrome_custom": {
        "cipher_suites": [],
        "tls_curves": ["X25519", "P-256"],
        "tls_extensions": [],
        "signature_algorithms": [],
    }
})
```

相关接口：

- `get_tls_profiles()`
- `add_tls_profile(name, profile)`
- `set_tls_profiles(profiles)`
- `clear_tls_profiles_cache()`

这些接口修改当前 Python 进程中的配置。需要持久化时，请修改 `python/chrome_client/tls_profiles.json` 后重新构建 Wheel。

## 异常处理

```python
from chrome_client import (
    ConnectionError,
    HTTPStatusError,
    ProxyError,
    RequestError,
    SSLError,
    Timeout,
)

try:
    response = chrome_client.get("https://example.com", timeout=10)
    response.raise_for_status()
except Timeout as error:
    print("timeout:", error)
except HTTPStatusError as error:
    print("http status:", error.response.status_code)
except ProxyError as error:
    print("proxy:", error)
except SSLError as error:
    print("tls:", error)
except ConnectionError as error:
    print("connection:", error)
except RequestError as error:
    print("request:", error)
```

Requests 别名：

- `RequestException = RequestError`
- `HTTPError = HTTPStatusError`

## 兼容性边界

`chrome_client` 优先兼容 Requests 的高频请求接口，但并不是 Requests 所有内部模块的逐项复制。

当前明确限制：

- `files=`：请改用 `upload_file()`。
- `cert=`：客户端证书尚未实现。
- `verify="/path/to/ca.pem"`：当前只支持布尔值。
- `timeout=None`：原生层使用 30 秒安全默认值，不表示无限等待。
- Requests 的 Adapter、Transport Adapter、AuthBase、完整 CookiePolicy 等内部扩展点未实现。
- Windows WebSocket 不支持任意额外 Header。

迁移时建议优先使用：

- 模块级 `request/get/post/...`
- `Session`、`Request`、`PreparedRequest`、`Response`
- `params/headers/cookies/data/json/auth/proxies/timeout/verify/allow_redirects/stream`

## 原生库排查

官方 Wheel 会携带对应平台的 Cronet 动态库。

### Windows

若出现：

```text
ImportError: DLL load failed: 找不到指定的程序。
```

请确认：

1. Python 架构与 Wheel 一致，例如 x64 Python 使用 x64 Wheel。
2. `chrome_client` 包目录中存在 `cronet.<version>.dll`。
3. Wheel 中的 `cronet_cloak.pyd` 和 Cronet DLL 来自同一次构建。
4. 未被其他目录中的旧版 Cronet DLL 抢先加载。

当前项目附带的 Windows x86/x64 Cronet 库均导出：

- `Cronet_WebSocket_Create`
- `Cronet_WebSocket_Connect`
- `Cronet_WebSocket_Send`
- `Cronet_WebSocket_Close`
- `Cronet_WebSocket_Destroy`

### Linux

源码安装后如果出现：

```text
libcronet.144.0.7506.0.so: cannot open shared object file
```

可临时添加包目录：

```bash
export LD_LIBRARY_PATH="$(python -c 'import os, chrome_client; print(os.path.dirname(chrome_client.__file__))'):$LD_LIBRARY_PATH"
```

当前 Wheel 不是 musllinux Wheel，Alpine Linux 请改用 glibc 系发行版。

## API 参考

- [`chrome_client` 类型声明](python/chrome_client/__init__.pyi)

## 致谢

感谢 [`2833844911/cyCronet`](https://github.com/2833844911/cyCronet) 项目及其作者提供跨平台 Cronet 基座。本项目在其基础上进行 Python API、类型声明、Cookie、流式请求、WebSocket 和 Wheel 打包方面的二次开发。

## License

以下版权声明和许可条款必须完整保留：

```text
MIT License

Copyright (c) 2026 Cronet-Cloak

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

完整许可内容见 [`LICENSE`](LICENSE)。
