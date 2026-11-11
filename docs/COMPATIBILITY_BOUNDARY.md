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
- Forcing HTTP/1.1 (`http_version=v1`) changes the ClientHello in exactly two
  ways, both of them what Chrome does, and comparing such a request against an
  **h2** capture will always report those two as differences:

  | 位置 | h2 capture | our `v1` request | 为什么 |
  | --- | --- | --- | --- |
  | ALPN | `h2, http/1.1` | `http/1.1` | the pin forces a single protocol |
  | extension 17613 (ALPS) | present | absent | ALPS is only offered per protocol in `alpn_protos` |

  ALPS is not dropped by choice: `net/socket/ssl_client_socket_impl.cc` walks
  `ssl_config_.alpn_protos` and calls `SSL_add_application_settings` only when the
  same protocol has an `application_settings` entry, and
  `net/http/http_network_session.cc` installs that entry for `kProtoHTTP2` alone.
  With `alpn_protos` reduced to `http/1.1` there is nothing to match, so no ALPS
  extension is emitted. Chrome runs the same code and behaves the same way — the
  reference **h1** capture carries no ALPS extension for any release, while its
  **h2** capture does from 91 onward.

  `trust_anchors` (0xca34) is *not* in this set: it is protocol-independent and
  the reference h1 capture carries it from 152, as does this Core.
  `tools/audit-h1-alps-absence.py` pins all of it down.

