#include "minicronet.h"

#include <algorithm>
#include <atomic>
#include <new>
#include <string>
#include <utility>
#include <vector>

#include "base/compiler_specific.h"
#include "base/containers/span.h"
#include "base/i18n/streaming_utf8_validator.h"
#include "base/strings/string_util.h"
#include "minicronet/engine.h"
#include "minicronet/profile_context.h"
#include "minicronet/request.h"
#include "minicronet/websocket.h"
#include "net/http/http_util.h"
#include "net/proxy_resolution/proxy_config.h"
#include "url/gurl.h"
#include "url/origin.h"

struct mn_engine {
  std::atomic_uint32_t refs{1};
  scoped_refptr<minicronet::Engine> impl;
};

struct mn_request {
  std::atomic_uint32_t refs{1};
  scoped_refptr<minicronet::Request> impl;
};

struct mn_websocket {
  std::atomic_uint32_t refs{1};
  scoped_refptr<minicronet::WebSocket> impl;
};

namespace {

constexpr char kVersion[] = "0.4.0";
constexpr char kDefaultUserAgent[] = "minicronet/0.4.0";

std::string HistoricalUserAgent(const minicronet::ProfileContext& profile) {
  static constexpr const char* kFullVersions[] = {
      "99.0.4844.84", "100.0.4896.127", "101.0.4951.64",
      "102.0.5005.115", "103.0.5060.134", "104.0.5112.101",
  };
  int major = 0;
  for (char digit : profile.id().substr(7)) {
    major = major * 10 + digit - '0';
  }
  const std::string version =
      major <= 104 ? base::span(kFullVersions)[major - 99]
                   : std::to_string(major) + ".0.0.0";
  return "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/" +
         version + " Safari/537.36";
}

bool IsForbiddenWebSocketHeader(std::string_view name) {
  return base::EqualsCaseInsensitiveASCII(name, "Connection") ||
         base::EqualsCaseInsensitiveASCII(name, "Host") ||
         base::EqualsCaseInsensitiveASCII(name, "Origin") ||
         base::EqualsCaseInsensitiveASCII(name, "User-Agent") ||
         base::EqualsCaseInsensitiveASCII(name, "Upgrade") ||
         base::StartsWith(name, "Sec-WebSocket-",
                          base::CompareCase::INSENSITIVE_ASCII);
}

bool IsValidWebSocketCloseCode(uint16_t code) {
  switch (code) {
    case 1000:
    case 1001:
    case 1002:
    case 1003:
    case 1007:
    case 1008:
    case 1009:
    case 1010:
    case 1011:
    case 1012:
    case 1013:
    case 1014:
      return true;
    default:
      return code >= 3000 && code <= 4999;
  }
}

template <typename T> void Retain(T *object) {
  uint32_t refs = object->refs.load(std::memory_order_relaxed);
  while (refs != UINT32_MAX && !object->refs.compare_exchange_weak(
                                   refs, refs + 1, std::memory_order_relaxed,
                                   std::memory_order_relaxed)) {
  }
}

} // namespace

uint32_t MN_CALL mn_abi_version(void) { return MN_ABI_VERSION; }

const char *MN_CALL mn_version_string(void) { return kVersion; }

