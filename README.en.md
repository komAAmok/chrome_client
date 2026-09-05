# chrome_client

Current release: `0.3.0`

An HTTP/WebSocket client built on the Chromium network stack. The native Core
owns TLS, HTTP/1.1, HTTP/2, HTTP/3/QUIC, proxy handling and WebSocket/WSS.
Language bindings only translate arguments, types, errors and lifetimes; they do
not ship a second networking stack.

The Python API matches two conventions at once: the `requests` shapes of
`Session`, `Response` and the exception hierarchy, and the `curl_cffi` surface of
`impersonate`, `http_version`, `AsyncSession`, `CurlMime` and WebSocket. Options
Chromium cannot honour faithfully raise instead of being silently ignored -- see
the [compatibility boundary](https://github.com/komAAmok/chrome_client/blob/main/docs/COMPATIBILITY_BOUNDARY.md).

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
v8. Go and Node directories currently contain binding design notes, not released
installable packages.

Cores range from 7.8 MB to 11.3 MB (macOS ARM64 smallest, Windows x86_64 largest,
where static MSVC/UCRT adds about 2 MB). The IDNA-only ICU dataset is linked in,
so no external `icudtl.dat` travels with the library, and the unused disk cache
backends are not linked at all. `tools/audit-core-*.sh` enforces a per-platform
size ceiling, so a regression fails the build.

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
`chrome<major>` alias and a bare `chrome`, resolving to the newest pinned major,
are also accepted). Chrome 99 through 152 are supported (54 profiles); a version
outside that range raises `ImpersonateError` rather than being downgraded, and
`available_profiles()` returns the full list. Non-Chromium targets such as Edge,
Safari, Firefox and Tor also raise rather than being treated as Chrome.

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
| HTTP/1.1, HTTP/2, HTTP/3/QUIC | ✓ | ✓ | Chromium negotiates by default; `http_version="v1"/"v2"/"v3"` pins it |
| HTTPS/TLS and certificate verification | ✓ | ✓ | `verify=False`, or `verify="/path/ca.pem"` for a custom CA; failures name the failing check |
| HTTP/HTTPS/SOCKS proxies | ✓ | ✓ | `proxy`, Requests-style `proxies`, mutable at runtime |
| Synchronous requests | ✓ | ✓ | Blocking calls release the GIL, so a thread pool works |
| asyncio requests | ✓ | — | Core callbacks wake the loop; no request thread pool, batched per wakeup |
| Streaming responses | `iter_content` / `aiter_content` / `raw` | `ResponseStream` | 1 MiB body queue per request, paused by ABI v8 above it |
| Chunked uploads | `data=` with a file object or iterator | `Upload::Chunked` | Same `upload_write` path for sync and async |
| Cancellation, timeouts, size limits | ✓ | ✓ | `Timeout`, `ResponseTooLarge`, `RequestException` |
| WebSocket / WSS | ✓ | ✓ | Sync and async, with the curl-cffi method names |
| Cookies | ✓ | ✓ | `session.cookies` is a `RequestsCookieJar`; reads and writes both take effect |
| Redirects | ✓ | ✓ | `history`, final URL, `max_redirects`, `allow_redirects=False` |
| In-memory cache | ✓ | ✓ | Lives for the Engine's lifetime; `cache=False` disables it |

## Install and load the Core

```bash
python -m pip install chrome-client
```

Published wheels contain the matching native extension. From a source checkout,
build the extension against a Core directory (`MINICRONET_CORE_DIR` is read at
build time) and put that same directory on the loader path at run time:

```bash
MINICRONET_CORE_DIR=$PWD/core/binaries/linux-x86_64 cargo build --release -p chrome-client-python

LD_LIBRARY_PATH=$PWD/core/binaries/linux-x86_64 PYTHONPATH=bindings/python python -c \
  'import chrome_client; print(chrome_client.get("https://example.com").status_code)'
```

## Python examples

### The requests shape

```python
import chrome_client as requests          # or: from chrome_client import requests

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

`Session` has the shape of `requests.Session`: `headers`, `params`, `cookies`,
`proxies`, `auth`, `hooks`, `stream`, `verify`, `max_redirects`, `trust_env`,
`adapters`, `mount()`, `prepare_request()`, `send()`, `resolve_redirects()`,
`close()` and the context manager protocol.

```python
from chrome_client import Session