- `MN_PROTOCOL_FORCE_H3` on a profile whose release has no reproducible HTTP/3
  behaviour (`chrome_86`–`chrome_92`: the reference capture set contains zero
  usable H3 samples for those majors) returns `MN_ERROR_PROFILE_UNSUPPORTED` at
  engine creation. Measured before the guard existed: a chrome_86 engine forced
  to H3 sent a QUIC ClientHello and got HTTP 200 back, i.e. it produced a
  fingerprint that release never had. Profile support is therefore two separate
  facts — `activation.h3_disabled` ("this release does not attempt QUIC by
  default", true for 86–122) and `activation.h3_unsupported` ("this release
  cannot do HTTP/3 at all", true for 86–92 only).
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

### Cookies: a caller write retires, a server write merges

The jar has two kinds of writer and they must not share one rule.

**Caller writes retire.** ``jar.set(name, value)`` and ``jar.update({...})`` are
assignments: a write of a name that already exists replaces the stored cookie
instead of adding a second one beside it. ``cookielib.CookieJar.set_cookie`` only
overwrites when the ``(domain, path, name)`` triple matches exactly, and upstream
requests inherits that: measured against requests 2.34.2, a jar holding ``a`` for
``.example.com`` plus a caller-written ``a`` for the default empty domain sends
``Cookie: a=server; a=1`` and raises ``CookieConflictError`` on ``jar["a"]``.
Neither the read nor the outgoing header agrees with the write, which for a
setter is the wrong answer.

| write | what it replaces |
| --- | --- |
| ``set("a", "1")`` / ``update({"a": "1"})`` | every entry named ``a``, whatever its domain |
| ``set("a", "1", domain="example.invalid")`` | the entries that domain covers **and** any domain-less ``a`` |

A write scoped to one domain still leaves a *different* domain alone:
``example.invalid`` and ``other.invalid`` keep their own ``a``, and RFC 6265 requires a
request to each to send only its own. A parent scope absorbs a narrower one
(``.example.com`` retires ``sub.example.com``), while the narrow write does not retire
the parent, which still covers hosts the new cookie never claimed.

**Server writes merge.** ``Set-Cookie`` reaches the jar through ``extract_cookies`` ->
``set_cookie``, and it must keep every distinct ``(domain, path, name)`` entry,
because a request to ``/sub/x`` is required to carry both ``a=sub; Path=/sub`` and
``a=root; Path=/``. An earlier revision applied the caller rule here too and collapsed
the two, and because the facade pushes the jar's ``Cookie`` line whenever it
disagrees with the Core, the loss reached the wire: measured, the next
``/sub/x`` request carried ``Cookie: a=root`` and the ``Path=/sub`` cookie was gone
(it is also a regression against v0.2.8, which kept both).

Two entry points, two rules: ``set()`` and ``update()`` call
``_retire_replaced``; a bare ``set_cookie`` inserts, exactly as ``cookielib`` does.
Path identity is the slot: ``/su`` never retires ``/sub``, and ``/`` never retires ``/sub`` either.
An RFC 6265 §5.1.4 path-match answers "would this request carry that cookie",
which is a different question, and using it here made ``set("a", "root", path="/")``
silently delete the cookie at ``/sub``.

The cost of the caller rule is that a domain-less write is a statement about the
name, not about a host, so every host-scoped ``a`` is retired along with it. The
alternative -- leaving them in place -- is what made the write unreadable and sent
the value twice.
### curl_cffi 参数审计：哪些在本项目里结构上无意义

curl_cffi 同时模拟 Chrome / Firefox / Safari，所以它的 `extra_fp` 里有些旋钮是为**别的浏览器**
或为**非 Chromium 的 TLS 栈**准备的。本项目只做 Chrome、且协议栈就是 Chromium 本身，
于是这些旋钮在这里没有对应物。下表逐项给出判定和依据。

判定分三类：

- **无意义**：Chrome 自己就这么做，或本项目根本没有那个机制。接受它只会让调用方以为
  自己改变了一个并不存在的自由度。
- **已由 profile 决定**：值真实存在，但由 `impersonate=` 选定的 profile 逐版本冻结，
  按请求覆盖必然破坏指纹一致性。
- **有意义**：facade 自己能实现，且不影响指纹。

| curl_cffi 参数 | 判定 | 依据 |
| --- | --- | --- |
| `tls_delegated_credential` | **无意义** | Delegated Credentials 是 Firefox/Safari 的机制，Chrome 不发；Core 里 `grep delegated` 零命中。默认值 `""` 表示「关」，而这个「关」是唯一状态。 |
| `http3_sig_hash_algs` | **无意义** | H3 的 signature_algorithms 与 TLS 层同一份（`profile_table_generated.h` 的 `signature_algorithms` span），不存在独立的 H3 集合。 |
| `http3_tls_extension_order` | **无意义** | H3 的扩展顺序由 profile 的 `tls_extension_order_policy` 决定（`boringssl_random_fisher_yates`），Chrome 每连接重排；没有「H3 专用顺序」这回事。 |
| `tls_record_size_limit` | **无意义** | Chrome 不发 record_size_limit 扩展。默认值 `0` 表示「不发」，也是唯一状态。 |
| `split_cookies` | **无意义** | 这是 curl 的 cookie 引擎概念（把 Set-Cookie 拆开解析）。本项目用 Chromium 的 `CookieStore`，拆分语义由它自己决定，facade 里 `split_cookies` 只出现在字段声明，无任何实现。 |
| `http2_no_priority` | **无意义** | 实测全部 72 个版本的 HEADERS 帧 priority 恒为 `(weight=256, depends_on=0, exclusive=1)`，Chrome 从不发「无 priority」。默认 `False` 表示「按 Chrome 发」，也是唯一状态。 |
| `http2_stream_weight` | **已由 profile 决定** | 同上：值恒为 256。要改就得改 HEADERS 帧，那是指纹的一部分。 |
| `http2_stream_exclusive` | **已由 profile 决定** | 同上：值恒为 1（exclusive）。 |
| `tls_grease` | **已由 profile 决定** | Chrome 自己发 GREASE，实测 6 条连接出现 8–9 个不同值，由 BoringSSL CSPRNG 每连接抽取。固定它会立刻变成「固定种子」，违反 §3。 |
| `tls_permute_extensions` | **已由 profile 决定** | Chrome 自己每连接重排扩展，实测 6 条连接 6 种顺序。固定顺序同样是固定种子。 |
| `tls_min_version` | **已由 profile 决定** | 由 `profile_.tls_version_min` 决定（86–97 是 `0x0301`，98+ 是 `0x0303`），`audit-tls-version-range.py` 断言表与线缆两侧。 |
| `tls_cert_compression` | **已由 profile 决定** | 实测全部版本的 compress_certificate 都是 `['brotli (2)']`，无版本差异。 |
| `tls_signature_algorithms` | **已由 profile 决定** | profile 的 `signature_algorithms` span，逐版本审计。 |
| `header_order` | **有意义** | facade 自己按给定顺序发头，不碰 TLS/H2 指纹。 |
| `form_boundary` | **有意义** | multipart boundary 由 Python 侧生成。 |

**净结论**：15 个字段里 **6 个无意义**（`tls_delegated_credential`、`http3_sig_hash_algs`、
`http3_tls_extension_order`、`tls_record_size_limit`、`split_cookies`、`http2_no_priority`）、
**7 个已由 profile 决定**、**2 个有意义**。

### 为什么保留这些字段而不是删掉

保留的理由是迁移成本，不是语义：curl_cffi 的调用方会把这些参数原样传过来，删掉字段会让
`ExtraFingerprints(...)` 直接抛 `TypeError`，而迁移者拿到的错误信息是「没有这个参数」，
而不是「这个参数在这里没有意义」。当前实现让它们**接受中性值、拒绝任何实际设置**，
并在拒绝时说清原因（`the Chromium profile owns the TLS and HTTP/2 fingerprint`）。

这符合 §8 的 fail-closed 承诺：无法忠实实现的选项必须抛错，不得静默忽略。

### Fingerprints are a profile choice, not a set of knobs

The pinned profile owns the TLS ClientHello, ALPN, HTTP/2 SETTINGS and the
priority signalling that goes with them, HTTP/3 transport parameters, and the
default header set with its order. That priority signalling has two halves,
and which one a profile gets is a measured version boundary: chrome_86-123
send the HEADERS dependency/weight fields and no RFC 9218 header, chrome_124
and later additionally send a `priority` request header. Neither half is a
knob -- `priority=` selects the request priority that feeds both, exactly as
it does in Chromium.
`ja3=`, `akamai=`, `perk=` and the TLS/HTTP2 fields of `extra_fp` raise. Only
`extra_fp.header_order` and `extra_fp.form_boundary` are honoured, because the
facade implements both itself.

`utils.default_headers()` returns an empty mapping: `requests` seeds
`User-Agent`, `Accept`, `Accept-Encoding` and `Connection` unconditionally, and a
constant set here would overwrite or duplicate what the profile emits -- an
injected `Accept: */*` is visible to a fingerprinter.

What the facade does add is **profile data, not a constant**: when
`impersonate=` names a pinned major, `browser_headers.browser_headers()` supplies
the exact ordered header list that release's own build sent, taken from
`profiles/chrome_<major>/h1/captures.json`, and
`BaseSession._browser_headers` merges it *under* the caller's headers.  All 72
pinned profiles reproduce their capture byte for byte, order included.

Three limits on that:

* **The platform follows the host.** The captures are from a Windows desktop;
  `User-Agent`'s parenthesised group and `sec-ch-ua-platform` are rewritten to
  what the Core itself derives from `BUILDFLAG(IS_WIN/IS_MAC)` and
  `ARCH_CPU_*`.  A `"Windows"` hint beside an `X11; Linux` User-Agent is a louder
  contradiction than a missing hint.
* **`Sec-Fetch-*` describe a top-level navigation**, which is what every capture
  is and what `session.get(url)` does.  A subresource fetch passes its own two or
  three fields; the rest of the set stays.
* **`impersonate=None` and `default_headers=False` inject nothing.** A session
  that never claimed a profile must not start looking like Chrome.

These headers travel as extra headers, not through Chromium's URLLoader factory,
so Chromium's own `Sec-Fetch` policy and client-hint negotiation (the `Accept-CH`
response header) do not apply to them: a server that only wants hints after
`Accept-CH` still receives them.  Moving the injection into the Core is the
ABI v9 item in `docs/NEXT_STEPS.md`.