mn_result_t MN_CALL mn_engine_create(const mn_engine_config_t *config,
                                     mn_engine_t **out_engine) {
  if (!config || !out_engine) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  *out_engine = nullptr;

  if (config->size != sizeof(mn_engine_config_t) ||
      config->version != MN_ABI_VERSION) {
    return config->version != MN_ABI_VERSION ? MN_ERROR_UNSUPPORTED_ABI
                                             : MN_ERROR_INVALID_ARGUMENT;
  }

  std::string user_agent(kDefaultUserAgent);
  std::string accept_language;
  net::ProxyConfig proxy_config;
  if (config->user_agent_length && !config->user_agent) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (config->user_agent) {
    user_agent.assign(config->user_agent, config->user_agent_length);
    if (!net::HttpUtil::IsValidHeaderValue(user_agent)) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
  }
  if (config->accept_language_length && !config->accept_language) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (config->accept_language) {
    accept_language.assign(config->accept_language,
                           config->accept_language_length);
    if (!net::HttpUtil::IsValidHeaderValue(accept_language)) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
  }
  if (config->proxy_rules_length && !config->proxy_rules) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (config->proxy_rules) {
    std::string proxy_rules(config->proxy_rules, config->proxy_rules_length);
    if (proxy_rules.find('\0') != std::string::npos) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
    proxy_config.proxy_rules().ParseFromString(proxy_rules);
    if (!proxy_rules.empty() && proxy_config.proxy_rules().empty()) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
  }
  if ((config->proxy_username_length && !config->proxy_username) ||
      (config->proxy_password_length && !config->proxy_password)) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  std::string proxy_username;
  std::string proxy_password;
  if (config->proxy_username) {
    proxy_username.assign(config->proxy_username,
                          config->proxy_username_length);
  }
  if (config->proxy_password) {
    proxy_password.assign(config->proxy_password,
                          config->proxy_password_length);
  }
  if (proxy_username.find('\0') != std::string::npos ||
      proxy_password.find('\0') != std::string::npos ||
      !base::IsStringUTF8(proxy_username) ||
      !base::IsStringUTF8(proxy_password)) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (config->http_cache_mode != MN_HTTP_CACHE_ENABLED &&
      config->http_cache_mode != MN_HTTP_CACHE_DISABLED) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (config->protocol_mode < MN_PROTOCOL_NATIVE ||
      config->protocol_mode > MN_PROTOCOL_FORCE_H3) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (config->tls_verify_mode < MN_TLS_VERIFY_CHROMIUM_DEFAULT ||
      config->tls_verify_mode > MN_TLS_VERIFY_INSECURE) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (config->custom_ca_pem_length && !config->custom_ca_pem) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  constexpr size_t kMaxCustomCaPemLength = 16u * 1024u * 1024u;
  if (config->custom_ca_pem_length > kMaxCustomCaPemLength) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (config->tls_verify_mode == MN_TLS_VERIFY_CHROMIUM_DEFAULT &&
      config->custom_ca_pem_length != 0) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (config->tls_verify_mode == MN_TLS_VERIFY_INSECURE &&
      config->custom_ca_pem_length != 0) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (config->tls_verify_mode == MN_TLS_VERIFY_CUSTOM_CA &&
      config->custom_ca_pem_length == 0) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  std::string custom_ca_pem;
  if (config->custom_ca_pem_length) {
    custom_ca_pem.assign(
        reinterpret_cast<const char*>(config->custom_ca_pem),
        config->custom_ca_pem_length);
    if (custom_ca_pem.find('\0') != std::string::npos ||
        !minicronet::ValidateCustomCaPem(custom_ca_pem)) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
  }

  if ((config->profile_id_length && !config->profile_id) ||
      (config->profile_namespace_length && !config->profile_namespace)) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  std::string profile_id;
  std::string profile_namespace;
  if (config->profile_id) {
    profile_id.assign(config->profile_id, config->profile_id_length);
  }
  if (config->profile_namespace) {
    profile_namespace.assign(config->profile_namespace,
                             config->profile_namespace_length);
  }
  if (profile_id.find('\0') != std::string::npos ||
      profile_namespace.find('\0') != std::string::npos) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (!profile_namespace.empty() &&
      profile_namespace != "minicronet/" +
                               (profile_id.empty() ? "chromium_current"
                                                   : profile_id)) {
    return MN_ERROR_PROFILE_CONFLICT;
  }
  minicronet::ProfileContext profile;
  if (!minicronet::ProfileContext::Create(std::move(profile_id),
                                          std::move(profile_namespace),
                                          &profile)) {
    return MN_ERROR_PROFILE_UNSUPPORTED;
  }
  if (!profile.is_current()) {
    std::string profile_user_agent = HistoricalUserAgent(profile);
    if (config->user_agent && user_agent != profile_user_agent) {
      return MN_ERROR_PROFILE_CONFLICT;
    }
    user_agent = std::move(profile_user_agent);
  }

  scoped_refptr<minicronet::Engine> impl = minicronet::Engine::Create(
      std::move(user_agent), std::move(accept_language),
      std::move(proxy_config),
      std::move(proxy_username), std::move(proxy_password),
      config->http_cache_mode == MN_HTTP_CACHE_ENABLED, config->protocol_mode,
      config->tls_verify_mode, std::move(custom_ca_pem),
      std::move(profile));
  if (!impl) {
    return MN_ERROR_INITIALIZATION_FAILED;
  }

  auto *engine = new (std::nothrow) mn_engine;
  if (!engine) {
    return MN_ERROR_OUT_OF_MEMORY;
  }
  engine->impl = std::move(impl);
  *out_engine = engine;
  return MN_OK;
}

