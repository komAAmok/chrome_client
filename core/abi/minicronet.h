#ifndef MINICRONET_H_
#define MINICRONET_H_

#include <stddef.h>
#include <stdint.h>

#if defined(_WIN32)
#if defined(MINICRONET_IMPLEMENTATION)
#define MN_EXPORT __declspec(dllexport)
#else
#define MN_EXPORT __declspec(dllimport)
#endif
#define MN_CALL __cdecl
#else
#define MN_EXPORT __attribute__((visibility("default")))
#define MN_CALL
#endif

#ifdef __cplusplus
extern "C" {
#endif

#define MN_ABI_VERSION 12u

/* tools/sync-core.sh installs core/exports/minicronet.lds, whose node name is
 * part of the link-time contract: every extension already built against this
 * library records the node name it needs. v9 grew the symbol set without
 * renaming that node, because renaming it would make every previously built
 * binding fail to load with "version MINICRONET_2 not found" while gaining
 * nothing -- MN_ABI_VERSION is what versions this interface. */

typedef struct mn_engine mn_engine_t;
typedef struct mn_request mn_request_t;
typedef struct mn_websocket mn_websocket_t;

/*
 * Each create call returns one caller-owned reference. Engine, request, and
 * WebSocket operations plus retain/release pairs are thread-safe; releasing a
 * handle after its final reference is invalid.
 * Start adds an internal reference until the single terminal callback. A
 * callback receives a borrowed handle; retain it before using it after the
 * callback returns.
 *
 * Callbacks for one object are serialized.
 *
 * Engine lifetime: a Request and a WebSocket each hold a reference to the
 * Engine they were created from (core/source/minicronet/request.h and
 * websocket.h both keep a scoped_refptr<Engine>), so the URLRequestContext
 * cannot be torn down while a request is registered against it, even if the
 * caller has already released its own engine handle. That is what keeps the
 * "Chromium asserts no URLRequest is still registered" abort unreachable.
 *
 * The requirement is therefore on the caller, not on the wire: finish or
 * cancel the work before releasing the engine, because an engine released
 * mid-flight leaves the context alive for the request but no longer part of
 * any cache the caller can reach -- the socket pool and TLS session cache it
 * was reusing are gone, and the in-flight request keeps them alive alone.
 *
 * Callback ordering: for one request, on_response and on_body precede the
 * single terminal on_complete. on_redirect is posted on the object's callback
 * runner while a redirect that fails closed (MN_REDIRECT_ERROR) completes on
 * the network runner, so no ordering is promised between on_redirect and
 * on_complete. Do not treat the redirect callback as a guarantee that the
 * request has not already finished.
 *
 * Struct compatibility: every config struct starts with size and version.
 * version must equal MN_ABI_VERSION; size must equal the size the caller
 * compiled against. A caller built against a newer header (larger struct, same
 * version) is rejected with MN_ERROR_INVALID_ARGUMENT, and a version mismatch
 * with MN_ERROR_UNSUPPORTED_ABI, so the two are distinguishable.
 */

typedef enum mn_result {
  MN_OK = 0,
  MN_ERROR_INVALID_ARGUMENT = 1,
  MN_ERROR_UNSUPPORTED_ABI = 2,
  MN_ERROR_OUT_OF_MEMORY = 3,
  MN_ERROR_INITIALIZATION_FAILED = 4,
  MN_ERROR_INVALID_STATE = 5,
  MN_ERROR_TIMEOUT = 6,
  MN_ERROR_CANCELED = 7,
  MN_ERROR_NETWORK = 8,
  MN_ERROR_TLS = 9,
  MN_ERROR_PROXY = 10,
  MN_ERROR_PROTOCOL = 11,
  MN_ERROR_REDIRECT = 12,
  MN_ERROR_CACHE_MISS = 13,
  MN_ERROR_PROFILE_CONFLICT = 14,
  MN_ERROR_PROFILE_UNSUPPORTED = 15,
} mn_result_t;

typedef enum mn_http_cache_mode {
  /* The engine owns an in-memory Chromium HttpCache. */
  MN_HTTP_CACHE_ENABLED = 0,
  MN_HTTP_CACHE_DISABLED = 1,
} mn_http_cache_mode_t;

/* A profile records what its release could actually do, and HTTP/3 is not
   uniform across them. MN_PROTOCOL_FORCE_H3 with a profile whose release has no
   HTTP/3 behaviour to reproduce (chrome_86..chrome_92) is rejected with
   MN_ERROR_PROFILE_UNSUPPORTED at engine creation. The Core does not fall back
   to another protocol and does not send a QUIC ClientHello that release never
   sent: an error is the only answer that does not misreport the fingerprint.
   MN_PROTOCOL_NATIVE and the H1/H2 pins are unaffected. */
typedef enum mn_protocol_mode {
  /* Chromium chooses the protocol and may use its normal fallback policy. */
  MN_PROTOCOL_NATIVE = 0,
  /* Restrict new connections to HTTP/1.1. */
  MN_PROTOCOL_FORCE_H1 = 1,
  /* Restrict new connections to HTTP/2; no HTTP/1.1 fallback. */
  MN_PROTOCOL_FORCE_H2 = 2,
  /* Restrict new connections to HTTP/3; no TCP fallback. */
  MN_PROTOCOL_FORCE_H3 = 3,
} mn_protocol_mode_t;

typedef enum mn_tls_verify_mode {
  /* Chromium's compiled Chrome Root Store and normal validation policy. */
  MN_TLS_VERIFY_CHROMIUM_DEFAULT = 0,
  /* Chromium roots plus the Engine's copied PEM CA bundle. */
  MN_TLS_VERIFY_CUSTOM_CA = 1,
  /* Explicitly skip certificate-chain and hostname validation. */
  MN_TLS_VERIFY_INSECURE = 2,
} mn_tls_verify_mode_t;

/* Which platform identity the engine presents. The User-Agent group, the
 * sec-ch-ua-platform hint and sec-ch-ua-mobile are three statements of one
 * fact, so all three derive from this single value. AUTO follows the host the
 * Core runs on, which is what every v8 engine did. */
typedef enum mn_platform {
  MN_PLATFORM_AUTO = 0,
  MN_PLATFORM_WINDOWS = 1,
  MN_PLATFORM_MACOS = 2,
  MN_PLATFORM_LINUX = 3,
} mn_platform_t;

typedef struct mn_engine_config {
  uint32_t size;
  uint32_t version;
  /* chromium_current only. Historical profiles derive and freeze Chrome UA. */
  const char *user_agent;
  size_t user_agent_length;
  const char *proxy_rules;
  size_t proxy_rules_length;
  const char *proxy_username;
  size_t proxy_username_length;
  const char *proxy_password;
  size_t proxy_password_length;
  mn_http_cache_mode_t http_cache_mode;
  const char *accept_language;
  size_t accept_language_length;
  /* Optional. Empty selects the compiled Chromium profile. */
  const char *profile_id;
  size_t profile_id_length;
  /* Optional. Must equal "minicronet/" + profile_id when supplied. */
  const char *profile_namespace;
  size_t profile_namespace_length;
  /* Frozen at engine creation. Invalid values are rejected. */
  mn_protocol_mode_t protocol_mode;
  /* Frozen at engine creation; never changes per request. */
  mn_tls_verify_mode_t tls_verify_mode;
  const uint8_t *custom_ca_pem;
  size_t custom_ca_pem_length;

  /* ---- v9 appends. A caller built against the v8 header is rejected by the
   * size/version check rather than having its tail read as these fields. Every
   * field below either takes effect or makes engine creation fail; none is
   * accepted and then ignored. ---- */

  /* Chromium MappedHostResolver rules, e.g. "MAP example.test 127.0.0.1" or
   * "MAP *.invalid ^NOTFOUND". Empty means no rules, which is v8 behaviour.
   * The syntax is parsed by Chromium's HostMappingRules, never here.
   *
   * A rule rewrites only the resolution result: the TCP destination changes
   * while SNI, the Host header and certificate verification keep using the URL
   * host. That is what makes a CDN address reachable without turning its
   * certificate into an IP-SAN certificate. */
  const char *host_mapping_rules;
  size_t host_mapping_rules_length;
  /* Comma-separated nameservers ("8.8.8.8:53,1.1.1.1"). Empty keeps the system
   * configuration. */
  const char *dns_nameservers;
  size_t dns_nameservers_length;
  /* DNS-over-HTTPS template ("https://dns.example/dns-query"). Empty disables
   * DoH. A non-empty template with dns_secure_mode == 0 is rejected, because
   * the two would disagree about whether DoH may be used. */
  const char *doh_template;
  size_t doh_template_length;
  /* Hosts the compiled HSTS preload list must not upgrade. Each entry must be
   * a single label: TransportSecurityState only matches single-label entries
   * and silently ignores anything else in a release build, so a multi-label
   * entry is rejected here instead of being accepted and dropped.
   *
   * There is deliberately no way to switch HSTS off entirely. See the note on
   * mn_engine_cookies_get's siblings below; the short version is that Chromium
   * CHECKs the TransportSecurityState pointer non-null and dereferences it on
   * every handshake, so "no HSTS" is not a state this build can reach. */
  const char *hsts_bypass_list;
  size_t hsts_bypass_list_length;
  /* Ordered proxy chain, "scheme://host:port" entries separated by commas.
   * Empty keeps the single-hop proxy_rules semantics. Supplying both this and
   * proxy_rules is rejected: they describe the same connection path twice. */
  const char *proxy_chain;
  size_t proxy_chain_length;
  /* 0=off 1=automatic 2=secure. Any other value is rejected. */
  uint8_t dns_secure_mode;
  uint8_t dns_over_tls;
  uint8_t dns_rotate;
  uint8_t dns_padding;
  /* 0 = follow Chrome's default cookie access semantics, which is what the
   * Core already does. Values 1..3 would be a policy engine of our own, which
   * is out of scope, so they are rejected rather than silently mapped onto a
   * default. */
  uint8_t cookie_samesite_override;
  /* 1 (the default) makes the Core send the captured browser request-header
   * set for the selected profile, with the User-Agent group and
   * sec-ch-ua-platform rewritten to describe this host. 0 suppresses it and
   * sends only the caller's headers; the caller's values always win over the
   * captured ones field by field. A historical profile with 0 is accepted: it
   * is how a caller asks for the release's TLS and HTTP/2 behaviour without
   * its header set. Chromium_current has no captured set, so the field has no
   * effect there. Values above 1 are rejected. */
  uint8_t inject_client_hints;
  mn_platform_t platform;
  /* ---- v11 appends. A caller built against the v10 header is rejected by the
   * size check in mn_engine_create (INVALID_ARGUMENT) rather than reading a
   * struct shorter than this one.
   *
   * Maximum TCP connections this engine keeps open to one origin. 0 (the
   * default) keeps Chromium's own limit, which is 6 for the normal pool and is
   * what every captured Chrome release does; a server counting concurrent
   * connections from one client never sees more than 6 on one origin, with or
   * without this field.
   *
   * A non-zero value raises this engine's ceiling, and that is a deliberate,
   * observable deviation from the browser being imitated: a real Chrome never
   * opens a 7th connection to one origin, so a caller setting 64 is asking to
   * be distinguishable on the wire in exchange for throughput. Throughput per
   * origin is bounded by this number times 1/RTT -- measured on this host, 6
   * connections at 20 ms RTT caps out near 240 req/s whether the caller runs 8
   * or 64 threads -- so this is the only knob that moves that ceiling.
   *
   * The value is per engine, not process-wide: Chromium's limit is a static
   * array, and drivers that keep several immutable profiles in one process
   * cannot each have a different one through a static.
   *
   * Values above the pool's soft cap (256) can never be reached and are
   * rejected rather than clamped, so a caller cannot believe it asked for
   * something it did not get. */
  uint32_t max_sockets_per_group;
} mn_engine_config_t;

/* Returns MN_ABI_VERSION compiled into the library. */
MN_EXPORT uint32_t MN_CALL mn_abi_version(void);
/* Returns the library version string, injected at build time from the
 * workspace version (tools/version.py + tools/sync-core.sh). */
MN_EXPORT const char *MN_CALL mn_version_string(void);

MN_EXPORT mn_result_t MN_CALL mn_engine_create(const mn_engine_config_t *config,
                                               mn_engine_t **out_engine);
MN_EXPORT void MN_CALL mn_engine_retain(mn_engine_t *engine);
MN_EXPORT void MN_CALL mn_engine_release(mn_engine_t *engine);

/* One cookie, as a read-only view. The strings are owned by the Core and stay
 * valid only for the duration of the call that produced them; copy what you
 * keep. Cookies are written into a caller-provided array, which is why this is
 * a fill-a-buffer call rather than an array of borrowed pointers. */
typedef struct mn_cookie {
  uint32_t size;
  uint32_t version;
  const char *name;
  size_t name_length;
  const char *value;
  size_t value_length;
  const char *domain;
  size_t domain_length;
  const char *path;
  size_t path_length;
  /* Seconds since the Unix epoch. Meaningful only when has_expiry is 1. */
  int64_t expiry_unix_seconds;
  /* 1 when the cookie carries an expiry, 0 for a session cookie. Kept separate
   * from the value so "no expiry" cannot be confused with "expired at the
   * epoch". */
  uint8_t has_expiry;
  uint8_t secure;
  uint8_t http_only;
  uint8_t same_site;
} mn_cookie_t;

typedef enum mn_cookie_same_site {
  MN_COOKIE_SAME_SITE_UNSPECIFIED = 0,
  MN_COOKIE_SAME_SITE_NO_RESTRICTION = 1,
  MN_COOKIE_SAME_SITE_LAX = 2,
  MN_COOKIE_SAME_SITE_STRICT = 3,
} mn_cookie_same_site_t;

/* Reads the engine's Chromium CookieMonster.
 *
 * url == NULL returns every cookie in the store; otherwise it returns the
 * cookies a request to that URL would carry. Cookies are written into
 * out_cookies, which the caller allocates for capacity entries. out_cookies may
 * be NULL when capacity is 0, which is how a caller asks for the count first.
 *
 * When the store holds more than capacity cookies this returns
 * MN_ERROR_INVALID_ARGUMENT and still writes the required count to out_count,
 * matching the out-param convention of the create functions.
 *
 * These calls block until the CookieMonster has answered. Do not call them from
 * inside a Core callback: that callback runs on the same thread they wait on. */
MN_EXPORT mn_result_t MN_CALL
mn_engine_cookies_get(mn_engine_t *engine, const char *url, size_t url_length,
                      mn_cookie_t *out_cookies, size_t capacity,
                      size_t *out_count);

/* Writes one cookie through the same path a Set-Cookie response takes:
 * CanonicalCookie::CreateSanitizedCookie followed by
 * CookieStore::SetCanonicalCookieAsync. The store applies its own domain, path
 * and size rules, so a cookie it rejects is rejected here too -- this is not a
 * way around the store's validation.
 *
 * source_url decides domain matching and origin-bound semantics and is
 * required. creation_unix_seconds may be 0 to use the current time. A return of
 * MN_OK means the store accepted the cookie. */
MN_EXPORT mn_result_t MN_CALL
mn_engine_cookies_set(mn_engine_t *engine, const mn_cookie_t *cookie,
                      const char *source_url, size_t source_url_length,
                      int64_t creation_unix_seconds);

/* Deletes every cookie matching (domain, path, name) and reports how many were
 * deleted through out_removed (pass NULL when the count is not needed).
 * Returns MN_OK even when nothing matched.
 *
 * Both the deletion and the count are complete when this returns: Chromium
 * runs the match predicate synchronously, once per cookie, so the count is
 * taken there rather than from the store's completion callback -- that
 * callback is posted onto the same thread this call runs on and could never be
 * observed here. */
MN_EXPORT mn_result_t MN_CALL
mn_engine_cookies_delete(mn_engine_t *engine, const char *domain,
                         size_t domain_length, const char *path,
                         size_t path_length, const char *name,
                         size_t name_length, size_t *out_removed);

/* Deletes every cookie in the store. */
MN_EXPORT mn_result_t MN_CALL mn_engine_cookies_clear(mn_engine_t *engine);

/* Registers a client certificate for one server. cert_pem is a chain of PEM
 * "CERTIFICATE" blocks, key_pem a single unencrypted PEM private key. The
 * certificate is used only when the server sends a CertificateRequest, so it
 * does not change the ClientHello of any other connection and cannot disturb
 * a pinned TLS fingerprint.
 *
 * Encrypted (passphrase-protected) keys are rejected with
 * MN_ERROR_INVALID_ARGUMENT: unlocking one would need a passphrase the ABI has
 * no field for, and sending no certificate silently is worse than failing.
 *
 * Chromium keeps the private key in process memory; there is no OS keystore
 * behind this call. Replacing the certificate for a server refreshes that
 * origin's connection group, so the idle connections opened under the old
 * certificate are closed instead of being reused. */
MN_EXPORT mn_result_t MN_CALL
mn_engine_set_client_certificate(mn_engine_t *engine, const char *host,
                                 size_t host_length, uint16_t port,
                                 const uint8_t *cert_pem, size_t cert_pem_length,
                                 const uint8_t *key_pem, size_t key_pem_length);

/* Forgets the client certificate registered for (host, port). Returns MN_OK
 * even when none was registered. */
MN_EXPORT mn_result_t MN_CALL
mn_engine_clear_client_certificate(mn_engine_t *engine, const char *host,
                                   size_t host_length, uint16_t port);

typedef struct mn_header {
  const char *name;
  size_t name_length;
  const char *value;
  size_t value_length;
} mn_header_t;

typedef struct mn_string {
  const char *data;
  size_t length;
} mn_string_t;

typedef enum mn_upload_mode {
  MN_UPLOAD_NONE = 0,
  MN_UPLOAD_FIXED = 1,
  MN_UPLOAD_CHUNKED = 2,
} mn_upload_mode_t;

typedef enum mn_cache_mode {
  MN_CACHE_DEFAULT = 0,
  MN_CACHE_VALIDATE = 1,
  MN_CACHE_BYPASS = 2,
  MN_CACHE_NO_STORE = 3,
  MN_CACHE_FORCE = 4,
  MN_CACHE_ONLY_IF_CACHED = 5,
} mn_cache_mode_t;

typedef enum mn_redirect_mode {
  MN_REDIRECT_FOLLOW = 0,
  MN_REDIRECT_MANUAL = 1,
  MN_REDIRECT_ERROR = 2,
} mn_redirect_mode_t;

typedef enum mn_request_priority {
  MN_REQUEST_PRIORITY_DEFAULT = 0,
  MN_REQUEST_PRIORITY_HIGHEST = 1,
  MN_REQUEST_PRIORITY_MEDIUM = 2,
  MN_REQUEST_PRIORITY_LOW = 3,
  MN_REQUEST_PRIORITY_LOWEST = 4,
  MN_REQUEST_PRIORITY_IDLE = 5,
} mn_request_priority_t;

typedef enum mn_read_disposition {
  /* Stop reading the response body. The Core delivers no further body data and
   * no terminal callback for body progress until mn_request_resume_read runs.
   * Nothing blocks: the callback returns and the Core simply stops issuing
   * reads, so a slow consumer holds no Core thread. */
  MN_READ_PAUSE = 0,
  /* Keep reading. */
  MN_READ_CONTINUE = 1,
  /* Keep reading, and take ownership of the buffer the callback was handed.
   *
   * The default (MN_READ_CONTINUE) lends the Core single read buffer for the
   * duration of the call, which forces every binding to copy the bytes out
   * before returning. Returning this instead transfers that one buffer: the
   * caller may read it after the callback returns and must hand it back with
   * mn_request_release_buffer, which is what lets a binding wrap the bytes
   * without a copy.
   *
   * The Core allocates a fresh buffer for the next read, so this trades the copy
   * for one allocation per chunk. Measured on a 64 MiB local stream that trade
   * is worth about 0.7% of the wall clock: the copy is 1.6% of the total and
   * the local transport is 95%. It is offered for bindings that must avoid the
   * copy for another reason -- a very large body, or a callback that cannot
   * afford the extra memcpy -- not as a throughput win.
   *
   * The buffer stays valid until released and is never reused by the Core while
   * it is outstanding. Releasing is required: the request cannot finish while a
   * buffer it handed out is still held.
   */
  MN_READ_TAKE_BUFFER = 2,
} mn_read_disposition_t;

/* The header block reaches the caller twice on purpose:
 *
 *   `headers` / `headers_length` is HttpResponseHeaders::raw_headers() with NUL
 *   bytes turned into newlines. It is the status line plus the fields as they
 *   arrived, which is what a caller reproducing a browser's byte stream wants.
 *
 *   `header_fields` / `header_field_count` is the same information already
 *   parsed. Every binding used to split the raw block itself, which meant four
 *   implementations of one grammar and a status line kept as a string so the
 *   reason phrase could be re-derived from it. The reason phrase is now its own
 *   parameter, and the fields are name/value pairs with duplicates preserved in
 *   arrival order -- Set-Cookie depends on that.
 *
 * Both are borrowed for the duration of the call. */
typedef void(MN_CALL *mn_request_response_fn)(void *user_data,
                                              mn_request_t *request,
                                              int status_code,
                                              const char *reason_phrase,
                                              size_t reason_phrase_length,
                                              const mn_header_t *header_fields,
                                              size_t header_field_count,
                                              const char *headers,
                                              size_t headers_length);
typedef mn_read_disposition_t(MN_CALL *mn_request_body_fn)(void *user_data,
                                                          mn_request_t *request,
                                                          const uint8_t *data,
                                                          size_t data_length);
typedef void(MN_CALL *mn_request_complete_fn)(void *user_data,
                                              mn_request_t *request,
                                              mn_result_t result,
                                              int net_error);
typedef void(MN_CALL *mn_request_redirect_fn)(
    void *user_data, mn_request_t *request, int status_code,
    const mn_header_t *header_fields, size_t header_field_count,
    const char *headers, size_t headers_length, const char *new_url,
    size_t new_url_length, const char *new_method, size_t new_method_length);

/* Header and body pointers are borrowed only for the callback duration. The
 * body pointer stays valid for the whole call but must not be retained: the
 * buffer is owned by the callback runner and released when the callback
 * returns. */

typedef struct mn_request_callbacks {
  uint32_t size;
  uint32_t version;
  void *user_data;
  mn_request_response_fn on_response;
  mn_request_body_fn on_body;
  mn_request_complete_fn on_complete;
  mn_request_redirect_fn on_redirect;
} mn_request_callbacks_t;

typedef struct mn_request_config {
  uint32_t size;
  uint32_t version;
  const char *url;
  size_t url_length;
  const char *method;
  size_t method_length;
  const mn_header_t *headers;
  size_t header_count;
  uint64_t timeout_ms;
  mn_request_callbacks_t callbacks;
  const uint8_t *body;
  size_t body_length;
  mn_upload_mode_t upload_mode;
  mn_cache_mode_t cache_mode;
  mn_redirect_mode_t redirect_mode;
  mn_request_priority_t priority;
  /* 1 = drop a caller-supplied Authorization when a redirect changes the
   * origin, which is what a browser does one layer above //net
   * (services/network/url_loader.cc:1025). 0 keeps the header, which is what
   * every ABI 11 caller got, so the default behaviour is unchanged. A hop that
   * stays on the same origin always keeps it. */
  uint8_t strip_authorization_on_cross_origin_redirect;
} mn_request_config_t;

MN_EXPORT mn_result_t MN_CALL
mn_request_create(mn_engine_t *engine, const mn_request_config_t *config,
                  mn_request_t **out_request);
MN_EXPORT void MN_CALL mn_request_retain(mn_request_t *request);
MN_EXPORT void MN_CALL mn_request_release(mn_request_t *request);
MN_EXPORT mn_result_t MN_CALL mn_request_start(mn_request_t *request);
/* Requests cancellation. Returns MN_ERROR_INVALID_STATE when the request has
 * not started or has already finished. A request can finish between that check
 * and the cancellation itself, in which case MN_OK is returned for a cancel
 * that had no effect: the terminal callback is still delivered exactly once. */
MN_EXPORT mn_result_t MN_CALL mn_request_cancel(mn_request_t *request);
MN_EXPORT mn_result_t MN_CALL mn_request_upload_write(mn_request_t *request,
                                                      const uint8_t *data,
                                                      size_t data_length,
                                                      int final_chunk);
MN_EXPORT mn_result_t MN_CALL mn_request_follow_redirect(mn_request_t *request);
/* Resumes body reads after on_body returned MN_READ_PAUSE. Safe to call from
 * any thread, more than once, and after completion; a resume that arrives
 * before the pause takes effect is not lost.
 *
 * Called from inside on_body it is a no-op beyond clearing the pause: the read
 * that is already in flight owns the buffer the callback was handed, so the
 * Core only schedules the next read once that callback has returned. */
MN_EXPORT mn_result_t MN_CALL mn_request_resume_read(mn_request_t *request);

/* Hands back a buffer taken by returning MN_READ_TAKE_BUFFER from on_body.
 *
 * `data` must be the exact pointer the callback received, and it must be
 * released exactly once. Releasing is what lets the request finish: a request
 * whose handed-out buffer is never returned stays alive, because the Core
 * cannot know the caller stopped reading it.
 *
 * Safe to call from any thread, including the one that received the callback.
 * Returns MN_ERROR_INVALID_ARGUMENT for a null pointer and
 * MN_ERROR_INVALID_STATE for a pointer this request never handed out, so a
 * double release fails instead of corrupting the heap.
 */
MN_EXPORT mn_result_t MN_CALL mn_request_release_buffer(mn_request_t *request,
                                                       const uint8_t *data);


/* The protocol the connection actually used.
 *
 * Readable from on_response onwards. Before the response has started, and
 * therefore after a request that never got one, this returns
 * MN_ERROR_INVALID_STATE.
 *
 * MN_HTTP_VERSION_UNKNOWN is a real answer, not a placeholder: Chromium reports
 * kUNKNOWN when it genuinely does not know, for example for a response served
 * from the HTTP cache. Reporting HTTP/1.1 there would be a lie, so the value
 * stays UNKNOWN and the caller reports the protocol as unknown too.
 *
 * The values come from HttpResponseInfo::connection_info, classified through
 * HttpConnectionInfoToCoarse so that draft HTTP/2 and every QUIC version are
 * covered without this header tracking Chromium's enum. 0.9, 1.0 and 1.1 keep
 * their own values because coarsening would merge them into one. */
typedef enum mn_http_version {
  MN_HTTP_VERSION_UNKNOWN = 0,
  MN_HTTP_VERSION_0_9 = 1,
  MN_HTTP_VERSION_1_0 = 2,
  MN_HTTP_VERSION_1_1 = 3,
  MN_HTTP_VERSION_2 = 4,
  MN_HTTP_VERSION_3 = 5,
} mn_http_version_t;

MN_EXPORT mn_result_t MN_CALL
mn_request_negotiated_protocol(mn_request_t *request,
                               mn_http_version_t *out_version);

/* Whether Encrypted ClientHello was used for this connection.
 *
 * Readable under the same rule as mn_request_negotiated_protocol: from
 * on_response onwards, MN_ERROR_INVALID_STATE before that.
 *
 * MN_ECH_FAILED exists so the enumeration does not have to change if Chromium
 * ever exposes the distinction, but it is currently unreachable and callers
 * must not wait for it: net/ssl/ssl_info.h carries a single boolean
 * (encrypted_client_hello), which is false both for "tried and rejected" and
 * for "never tried".
 *
 * MN_ECH_NOT_ATTEMPTED is what a desktop build returns in practice. Chromium
 * only obtains an ECHConfigList from a DNS HTTPS record, and this engine does
 * not fetch one, so no ECH extension is ever offered -- the value means "no
 * ECHConfigList was available", not "the platform disabled ECH". */
typedef enum mn_ech_status {
  MN_ECH_NOT_ATTEMPTED = 0,
  MN_ECH_SUCCESS = 1,
  MN_ECH_FAILED = 2,
} mn_ech_status_t;

MN_EXPORT mn_result_t MN_CALL mn_request_ech_status(mn_request_t *request,
                                                    mn_ech_status_t *out_status);

typedef enum mn_websocket_message_type {
  MN_WEBSOCKET_MESSAGE_CONTINUATION = 0,
  MN_WEBSOCKET_MESSAGE_TEXT = 1,
  MN_WEBSOCKET_MESSAGE_BINARY = 2,
} mn_websocket_message_type_t;

typedef void(MN_CALL *mn_websocket_open_fn)(
    void *user_data, mn_websocket_t *websocket, const char *protocol,
    size_t protocol_length, const char *extensions, size_t extensions_length);
typedef void(MN_CALL *mn_websocket_message_fn)(void *user_data,
                                               mn_websocket_t *websocket,
                                               mn_websocket_message_type_t type,
                                               int final, const uint8_t *data,
                                               size_t data_length);
typedef void(MN_CALL *mn_websocket_closing_fn)(void *user_data,
                                               mn_websocket_t *websocket);
typedef void(MN_CALL *mn_websocket_closed_fn)(void *user_data,
                                              mn_websocket_t *websocket,
                                              int was_clean, uint16_t code,
                                              const char *reason,
                                              size_t reason_length);
typedef void(MN_CALL *mn_websocket_failure_fn)(void *user_data,
                                               mn_websocket_t *websocket,
                                               mn_result_t result,
                                               int net_error, int response_code,
                                               const char *message,
                                               size_t message_length);

/* Callback string and message pointers are borrowed only during the call.
 * on_closed or on_failure is delivered exactly once after a successful start.
 */

typedef struct mn_websocket_callbacks {
  uint32_t size;
  uint32_t version;
  void *user_data;
  mn_websocket_open_fn on_open;
  mn_websocket_message_fn on_message;
  mn_websocket_closing_fn on_closing;
  mn_websocket_closed_fn on_closed;
  mn_websocket_failure_fn on_failure;
} mn_websocket_callbacks_t;

typedef struct mn_websocket_config {
  uint32_t size;
  uint32_t version;
  const char *url;
  size_t url_length;
  const char *origin;
  size_t origin_length;
  const mn_string_t *protocols;
  size_t protocol_count;
  const mn_header_t *headers;
  size_t header_count;
  mn_websocket_callbacks_t callbacks;
  uint64_t timeout_ms;
} mn_websocket_config_t;

MN_EXPORT mn_result_t MN_CALL
mn_websocket_create(mn_engine_t *engine, const mn_websocket_config_t *config,
                    mn_websocket_t **out_websocket);
MN_EXPORT void MN_CALL mn_websocket_retain(mn_websocket_t *websocket);
MN_EXPORT void MN_CALL mn_websocket_release(mn_websocket_t *websocket);
MN_EXPORT mn_result_t MN_CALL mn_websocket_start(mn_websocket_t *websocket);
MN_EXPORT mn_result_t MN_CALL
mn_websocket_send(mn_websocket_t *websocket, mn_websocket_message_type_t type,
                  const uint8_t *data, size_t data_length);
MN_EXPORT mn_result_t MN_CALL mn_websocket_close(mn_websocket_t *websocket,
                                                 uint16_t code,
                                                 const char *reason,
                                                 size_t reason_length);
MN_EXPORT mn_result_t MN_CALL mn_websocket_cancel(mn_websocket_t *websocket);

#ifdef __cplusplus
}
#endif

#endif // MINICRONET_H_
