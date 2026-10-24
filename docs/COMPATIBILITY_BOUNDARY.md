# Chrome compatibility boundary

`chrome_client` targets Chromium desktop network behavior, not the complete
Chrome browser process. Chromium `net`, BoringSSL, and QUICHE remain the
owners of HTTP/1.1, HTTP/2, HTTP/3, TLS, certificate validation, redirects,
WebSocket, proxy handling, and connection reuse.

The Core retains the pinned Chromium defaults for Brotli, Zstd, HSTS preload,
DNS caching, connection pools, TLS sessions, Alt-Svc, the Chrome Root Store,
the transport security state, proxy resolution, HTTP authentication, and the
HTTP network session. It supports arbitrary HTTP methods, fixed and chunked
uploads, streamed responses, request cache modes, cancellation, and deadlines.

Each Engine owns an in-memory HTTP cache and CookieStore. These are discarded
when the Engine is released. Requests use Chromium request isolation with the
request origin as the top-frame origin, preserving same-site cookie behavior
without compiling browser frame policy.

The minimal Core deliberately excludes persistent HTTP cache and cookie
databases, Chrome Profile state, Blink resource caching, Fetch/CORS
enforcement, Service Workers, browser UI, PAC, extensions, and renderer
metadata. Compatibility claims therefore apply to the pinned Chromium
network stack, not byte-for-byte equivalence with a complete page load.

Chrome Variations, Mojo, and browser-process state are not part of the Core.
Historical profiles must be backed by source and wire evidence; unsupported
behavior fails closed instead of silently using current Chromium defaults.

## Redirect, cookie, and cache semantics

- Chromium `URLRequest` owns redirect limits, URL validation, method rewriting,
  upload replay, referrer updates, and sensitive-header removal.
- Chromium `CookieMonster` owns cookie parsing, selection, storage, and
  attachment. The store is memory-only and does not reproduce Chrome Profile
  persistence or page-frame policy.
- Chromium `HttpCache` owns freshness, validators, `Vary`, authorization,
  redirects, and load flags. The Core always uses an in-memory backend, and the
  disk backends are not linked in at all: `CreateCacheBackendImpl` returns
  `ERR_NOT_IMPLEMENTED` for a disk cache type under `BUILDFLAG(MINICRONET_BUILD)`,
  which lets the linker drop the blockfile and simple backends
  (-1,887,180 bytes across the eight targets). No ABI field selects a cache
  directory, so that path is unreachable.

## ABI consequences

- ABI v8 accepts only the current complete configuration and callback
  structures; older prefixes are rejected.
- Engine creation freezes profile, User-Agent, cache mode, protocol mode, and
  TLS verification policy.
- `MN_PROTOCOL_NATIVE` preserves Chromium protocol selection. Forced H1/H2/H3
  modes fail closed and never fall back to another version.
- Handles use balanced, thread-safe retain/release ownership. A successful
  start owns one internal reference until its single terminal callback.
- Engine-level cache and TLS settings are immutable; request cache modes only
  affect the individual request.
- Redirect rewriting, cookie handling, cache validation, and upload framing
  remain Chromium responsibilities.

## Python API boundary

The Python facade offers the `requests` and `curl_cffi` shapes over this Core.
Where an option cannot be honoured faithfully it raises `UnsupportedFeature`
rather than being ignored, because silently dropping a fingerprint or transport
setting reports a fidelity this build does not have.

### Engine-level settings become an Engine choice

`impersonate`, `proxy`, `verify`, `http_version`, `user_agent`,
`accept_language` and `cache` are frozen at Engine creation, so a per-request
override selects a *different* Engine. Sessions keep those Engines in a bounded
per-session cache (`max_engines`, default 8): an override costs one Engine the
first time and nothing afterwards. Engines are never shared between sessions --
two sessions with identical configuration must not see each other's cookies.

### Cookies: two stores, one of which wins

Chromium's `CookieMonster` parses `Set-Cookie`, applies domain, path, `SameSite`
and `Secure` policy, and attaches the `Cookie` header itself. ABI v8 exposes no
handle to it: cookies cannot be read, written, cleared or persisted through the
ABI. Two consequences the facade works around:

- The Core's store *overrides* a caller-supplied `Cookie` header for any URL it
  already holds a cookie for, so sending that header is only meaningful when the
  store would send nothing.
- A caller edit that conflicts with the store can only take effect by discarding
  the store, which means using a structurally identical Engine with an empty one.
  `Session` does that automatically when `session.cookies` diverges from what it
  last mirrored.

`session.cookies` is a real `RequestsCookieJar`: the facade mirrors every
`Set-Cookie` from the final response and from each redirect hop, so the jar
carries domain, path, secure and expiry metadata and `get_dict(domain=...)`
answers from it. Cookie state survives a proxy or profile change because the jar
is re-sent to the new Engine.

### Fingerprints are a profile choice, not a set of knobs

The pinned profile owns the TLS ClientHello, ALPN, HTTP/2 SETTINGS and priority
frames, HTTP/3 transport parameters, and the default header set with its order.
`ja3=`, `akamai=`, `perk=` and the TLS/HTTP2 fields of `extra_fp` raise. Only
`extra_fp.header_order` and `extra_fp.form_boundary` are honoured, because the
facade implements both itself.

For the same reason the facade adds **no** default headers.
`utils.default_headers()` returns an empty mapping: `requests` seeds
`User-Agent`, `Accept`, `Accept-Encoding` and `Connection`, and doing that here
would overwrite or duplicate what the profile emits -- an injected `Accept: */*`
is visible to a fingerprinter.

### Options that fail closed

| Option | Why |
| --- | --- |
| `ja3`, `akamai`, `perk`, TLS/HTTP2 `extra_fp` fields | The profile owns the fingerprint |
| `cert` (client certificates) | No ABI v8 field |
| `interface`, `doh_url` | No ABI v8 field |
| `curl_options` | No libcurl handle exists |
| `max_recv_speed` | Chromium owns transfer pacing |
| `referer=` or a `Referer` header | Routed to `URLRequest::SetReferrer`, so it is sent like a real Chrome's |
| `impersonate` outside `chrome_99`–`chrome_155`, or a non-Chromium family | The Core registers Chromium desktop profiles only |

### Certificate errors

A rejected certificate reports which check failed: `ERR_CERT_DATE_INVALID`
(-201) for expiry, `ERR_CERT_COMMON_NAME_INVALID` (-200) for a name mismatch,
`ERR_CERT_AUTHORITY_INVALID` (-202) for an untrusted issuer. All raise
`CertificateVerifyError`.

This needs a Core override. Chromium hands the real code to
`URLRequest::Delegate::OnSSLCertificateError` and relies on the delegate to end
the request; the base implementation calls `URLRequest::Cancel()`, which is
`DoCancel(ERR_ABORTED, SSLInfo())` and discards both the code and the SSLInfo.
The Core therefore overrides the method and calls
`CancelWithSSLError(net_error, ssl_info)`, the same call
`services/network/url_loader.cc` makes when it decides not to proceed. Core
builds without that override report every certificate failure as `ERR_ABORTED`
(-3), indistinguishable from a caller cancellation.

There is no per-request bypass: the Core never offers to proceed despite a
certificate error, so `fatal` (an HSTS or policy pin) has nothing to gate.
Relaxing verification is an Engine-level decision taken before the request
starts -- `verify=False` (an always-OK verifier plus
`ignore_certificate_errors`, which bypasses the delegate entirely) or
`verify="/path/ca.pem"` (an additional trust anchor).

### Proxies

Precedence, highest first: the per-call `proxy=`, the per-call `proxies=`, the
session's `proxy=`, the session's `proxies`, then the environment
(`HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`, minus `NO_PROXY`) when `trust_env` is on.

**Chromium implicitly bypasses localhost and link-local addresses for every proxy
configuration.** This is upstream Chrome behaviour, not a binding choice:
`net/proxy_resolution/proxy_host_matching_rules.cc` unconditionally appends
`SubtractImplicitBypassesRule`, so a request to `127.0.0.1`, `::1` or a `.local`
name goes **direct** even when a proxy is configured, and nothing is logged. ABI v8
exposes only `proxy_rules` and has no `bypass_rules` field, so the `<‑loopback>`
escape hatch cannot be passed through either. To force loopback traffic through a
proxy you currently need a non-loopback alias for the target (a hosts entry, or the
`all://`/scheme mapping plus a name that resolves off-host); adding a bypass field
is tracked in `NEXT_STEPS.md`.

