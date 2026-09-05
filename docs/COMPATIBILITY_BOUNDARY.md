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
| `referer=` or a `Referer` header | Chromium owns the referrer and strips the extra header (verified empirically) |
| `impersonate` outside `chrome_99`–`chrome_152`, or a non-Chromium family | The Core registers Chromium desktop profiles only |

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
- `Response.raw` is a small file-like view over the Core body stream, not a
  urllib3 `HTTPResponse`.

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