with Session() as session:
    session.headers.update({"Accept": "application/json"})
    session.get("https://example.com/login")           # server sets cookies
    print(session.cookies.get_dict())                   # visible in the session
    session.cookies.set("consent", "1", domain="example.com", path="/")
    session.get("https://example.com/private")          # both kinds are sent
```

### Session state: cookies and proxies

`session.cookies` is a `RequestsCookieJar` (a `http.cookiejar.CookieJar`
subclass) with domain, path and secure metadata. Reads and writes both take
effect:

```python
session.cookies.get_dict()
session.cookies.get_dict(domain="example.com")
session.cookies.set("sid", "abc", domain="example.com", path="/")
session.cookies.update({"a": "1"})
del session.cookies["sid"]
session.cookies.clear()
```

Response cookies are owned and attached by Chromium's `CookieMonster` inside the
Core. The facade mirrors every `Set-Cookie` -- including redirect hops -- into
`session.cookies` and `response.cookies`. When a caller edit conflicts with what
the Core already stored, the Core would override the request header, so the
facade switches to an identically configured Engine with an empty cookie store,
which is what makes the edit take effect.

`session.proxies` is an ordinary mutable mapping, applied on the next request:

```python
session.proxies.update({"https": "http://user:pass@127.0.0.1:8080"})
session.get("https://example.com")                      # through the proxy
session.proxies.clear()
session.get("https://example.com")                      # direct
session.get("https://example.com", proxy="socks5://127.0.0.1:1080")  # one call
```

Lookup order is `scheme://host`, `scheme`, `all://host`, `all`; an explicit
`proxy=` wins. With `trust_env=True` (the default) `HTTP_PROXY`, `HTTPS_PROXY`
and `NO_PROXY` are honoured. A proxy is an Engine-level setting, so switching one
selects a different Engine -- cookies in the jar are still sent.

### Redirects, exceptions and responses

```python
response = session.get("https://example.com/r")
print(response.url, response.history, response.redirect_count)
response = session.get("https://example.com/r", allow_redirects=False)
print(response.status_code, response.headers["Location"], response.next.url)
session.get("https://example.com/r", max_redirects=5)   # raises TooManyRedirects
```

The exception hierarchy matches `requests.exceptions` (`RequestException` derives
from `IOError`) plus curl-cffi's extra leaves. Chromium net errors map to a
specific class and a readable name:

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

Certificate failures are distinguished by which check failed: expiry is
`ERR_CERT_DATE_INVALID (-201)`, a name mismatch is
`ERR_CERT_COMMON_NAME_INVALID (-200)`, and an untrusted issuer is
`ERR_CERT_AUTHORITY_INVALID (-202)`. All three raise `CertificateVerifyError`
(a subclass of `SSLError`, itself a `ConnectionError`). Use
`verify="/path/ca.pem"` to trust a private CA, or `verify=False` to skip
verification entirely -- both are Engine-level settings.

`Response` provides `status_code`, `reason`, `headers`, `cookies`, `history`,
`elapsed`, `request`, `url`, `encoding`, `apparent_encoding`, `text`, `content`,
`json()`, `raw`, `links`, `is_redirect`, `is_permanent_redirect`, `next`, `ok`,
`raise_for_status()`, `iter_content()`, `iter_lines()`, `close()`, and the
curl-cffi additions `http_version`, `charset`, `redirect_count`, `redirect_url`.

### The curl-cffi shape: fingerprints and protocols

```python
from chrome_client import Session, AsyncSession, CurlMime

with Session(impersonate="chrome152", http_version="v2") as session:
    session.get("https://example.com")
```

`impersonate` accepts `chrome_152`, `chrome152`, and `chrome` (resolving to the
newest pinned major). The TLS ClientHello, ALPN, HTTP/2 settings and priorities,
HTTP/3 transport parameters and default header order all come from that profile.
That is why `ja3=`, `akamai=`, `perk=` and most `extra_fp` fields raise
`UnsupportedFeature`: accepting a JA3 string while still sending Chromium's own
ClientHello would report a fidelity this build does not have. The two `extra_fp`
fields the facade can honour are accepted: `header_order` and `form_boundary`.