Proxy failures report the Chromium error name, because the code alone does not say
what to fix. The names are checked against `net_error_list.h` by
`tools/audit-net-error-names.py`; the ones that matter here are
`ERR_TUNNEL_CONNECTION_FAILED` (-111, the proxy refused or could not reach the
origin), `ERR_PROXY_CONNECTION_FAILED` (-130, the proxy itself is unreachable),
`ERR_PROXY_AUTH_UNSUPPORTED` (-115) and `ERR_PROXY_AUTH_REQUESTED` (-127) for a
proxy demanding credentials, `ERR_NO_SUPPORTED_PROXIES` (-336), and
`ERR_MANDATORY_PROXY_CONFIGURATION_FAILED` (-131).

**-111 does not mean the proxy is broken.** Chromium turns *any* CONNECT response that is not 200 or 407 into `ERR_TUNNEL_CONNECTION_FAILED` (`http_proxy_client_socket.cc`, `DoProcessResponseCode`: the default branch is deliberate, so a proxy cannot impersonate the origin by returning 403/404/501 bodies). A proxy answering 403, 404, 429, 500, 502, 503 or 504 to CONNECT all surface as -111, so the code alone cannot distinguish "proxy is down" from "proxy refuses this destination" or "proxy is rate limiting". Check the proxy logs, not this error name.

**Every redirect mode reports the same error name.** The manual-redirect path (`allow_redirects=False`, which `Session.head` also uses) reads its result from `ResponseFuture`, and it used to unwrap `RequestError` down to its `.error` category -- so a failed tunnel arrived as the single word `Proxy` while the follow-redirect path named `ERR_TUNNEL_CONNECTION_FAILED (net error -111)`. The numeric code lives only in the `net_error` field (`crates/minicronet/src/request.rs`), and it is now carried through every path: sync, async, streaming, WebSocket, and the `chrome_client.requests` shim, which re-exports the same classes and therefore has no separate route.

**Rejected proxy credentials do not surface as the 407 the proxy sent.** Chromium
re-tries an auth challenge inside the same `URLRequest` and gives up after
`kMaxRestarts` (32), so a proxy that keeps answering 407 fails the request with
`ERR_TOO_MANY_RETRIES` (-375). The 407 body is drained as part of that retry
loop, which is why a rejected request reports a proxy error rather than a
response the caller can inspect.

`NO_PROXY` is applied by the facade *and* Chromium applies its own rules; the facade
decides which Engine (and therefore which proxy) to use, so a `NO_PROXY` match makes
it select a direct Engine rather than relying on Chromium to bypass.

### Proxy rule formats

**Chromium's own rule syntax is passed through untouched.** `ProxyRules::ParseFromString` (`net/proxy_resolution/proxy_config.cc`) already understands
three forms, and the facade used to rewrite all of them into a single
`scheme://host` URI -- which broke a configuration the Core parses correctly:

| Native form | Meaning | Previously |
| --- | --- | --- |
| `http=h:p` | one proxy for HTTP only | mangled into `http://http=h:p` |
| `http=h:p;https=h:p` | a different proxy per destination scheme | mangled |
| `http://a:p,http://b:p` | fallback list, tried in order | rejected |
| `direct://` | no proxy | rejected as an unknown scheme |

Those are validated (rule keys, `direct://` placement, embedded credentials) and
returned byte-identical. Everything outside them is the **extension layer**,
which only exists because Chromium rejects the input outright:

| Caller spelling | Handling | Why |
| --- | --- | --- |
| `http://`, `https://`, `socks://`, `socks4://`, `socks5://` | used as written | native `GetSchemeFromUriScheme` set |
| `socks5h://` | rewritten to `socks5://` | same behaviour under a name Chromium lacks |
| `socks4a://` | `UnsupportedFeature` | asks for remote resolution the Core cannot do |
| `quic://` | `UnsupportedFeature` | `ENABLE_QUIC_PROXY_SUPPORT` is off in this build |
| `host:port` (no scheme) | `http://host:port` | Chromium defaults a bare rule to HTTP |
| trailing `/`, `/path`, `#fragment`, newline, spaces | dropped | a rule is host:port only |
| `host:` (colon, empty port) | `InvalidProxyURL` | Chromium rejects it too |
| `host:0` | kept as port 0 | `ParsePort("0")` is a real port, not the default |
| trailing text after the authority | `InvalidProxyURL` | not part of a rule |
| two schemes (`http://socks5://h:p`) | `InvalidProxyURL` | a pool prefixed twice |
| unknown scheme, `?query`, port out of range, no host | `InvalidProxyURL` | unparseable |
| empty / whitespace only | no proxy, as in requests | requests semantics |

Rule keys accepted by the native form are `http`, `https`, `ftp` and `socks`
(`MapUrlSchemeToProxyListNoFallback` plus the special `socks` fallback key).
A `socks=` entry means SOCKS4 by default -- that is Chromium's reading, kept
as-is; write `socks=socks5://h:p` for SOCKS5.

`socks5h` is a spelling, not a mode: Chromium's SOCKS5 client always sends the
hostname to the proxy and lets it resolve, so plain `socks5://` already routes
DNS through the proxy. `socks4a` is the opposite case -- Chromium's SOCKS4
client resolves locally (`socks_client_socket.cc`, `DoResolveHost`) and ABI v8
exposes no remote-resolve option -- so the spelling raises rather than silently
sending the caller's DNS out through this machine. SOCKS4a is therefore **not
supported**; use `socks5://` when the proxy must resolve names.
**SOCKS5 username/password authentication is not supported.** Chromium's
`socks5_client_socket.cc` offers only the `0x00` ("no authentication") method,
so `proxy_auth` and userinfo have no effect on a SOCKS proxy; a proxy that
offers only `0x02` fails the handshake with `ERR_SOCKS_CONNECTION_FAILED` (-120).
Credential-bearing proxying requires an HTTP or HTTPS proxy.

**`proxy_auth` is scoped to the session's own proxy.** An Engine -- and
therefore a proxy -- is chosen per request, while `proxy_auth` is session state.
Attaching it unconditionally sent the session's username and password to
whatever endpoint a rotating pool selected for one call, so the credentials now
apply only when the request uses the proxy the session itself configured --
named either by `proxy=` or by the matching entry of `proxies`. A per-request
`proxy=` / `proxies=` is what the *call* uses, not what the session configured,
so it is not an owner of the credentials and receives none of them. A session
that configures neither has nothing to scope against, and the per-request
proxy is then the only candidate. Credentials written into the proxy URL
always win over `proxy_auth`.

**One Engine per distinct proxy.** The proxy is an Engine-level setting
(`engine.py`, `EngineConfig`), and an Engine is a whole Chromium
`URLRequestContext` with its own threads, socket pools, DNS cache and TLS session
cache. A session keeps `max_engines` of them (default 8) and evicts the
least-recently-used one, so a proxy pool larger than `max_engines` rebuilds a
context and opens a fresh TCP connection on **every** request instead of reusing
one. Measured on a 20-endpoint pool over 200 requests:

| `max_engines` | Chromium contexts built | proxy TCP connections | TIME_WAIT left behind |
| --- | --- | --- | --- |
| 8 (default) | 200 | 200 | +180 |
| 20 | 20 | 20 | +20 |

Set `max_engines` to at least the pool size. The connection-per-request pattern
is what turns a pool into `ERR_PROXY_CONNECTION_FAILED` (-130) after a while:
each abandoned connection leaves a `TIME_WAIT` entry, and once the ephemeral
port range is consumed a `connect()` to the proxy cannot get a source port.
Resource exhaustion of any kind surfaces as -130 on the proxy path -- a process
that has run out of file descriptors reports `ERR_PROXY_CONNECTION_FAILED` for
proxied requests while direct requests report a different code.

