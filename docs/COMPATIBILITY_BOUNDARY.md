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
  redirects, and load flags. The Core always uses an in-memory backend.

## ABI consequences

- ABI v7 accepts only the current complete configuration and callback
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