Also rejected explicitly: `cert=` (client certificates), `interface=`,
`doh_url=`, `curl_options=`, `max_recv_speed=` and `referer=`. The last one is
easy to get wrong: Chromium owns the referrer and strips a caller-supplied
`Referer` header, and ABI v8 has no referrer field, so setting it would never
reach the wire on any path.

Both multipart spellings work:

```python
session.post(url, data={"title": "t"}, files={"f": ("a.txt", b"...", "text/plain")})

mime = CurlMime()
mime.addpart(name="title", data="hello")
mime.addpart(name="photo", filename="p.jpg", content_type="image/jpeg",
             local_path="/tmp/p.jpg")
session.post(url, multipart=mime)
```

### Streaming and chunked uploads

```python
with session.stream("GET", "https://example.com/large") as response:
    for chunk in response.iter_content(64 * 1024):
        ...

response = session.get(url, stream=True, max_response_bytes=16 * 1024 * 1024)
try:
    for line in response.iter_lines():
        ...
finally:
    response.close()        # cancels the native request if not fully read

with open("big.bin", "rb") as handle:
    session.post(url, data=handle)      # file objects and iterators upload chunked
```

Exceeding `max_response_bytes` cancels the request and raises
`ResponseTooLarge`. Each request buffers at most 1 MiB of body; above that ABI v8
answers `MN_READ_PAUSE`, so nothing occupies a Core thread.

### Concurrency

A synchronous `Session` is safe to share across threads: every blocking call
releases the GIL and the Engine cache is locked.

```python
from concurrent.futures import ThreadPoolExecutor

with Session() as session, ThreadPoolExecutor(max_workers=32) as pool:
    codes = list(pool.map(lambda url: session.get(url).status_code, urls))
```

The asyncio path creates no thread pool: Core callbacks wake the loop with
`call_soon_threadsafe` and each wakeup drains a batch of events, so a multi-megabyte
response does not cost one loop round trip per chunk.

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

`max_clients` bounds in-flight requests. Use `spawn` or `forkserver` for process
pools: once an Engine exists the process is multi-threaded with Chromium threads
holding locks, so `fork()` can deadlock the child before it runs any Python.

Chromium allows at most 6 concurrent HTTP/1.1 connections per host group
(measured: exactly 6 sockets in flight against one host, and throughput rises
with several hostnames). That is browser semantics, not a binding bottleneck --
use HTTP/2 or HTTP/3 endpoints when you need more per-host concurrency.

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

Sync and async both provide `send`/`send_str`/`send_bytes`/`send_json`/`ping`,
`recv`/`recv_str`/`recv_bytes`/`recv_json`/`recv_fragment`, `close` and
`terminate`; the synchronous socket also has
`run_forever(on_message=..., on_error=..., on_open=..., on_close=...)`. The
constructor returns an already-open socket, because the Core rejects `send()` and
`close()` before the handshake completes.

Two Chromium rules govern the handshake headers:

- **The User-Agent cannot be passed per call.** The Core rejects `User-Agent`,
  `Host`, `Origin`, `Connection`, `Upgrade` and `Sec-WebSocket-*` as extra headers
  (`IsForbiddenWebSocketHeader`), because Chromium decides both their values and
  their position in the handshake -- and the UA's position is itself part of the
  fingerprint. To change the WebSocket UA use `Session(user_agent=...)`, an
  Engine-level setting shared by HTTP and WS, or a different `impersonate`
  profile. Passing the header raises `UnsupportedFeature` rather than silently
  producing a handshake whose UA disagrees with every other request.
- **`Origin` defaults to the URL's own origin.** The Core rejects an empty origin
  and there is no page to inherit one from, so `ws://host:port` becomes
  `http://host:port` -- what a same-origin page connection looks like. Pass
  `origin=` for anything else.

Cookies matching the URL are attached to the handshake.

## Requests and curl-cffi compatibility