A failed attempt to reach the proxy (-130) or to open a tunnel through it (-111)
is retried **once** when the caller configured no retry policy, because the
origin never saw the request. The retry is skipped when the request cannot be
replayed (any non-idempotent method, and any streamed body, whose iterator is
already consumed), and for `ERR_TOO_MANY_RETRIES` (-375), which means the proxy
rejected the credentials. A configured policy replaces the default rather than
stacking on it: `retry=2` means three attempts however the request failed.
### Redirects

`allow_redirects=True` lets Chromium follow inside one `URLRequest`, which
preserves the original request's site-for-cookies and is the higher-fidelity
path; the facade reconstructs `Response.history` and the post-redirect URL from
the per-hop redirect events the Core reports. Because Chromium enforces its own
limit rather than the caller's, a `max_redirects` tighter than Chromium's 20
makes the facade re-issue each hop from Python instead. `allow_redirects=False`
uses manual redirect mode: the Core defers the hop and the facade returns the 3xx.

The facade's `max_redirects` default is `None`, which means "adopt Chromium's 20"
-- **not** requests' 30. `requests.models.DEFAULT_REDIRECT_LIMIT` is still 30 and
still importable, but adopting it as this session's default would promise 30 hops
from a build that stops at 20. The two numbers are different on purpose.

Chromium's own ceiling is **20 hops** (`net::URLRequest::kMaxRedirects`, installed
as the request's redirect limit in `url_request.cc`). Asking for more than that
cannot be honoured: a `max_redirects` at or above 20 raises `UnsupportedFeature`
instead of being silently clamped to 20. Clamping would tell the caller a limit
this build does not enforce, and the lie would surface later as an unexplained
`TooManyRedirects` at hop 21. `allow_redirects` defaults to the session setting.

### Response fields the ABI cannot provide

- `reason` comes from the standard status table, because ABI v8 carries only the
  numeric status. A non-standard reason phrase is not visible.
- `http_version` is `None` unless the status line proves the protocol. ABI v8
  reports a numeric status plus the header block, and Chromium normalizes every
  HTTP/2 and HTTP/3 response onto an `HTTP/1.1` status line
  (`net/spdy/spdy_http_utils.cc` builds `"HTTP/1.1 " + status`), so the status
  line cannot witness the protocol. Only `HTTP/1.0` is unambiguous (nothing
  normalizes down to 1.0). Reporting `HTTP/1.1` for an h2 connection -- which is
  what parsing the status line did -- claimed a fidelity this build cannot
  observe, so the field reports "not knowable" instead. Exposing the real value
  needs a new ABI field; it is tracked in `NEXT_STEPS.md`.
- `Response.raw` is a small file-like view over the Core body stream, not a
  urllib3 `HTTPResponse`.

### User-Agent platform token

The pinned profile owns the Chrome **version** in the User-Agent, because that is
the part the version-level wire evidence actually differs on. It does not own the
**platform** token: that follows the host the Core is built for, the way a real
Chrome build does, so `HistoricalUserAgent()` emits `X11; Linux x86_64` on Linux,
`Windows NT 10.0; Win64; x64` on Windows and `Macintosh; Intel Mac OS X 10_15_7`
on macOS (with `X11; Linux i686` / `X11; Linux aarch64` for the other Linux
architectures). The committed captures were taken from a Windows client, so a
Linux build does not reproduce their platform token -- a profile cannot claim a
platform the socket is not on. A caller that needs the captured token verbatim
passes `user_agent=` explicitly, which overrides the derived value.

### Concurrency

Blocking calls release the GIL, so a synchronous `Session` can be shared across
threads. The asyncio path creates no threads: the Core wakes the running loop and
each wakeup drains a batch of events.

`fork()` is unsafe once an Engine exists, because the process is multi-threaded
with Chromium threads holding locks, so process pools must use `spawn` or
`forkserver`. An engine cache that does cross a fork rebuilds itself, but that
only helps a child which reaches Python at all.

Chromium allows at most 6 concurrent HTTP/1.1 connections per host group. That
bounds per-host throughput regardless of how many requests are outstanding, and
it is browser semantics rather than a binding limit.
