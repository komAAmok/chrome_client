# Immutable ProfileContext design

`ProfileContext` is an internal value created once for an Engine. It binds one
audited Chrome profile to the exact Chromium/BoringSSL/QUICHE evidence used by
the Core. It is not a bag of request options and cannot change after network
initialization.

```text
Uninitialized -> Validated -> Frozen -> Running -> Closing -> Destroyed
```

`Validated` means the profile manifest, source evidence, and compile
capabilities match. In `Frozen`, feature state, TLS/H2/H3/WebSocket parameters,
and namespace IDs are immutable. Multiple Engines may select different
profiles in one process; each owns a separate Chromium `URLRequestContext`.

## Data model

```text
ProfileContext
  identity       profile id, Chromium/BoringSSL/QUICHE revisions, evidence digest
  feature_state  explicit feature values and compile-capability mask
  tls            protocol versions, cipher/group/sigalgs, GREASE, extensions,
                 ECH, ALPS, certificate compression, ticket/key-share policy
  alpn           H1/H2/H3 preference and ALPS settings
  http2          ordered SETTINGS, windows, stream limits, priority behavior
  http3_quic     H3 SETTINGS, transport parameters, flow control, 0-RTT policy
  websocket      Chromium handshake and CONNECT behavior
  namespaces     profile identity used in reusable network-state keys
```

The C++ representation stays inside the Engine. No STL object crosses the C
ABI. The migration policy covers `chrome_99` through `chrome_151`; each
selector must have a migrated, hash-checked profile table and a true
`wire_verified` gate before runtime support is enabled.

## Validation and isolation

Selection requires a known profile, hash-checked source evidence, and support
for every requested behavior. After selection, callers cannot override TLS,
H2, H3/QUIC, or WebSocket wire parameters, and cannot change the profile after
the first network initialization.

Every reusable state is scoped to the Engine's profile-bound context and
includes the profile namespace, destination origin, proxy chain, ALPN mode,
Network Anonymization Key, and privacy mode. This covers HTTP/2 and HTTP/3
session pools, TLS sessions, QUIC server config, Alt-Svc, proxy tunnels, and
the HTTP cache. State from one profile must never be reused by another.

The process-wide Chromium `FeatureList` is not mutated at runtime. A
historical difference must be represented by an immutable context or a
per-connection field; otherwise validation fails instead of using the current
default.

## Public boundary and acceptance gates

The public surface exposes only a profile selector and independent cache,
proxy, request, timeout, body, and redirect controls. TLS, H2, H3, QUIC, and
WebSocket tuning fields remain intentionally absent.

Before a profile is supported, source evidence and manifests must verify,
wire behavior must match, random values must vary per connection, isolation
must pass under concurrency, conflicting overrides must fail deterministically,
and no fixed RNG seed or silent fallback may exist in a release build.
