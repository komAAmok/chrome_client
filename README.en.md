# chrome_client

[![PyPI version](https://img.shields.io/pypi/v/chrome-client)](https://pypi.org/project/chrome-client/)
[![Python versions](https://img.shields.io/badge/python-3.6%20%7C%203.7%20%7C%203.8%20%7C%203.9%20%7C%203.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14%20%7C%203.15-blue)](https://pypi.org/project/chrome-client/)
[![License: MIT](https://img.shields.io/pypi/l/chrome-client)](https://pypi.org/project/chrome-client/)

**A Chromium-powered HTTP/HTTPS and WebSocket fingerprinted client.**

This is not a client that merely approximates a browser fingerprint. The Chromium
network stack itself is compiled into `libminicronet` and issues the requests, so
the TLS ClientHello, the HTTP/2 SETTINGS frame, the QUIC transport parameters and
the request-header order are that Chrome release's own behaviour rather than an
imitation of it.

```python
from chrome_client import Session

with Session(impersonate="chrome_155") as session:
    response = session.get("https://example.com")
```

The Python API matches two conventions at once: the `requests` shapes of
`Session`, `Response` and the exception hierarchy, and the `curl_cffi` surface of
`impersonate`, `http_version`, `AsyncSession` and WebSocket. The
synchronous and asynchronous APIs share one engine and differ only in how they
wait.

**Options Chromium cannot honour faithfully raise; they are never silently
ignored.** A silent downgrade is worse than a missing feature, because it leaves
the caller believing it is imitating a fingerprint when it is not. See the
[compatibility boundary](https://github.com/komAAmok/chrome_client/blob/main/docs/COMPATIBILITY_BOUNDARY.md).

[中文 README](https://github.com/komAAmok/chrome_client/blob/main/README.md) · [Build guide](https://github.com/komAAmok/chrome_client/blob/main/docs/BUILD.md) · [Compatibility boundary](https://github.com/komAAmok/chrome_client/blob/main/docs/COMPATIBILITY_BOUNDARY.md) · [Third-party notices](https://github.com/komAAmok/chrome_client/blob/main/THIRD_PARTY_NOTICES.md)

## Support matrix

### Operating systems, architectures and bindings

| OS | Core targets | Python 3.9–3.15 | Python 3.6–3.8 | Rust | Notes |
| --- | --- | --- | --- | --- | --- |
| Linux | x86 (`i686`), x86_64, ARM64 | ✓ | ✓ (separate abi3 extension) | ✓ | Runtime dependencies are listed in each manifest |
| Linux | ARMv7 (hard-float, NEON) | not published | not published | ✓ | Core is built and audited, but **never loaded and never sent a request** (qemu attempted, see `docs/NEXT_STEPS.md`); build the extension yourself |
| Windows | x86, x86_64, ARM64 | ✓ | x86/x86_64 ✓ | ✓ | Wheels bundle the DLL; ICU data is linked in |
| macOS | x86_64, ARM64 | ✓ | ✓ (separate abi3 extension) | ✓ | dylib is architecture-specific |


Core artifacts are in `core/binaries/<target>/`. Each artifact records the ABI
version, Chromium revision, SHA-256 and runtime dependencies. The current ABI is
v12. Go and Node directories currently contain binding design notes, not released
installable packages.

### Chrome profiles

Pass an exact `chrome_<major>` string through `impersonate` (the curl-cffi-style
`chrome<major>` alias is also accepted). A bare `chrome`/`chromium` is **not**
accepted: it would resolve to whichever profile this build happens to ship last,
so a caller's fingerprint would change whenever a profile is added, silently and
without the caller asking -- it raises `ImpersonateError` and asks for the exact
major instead. Chrome 86 through 158 are supported (73 profiles); a version
outside that range raises `ImpersonateError` rather than being downgraded, and
`available_profiles()` returns the full list. Non-Chromium targets such as Edge,
Safari, Firefox and Tor also raise rather than being treated as Chrome.

| Chrome major | Profile IDs |
| --- | --- |
| 86–92 | `chrome_86` … `chrome_92` (audited per tag: no ALPS extension, no H3 baseline) |
| 93–98 | `chrome_93` … `chrome_98` |
| 99–105 | `chrome_99` … `chrome_105` |
| 106–112 | `chrome_106` … `chrome_112` |
| 113–119 | `chrome_113` … `chrome_119` |
| 120–126 | `chrome_120` … `chrome_126` |
| 127–133 | `chrome_127` … `chrome_133` |
| 134–140 | `chrome_134` … `chrome_140` |
| 141–147 | `chrome_141` … `chrome_147` |
| 148–157 | `chrome_148` … `chrome_157` (`chrome_154`–`chrome_157` use the 156-kernel Core; each is audited against its own tag) |
| 158 | `chrome_158` (same 156-kernel Core; captured from 158.0.8127.37, source baseline is that release line's latest tag 158.0.8127.99) |

Profiles control TLS ClientHello, ALPN, HTTP/2 settings, QUIC/H3 and related
Chromium network parameters. This is not a full browser: Blink, extensions,
Service Workers and persistent browser profiles are not included.

### Feature matrix

| Feature | Python | Rust/Core | Notes |
| --- | --- | --- | --- |
| HTTP/1.1, HTTP/2, HTTP/3/QUIC | ✓ | ✓ | Chromium negotiates by default; `http_version="v1"/"v2"/"v3"` pins it |
| HTTPS/TLS and certificate verification | ✓ | ✓ | `verify=False`, or `verify="/path/ca.pem"` for a custom CA; failures name the failing check |
| mTLS client certificates | ✓ | ✓ | `cert=("c.pem", "k.pem")` or one combined PEM; presented only when the server asks, so the ClientHello is unchanged |
| Domain mapped to a fixed IP | ✓ | ✓ | `host_mapping="MAP cdn.test 93.184.216.34"`; rewrites **only the resolution result**, so SNI, the Host header and certificate verification keep the domain |
| DNS servers / DoH (DoT unsupported, raises) | ✓ | ✓ | `dns_nameservers="1.1.1.1"`, `doh_template=...` (needs `dns_secure_mode=1/2`), `dns_rotate` (`dns_padding` is refused). Measured: `dns_nameservers` pointed at a dead server still returns 200 (Chromium falls back to the SYSTEM task), and a `dns_secure_mode=2` DoH never falls back |
| ECH (Encrypted ClientHello) | ✓ GREASE | ✓ encrypted by default | `Response.ech_status` reports `success` / `rejected` / `not_attempted`. **Version boundary: `chrome_118` and earlier send no ECH extension; only `chrome_119` and later send it** (with GREASE, the payload length varies among 372/436/500/564). **SNI is encrypted by default**: the engine always builds its own built-in resolver (`core/source/engine.cc`), which queries the DNS HTTPS record (type 65) and turns a non-empty `ech=` parameter into a real encrypted ClientHello — the same configuration desktop Chrome ships (`stub_resolver_config_reader.cc:190` -> `kAsyncDns`). `dns_nameservers` / `doh_template` / `dns_secure_mode` override fields; they are not the switch that enables ECH. A host with no HTTPS record (such as `tls.peet.ws`), **or an upstream resolver that does not answer type 65** (such as the systemd-resolved stub), still gets GREASE only and `ech_status` stays `not_attempted` -- real Chrome on the same machine behaves identically, because the DNS answer decides, not the client. Per-release evidence is in the last section of `docs/ABI.md` |
| Platform identity | ✓ | ✓ | `platform="windows"/"macos"/"linux"`; the UA group, `sec-ch-ua-platform` and `sec-ch-ua-mobile` share one source, so a Linux host can send a Windows UA |
| Per-origin connection ceiling | ✓ | ✓ | `max_sockets_per_group=32`; the default 0 keeps Chromium's own 6. Raising it is an observable deviation from the browser being imitated |
| HSTS bypass and multi-hop proxy | ✓ | ✓ | `hsts_bypass="example"` (single labels only), `proxy_chain="socks5://a:1080,http://b:8080"` |
| HTTP/HTTPS/SOCKS proxies | ✓ | ✓ | `proxy`, Requests-style `proxies`, mutable at runtime |
| Synchronous requests | ✓ | ✓ | Blocking calls release the GIL, so a thread pool works |
| asyncio requests | ✓ | — | Core callbacks wake the loop; no request thread pool, batched per wakeup |
| Streaming responses | `iter_content` / `aiter_content` / `raw` | `ResponseStream` | 1 MiB body queue per request; at the ceiling the binding returns `MN_READ_PAUSE` (ABI v11) so the Core stops reading the socket, and `resume_read` restarts it once the consumer takes data |
| Chunked uploads | `data=` with a file object or iterator | `Upload::Chunked` | Same `upload_write` path for sync and async |
| Cancellation, timeouts, size limits | ✓ | ✓ | `Timeout`, `ResponseTooLarge`, `RequestException` |
| WebSocket / WSS | ✓ | ✓ | Sync and async, with the curl-cffi method names |
| Cookies | ✓ | ✓ | `session.cookies` is a `RequestsCookieJar`; reads and writes both take effect |
| Redirects | ✓ | ✓ | `history`, final URL, `allow_redirects=False`; the ceiling is Chromium 20 (`CHROMIUM_MAX_REDIRECTS`), `max_redirects=` removed |
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

### Type hints

The package ships `py.typed` and complete `.pyi` stubs
(`bindings/python/chrome_client/**/*.pyi`), so no separate stub package is needed:
an IDE completes every parameter, infers return types and shows the
Args/Returns/example blocks on hover. Runtime behaviour is unchanged -- stubs are
never imported, and `typing_extensions` is a type-checking dependency only.

```python
from chrome_client import Session

with Session(impersonate="chrome_155") as session:
    session.get(url, timeout=(5, 15))
    # hovering impersonate= lists chrome_86 ... chrome_158 and the curl-cffi
    # chrome<major> aliases; bare chrome/chromium is rejected
    # session.get(...) completes headers/cookies/data/json/timeout/proxies/verify/...
```

`impersonate` and `http_version` enumerate every accepted value as a `Literal`, and
every request-method option (`headers`, `cookies`, `data`, `json`, `timeout`,
`proxies`, `verify`, `auth`, ...) is a named parameter rather than something hidden
behind `**kwargs`, so any IDE -- PyCharm included -- completes them, and
`impersonate="chrome_200"` or a misspelled option is flagged while you type rather
than at run time. Coverage and how the stubs are verified against the runtime are
documented in
[the typing notes](https://github.com/komAAmok/chrome_client/blob/main/docs/PYTHON_TYPING.md).

## Python examples

### The requests shape

```python
import chrome_client as requests          # or: from chrome_client import requests

response = requests.get(
    "https://example.com/api",
    params={"page": 1},
    headers={"Accept": "application/json"},
    impersonate="chrome_155",
    timeout=15,
)
response.raise_for_status()
print(response.status_code, response.reason, response.json())
```

`Session` has the shape of `requests.Session`: `headers`, `params`, `cookies`,
`proxies`, `auth`, `hooks`, `stream`, `verify`, `trust_env`,
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
A `cookies={...}` mapping is scoped to **that one request**: measured, a plain
`get(url)` after `get(url, cookies={"per": "P"})` carries no `per` (as requests
does), and a same-name override is restored to the session value afterwards
rather than deleting it. One documented exception: on a `discard_cookies=True`
302 chain the second hop still carries the cookie the first hop set --
suppressing it needs a per-hop switch this Chromium build does not have
(`docs/TBD_VERIFICATION.md` T-1), and `allow_redirects=False` is the spelling that
keeps the promise. Passing `cookies=` together with `discard_cookies=True` now raises
`UnsupportedFeature` instead of dropping the value silently; a literal `Cookie`
header still loses to the option, without rewriting the caller's request object.

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

**A proxy pool needs `max_engines` at least as large as the pool.** An Engine is
a whole Chromium `URLRequestContext` (its own threads, socket pools, DNS cache and
TLS session cache); a session keeps `max_engines` of them (8 by default) and
evicts the least-recently-used one. Once the pool is larger than that, every
request rebuilds a context and opens a fresh TCP connection instead of reusing
one. Measured on a 20-endpoint pool over 200 requests:

| `max_engines` | Chromium contexts built | proxy TCP connections | TIME_WAIT left behind |
| --- | --- | --- | --- |
| 8 (default) | 200 | 200 | +180 |
| 20 | 20 | 20 | +20 |

```python
session = chrome_client.Session(max_engines=32)   # pool of 32 endpoints
```

Proxy rules **use Chromium's native syntax first** and only extend it where
native support genuinely does not exist. The three forms the Core already parses
are passed through unchanged (the facade used to rewrite all of them into a
single `scheme://host`, which broke them):

| Native form | Meaning | Previously |
| --- | --- | --- |
| `http=h:p` | this proxy for HTTP only | rewritten to `http://http=h:p` |
| `http=h:p;https=h:p` | a different proxy per destination scheme | rewritten |
| `http://a:p,http://b:p` | fallback list, tried in order | rejected |
| `direct://` | no proxy | rejected as an unknown scheme |

Those are validated (rule keys, `direct://` placement, embedded credentials) and
returned byte-identical. Everything else is the **extension layer**, which only
exists because Chromium rejects the input outright:

| Spelling | Handling | Why |
| --- | --- | --- |
| `http://` `https://` `socks://` `socks4://` `socks5://` | used as written | native `GetSchemeFromUriScheme` set |
| `socks5h://` | rewritten to `socks5://` | same behaviour, a spelling Chromium lacks |
| `socks4a://` | `UnsupportedFeature` | asks for remote resolution the Core cannot do |
| `quic://` | used as given | this project turns `ENABLE_QUIC_PROXY_SUPPORT` on; see `capabilities()` |
| `host:port` (no scheme) | `http://host:port` | Chromium defaults a bare rule to HTTP |
| trailing `/`, path, `#fragment`, newline, spaces | dropped | a rule is host:port only |
| `host:` (empty port) | `InvalidProxyURL` | Chromium rejects it too |
| `host:0` | kept as port 0 | `ParsePort("0")` is a real port, not the default |
| trailing text / unknown scheme / `?query` / doubled scheme | `InvalidProxyURL` | unparseable |

Native rule keys are `http`/`https`/`ftp`/`socks`; a `socks=` entry means
**SOCKS4** in Chromium's reading, so write `socks=socks5://h:p` for SOCKS5.
**SOCKS5 username/password authentication is not supported** (Chromium offers
only the `0x00` no-auth method); use an HTTP or HTTPS proxy when credentials are
required. See
[docs/COMPATIBILITY_BOUNDARY.md](https://github.com/komAAmok/chrome_client/blob/main/docs/COMPATIBILITY_BOUNDARY.md).

### Redirects, exceptions and responses

```python
response = session.get("https://example.com/r")
print(response.url, response.history, response.redirect_count)
response = session.get("https://example.com/r", allow_redirects=False)
print(response.status_code, response.headers["Location"], response.next.url)
session.get("https://example.com/r", allow_redirects=False)  # returns the 3xx
```

The hop ceiling belongs to Chromium -- 20 hops inside one `URLRequest` -- and is
published as `chrome_client.CHROMIUM_MAX_REDIRECTS`. `max_redirects=` is **not a
parameter any more**: it could only ever be `None`, no number can lower the ceiling,
and re-issuing hops from Python would reimplement redirect semantics the Core owns. Use
`allow_redirects=False` for a single hop.

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
from chrome_client import Session, AsyncSession

with Session(impersonate="chrome152", http_version="v2") as session:
    session.get("https://example.com")
```

`impersonate` accepts `chrome_155`, `chrome155`, and `chrome` (resolving to the
newest pinned major). The TLS ClientHello, ALPN, HTTP/2 settings and priorities,
and HTTP/3 transport parameters all come from that profile. That is why `ja3=`,
`akamai=` and `perk=` do not exist (a `TypeError` if you pass one): accepting a JA3
string while still sending Chromium's own ClientHello would misreport fidelity.
`extra_fp` keeps only the two knobs the facade really implements, `header_order` and
`form_boundary`; the other 13 curl_cffi fields are gone as well.

**Default request headers come from the Core, built from the profile's own
captures.** `impersonate="chrome_155"` sends the same header set that release
left in `profiles/chrome_155/h1/captures.json` across six independent
connections: `sec-ch-ua`, `sec-ch-ua-mobile`, `sec-ch-ua-platform`,
`Upgrade-Insecure-Requests`, `User-Agent`, `Accept`, four `Sec-Fetch-*`,
`Accept-Encoding`, `Accept-Language` -- in capture order. ABI v9 compiles
those sets into `libminicronet` (three distinct sets cover all 73 profiles: 9,
11 and 12 fields, 39 KB of text) and `Request::StartOnNetworkThread` sends them
through `SetExtraRequestHeaders`.

That placement is the point. Chrome builds these headers in the
`services/network` URLLoader factory, which is the same layer
`SetExtraRequestHeaders` reaches, so `Accept-CH` negotiation sees a real
request. When the binding assembled them instead they sat outside `net` and
`net` did not know they existed.

`mn_engine_config_t.inject_client_hints` turns this off (1 is the default, 0
sends only the caller's headers, which always win field by field).
`bindings/python/tests/test_client_hints.py` compares every profile against
its captures, and `tools/audit-client-hints.py` re-derives the whole table
from the captures before a commit.

`interface=`, `doh_url=`, `curl_options=`, `max_recv_speed`, `thread=`, `debug`,
`quote`, `dns_over_tls`, `dns_padding` and `referer` are **not parameters any more** (a
`TypeError`). `referer` is the header: the Core routes `Referer` to
`URLRequest::SetReferrer` (`core/source/request.cc:245`), so
`headers={"Referer": ...}` is the spelling that reaches the wire. `doh_url` becomes
`doh_template=` with `dns_secure_mode=2`.

Both multipart spellings work:

```python
session.post(url, data={"title": "t"}, files={"f": ("a.txt", b"...", "text/plain")})

# files= is the only multipart spelling; (None, data) is a plain field and
# the fourth slot carries per-part headers.
session.post(url,
             data={"title": "hello"},
             files={"photo": ("p.jpg", open("/tmp/p.jpg", "rb"), "image/jpeg"),
                    "note": (None, b"plain field")})
```


### Migrating from curl_cffi (keywords removed in 0.3.0)

This project is a Chromium-backed fingerprint client, not a libcurl drop-in. These
keywords are **gone**: passing one raises `TypeError`, instead of being accepted and
then refused or ignored.

| Removed | Replacement | Why |
| --- | --- | --- |
| `ja3=` / `akamai=` / `perk=` | `impersonate="chrome_<major>"` | the TLS/H2 fingerprint comes from the profile; the ABI cannot reach BoringSSL |
| 13 fields of `extra_fp` | same; only `header_order` and `form_boundary` remain | the rest are frozen per profile or do not exist in Chrome |
| `interface=` | none | Chromium has no interface binding |
| `doh_url=` | `doh_template=` with `dns_secure_mode=2` | that pair is what the Core implements |
| `curl_options=` | none | there is no libcurl option table |
| `max_recv_speed=` | none | Chromium owns transfer pacing |
| `thread=` / `debug=` | none | libcurl handle concepts |
| `quote=` | none | the URL is always canonicalised and percent-encoded |
| `max_redirects=` | `allow_redirects=False` for one hop; the ceiling is 20 (`CHROMIUM_MAX_REDIRECTS`) | the option could only be `None` |
| `dns_over_tls=` / `dns_padding=` | none (use `doh_template` for an encrypted resolver) | desktop Chrome has neither |
| `referer=` | `headers={"Referer": ...}` | the Core routes the header to `URLRequest::SetReferrer` |
| `multipart=` / `CurlMime` | `files=` (byte-identical bodies for the same parts) | the two spellings differed only in part order |
| `accept_encoding=` | none (`header_policy="drop"` removes the header entirely) | measured: six probes all reached the wire as the profile value `gzip, deflate, br, zstd` -- Chromium owns this header |
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
`ResponseTooLarge`. Each request buffers at most 1 MiB of body; above that ABI v11
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
    async with AsyncSession(impersonate="chrome_155", max_clients=64) as session:
        responses = await asyncio.gather(*[session.get(u) for u in urls])
        async with session.stream("GET", big_url) as response:
            async for chunk in response.aiter_content(65536):
                ...

asyncio.run(main())
```

`max_clients` bounds **admitted requests**. With `stream=False` it covers the whole
response; with `stream=True` it covers only up to the headers, and the body is
read outside the gate (see [the compatibility boundary](https://github.com/komAAmok/chrome_client/blob/main/docs/COMPATIBILITY_BOUNDARY.md)).
Use `spawn` or `forkserver` for process pools: once an Engine exists the process is
multi-threaded with Chromium threads holding locks, so `fork()` can deadlock the
child before it runs any Python.

Chromium allows at most 6 concurrent HTTP/1.1 connections per host group
(measured: exactly 6 sockets in flight against one host, and throughput rises
with several hostnames). That is browser semantics, not a binding bottleneck --
use HTTP/2 or HTTP/3 endpoints when you need more per-host concurrency.

### WebSocket / WSS

```python
from chrome_client import WebSocket

with WebSocket(url="wss://echo.example.com", impersonate="chrome_155") as socket:
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
| `headers`, `cookies`, `auth`, `timeout` | ✓ |
| Removing a Core-owned header (`User-Agent`, `Accept-Encoding`, ...) | ✗ raises `UnsupportedFeature`; overriding still works |
| `dns_padding=` | ✗ **removed** (`TypeError`); Chromium exposes no EDNS0 padding control. The value used to travel into `EngineOptions` and stop -- `core/source/engine.cc` never read it -- so a caller asking for padded queries got unpadded ones with no error |
| `dns_over_tls=` | ✗ **removed** (`TypeError`); Chromium desktop ships no DoT client, so setting it only disables the built-in resolver (`net/dns/dns_client.cc:168`) and silently falls back to the system one, dropping `dns_nameservers`/`doh_template`. Use `doh_template` with `dns_secure_mode=2` for an encrypted resolver |
| Setting a hop-by-hop field (`Connection`, `Keep-Alive`, `TE`, `Trailer`, `Upgrade`, `Proxy-Connection`) | ✓ dropped by default and counted in `session.dropped_headers` (Chromium owns connection lifetime and h2/h3 forbid these); raises `UnsupportedFeature` under `header_policy="strict"` |
| `timeout=(connect, read)` with two different values | ✗ raises `UnsupportedFeature` (ABI v11 carries one deadline); an agreeing pair works |
| `proxies`, `proxy`, `proxy_auth`, `trust_env`, `NO_PROXY` | ✓ |
| `verify=True/False/"/path/ca.pem"` | ✓ |
| `host_mapping="MAP <domain> <ip>[:port]"`, `EXCLUDE <domain>`, or a list of either | ✓ rewrites only the resolution result; SNI, Host and certificate verification keep the domain. A misspelled keyword raises `UnsupportedFeature` |
| `dns_nameservers`, `doh_template`, `dns_secure_mode`, `dns_rotate` | ✓ these only override fields; the engine **always** uses Chromium's own resolver (which queries the DNS HTTPS record by default); a `doh_template` without `dns_secure_mode` raises rather than being ignored. **Measured**: with `dns_nameservers` pointing at a local port 53, the server really receives both A (1) and HTTPS (65) queries; a `doh_template` pointing at `127.0.0.1:1` fails under `dns_secure_mode=2` and falls back to system resolution under `=1` -- which is exactly the difference between secure and opportunistic |
| `platform`, `max_sockets_per_group`, `hsts_bypass`, `proxy_chain` | ✓ all engine-level and part of the engine cache key; `platform` also decides the UA group and the client hints |
| `cert=("c.pem", "k.pem")`, `cert="combined.pem"`, `set_client_certificate()` | ✓ sent only when the server issues a CertificateRequest, so the ClientHello fingerprint is unchanged |
| `allow_redirects`, `history`, final URL | ✓ (`max_redirects=` removed; the ceiling is 20) |
| `stream=True`, `iter_content`, `iter_lines`, `raw` | ✓ |
| `aiter_content`, `aiter_lines`, `atext`, `acontent`, `session.stream(...)` | ✓ |
| `hooks={"response": ...}`, `prepare_request`, `send`, `mount`, custom adapters | ✓ |
| `Request`, `PreparedRequest`, `codes`, `CaseInsensitiveDict`, `Headers`, `Cookies` | ✓ |
| `HTTPBasicAuth`, `HTTPProxyAuth`, `AuthBase` | ✓ |
| The whole `requests.exceptions` tree plus curl-cffi's leaves | ✓ |
| `impersonate`, `http_version`, `retry`/`RetryStrategy`, `raise_for_status=True` | ✓ |
| `base_url`, `discard_cookies`, `default_encoding`, `content_callback` | ✓ |
| multipart through `files=`, `ExtraFingerprints(header_order=..., form_boundary=...)` | ✓ |
| `HTTPDigestAuth` | Partial: drive it with `parse_challenge()`/`handle_401()` |
| `HTTPAdapter(pool_connections=..., pool_maxsize=...)` | Accepted but inert: Chromium owns the pools |
| `ja3`, `akamai`, `perk`, TLS/HTTP2 fields of `extra_fp` | ✗ raises `UnsupportedFeature` |
| `interface`, `doh_url`, `curl_options`, `max_recv_speed` | ✗ raises `UnsupportedFeature` |
| `referer=` / a `Referer` header | ✗ raises `UnsupportedFeature` (Chromium strips it) |
| Persistent cookie/cache files, the low-level `Curl` handle, `CurlOpt`/`CurlInfo` | ✗ |

Deliberate behavioural differences:

- Module-level `chrome_client.get(...)` shares one process-wide `Session`, and so
  shares a connection pool and cookie store. requests builds a new `Session` per
  call; here a Session is an entire Chromium `URLRequestContext`, so per-call
  construction would cost threads and memory. Build an explicit `Session` when you
  need isolation, or call `close_shared_session()`.
- `session.headers` starts empty. requests seeds `User-Agent`, `Accept`,
  `Accept-Encoding` and `Connection`; here Chromium sends only `Host`,
  `Connection`, `User-Agent` and `Accept-Encoding` on its own. `Accept` is sent by
  neither, so injecting `Accept: */*` is visible to a fingerprinter (real Chrome
  does send it — see the client hints note above).
- `Response.raw` is a file object exposing `read`/`stream`/`close`, not a urllib3
  `HTTPResponse`.
- `Response.ok` uses the requests rule (`status_code < 400`), not curl-cffi's
  200–399.

### Core size and directories

Cores range from 7.3 MB to 9.4 MB (macOS ARM64 smallest, Windows x86_64 largest,
where static MSVC/UCRT adds about 2 MB). The IDNA-only ICU dataset is linked in,
so no external `icudtl.dat` travels with the library, and the unused disk cache
backends are not linked at all. `tools/audit-core-*.sh` enforces a per-platform
size ceiling, so a regression fails the build.

| Core directory | Rust target | Core file |
| --- | --- | --- |
| `linux-x86` | `i686-unknown-linux-gnu` | `libminicronet.so` |
| `linux-x86_64` | `x86_64-unknown-linux-gnu` | `libminicronet.so` |
| `linux-armv7` | `armv7-unknown-linux-gnueabihf` | `libminicronet.so` |
| `linux-arm64` | `aarch64-unknown-linux-gnu` | `libminicronet.so` |
| `windows-x86` | `i686-pc-windows-msvc` | `minicronet.dll` + `minicronet.lib` |
| `windows-x86_64` | `x86_64-pc-windows-msvc` | `minicronet.dll` + `minicronet.lib` |
| `windows-arm64` | `aarch64-pc-windows-msvc` | `minicronet.dll` + `minicronet.lib` |
| `macos-x86_64` | `x86_64-apple-darwin` | `libminicronet.dylib` |
| `macos-arm64` | `aarch64-apple-darwin` | `libminicronet.dylib` |


## Repository and verification

| Path | Contents |
| --- | --- |
| `core/abi/` | Stable C ABI v15 |
| `core/binaries/` | Nine audited Core targets |
| `crates/minicronet/` | Rust safety layer, streams and lifetimes |
| `bindings/python/` | Python 3.9–3.15 binding and the shared facade |
| `bindings/python36/` | Separate Python 3.6–3.8 abi3 binding (same facade) |
| `bindings/python/tests/` | `test_stability.py` for lifetimes and concurrency; `test_compat.py` for the requests/curl-cffi surface |
| `docs/` | Build, platform, ABI, compatibility and audit docs |

The regression suites need an audited Core:

```bash
LD_LIBRARY_PATH=core/binaries/linux-x86_64 PYTHONPATH=bindings/python \
  python -m unittest discover -s bindings/python/tests -p "test_*.py"
```
