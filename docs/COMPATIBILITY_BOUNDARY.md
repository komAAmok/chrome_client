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
| `impersonate` outside `chrome_99`–`chrome_153`, or a non-Chromium family | The Core registers Chromium desktop profiles only |

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

**Rejected proxy credentials do not surface as the 407 the proxy sent.** Chromium
re-tries an auth challenge inside the same `URLRequest` and gives up after
`kMaxRestarts` (32), so a proxy that keeps answering 407 fails the request with
`ERR_TOO_MANY_RETRIES` (-375). The 407 body is drained as part of that retry
loop, which is why a rejected request reports a proxy error rather than a
response the caller can inspect.

`NO_PROXY` is applied by the facade *and* Chromium applies its own rules; the facade
decides which Engine (and therefore which proxy) to use, so a `NO_PROXY` match makes
it select a direct Engine rather than relying on Chromium to bypass.

### Redirects

`allow_redirects=True` lets Chromium follow inside one `URLRequest`, which
preserves the original request's site-for-cookies and is the higher-fidelity
path; the facade reconstructs `Response.history` and the post-redirect URL from
the per-hop redirect events the Core reports. Because Chromium enforces its own
limit rather than the caller's, a `max_redirects` tighter than the default makes
the facade re-issue each hop from Python instead. `allow_redirects=False` uses
manual redirect mode: the Core defers the hop and the facade returns the 3xx.

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