void MN_CALL mn_engine_retain(mn_engine_t *engine) {
  if (engine) {
    Retain(engine);
  }
}

void MN_CALL mn_engine_release(mn_engine_t *engine) {
  if (!engine) {
    return;
  }
  if (engine->refs.fetch_sub(1, std::memory_order_acq_rel) == 1) {
    delete engine;
  }
}

mn_result_t MN_CALL mn_request_create(mn_engine_t *engine,
                                      const mn_request_config_t *config,
                                      mn_request_t **out_request) {
  if (!engine || !config || !out_request) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  *out_request = nullptr;
  if (config->version != MN_ABI_VERSION ||
      config->callbacks.version != MN_ABI_VERSION) {
    return MN_ERROR_UNSUPPORTED_ABI;
  }
  if (config->size != sizeof(mn_request_config_t) || !config->url ||
      config->url_length == 0 || !config->method ||
      config->method_length == 0 ||
      config->callbacks.size != sizeof(mn_request_callbacks_t) ||
      (config->header_count > 0 && !config->headers)) {
    return MN_ERROR_INVALID_ARGUMENT;
  }

  mn_request_callbacks_t callbacks = config->callbacks;
  if (!callbacks.on_complete) {
    return MN_ERROR_INVALID_ARGUMENT;
  }

  mn_upload_mode_t upload_mode = MN_UPLOAD_NONE;
  mn_cache_mode_t cache_mode = MN_CACHE_DEFAULT;
  mn_redirect_mode_t redirect_mode = MN_REDIRECT_FOLLOW;
  std::vector<uint8_t> body;
  upload_mode = config->upload_mode;
  cache_mode = config->cache_mode;
  redirect_mode = config->redirect_mode;
  if (upload_mode < MN_UPLOAD_NONE || upload_mode > MN_UPLOAD_CHUNKED ||
      cache_mode < MN_CACHE_DEFAULT || cache_mode > MN_CACHE_ONLY_IF_CACHED ||
      redirect_mode < MN_REDIRECT_FOLLOW || redirect_mode > MN_REDIRECT_ERROR ||
      config->priority < MN_REQUEST_PRIORITY_DEFAULT ||
      config->priority > MN_REQUEST_PRIORITY_IDLE ||
      (upload_mode != MN_UPLOAD_FIXED && config->body_length != 0) ||
      (config->body_length != 0 && !config->body) ||
      (redirect_mode == MN_REDIRECT_MANUAL && !callbacks.on_redirect)) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  if (upload_mode == MN_UPLOAD_FIXED && config->body_length != 0) {
    auto input_body =
        UNSAFE_BUFFERS(base::span(config->body, config->body_length));
    body.assign(input_body.begin(), input_body.end());
  }

  std::string url(config->url, config->url_length);
  std::string method(config->method, config->method_length);
  if (url.find('\0') != std::string::npos ||
      !net::HttpUtil::IsValidHeaderName(method)) {
    return MN_ERROR_INVALID_ARGUMENT;
  }

  auto *request = new (std::nothrow) mn_request;
  if (!request) {
    return MN_ERROR_OUT_OF_MEMORY;
  }

  std::vector<std::pair<std::string, std::string>> headers;
  headers.reserve(config->header_count);
  auto input_headers =
      UNSAFE_BUFFERS(base::span(config->headers, config->header_count));
  for (const mn_header_t &header : input_headers) {
    if (!header.name || !header.value || header.name_length == 0) {
      delete request;
      return MN_ERROR_INVALID_ARGUMENT;
    }
    std::string name(header.name, header.name_length);
    std::string value(header.value, header.value_length);
    if (!net::HttpUtil::IsValidHeaderName(name) ||
        !net::HttpUtil::IsValidHeaderValue(value)) {
      delete request;
      return MN_ERROR_INVALID_ARGUMENT;
    }
    headers.emplace_back(std::move(name), std::move(value));
  }

  request->impl = base::MakeRefCounted<minicronet::Request>(
      engine->impl, std::move(url), std::move(method), std::move(headers),
      std::move(body), upload_mode, cache_mode, redirect_mode,
      config->priority, callbacks, config->timeout_ms, request);
  *out_request = request;
  return MN_OK;
}

