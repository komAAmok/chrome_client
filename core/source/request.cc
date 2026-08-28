#include "minicronet/request.h"

#include <algorithm>
#include <optional>
#include <utility>

#include "base/compiler_specific.h"
#include "base/functional/bind.h"
#include "base/location.h"
#include "base/time/time.h"
#include "base/strings/utf_string_conversions.h"
#include "minicronet/error_mapping.h"
#include "net/base/auth.h"
#include "minicronet/engine.h"
#include "net/base/elements_upload_data_stream.h"
#include "net/base/io_buffer.h"
#include "net/base/isolation_info.h"
#include "net/base/load_flags.h"
#include "net/base/net_errors.h"
#include "net/base/request_priority.h"
#include "net/base/upload_bytes_element_reader.h"
#include "net/http/http_request_headers.h"
#include "net/http/http_response_headers.h"
#include "net/traffic_annotation/network_traffic_annotation.h"
#include "net/url_request/redirect_info.h"
#include "net/url_request/url_request_context.h"
#include "url/gurl.h"
#include "url/origin.h"

namespace minicronet {
namespace {

constexpr int kReadBufferSize = 32 * 1024;

DISABLE_CFI_ICALL void
InvokeResponseCallback(mn_request_response_fn callback, void *user_data,
                       mn_request_t *request, int status_code,
                       const char *headers, size_t headers_length) {
  callback(user_data, request, status_code, headers, headers_length);
}

DISABLE_CFI_ICALL void InvokeBodyCallback(mn_request_body_fn callback,
                                          void *user_data,
                                          mn_request_t *request,
                                          const uint8_t *data,
                                          size_t data_length) {
  callback(user_data, request, data, data_length);
}

DISABLE_CFI_ICALL void InvokeCompleteCallback(mn_request_complete_fn callback,
                                              void *user_data,
                                              mn_request_t *request,
                                              mn_result_t result,
                                              int net_error) {
  callback(user_data, request, result, net_error);
}

DISABLE_CFI_ICALL void
InvokeRedirectCallback(mn_request_redirect_fn callback, void *user_data,
                       mn_request_t *request, int status_code,
                       const char *headers, size_t headers_length,
                       const char *new_url, size_t new_url_length,
                       const char *new_method, size_t new_method_length) {
  callback(user_data, request, status_code, headers, headers_length, new_url,
           new_url_length, new_method, new_method_length);
}

int CacheLoadFlags(mn_cache_mode_t mode) {
  switch (mode) {
  case MN_CACHE_DEFAULT:
    return net::LOAD_NORMAL;
  case MN_CACHE_VALIDATE:
    return net::LOAD_VALIDATE_CACHE;
  case MN_CACHE_BYPASS:
    return net::LOAD_BYPASS_CACHE;
  case MN_CACHE_NO_STORE:
    return net::LOAD_DISABLE_CACHE;
  case MN_CACHE_FORCE:
    return net::LOAD_SKIP_CACHE_VALIDATION;
  case MN_CACHE_ONLY_IF_CACHED:
    return net::LOAD_ONLY_FROM_CACHE | net::LOAD_SKIP_CACHE_VALIDATION;
  }
  return net::LOAD_NORMAL;
}

constexpr net::NetworkTrafficAnnotationTag kTrafficAnnotation =
    net::DefineNetworkTrafficAnnotation("minicronet_request", R"(
      semantics {
        sender: "MiniCronet"
        description: "Network request explicitly created by the embedding app."
        trigger: "The embedding app starts a request."
        data: "App-provided URL, headers, and request data."
        destination: WEBSITE
      }
      policy {
        cookies_allowed: YES
        cookies_store: "MiniCronet in-memory CookieStore"
        setting: "Controlled by the embedding application."
        policy_exception_justification: "Not a Chrome feature."
      })");

} // namespace

Request::Request(scoped_refptr<Engine> engine, std::string url,
                 std::string method,
                 std::vector<std::pair<std::string, std::string>> headers,
                 std::vector<uint8_t> body, mn_upload_mode_t upload_mode,
                 mn_cache_mode_t cache_mode, mn_redirect_mode_t redirect_mode,
                 mn_request_priority_t priority,
                 mn_request_callbacks_t callbacks, uint64_t timeout_ms,
                 mn_request_t *public_handle)
    : engine_(std::move(engine)), url_(std::move(url)),
      method_(std::move(method)), headers_(std::move(headers)),
      body_(std::move(body)), upload_mode_(upload_mode),
      cache_mode_(cache_mode), redirect_mode_(redirect_mode),
      priority_(priority), callbacks_(callbacks), public_handle_(public_handle),
      timeout_ms_(timeout_ms) {}

Request::~Request() = default;

mn_result_t Request::Start() {
#if !defined(MINICRONET_PROFILE_VERIFICATION)
  if (!engine_->profile().wire_verified()) {
    return MN_ERROR_PROFILE_UNSUPPORTED;
  }
#endif
  bool expected = false;
  if (!started_.compare_exchange_strong(expected, true)) {
    return MN_ERROR_INVALID_STATE;
  }
  keep_alive_ = this;
  engine_->task_runner()->PostTask(
      FROM_HERE,
      base::BindOnce(&Request::StartOnNetworkThread, base::RetainedRef(this)));
  return MN_OK;
}

mn_result_t Request::Cancel() {
  if (!started_.load() || completed_.load()) {
    return MN_ERROR_INVALID_STATE;
  }
  engine_->task_runner()->PostTask(
      FROM_HERE,
      base::BindOnce(&Request::CancelOnNetworkThread, base::RetainedRef(this)));
  return MN_OK;
}

mn_result_t Request::UploadWrite(std::vector<uint8_t> data, bool final_chunk) {
  if (upload_mode_ != MN_UPLOAD_CHUNKED || !started_.load() ||
      completed_.load() || (data.empty() && !final_chunk)) {
    return MN_ERROR_INVALID_STATE;
  }
  base::AutoLock lock(upload_lock_);
  if (completed_.load() || upload_finished_) {
    return MN_ERROR_INVALID_STATE;
  }
  upload_finished_ = final_chunk;
  engine_->task_runner()->PostTask(
      FROM_HERE,
      base::BindOnce(&Request::UploadWriteOnNetworkThread,
                     base::RetainedRef(this), std::move(data), final_chunk));
  return MN_OK;
}

mn_result_t Request::FollowRedirect() {
  if (redirect_mode_ != MN_REDIRECT_MANUAL ||
      !redirect_deferred_.exchange(false) || completed_.load()) {
    return MN_ERROR_INVALID_STATE;
  }
  engine_->task_runner()->PostTask(
      FROM_HERE, base::BindOnce(&Request::FollowRedirectOnNetworkThread,
                                base::RetainedRef(this)));
  return MN_OK;
}

void Request::StartOnNetworkThread() {
  if (completed_.load()) {
    return;
  }
  GURL url(url_);
  if (!url.is_valid() || !url.SchemeIsHTTPOrHTTPS()) {
    Complete(MN_ERROR_INVALID_ARGUMENT, net::ERR_INVALID_URL);
    return;
  }
  if ((engine_->protocol_mode() == MN_PROTOCOL_FORCE_H2 ||
       engine_->protocol_mode() == MN_PROTOCOL_FORCE_H3) &&
      !url.SchemeIs("https")) {
    Complete(MN_ERROR_PROTOCOL, net::ERR_ALPN_NEGOTIATION_FAILED);
    return;
  }

  net::RequestPriority priority = net::DEFAULT_PRIORITY;
  switch (priority_) {
  case MN_REQUEST_PRIORITY_DEFAULT:
  case MN_REQUEST_PRIORITY_LOWEST:
    priority = net::LOWEST;
    break;
  case MN_REQUEST_PRIORITY_HIGHEST:
    priority = net::HIGHEST;
    break;
  case MN_REQUEST_PRIORITY_MEDIUM:
    priority = net::MEDIUM;
    break;
  case MN_REQUEST_PRIORITY_LOW:
    priority = net::LOW;
    break;
  case MN_REQUEST_PRIORITY_IDLE:
    priority = net::IDLE;
    break;
  }
  request_ =
      engine_->context()->CreateRequest(url, priority, this, kTrafficAnnotation,
                                        net::handles::kInvalidNetworkHandle);
  request_->set_isolation_info(
      net::IsolationInfo::CreateForInternalRequest(url::Origin::Create(url)));
  request_->set_site_for_cookies(
      net::SiteForCookies::FromOrigin(url::Origin::Create(url)));
  request_->set_method(method_);
  request_->SetLoadFlags(CacheLoadFlags(cache_mode_));
  net::HttpRequestHeaders headers;
  for (const auto &[name, value] : headers_) {
    headers.SetHeader(name, value);
  }
  request_->SetExtraRequestHeaders(headers);
  if (upload_mode_ == MN_UPLOAD_FIXED) {
    auto reader = std::make_unique<net::UploadBytesElementReader>(
        base::span<const uint8_t>(body_));
    request_->set_upload(
        net::ElementsUploadDataStream::CreateWithReader(std::move(reader)));
  } else if (upload_mode_ == MN_UPLOAD_CHUNKED) {
    auto upload = std::make_unique<net::ChunkedUploadDataStream>(0);
    upload_writer_ = upload->CreateWriter();
    request_->set_upload(std::move(upload));
  }
  if (timeout_ms_ > 0) {
    timeout_timer_.Start(
        FROM_HERE, base::Milliseconds(timeout_ms_),
        base::BindOnce(
            [](Request *self) {
              if (self->request_) {
                self->request_->CancelWithError(net::ERR_TIMED_OUT);
              }
              self->Complete(MN_ERROR_TIMEOUT, net::ERR_TIMED_OUT);
            },
            base::Unretained(this)));
  }
  request_->Start();
}

void Request::OnAuthRequired(net::URLRequest *request,
                             const net::AuthChallengeInfo &auth_info) {
  if (!auth_info.is_proxy || engine_->proxy_username().empty()) {
    request->CancelAuth();
    return;
  }
  request->SetAuth(net::AuthCredentials(
      base::UTF8ToUTF16(engine_->proxy_username()),
      base::UTF8ToUTF16(engine_->proxy_password())));
}

void Request::UploadWriteOnNetworkThread(std::vector<uint8_t> data,
                                         bool final_chunk) {
  if (!completed_.load() && upload_writer_) {
    upload_writer_->AppendData(data, final_chunk);
  }
}

void Request::FollowRedirectOnNetworkThread() {
  if (!completed_.load() && request_) {
    request_->FollowDeferredRedirect(std::nullopt, std::nullopt);
  }
}

void Request::CancelOnNetworkThread() {
  if (request_) {
    request_->CancelWithError(net::ERR_ABORTED);
  }
  Complete(MN_ERROR_CANCELED, net::ERR_ABORTED);
}

void Request::OnReceivedRedirect(net::URLRequest *request,
                                 const net::RedirectInfo &redirect_info,
                                 bool *defer_redirect) {
  std::string headers;
  if (net::HttpResponseHeaders *response_headers =
          request->response_headers()) {
    headers = response_headers->raw_headers();
    std::replace(headers.begin(), headers.end(), '\0', '\n');
  }

  if (redirect_mode_ != MN_REDIRECT_FOLLOW) {
    *defer_redirect = true;
    redirect_deferred_.store(true);
  }

  if (callbacks_.on_redirect) {
    auto callback = callbacks_.on_redirect;
    void *user_data = callbacks_.user_data;
    engine_->callback_runner()->PostTask(
        FROM_HERE, base::BindOnce(
                       [](mn_request_redirect_fn callback, void *user_data,
                          Request *self, int status_code, std::string headers,
                          std::string new_url, std::string new_method,
                          mn_request_t *public_handle) {
                         InvokeRedirectCallback(
                             callback, user_data, public_handle,
                             status_code, headers.data(), headers.size(),
                             new_url.data(), new_url.size(), new_method.data(),
                             new_method.size());
                       },
                       callback, user_data, base::RetainedRef(this),
                       redirect_info.status_code, std::move(headers),
                       redirect_info.new_url.spec(), redirect_info.new_method,
                       public_handle_));
  }

  if (redirect_mode_ == MN_REDIRECT_ERROR) {
    engine_->task_runner()->PostTask(
        FROM_HERE, base::BindOnce(&Request::Complete, base::RetainedRef(this),
                                  MN_ERROR_REDIRECT, net::ERR_ABORTED));
  }
}

void Request::OnResponseStarted(net::URLRequest *request, int net_error) {
  if (net_error != net::OK) {
    Complete(net_error == net::ERR_ABORTED ? MN_ERROR_CANCELED
                                           : MapNetError(net_error),
             net_error);
    return;
  }

  int status_code = request->GetResponseCode();
  std::string headers;
  if (net::HttpResponseHeaders *response_headers =
          request->response_headers()) {
    headers = response_headers->raw_headers();
    std::replace(headers.begin(), headers.end(), '\0', '\n');
  }
  if (callbacks_.on_response) {
    auto callback = callbacks_.on_response;
    void *user_data = callbacks_.user_data;
    engine_->callback_runner()->PostTask(
        FROM_HERE,
        base::BindOnce(
            [](mn_request_response_fn callback, void *user_data, Request *self,
               int status_code, std::string headers) {
              InvokeResponseCallback(callback, user_data, self->public_handle_,
                                     status_code, headers.data(),
                                     headers.size());
              self->engine_->task_runner()->PostTask(
                  FROM_HERE,
                  base::BindOnce(&Request::ReadMore, base::RetainedRef(self)));
            },
            callback, user_data, base::RetainedRef(this), status_code,
            std::move(headers)));
  } else {
    ReadMore();
  }
}

void Request::ReadMore() {
  if (completed_.load() || !request_) {
    return;
  }
  if (!read_buffer_) {
    read_buffer_ = base::MakeRefCounted<net::IOBufferWithSize>(kReadBufferSize);
  }
  int result = request_->Read(read_buffer_.get(), kReadBufferSize);
  if (result != net::ERR_IO_PENDING) {
    OnReadCompleted(request_.get(), result);
  }
}

void Request::OnReadCompleted(net::URLRequest *request, int bytes_read) {
  if (bytes_read < 0) {
    Complete(bytes_read == net::ERR_ABORTED ? MN_ERROR_CANCELED
                                            : MapNetError(bytes_read),
             bytes_read);
    return;
  }
  if (bytes_read == 0) {
    Complete(MN_OK, net::OK);
    return;
  }

  if (callbacks_.on_body) {
    auto bytes = UNSAFE_BUFFERS(
        base::span(reinterpret_cast<const uint8_t *>(read_buffer_->data()),
                   static_cast<size_t>(bytes_read)));
    std::vector<uint8_t> data(bytes.begin(), bytes.end());
    auto callback = callbacks_.on_body;
    void *user_data = callbacks_.user_data;
    engine_->callback_runner()->PostTask(
        FROM_HERE,
        base::BindOnce(
            [](mn_request_body_fn callback, void *user_data, Request *self,
               std::vector<uint8_t> data) {
              InvokeBodyCallback(callback, user_data, self->public_handle_,
                                 data.data(), data.size());
              self->engine_->task_runner()->PostTask(
                  FROM_HERE,
                  base::BindOnce(&Request::ReadMore, base::RetainedRef(self)));
            },
            callback, user_data, base::RetainedRef(this), std::move(data)));
  } else {
    ReadMore();
  }
}

void Request::Complete(mn_result_t result, int net_error) {
  bool expected = false;
  if (!completed_.compare_exchange_strong(expected, true)) {
    return;
  }
  timeout_timer_.Stop();
  upload_writer_.reset();
  request_.reset();

  auto callback = callbacks_.on_complete;
  void *user_data = callbacks_.user_data;
  scoped_refptr<Request> keep_alive = std::move(keep_alive_);
  engine_->callback_runner()->PostTask(
      FROM_HERE, base::BindOnce(
                     [](mn_request_complete_fn callback, void *user_data,
                        Request *self, mn_result_t result, int net_error,
                        scoped_refptr<Request> keep_alive) {
                       if (callback) {
                         InvokeCompleteCallback(callback, user_data,
                                                self->public_handle_, result,
                                                net_error);
                       }
                       mn_request_release(self->public_handle_);
                     },
                     callback, user_data, base::RetainedRef(this), result,
                     net_error, std::move(keep_alive)));
}

} // namespace minicronet
