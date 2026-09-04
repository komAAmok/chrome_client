# chrome_client

Current release: `0.2.1.1`

An HTTP/WebSocket client built on the Chromium network stack. The native Core
owns TLS, HTTP/1.1, HTTP/2, HTTP/3/QUIC, proxy handling and WebSocket/WSS.
Language bindings only translate arguments, types, errors and lifetimes; they do
not ship a second networking stack.

[中文 README](https://github.com/komAAmok/chrome_client/blob/main/README.md) · [Build guide](https://github.com/komAAmok/chrome_client/blob/main/docs/BUILD.md) · [Compatibility boundary](https://github.com/komAAmok/chrome_client/blob/main/docs/COMPATIBILITY_BOUNDARY.md)

## Support matrix

### Operating systems, architectures and bindings

| OS | Core targets | Python 3.7–3.13 | Python 3.6 | Rust | Notes |
| --- | --- | --- | --- | --- | --- |
| Linux | x86 (`i686`), x86_64, ARM64 | ✓ | ✓ (separate abi3 extension) | ✓ | Runtime dependencies are listed in each manifest |
| Windows | x86, x86_64, ARM64 | ✓ | x86/x86_64 ✓ | ✓ | Wheels bundle the DLL; ICU data is linked in |
| macOS | x86_64, ARM64 | ✓ | ✓ (separate abi3 extension) | ✓ | dylib is architecture-specific |


Core artifacts are in `core/binaries/<target>/`. Each artifact records the ABI
version, Chromium revision, SHA-256 and runtime dependencies. The current ABI is
v7. Go and Node directories currently contain binding design notes, not released
installable packages.

| Core directory | Rust target | Core file |
| --- | --- | --- |
| `linux-x86` | `i686-unknown-linux-gnu` | `libminicronet.so` |
| `linux-x86_64` | `x86_64-unknown-linux-gnu` | `libminicronet.so` |
| `linux-arm64` | `aarch64-unknown-linux-gnu` | `libminicronet.so` |
| `windows-x86` | `i686-pc-windows-msvc` | `minicronet.dll` + `minicronet.lib` |
| `windows-x86_64` | `x86_64-pc-windows-msvc` | `minicronet.dll` + `minicronet.lib` |
| `windows-arm64` | `aarch64-pc-windows-msvc` | `minicronet.dll` + `minicronet.lib` |
| `macos-x86_64` | `x86_64-apple-darwin` | `libminicronet.dylib` |
| `macos-arm64` | `aarch64-apple-darwin` | `libminicronet.dylib` |

### Chrome profiles

Pass an exact `chrome_<major>` string through `impersonate` (the curl-cffi-style
`chrome<major>` alias is also accepted). Chrome 99 through 152 are supported
(54 profiles); an unknown version is rejected rather than silently downgraded.

| Chrome major | Profile IDs |
| --- | --- |
| 99–105 | `chrome_99` … `chrome_105` |
| 106–112 | `chrome_106` … `chrome_112` |
| 113–119 | `chrome_113` … `chrome_119` |
| 120–126 | `chrome_120` … `chrome_126` |
| 127–133 | `chrome_127` … `chrome_133` |
| 134–140 | `chrome_134` … `chrome_140` |
| 141–147 | `chrome_141` … `chrome_147` |
| 148–152 | `chrome_148`, `chrome_149`, `chrome_150`, `chrome_151`, `chrome_152` |

Profiles control TLS ClientHello, ALPN, HTTP/2 settings, QUIC/H3 and related
Chromium network parameters. This is not a full browser: Blink, extensions,
Service Workers and persistent browser profiles are not included.

### Feature matrix

| Feature | Python | Rust/Core | Notes |
| --- | --- | --- | --- |
| HTTP/1.1, HTTP/2, HTTP/3/QUIC | ✓ | ✓ | Chromium negotiates by default; Rust can force H1/H2/H3 |
| HTTPS/TLS and certificate verification | ✓ | ✓ | Use `verify=False` only deliberately |
| HTTP/HTTPS/SOCKS proxies | ✓ | ✓ | Python accepts `proxy` and Requests-style `proxies` |
| Synchronous requests | ✓ | ✓ | Synchronous waits release the GIL |
| asyncio requests | ✓ | — | Core callbacks wake the event loop; no request thread pool |
| Streaming responses | `iter_content` / `aiter_bytes` | `ResponseStream` | 1 MiB body queue per request |
| Cancellation, timeouts, size limits | ✓ | ✓ | `Timeout`, `ResponseTooLarge`, `RequestException` |
| WebSocket / WSS | ✓ | ✓ | Synchronous and asynchronous `recv` |
| Cookies, in-memory cache, redirects, uploads | ✓ | ✓ | Cookie/cache lifetime is the Engine lifetime |

## Install and load the Core

```bash
python -m pip install chrome-client
```

Published wheels contain the matching native extension. From a source checkout,
point `MINICRONET_CORE_DIR` at the matching Core directory:

```bash
MINICRONET_CORE_DIR=$PWD/core/binaries/linux-x86_64 \
LD_LIBRARY_PATH=$PWD/core/binaries/linux-x86_64 \
PYTHONPATH=bindings/python:target/debug python -c \
  'import chrome_client; print(chrome_client.get("https://example.com").status_code)'
```

## Python examples

### Synchronous GET, query parameters, headers and JSON

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

The other Requests-style methods use the same parameters:

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

### Client/Session, cookies, proxy and TLS verification

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

An explicit `proxy="http://..."` wins over `proxies`; mappings use `http`,
`https` and `all` keys.

`client.cookies` is a `CookieJar` (a `dict` subclass) that supports the
Requests-style `get_dict()`:

```python
with Client(cookies={"session": "abc"}) as client:
    client.cookies["extra"] = "1"
    print(client.cookies.get_dict())   # {'session': 'abc', 'extra': '1'}
```

It holds only the cookies the caller configured for outgoing requests. Cookies
returned by responses are owned by the Chromium CookieStore inside the Core,
which ABI v8 does not export, so they never appear in this jar and Python never
re-attaches them. The jar carries no domain or path metadata, so
`get_dict(domain=...)` and `get_dict(path=...)` raise `ValueError` instead of
returning unfiltered data.

### asyncio concurrency

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

### Streaming, size limits and cancellation

```python
from chrome_client import Client

with Client() as client:
    response = client.get("https://example.com/large", stream=True,
                          max_response_bytes=16 * 1024 * 1024)
    try:
        total = sum(len(chunk) for chunk in response.iter_content(64 * 1024))
        print("bytes:", total)
    finally:
        response.close()       # Cancels an unfinished native request
```

For async streams use `async for chunk in response.aiter_bytes()` and finish
with `await response.aclose()`. Exceeding `max_response_bytes` cancels the
request and raises `ResponseTooLarge`.

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

### Timeouts and errors

```python
import chrome_client

try:
    chrome_client.get("https://example.com", timeout=0.5)
except chrome_client.Timeout:
    print("request timed out")
except chrome_client.RequestException as error:
    print("request failed:", error)
```

## Requests and curl-cffi syntax compatibility

The common call shape is intentionally compatible with Requests and curl-cffi:

| Syntax/parameter | Support |
| --- | --- |
| `get/post/put/delete`, `Client`, `Session` | ✓; `Session` aliases `Client` |
| `params`, `headers`, `cookies`, `data`, `json` | ✓ |
| `timeout`, `verify`, `proxies`, `proxy` | ✓ |
| `impersonate="chrome_151"` | ✓, using this project's Chromium profile |
| `stream=True`, `iter_content` | ✓; async uses `aiter_bytes` |
| `session.cookies.get_dict()` | ✓; configured outgoing cookies only, no response cookies |
| `curl_options`, `ja3`, `akamai`, libcurl handles | ✗ |
| Every Requests adapter/plugin/persistent-cookie feature | ✗ |

This is not a drop-in replacement for the `curl_cffi.requests` import. Change
`from curl_cffi import requests` to `from chrome_client import requests`, then
follow this README for response streaming, exception and WebSocket semantics.
Both libraries accept common `impersonate`, proxy and timeout arguments, but
their profile coverage, TLS behavior and connection pools are independent.

## Repository and verification

| Path | Contents |
| --- | --- |
| `core/abi/` | Stable C ABI v8 |
| `core/binaries/` | Eight audited Core targets |
| `crates/minicronet/` | Rust safety layer, streams and lifetimes |
| `bindings/python/` | Python 3.7–3.13 binding and facade |
| `bindings/python36/` | Separate Python 3.6 abi3 binding |
| `docs/` | Build, platform, ABI, compatibility and audit docs |