void MN_CALL mn_request_retain(mn_request_t *request) {
  if (request) {
    Retain(request);
  }
}

void MN_CALL mn_request_release(mn_request_t *request) {
  if (!request) {
    return;
  }
  if (request->refs.fetch_sub(1, std::memory_order_acq_rel) == 1) {
    delete request;
  }
}

mn_result_t MN_CALL mn_request_start(mn_request_t *request) {
  if (!request) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  mn_request_retain(request);
  mn_result_t result = request->impl->Start();
  if (result != MN_OK) {
    mn_request_release(request);
  }
  return result;
}

mn_result_t MN_CALL mn_request_cancel(mn_request_t *request) {
  return request ? request->impl->Cancel() : MN_ERROR_INVALID_ARGUMENT;
}

mn_result_t MN_CALL mn_request_upload_write(mn_request_t *request,
                                            const uint8_t *data,
                                            size_t data_length,
                                            int final_chunk) {
  if (!request || (data_length != 0 && !data) ||
      (final_chunk != 0 && final_chunk != 1)) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  std::vector<uint8_t> body;
  if (data_length != 0) {
    auto input = UNSAFE_BUFFERS(base::span(data, data_length));
    body.assign(input.begin(), input.end());
  }
  return request->impl->UploadWrite(std::move(body), final_chunk != 0);
}

mn_result_t MN_CALL mn_request_follow_redirect(mn_request_t *request) {
  return request ? request->impl->FollowRedirect() : MN_ERROR_INVALID_ARGUMENT;
}

mn_result_t MN_CALL mn_request_resume_read(mn_request_t *request) {
  return request ? request->impl->ResumeRead() : MN_ERROR_INVALID_ARGUMENT;
}

