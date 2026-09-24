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

#define MN_ABI_VERSION 8u

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
 * Engine lifetime: an engine must outlive every request and WebSocket created
 * from it. Releasing the last engine reference tears down its
 * URLRequestContext, and Chromium asserts that no URLRequest is still
 * registered against it, so releasing an engine with work in flight can abort
 * the host process. Finish or cancel the work first. The request and WebSocket
 * objects keep the engine alive, but that only protects the handle, not the
 * context.
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
} mn_read_disposition_t;

typedef void(MN_CALL *mn_request_response_fn)(void *user_data,
                                              mn_request_t *request,
                                              int status_code,
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