### Options that fail closed

| Option | Why |
| --- | --- |
| `ja3`, `akamai`, `perk`, TLS/HTTP2 `extra_fp` fields | The profile owns the fingerprint |
| `cert` (client certificates) | **ABI v9 支持**：`mn_engine_set_client_certificate` / `mn_engine_clear_client_certificate`。带口令的私钥仍拒绝 |
| `interface` | 仍无 ABI 字段，继续抛 `UnsupportedFeature` |
| `doh_url` | **ABI v9 支持**：`doh_template` + `dns_secure_mode`（模板非空而模式为 0 会被拒绝） |
| `curl_options` | No libcurl handle exists |
| `max_recv_speed` | Chromium owns transfer pacing |
| `referer=` or a `Referer` header | Routed to `URLRequest::SetReferrer`, so it is sent like a real Chrome's |
| `impersonate` outside `chrome_86`–`chrome_157`, or a non-Chromium family | The Core registers Chromium desktop profiles only |

### Headers: hop-by-hop fields are refused outright

RFC 9110 §7.6.1 names the hop-by-hop fields, and none of them can be honoured
through extra headers: Chromium owns connection lifetime, and
`SetExtraRequestHeaders` never reaches the connection pool.

| field | what happens if accepted |
| --- | --- |
| `Connection` | measured: `: close` was sent but three requests still shared one socket |
| `Proxy-Connection` / `Keep-Alive` / `TE` / `Trailer` / `Upgrade` | nothing, for the same reason |