Change `from curl_cffi import requests` to `from chrome_client import requests`,
or `import requests` to `import chrome_client as requests`, and most code needs
no further edits.

| Usage | Status |
| --- | --- |
| `get/post/put/patch/delete/head/options/trace/query` | ✓ |
| `Session`, `AsyncSession`, `Client`, `AsyncClient` | ✓ (`Client` aliases `Session`) |
| `params`, `data`, `json`, `content`, `files`, `multipart` | ✓ |
| `headers`, `cookies`, `auth`, `timeout` (including `(connect, read)`) | ✓ |
| `proxies`, `proxy`, `proxy_auth`, `trust_env`, `NO_PROXY` | ✓ |
| `verify=True/False/"/path/ca.pem"` | ✓ |
| `allow_redirects`, `max_redirects`, `history`, final URL | ✓ |
| `stream=True`, `iter_content`, `iter_lines`, `raw` | ✓ |
| `aiter_content`, `aiter_lines`, `atext`, `acontent`, `session.stream(...)` | ✓ |
| `hooks={"response": ...}`, `prepare_request`, `send`, `mount`, custom adapters | ✓ |
| `Request`, `PreparedRequest`, `codes`, `CaseInsensitiveDict`, `Headers`, `Cookies` | ✓ |
| `HTTPBasicAuth`, `HTTPProxyAuth`, `AuthBase` | ✓ |
| The whole `requests.exceptions` tree plus curl-cffi's leaves | ✓ |
| `impersonate`, `http_version`, `retry`/`RetryStrategy`, `raise_for_status=True` | ✓ |
| `base_url`, `discard_cookies`, `default_encoding`, `content_callback` | ✓ |
| `CurlMime`, `ExtraFingerprints(header_order=..., form_boundary=...)` | ✓ |
| `HTTPDigestAuth` | Partial: drive it with `parse_challenge()`/`handle_401()` |
| `HTTPAdapter(pool_connections=..., pool_maxsize=...)` | Accepted but inert: Chromium owns the pools |
| `ja3`, `akamai`, `perk`, TLS/HTTP2 fields of `extra_fp` | ✗ raises `UnsupportedFeature` |
| `cert` (client certificates), `interface`, `doh_url`, `curl_options`, `max_recv_speed` | ✗ raises `UnsupportedFeature` |
| `referer=` / a `Referer` header | ✗ raises `UnsupportedFeature` (Chromium strips it) |
| Persistent cookie/cache files, the low-level `Curl` handle, `CurlOpt`/`CurlInfo` | ✗ |

Deliberate behavioural differences:

- Module-level `chrome_client.get(...)` shares one process-wide `Session`, and so
  shares a connection pool and cookie store. requests builds a new `Session` per
  call; here a Session is an entire Chromium `URLRequestContext`, so per-call
  construction would cost threads and memory. Build an explicit `Session` when you
  need isolation, or call `close_shared_session()`.
- `session.headers` starts empty. requests seeds `User-Agent`, `Accept`,
  `Accept-Encoding` and `Connection`; here those belong to the profile and to
  Chromium, and an injected `Accept: */*` is visible to a fingerprinter.
- `Response.raw` is a file object exposing `read`/`stream`/`close`, not a urllib3
  `HTTPResponse`.
- `Response.ok` uses the requests rule (`status_code < 400`), not curl-cffi's
  200–399.

## Repository and verification

| Path | Contents |
| --- | --- |
| `core/abi/` | Stable C ABI v8 |
| `core/binaries/` | Eight audited Core targets |
| `crates/minicronet/` | Rust safety layer, streams and lifetimes |
| `bindings/python/` | Python 3.7–3.13 binding and the shared facade |
| `bindings/python36/` | Separate Python 3.6 abi3 binding (same facade) |
| `bindings/python/tests/` | `test_stability.py` for lifetimes and concurrency; `test_compat.py` for the requests/curl-cffi surface |
| `docs/` | Build, platform, ABI, compatibility and audit docs |

The regression suites need an audited Core:

```bash
LD_LIBRARY_PATH=core/binaries/linux-x86_64 PYTHONPATH=bindings/python \
  python -m unittest discover -s bindings/python/tests -p "test_*.py"
```