mn_result_t MN_CALL mn_websocket_create(mn_engine_t *engine,
                                        const mn_websocket_config_t *config,
                                        mn_websocket_t **out_websocket) {
  if (!engine || !config || !out_websocket) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  *out_websocket = nullptr;
  if (config->version != MN_ABI_VERSION ||
      config->callbacks.version != MN_ABI_VERSION) {
    return MN_ERROR_UNSUPPORTED_ABI;
  }
  if (config->size != sizeof(mn_websocket_config_t) || !config->url ||
      config->url_length == 0 || !config->origin ||
      config->origin_length == 0 ||
      config->callbacks.size != sizeof(mn_websocket_callbacks_t) ||
      !config->callbacks.on_failure ||
      (config->protocol_count > 0 && !config->protocols) ||
      (config->header_count > 0 && !config->headers)) {
    return MN_ERROR_INVALID_ARGUMENT;
  }

  std::string url(config->url, config->url_length);
  std::string origin(config->origin, config->origin_length);
  if (url.find('\0') != std::string::npos ||
      origin.find('\0') != std::string::npos) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  GURL parsed_url(url);
  GURL parsed_origin(origin);
  if (!parsed_url.is_valid() || !parsed_url.SchemeIsWSOrWSS() ||
      parsed_url.has_ref() || !parsed_origin.is_valid() ||
      url::Origin::Create(parsed_origin).opaque()) {
    return MN_ERROR_INVALID_ARGUMENT;
  }

  std::vector<std::string> protocols;
  protocols.reserve(config->protocol_count);
  auto input_protocols =
      UNSAFE_BUFFERS(base::span(config->protocols, config->protocol_count));
  for (const mn_string_t &protocol : input_protocols) {
    if (!protocol.data || protocol.length == 0) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
    std::string value(protocol.data, protocol.length);
    if (!net::HttpUtil::IsValidHeaderName(value)) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
    if (std::ranges::find(protocols, value) != protocols.end()) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
    protocols.push_back(std::move(value));
  }

  std::vector<std::pair<std::string, std::string>> headers;
  headers.reserve(config->header_count);
  auto input_headers =
      UNSAFE_BUFFERS(base::span(config->headers, config->header_count));
  for (const mn_header_t &header : input_headers) {
    if (!header.name || !header.value || header.name_length == 0) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
    std::string name(header.name, header.name_length);
    std::string value(header.value, header.value_length);
    if (!net::HttpUtil::IsValidHeaderName(name) ||
        !net::HttpUtil::IsValidHeaderValue(value) ||
        IsForbiddenWebSocketHeader(name)) {
      return MN_ERROR_INVALID_ARGUMENT;
    }
    headers.emplace_back(std::move(name), std::move(value));
  }

  auto *websocket = new (std::nothrow) mn_websocket;
  if (!websocket) {
    return MN_ERROR_OUT_OF_MEMORY;
  }
  websocket->impl = base::MakeRefCounted<minicronet::WebSocket>(
      engine->impl, std::move(url), std::move(origin), std::move(protocols),
      std::move(headers), config->callbacks, config->timeout_ms, websocket);
  *out_websocket = websocket;
  return MN_OK;
}

void MN_CALL mn_websocket_retain(mn_websocket_t *websocket) {
  if (websocket) {
    Retain(websocket);
  }
}

void MN_CALL mn_websocket_release(mn_websocket_t *websocket) {
  if (!websocket) {
    return;
  }
  if (websocket->refs.fetch_sub(1, std::memory_order_acq_rel) == 1) {
    delete websocket;
  }
}

mn_result_t MN_CALL mn_websocket_start(mn_websocket_t *websocket) {
  if (!websocket) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  mn_websocket_retain(websocket);
  mn_result_t result = websocket->impl->Start();
  if (result != MN_OK) {
    mn_websocket_release(websocket);
  }
  return result;
}

mn_result_t MN_CALL mn_websocket_send(mn_websocket_t *websocket,
                                      mn_websocket_message_type_t type,
                                      const uint8_t *data, size_t data_length) {
  if (!websocket || (data_length > 0 && !data) || data_length > INT_MAX ||
      (type != MN_WEBSOCKET_MESSAGE_TEXT &&
       type != MN_WEBSOCKET_MESSAGE_BINARY)) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  std::vector<uint8_t> bytes;
  if (data_length > 0) {
    auto input = UNSAFE_BUFFERS(base::span(data, data_length));
    bytes.assign(input.begin(), input.end());
  }
  if (type == MN_WEBSOCKET_MESSAGE_TEXT &&
      !base::StreamingUtf8Validator::Validate(
          std::string(bytes.begin(), bytes.end()))) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  return websocket->impl->Send(type, std::move(bytes));
}

mn_result_t MN_CALL mn_websocket_close(mn_websocket_t *websocket, uint16_t code,
                                       const char *reason,
                                       size_t reason_length) {
  if (!websocket || (reason_length > 0 && !reason) || reason_length > 123 ||
      !IsValidWebSocketCloseCode(code)) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  std::string close_reason;
  if (reason_length > 0) {
    close_reason.assign(reason, reason_length);
  }
  if (!base::StreamingUtf8Validator::Validate(close_reason)) {
    return MN_ERROR_INVALID_ARGUMENT;
  }
  return websocket->impl->Close(code, std::move(close_reason));
}

mn_result_t MN_CALL mn_websocket_cancel(mn_websocket_t *websocket) {
  return websocket ? websocket->impl->Cancel() : MN_ERROR_INVALID_ARGUMENT;
}