On HTTP/2 and HTTP/3 the situation is worse than "no effect":
`net/spdy/spdy_http_utils.cc:214-221` skips `connection`,
`proxy-connection` and `transfer-encoding` when building the header block, and
RFC 9113 §8.2.2 / RFC 9114 §4.2 forbid all of them. So accepting a value would
make the behaviour depend on the negotiated protocol -- silently.

All six are now refused with `UnsupportedFeature`, whether set or removed.
`transfer-encoding` and `content-length` stay settable: they are body framing,
which the caller does control, and removing them was already fail-closed.

### Headers: what a caller can add, override and remove

A caller may add any header, and may override any header the Core itself would
send. A caller may **not** remove one the Core owns: ``SetExtraRequestHeaders``
only adds, so there is no ABI call that deletes ``User-Agent``, ``Accept-Encoding``
``Accept-Language``, ``Host``, ``Connection`` or the body-framing headers.

An earlier revision accepted ``headers={"User-Agent": None}`` and sent the profile UA
anyway, with no error. Measured, both of these left the original value on the
wire:

```
headers={"User-Agent": None}       -> User-Agent: Mozilla/5.0 (X11; Linux x86_64) ...
headers={"Accept-Encoding": None}  -> Accept-Encoding: gzip, deflate, br, zstd
```

That is the degradation this project refuses elsewhere, so the removal now
raises ``UnsupportedFeature`` naming every offending header. Everything else keeps
requests semantics: removing a caller-owned header still removes it, and
overriding a Core-owned one still overrides it (``headers={"User-Agent": "mine/1.0"}``
reaches the wire as ``mine/1.0``).

### Timeouts: one deadline, and a pair that cannot be split

ABI v8 carries a single deadline per request, and Chromium owns both the
connect and the read side of it. A ``timeout=(connect, read)`` pair therefore has no
faithful representation.

An earlier revision took ``max(connect, read)``, which silently deleted the only thing
the pair is for. Measured against a server that accepts and never answers:

| call | requests waits | this build used to wait |
| --- | --- | --- |
| ``timeout=(0.2, 10)`` | 0.2s (``ConnectTimeout``) | 10.0s (``Timeout``) |
| ``timeout=(10, 0.2)`` | 0.2s (``ReadTimeout``) | 10.0s (``Timeout``) |

A pair with two *different* values now raises ``UnsupportedFeature``; a pair whose
halves agree is one deadline by definition and is accepted. Silently honouring
the larger half is the degradation this project refuses elsewhere
(``max_redirects``, ``extra_fp``).

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

**TLS 0-RTT is off over TCP and on over QUIC.** These are two different
switches, and only the first one is closed here:

- TCP: `engine.cc` sets `enable_tls13_early_data = false`, matching the
  upstream default at the pinned revision, so no early data is sent on a
  resumed TLS connection.
- QUIC: `disable_tls_zero_rtt` keeps its default `false`
  (`net/quic/quic_context.h`), and the QUICHE flag behind it
  (`quic_disable_client_tls_zero_rtt`) also defaults to `false`, so a QUIC
  connection to a previously visited origin can send 0-RTT early data.

RFC 9001 recommends restricting 0-RTT to safe methods because early data can
be replayed, and nothing in `net/` gates it by HTTP method: searching for a
method check around early data finds none. The exposure is therefore the same
one Chrome has, which is why it is left alone -- turning 0-RTT off would make
the QUIC handshake differ from the browser the profile claims to be. A caller
who cannot accept replay risk on a non-idempotent request should pin
`http_version="v2"` (or `"v1"`) so the request cannot reach QUIC at all.

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

**`discard_cookies=True` costs one connection per request.** That option has to
reach a cookie store with nothing in it, and ABI v8 exposes no way to clear one,
so the facade builds a throwaway Engine for the call and drops it afterwards
(`sessions.py`, `_one_shot_slot`). A new Engine is a new
`URLRequestContext`: no TLS session cache, no socket pool, no HTTP/2 session.
Measured over 20 requests to one origin:

| call shape | TCP connections |
| --- | --- |
| plain `get()` | 1 |
| `discard_cookies=True` on every request | 20 |

