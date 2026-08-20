# chrome_client

[![PyPI version](https://img.shields.io/pypi/v/chrome-client)](https://pypi.org/project/chrome-client)
[![Python](https://img.shields.io/pypi/pyversions/chrome-client)](https://pypi.org/project/chrome-client)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

English | [简体中文](README.md)

`chrome_client` is a Chromium Cronet based Python HTTP client with typed parameters, synchronous and asynchronous APIs, streaming requests, cookies, proxies, WebSocket support, Chrome-compatible HTTP(S) and TLS fingerprints/behavior, and APIs compatible with  [`requests`](https://github.com/psf/requests) and [`curl_cffi`](https://github.com/lexiforest/curl_cffi).   

## Table of contents

- [Features](#features)
- [Installation and platforms](#installation-and-platforms)
- [API layers and migration](#api-layers-and-migration)
- [Quick start](#quick-start)
- [Client and Session](#client-and-session)
- [Request API](#request-api)
- [Response](#response)
- [Async API](#async-api)
- [Streaming responses](#streaming-responses)
- [Cookies](#cookies)
- [PreparedRequest](#preparedrequest)
- [Upload and download](#upload-and-download)
- [WebSocket](#websocket)
- [Proxies, timeouts, and certificates](#proxies-timeouts-and-certificates)
- [TLS profiles and impersonate](#tls-profiles-and-impersonate)
- [Exceptions](#exceptions)
- [Compatibility boundaries](#compatibility-boundaries)
- [Native library troubleshooting](#native-library-troubleshooting)

## Features

- Requests-style module API: `get()`, `post()`, `request()`, and more.
- Requests-style namespace: `from chrome_client import requests`.
- Synchronous `Client` / `Session` and asynchronous `AsyncClient` / `AsyncSession`.
- HTTP, HTTPS, SOCKS5, and SOCKS5H proxies.
- Automatic redirects, cross-origin authentication protection, and `response.history`.
- Synchronous and asynchronous streaming responses.
- RFC 6265-style Cookie Domain, Path, Expires, Max-Age, and Secure handling.
- Configurable Chrome TLS profiles through the public `impersonate` parameter.
- Callback-based WebSocket API.
- Rust native extension built with PyO3 `abi3-py36`.

## Installation and platforms

Install:

```bash
pip install chrome_client
```

Upgrade:

```bash
pip install --upgrade chrome_client
```

Python `>= 3.6` is required.

Published wheels use the `py3-none` Python/ABI tag (for example,
`chrome_client-0.1.7-py3-none-win_amd64.whl`). The platform tag remains
platform-specific, while `Requires-Python >=3.6` keeps installation limited
to supported Python 3 versions.

| Platform | Architecture | Status |
|---|---|---|
| Windows | x86_64 | Supported |
| Windows | x86 / 32-bit | Supported |
| Linux | x86_64, glibc >= 2.18 (including 2.23) | Supported |
| macOS | Apple Silicon / arm64 | Supported |

Linux ARM64, macOS Intel, and Alpine Linux / musl wheels are not currently provided.

## API layers and migration

### Recommended imports

```python
import chrome_client

# Requests-style namespace; synchronous module API only
from chrome_client import requests

# Reusable sessions
from chrome_client import Session, AsyncSession

# Lower-level clients
from chrome_client import Client, AsyncClient
```

### Class relationships

- `Client`: lower-level synchronous client that owns one native Cronet Session.
- `AsyncClient`: lower-level asynchronous client.
- `Session`: extends `Client` and provides the Requests-style synchronous entry point.
- `AsyncSession`: extends `AsyncClient` and provides the asynchronous entry point.

`Client` and `AsyncClient` expose the same public method set. The difference is whether network methods must be awaited.

### Migrating from requests

Before:

```python
import requests

with requests.Session() as session:
    response = session.get("https://example.com/api", params={"page": 1})
```

After:

```python
from chrome_client import requests

with requests.Session(impersonate="chrome_150") as session:
    response = session.get("https://example.com/api", params={"page": 1})
```

Or use `Session` directly:

```python
from chrome_client import Session

with Session() as session:
    response = session.get("https://example.com/api", params={"page": 1})
```

Use `impersonate` consistently for TLS profile selection.

## Quick start

### One synchronous request

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

### Requests namespace

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

### Reusable Session

```python
from chrome_client import Session

with Session(
    base_url="https://example.com/api/",
    headers={"accept": "application/json"},
    params={"language": "en-US"},
    auth=("username", "password"),
    timeout=30,
    impersonate="chrome_150",
) as session:
    response = session.get("users", params={"page": 1})
    response.raise_for_status()
    print(response.json())
```

### JSON, forms, and raw POST content

```python
import chrome_client

# JSON accepts dictionaries, lists, strings, numbers, booleans, and None
json_response = chrome_client.post(
    "https://example.com/json",
    json=[{"id": 1}, {"id": 2}],
)

# A data dictionary is encoded as application/x-www-form-urlencoded
form_response = chrome_client.post(
    "https://example.com/form",
    data={"username": "alice", "enabled": "1"},
)

# content sends a raw body and cannot be combined with data or json
raw_response = chrome_client.post(
    "https://example.com/raw",
    content=b"raw body",
    headers={"content-type": "application/octet-stream"},
)
```

## Client and Session

Synchronous and asynchronous clients accept the same constructor arguments:

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

| Argument | Meaning |
|---|---|
| `verify` | Whether to verify TLS certificates. Boolean values only. |
| `proxies` | Proxy URL or Requests-style proxy dictionary. |
| `proxy` | `curl_cffi`-style single proxy; cannot be combined with `proxies`. |
| `timeout` | Timeout in seconds. |
| `timeout_ms` | Cronet timeout in milliseconds; cannot be combined with a non-default `timeout`. |
| `impersonate` | TLS profile name. Pass `None` to disable custom profiles. |
| `headers` | Default Session request headers. |
| `cookies` | Cookie mapping or `CookieJar`. |
| `auth` | `(username, password)` HTTP Basic authentication. |
| `base_url` | Base URL used for relative URLs. |
| `params` | Query parameters merged into every request. |
| `allow_redirects` | Whether redirects are followed by default. |
| `max_redirects` | Maximum redirects; defaults to `30`. |
| `default_headers` | When `False`, ignores constructor-level default `headers`. |
| `default_domain` | Default domain used when cookies are added manually. |

Use a context manager whenever possible so the native Session is released:

```python
with chrome_client.Client() as client:
    response = client.get("https://example.com")
```

Otherwise, close it explicitly:

```python
client = chrome_client.Client()
try:
    response = client.get("https://example.com")
finally:
    client.close()
```

## Request API

The module and clients provide:

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

Common request arguments:

| Argument | Meaning |
|---|---|
| `params` | Mapping or key-value sequence, encoded with `doseq=True`. |
| `headers` | Mapping or ordered `(name, value)` sequence. A `None` value removes a matching default header. |
| `cookies` | Request-only cookie mapping or `CookieJar`. |
| `data` | Form, string, or byte request body. |
| `content` | Raw request body; cannot be combined with `data/json`. |
| `json` | Any value serializable by `json.dumps()`. |
| `auth` | Request-level Basic authentication. |
| `timeout` | Request timeout in seconds. |
| `verify` | Request-level certificate verification setting. |
| `allow_redirects` | Whether to follow redirects automatically. |
| `max_redirects` | Request-level redirect limit. |
| `proxies` / `proxy` | Request-level proxy. |
| `impersonate` | Request-level TLS profile. |
| `hooks` | Requests-style `{"response": callback}` response hooks. |
| `stream` | Return a `StreamResponse`. |

If request-level `timeout`, `verify`, proxy, or `impersonate` settings differ from the current Client, a compatible temporary native Session is created and released after the response closes.

### Ordered headers

```python
headers = [
    ("user-agent", "Mozilla/5.0"),
    ("accept", "text/html,application/xhtml+xml"),
    ("accept-language", "en-US,en;q=0.9"),
]

response = chrome_client.get("https://example.com", headers=headers)
```

Header names and values must be strings. CR, LF, NUL, or invalid header-name characters raise an exception before entering the native layer.

### Redirects

```python
response = chrome_client.get(
    "https://example.com/redirect",
    allow_redirects=True,
    max_redirects=10,
)

for previous in response.history:
    print(previous.status_code, previous.url)
```

- Cross-origin redirects do not forward `Authorization`.
- 301, 302, and 303 change the method using browser semantics.
- 307 and 308 preserve the method and body.
- Session default query parameters are not appended repeatedly during redirects.

## Response

Normal requests return `Response`; `stream=True` returns `StreamResponse`.

Common attributes:

```python
response.status_code
response.headers       # case-insensitive
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

Common methods:

```python
response.json()
response.raise_for_status()
response.iter_content(chunk_size=8192)
response.iter_lines(chunk_size=512, decode_unicode=False)
response.close()
```

`Response.iter_content()`, `Response.iter_lines()`, and `StreamResponse.iter_lines()` yield bytes by default. Line iterators and non-streaming `iter_content()` can yield strings with `decode_unicode=True`. Streaming `StreamResponse.iter_content()` always yields bytes.

## Async API

### AsyncClient

Compatible with Python 3.6:

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

Python 3.7 and newer can use `asyncio.run(main())`.

### Module-level async functions

```python
async def module_api_example():
    response = await chrome_client.async_get("https://example.com")
    response = await chrome_client.async_post(
        "https://example.com/api",
        json={"name": "chrome_client"},
    )
```

Available functions:

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

## Streaming responses

### Synchronous streaming

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

The response can also close itself through a context manager:

```python
with chrome_client.get("https://example.com/file", stream=True) as response:
    for chunk in response.iter_content(8192):
        process(chunk)
```

### Asynchronous streaming

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

Async streams also support `acontent()`, `atext()`, and `aiter_lines()`.

## Cookies

Sessions parse `Set-Cookie` response headers automatically and send matching cookies with later requests.

### Automatic behavior

- Cookies are stored by `(domain, path, name)`; identical names with different paths can coexist.
- Host-only cookies are sent only to the original host.
- Cookies with `Domain` can be sent to matching subdomains.
- Request paths follow RFC 6265 path matching.
- For duplicate names, longer paths are sent first.
- If `Path` is omitted, the default is derived from the request URL that set the cookie.
- `Expires` and `Max-Age` are supported; `Max-Age` takes precedence.
- `Max-Age=0` or an expired `Expires` deletes the matching cookie.
- Expired cookies are removed before queries and requests.
- `Secure` cookies are sent only over HTTPS/WSS.
- Host-only IPv4, IPv6, and regular domain names are supported.

### Session cookies

```python
from chrome_client import Session

with Session(default_domain="example.com") as session:
    session.cookies.set("language", "en-US")
    session.cookies["theme"] = "dark"
    response = session.get("https://example.com/account")

    print(session.cookies["language"])
    print(session.cookies.get_dict(domain="example.com", path="/account"))
```

### Path and lifetime

```python
session.cookies.set(
    "token",
    "value",
    domain="example.com",
    path="/account",
    max_age=3600,
    secure=True,
)

# expires is a Unix timestamp
session.cookies.set(
    "persistent",
    "value",
    domain="example.com",
    path="/",
    expires=4102444800,
)
```

### Query and delete

```python
value = session.cookies.get(
    "token",
    domain="example.com",
    path="/account/settings",
)

cookies = session.cookies.cookies_for_request(
    "https://example.com/account/settings"
)

session.cookies.delete(name="token", domain="example.com", path="/account")
session.cookies.clear_expired_cookies()
session.cookies.clear_session_cookies()
session.cookies.clear(domain="example.com", path="/account")
```

Request-only cookies are not written back to the Session and override Session cookies with the same name:

```python
response = session.get(
    "https://example.com/account",
    cookies={"token": "request-only"},
)
```

## PreparedRequest

Synchronous and asynchronous clients support `Request`, `prepare_request()`, and `send()`.

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

Async example:

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

## Upload and download

### Upload a file

```python
result = chrome_client.upload_file(
    "https://example.com/upload",
    "./image.png",
    field_name="file",
    additional_fields={"category": "avatar"},
)
```

### Download a file

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

Use `async_upload_file()` and `async_download_file()` for asynchronous operations. General `files=` request handling is not implemented; use `upload_file()`.

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

`AsyncClient.websocket()` returns the same callback-based `WebSocketApp`. Its event loop runs in a separate thread, so `ws.run_forever()` is not awaited.

The Windows Cronet ABI supports `origin` and `sub_protocols`, but not arbitrary extra WebSocket headers. Linux and macOS support `headers`.

## Proxies, timeouts, and certificates

### Proxies

```python
client = chrome_client.Client(proxies="http://127.0.0.1:8080")

client = chrome_client.Client(proxies={
    "http": "http://127.0.0.1:8080",
    "https": "http://user:password@127.0.0.1:8080",
})

client = chrome_client.Client(
    proxy="socks5h://user:password@127.0.0.1:1080"
)
```

Supported schemes: `http://`, `https://`, `socks5://`, and `socks5h://`.

A Cronet Session ultimately uses one proxy URL. Proxy dictionaries are checked in this order: `https`, `http`, `all`, `all://`.

### Timeouts

```python
client = chrome_client.Client(timeout=15)
response = chrome_client.get("https://example.com", timeout=15)
client = chrome_client.Client(timeout_ms=15000)
```

The native Cronet Session cannot currently express an infinite timeout, so `timeout=None` uses a safe 30-second default.

### Certificate verification

```python
client = chrome_client.Client(verify=True)
client = chrome_client.Client(verify=False)  # test or self-signed services
```

`verify` currently accepts booleans only, not CA bundle paths. Client certificates through `cert=` are not implemented.

## TLS profiles and impersonate

The default profile is `chrome_150`:

```python
with chrome_client.Client(impersonate="chrome_150") as client:
    response = client.get("https://example.com")
```

Chrome 99 through Chrome 151 are supported. Valid `impersonate` values are:

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

IDEs display these options for `impersonate`, similar to `curl_cffi`, and show available request parameters for `chrome_client.requests.get/post/request/...`.

Disable profiles with `Client(impersonate=None)`.

Profile management APIs:

- `get_tls_profiles()`
- `add_tls_profile(name, profile)`
- `set_tls_profiles(profiles)`
- `clear_tls_profiles_cache()`

These APIs modify configuration in the current Python process. To persist changes, edit `python/chrome_client/tls_profiles.json` and rebuild the wheel.

## Exceptions

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

Requests aliases:

- `RequestException = RequestError`
- `HTTPError = HTTPStatusError`

## Compatibility boundaries

`chrome_client` targets frequently used Requests APIs; it is not a complete copy of every Requests internal module.

Known limitations:

- `files=`: use `upload_file()`.
- `cert=`: client certificates are not implemented.
- `verify="/path/to/ca.pem"`: only booleans are supported.
- `timeout=None`: the native layer uses a safe 30-second default, not an infinite wait.
- Requests Adapter, Transport Adapter, AuthBase, full CookiePolicy, and similar internal extension points are not implemented.
- Windows WebSocket does not support arbitrary extra headers.

For migration, prefer module-level `request/get/post/...`, `Session`, `Request`, `PreparedRequest`, `Response`, and the common `params/headers/cookies/data/json/auth/proxies/timeout/verify/allow_redirects/stream` arguments.

## Native library troubleshooting

Official wheels bundle the platform-specific Cronet dynamic library.

### Windows

For `ImportError: DLL load failed`, verify that:

1. The Python architecture matches the wheel, for example x64 Python with an x64 wheel.
2. `cronet.<version>.dll` exists in the `chrome_client` package directory.
3. `cronet_cloak.pyd` and the Cronet DLL came from the same build.
4. An older Cronet DLL from another directory was not loaded first.

### Linux

If a source installation reports that `libcronet.144.0.7506.0.so` cannot be opened, temporarily add the package directory:

```bash
export LD_LIBRARY_PATH="$(python -c 'import os, chrome_client; print(os.path.dirname(chrome_client.__file__))'):$LD_LIBRARY_PATH"
```

The current wheel is not a musllinux wheel. Alpine Linux users should use a glibc-based distribution.

## API reference

- [`chrome_client` type declarations](python/chrome_client/__init__.pyi)

## Acknowledgements

Thanks to [`2833844911/cyCronet`](https://github.com/2833844911/cyCronet) and its author for the cross-platform Cronet foundation. This project builds on it with Python APIs, type declarations, cookies, streaming requests, WebSocket support, and wheel packaging.

## License

The complete license is available in [`LICENSE`](LICENSE). The copyright notice and license terms must be retained in full.