Editing the session cookie jar between requests does **not** have this cost. It
rebuilds the Engine once, on the first request whose jar disagrees with what the
Core store holds; afterwards the replacement Engine mirror is empty, so the
facade takes the "the Core has nothing to override" path and reuses it. Measured:
20 requests with a jar edit before each one used 2 connections, not 20.
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
the per-hop redirect events the Core reports. `allow_redirects=False` uses
manual redirect mode: the Core defers the hop and the facade returns the 3xx.

A `max_redirects` tighter than Chromium's 20 is **refused**, and so is any
other numeric value. The facade used to re-issue each hop
from Python to make a tighter cap true, which put redirect semantics in the
binding layer where they drifted from Chromium's four measurable ways: a
`Referrer-Policy` on the 302 was not applied to the next hop, the previous
hop's `Set-Cookie` was not sent, `Response.redirect_count` came back 0, and a
302 turned a `PUT` into a `GET` although `net/url_request/redirect_info.cc`
only rewrites `POST`. Since ABI v8 cannot lower
`net::URLRequest::kMaxRedirects`, the only honest answer is
`UnsupportedFeature`; `max_redirects=None` means "Chromium's own ceiling".

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

- `reason` is read from the status line the Core returns, so a non-standard
  phrase survives: a server answering `200 Totally Custom` is reported verbatim.
  The standard table is the fallback for a status line that carries no phrase at
  all. (An earlier revision of this document claimed the opposite -- that the
  phrase was not visible -- which the measurement above contradicts.)
- `http_version` 现在是真值，不再靠状态行推断。ABI v9 的
  `mn_request_negotiated_protocol` 读 `HttpResponseInfo::connection_info`
  （`net/http/http_response_info.h:161`），经 `HttpConnectionInfoToCoarse`
  （`net/http/http_connection_info.cc:127`）归类，所以 h2 与 h3 各自可辨。
  0.9/1.0/1.1 三个值读原始枚举，因为粗化会把它们并成一个。

  仍然返回 `None` 的情形只有一种：`connection_info` 是 `kUNKNOWN`，那是 Chromium
  自己不知道（命中 HTTP 缓存时如此）。把它报成 `HTTP/1.1` 才是这条边界要消除的谎报，
  所以 `MN_HTTP_VERSION_UNKNOWN` 被单独定义并原样透出。

- `ech_status` 的 `FAILED` 不可达：`net/ssl/ssl_info.h` 只记录一个
  `encrypted_client_hello` 布尔值，「尝试后被拒」与「没尝试」都是 false。桌面端实际
  返回 `not_attempted`，含义是「没有 ECHConfigList」，不是「平台关闭了 ECH」。
- `Response.raw` is a small file-like view over the Core body stream, not a
  urllib3 `HTTPResponse`.

- **Truncated compressed bodies are not an error.** A `Content-Encoding: gzip` body
  that ends early yields `200` with whatever decompressed, matching requests byte for
  byte: 20 bytes of a 70-byte gzip stream give an empty body, 40 give 12 bytes,
  60 give 6726. Chromium's `GzipSourceStream` treats end-of-input in
  `STATE_GZIP_FOOTER` as success and requests behaves the same way, so this is the
  faithful answer rather than a defect to be "fixed" into an exception.
- **A body that is not its declared encoding raises `ContentDecodingError`.**
  Chromium reports `ERR_CONTENT_DECODING_FAILED` (-330); the table used to carry only
  -371 (decoder *creation* failure), so every real decoding error surfaced as
  `ConnectionError` and `except ContentDecodingError` never matched.
- `cache_mode="only_if_cached"` raises `CacheMiss` when the entry is absent,
  which is `ERR_CACHE_MISS` (-400). It used to map to no class, so the miss arrived as a
  bare `RequestException` and could not be caught specifically.

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

`max_clients` bounds in-flight requests, but only the ones whose body the facade
reads itself: a `stream=True` request returns at the headers and then streams
outside the gate. Measured with `max_clients=1` and two concurrent streaming
requests, the server saw both at once. The gate is a bound on buffered responses,
not on sockets, and the six-connection ceiling above is what actually limits
per-host concurrency.

An engine is torn down with its `Session`: `close()` is the intended path and
`__del__` is only a safety net. A session that is never closed leaves one
Chromium `URLRequestContext` resident -- measured, thirty unclosed sessions hold
about 6 MiB and six descriptors against a flat baseline for closed ones -- so
use `with Session(...)` or call `close()`.
