#include "minicronet/websocket.h"

#include <optional>
#include <utility>

#include "base/compiler_specific.h"
#include "base/functional/bind.h"
#include "base/location.h"
#include "base/task/thread_pool.h"
#include "base/strings/utf_string_conversions.h"
#include "minicronet/engine.h"
#include "minicronet/error_mapping.h"
#include "net/base/auth.h"
#include "net/base/io_buffer.h"
#include "net/base/isolation_info.h"
#include "net/base/net_errors.h"
#include "net/http/http_request_headers.h"
#include "net/http/http_response_headers.h"
#include "net/ssl/ssl_info.h"
#include "net/storage_access_api/status.h"
#include "net/traffic_annotation/network_traffic_annotation.h"
#include "net/url_request/url_request_context.h"
#include "net/websockets/websocket_channel.h"
#include "net/websockets/websocket_event_interface.h"
#include "net/websockets/websocket_frame.h"
#include "net/websockets/websocket_handshake_request_info.h"
#include "net/websockets/websocket_handshake_response_info.h"
#include "net/websockets/websocket_stream.h"
#include "url/gurl.h"
#include "url/origin.h"

namespace minicronet {
namespace {

DISABLE_CFI_ICALL void InvokeOpenCallback(mn_websocket_open_fn callback,
                                          void* user_data,
                                          mn_websocket_t* websocket,
                                          const std::string& protocol,
                                          const std::string& extensions) {
  callback(user_data, websocket, protocol.data(), protocol.size(),
           extensions.data(), extensions.size());
}

DISABLE_CFI_ICALL void InvokeMessageCallback(
    mn_websocket_message_fn callback,
    void* user_data,
    mn_websocket_t* websocket,
    mn_websocket_message_type_t type,
    bool final,
    const std::vector<uint8_t>& data) {
  callback(user_data, websocket, type, final, data.data(), data.size());
}

DISABLE_CFI_ICALL void InvokeClosingCallback(
    mn_websocket_closing_fn callback,
    void* user_data,
    mn_websocket_t* websocket) {
  callback(user_data, websocket);
}

DISABLE_CFI_ICALL void InvokeClosedCallback(mn_websocket_closed_fn callback,
                                            void* user_data,
                                            mn_websocket_t* websocket,
                                            bool was_clean,
                                            uint16_t code,
                                            const std::string& reason) {
  callback(user_data, websocket, was_clean, code, reason.data(), reason.size());
}

DISABLE_CFI_ICALL void InvokeFailureCallback(
    mn_websocket_failure_fn callback,
    void* user_data,
    mn_websocket_t* websocket,
    mn_result_t result,
    int net_error,
    int response_code,
    const std::string& message) {
  callback(user_data, websocket, result, net_error, response_code,
           message.data(), message.size());
}

constexpr net::NetworkTrafficAnnotationTag kTrafficAnnotation =
    net::DefineNetworkTrafficAnnotation("minicronet_websocket", R"(
      semantics {
        sender: "MiniCronet"
        description: "WebSocket selected explicitly by the embedding app."
        trigger: "The embedding app starts a MiniCronet WebSocket."
        data: "App-provided headers and WebSocket payloads."
        destination: WEBSITE
      }
      policy {
        cookies_allowed: YES
        cookies_store: "MiniCronet in-memory CookieStore"
        setting: "Controlled by the embedding application."
        policy_exception_justification: "Not a Chrome feature."
      })");

}  // namespace

class WebSocket::EventHandler final : public net::WebSocketEventInterface {
 public:
  explicit EventHandler(WebSocket* owner) : owner_(owner) {}

  void OnCreateURLRequest(net::URLRequest* request) override {}

  int OnURLRequestConnected(net::URLRequest* request,
                            const net::TransportInfo& info,
                            net::CompletionOnceCallback callback) override {
    return net::OK;
  }

  void OnAddChannelResponse(
      std::unique_ptr<net::WebSocketHandshakeResponseInfo> response,
      const std::string& selected_subprotocol,
      const std::string& extensions) override {
    owner_->OnOpen(selected_subprotocol, extensions);
  }

  void OnDataFrame(bool final,
                   WebSocketMessageType type,
                   base::span<const char> payload) override {
    owner_->OnMessage(final, type, payload);
  }

  bool HasPendingDataFrames() override {
    return owner_->pending_data_.load(std::memory_order_acquire) != 0;
  }

  void OnSendDataFrameDone() override {}
  void OnClosingHandshake() override { owner_->OnClosing(); }

  void OnDropChannel(bool was_clean,
                     uint16_t code,
                     const std::string& reason) override {
    owner_->OnClosed(was_clean, code, reason);
  }

  void OnFailChannel(const std::string& message,
                     int net_error,
                     std::optional<int> response_code) override {
    owner_->OnFailure(message, net_error, response_code.value_or(-1));
  }

  void OnStartOpeningHandshake(
      std::unique_ptr<net::WebSocketHandshakeRequestInfo> request) override {}

  void OnSSLCertificateError(
      std::unique_ptr<SSLErrorCallbacks> ssl_error_callbacks,
      const GURL& url,
      int net_error,
      const net::SSLInfo& ssl_info,
      bool fatal) override {
    ssl_error_callbacks->CancelSSLRequest(net_error, &ssl_info);
  }

  int OnAuthRequired(
      const net::AuthChallengeInfo& auth_info,
      scoped_refptr<net::HttpResponseHeaders> response_headers,
      const net::IPEndPoint& socket_address,
      base::OnceCallback<void(const net::AuthCredentials*)> callback,
      std::optional<net::AuthCredentials>* credentials) override {
    if (auth_info.is_proxy && !owner_->engine_->proxy_username().empty()) {
      *credentials = net::AuthCredentials(
          base::UTF8ToUTF16(owner_->engine_->proxy_username()),
          base::UTF8ToUTF16(owner_->engine_->proxy_password()));
    } else {
      *credentials = std::nullopt;
    }
    return net::OK;
  }

 private:
  WebSocket* const owner_;
};

WebSocket::WebSocket(
    scoped_refptr<Engine> engine,
    std::string url,
    std::string origin,
    std::vector<std::string> protocols,
    std::vector<std::pair<std::string, std::string>> headers,
    mn_websocket_callbacks_t callbacks,
    uint64_t timeout_ms,
    mn_websocket_t* public_handle)
    : engine_(std::move(engine)),
      callback_runner_(base::ThreadPool::CreateSequencedTaskRunner({})),
      url_(std::move(url)),
      origin_(std::move(origin)),
      protocols_(std::move(protocols)),
      headers_(std::move(headers)),
      callbacks_(callbacks),
      timeout_ms_(timeout_ms),
      public_handle_(public_handle) {}

WebSocket::~WebSocket() = default;

mn_result_t WebSocket::Start() {
  if (!engine_->profile().wire_verified()) {
    return MN_ERROR_PROFILE_UNSUPPORTED;
  }
  bool expected = false;
  if (!started_.compare_exchange_strong(expected, true)) {
    return MN_ERROR_INVALID_STATE;
  }
  keep_alive_ = this;
  engine_->task_runner()->PostTask(
      FROM_HERE, base::BindOnce(&WebSocket::StartOnNetworkThread,
                                base::RetainedRef(this)));
  return MN_OK;
}

mn_result_t WebSocket::Send(mn_websocket_message_type_t type,
                            std::vector<uint8_t> data) {
  if (!opened_.load() || closing_.load() || terminal_.load() ||
      (type != MN_WEBSOCKET_MESSAGE_TEXT &&
       type != MN_WEBSOCKET_MESSAGE_BINARY)) {
    return MN_ERROR_INVALID_STATE;
  }
  engine_->task_runner()->PostTask(
      FROM_HERE, base::BindOnce(&WebSocket::SendOnNetworkThread,
                                base::RetainedRef(this), type,
                                std::move(data)));
  return MN_OK;
}

mn_result_t WebSocket::Close(uint16_t code, std::string reason) {
  if (!started_.load() || !opened_.load() || terminal_.load() ||
      closing_.exchange(true)) {
    return MN_ERROR_INVALID_STATE;
  }
  engine_->task_runner()->PostTask(
      FROM_HERE, base::BindOnce(&WebSocket::CloseOnNetworkThread,
                                base::RetainedRef(this), code,
                                std::move(reason)));
  return MN_OK;
}

mn_result_t WebSocket::Cancel() {
  if (!started_.load() || terminal_.load()) {
    return MN_ERROR_INVALID_STATE;
  }
  engine_->task_runner()->PostTask(
      FROM_HERE, base::BindOnce(&WebSocket::CancelOnNetworkThread,
                                base::RetainedRef(this)));
  return MN_OK;
}

void WebSocket::StartOnNetworkThread() {
  if (terminal_.load()) {
    return;
  }
  GURL url(url_);
  GURL origin_url(origin_);
  if (!url.is_valid() || !url.SchemeIsWSOrWSS() || !origin_url.is_valid()) {
    CompleteFailure(MN_ERROR_INVALID_ARGUMENT, net::ERR_INVALID_URL, -1,
                    "Invalid WebSocket URL or origin");
    return;
  }

  net::HttpRequestHeaders headers;
  for (const auto& [name, value] : headers_) {
    headers.SetHeader(name, value);
  }
  // Chrome supplies its User-Agent as a WebSocket additional header, before
  // Chromium appends Upgrade/Origin/Sec-WebSocket-*.
  headers.SetHeader(net::HttpRequestHeaders::kUserAgent,
                    engine_->user_agent());
  channel_ = std::make_unique<net::WebSocketChannel>(
      std::make_unique<EventHandler>(this), engine_->context());
  url::Origin origin = url::Origin::Create(origin_url);
  channel_->SendAddChannelRequest(
      url, protocols_, origin, net::StorageAccessApiStatus::kNone,
      net::IsolationInfo::CreateForInternalRequest(origin), headers,
      net::WebSocketPriorityHint::kDefault, kTrafficAnnotation);
  if (timeout_ms_ > 0) {
    timeout_timer_.Start(
        FROM_HERE, base::Milliseconds(timeout_ms_),
        base::BindOnce(&WebSocket::TimeoutOnNetworkThread,
                       base::RetainedRef(this)));
  }
}

void WebSocket::TimeoutOnNetworkThread() {
  if (terminal_.load()) {
    return;
  }
  CompleteFailure(MN_ERROR_TIMEOUT, net::ERR_TIMED_OUT, -1, "Timed out");
}

void WebSocket::SendOnNetworkThread(mn_websocket_message_type_t type,
                                    std::vector<uint8_t> data) {
  if (!opened_.load() || closing_.load() || terminal_.load() || !channel_) {
    return;
  }
  size_t size = data.size();
  auto buffer = base::MakeRefCounted<net::VectorIOBuffer>(std::move(data));
  [[maybe_unused]] auto state = channel_->SendFrame(
      true,
      type == MN_WEBSOCKET_MESSAGE_TEXT
          ? net::WebSocketFrameHeader::kOpCodeText
          : net::WebSocketFrameHeader::kOpCodeBinary,
      std::move(buffer), size);
}

void WebSocket::CloseOnNetworkThread(uint16_t code, std::string reason) {
  if (terminal_.load() || !channel_) {
    return;
  }
  [[maybe_unused]] auto state = channel_->StartClosingHandshake(code, reason);
}

void WebSocket::CancelOnNetworkThread() {
  if (terminal_.exchange(true)) {
    return;
  }
  opened_.store(false);
  closing_.store(true);
  pending_data_.store(0, std::memory_order_release);
  channel_.reset();
  auto callback = callbacks_.on_failure;
  void* user_data = callbacks_.user_data;
  scoped_refptr<WebSocket> keep_alive = std::move(keep_alive_);
  callback_runner_->PostTask(
      FROM_HERE,
      base::BindOnce(
          [](mn_websocket_failure_fn callback, void* user_data,
             WebSocket* self, scoped_refptr<WebSocket> keep_alive) {
            if (callback) {
              InvokeFailureCallback(callback, user_data, self->public_handle_,
                                    MN_ERROR_CANCELED, net::ERR_ABORTED, -1,
                                    "Canceled");
            }
            self->ReleasePublicOwnership();
          },
          callback, user_data, base::RetainedRef(this), std::move(keep_alive)));
}

void WebSocket::OnOpen(const std::string& protocol,
                       const std::string& extensions) {
  if (terminal_.load()) {
    return;
  }
  opened_.store(true);
  if (!callbacks_.on_open) {
    StartReading();
    return;
  }
  auto callback = callbacks_.on_open;
  void* user_data = callbacks_.user_data;
  callback_runner_->PostTask(
      FROM_HERE,
      base::BindOnce(
          [](mn_websocket_open_fn callback, void* user_data, WebSocket* self,
             std::string protocol, std::string extensions) {
            if (self->terminal_.load()) {
              return;
            }
            InvokeOpenCallback(callback, user_data, self->public_handle_,
                               protocol, extensions);
            self->engine_->task_runner()->PostTask(
                FROM_HERE, base::BindOnce(&WebSocket::StartReading,
                                          base::RetainedRef(self)));
          },
          callback, user_data, base::RetainedRef(this), protocol, extensions));
}

void WebSocket::StartReading() {
  if (!terminal_.load() && channel_) {
    [[maybe_unused]] auto state = channel_->ReadFrames();
  }
}

void WebSocket::OnMessage(bool final,
                          int type,
                          base::span<const char> payload) {
  if (terminal_.load()) {
    return;
  }
  pending_data_.fetch_add(1, std::memory_order_acq_rel);
  std::vector<uint8_t> data(payload.begin(), payload.end());
  auto callback = callbacks_.on_message;
  void* user_data = callbacks_.user_data;
  callback_runner_->PostTask(
      FROM_HERE,
      base::BindOnce(
          [](mn_websocket_message_fn callback, void* user_data,
             WebSocket* self, mn_websocket_message_type_t type, bool final,
             std::vector<uint8_t> data) {
            if (callback && !self->terminal_.load()) {
              InvokeMessageCallback(callback, user_data, self->public_handle_,
                                    type, final, data);
            }
            self->engine_->task_runner()->PostTask(
                FROM_HERE, base::BindOnce(&WebSocket::ResumeReading,
                                          base::RetainedRef(self)));
          },
          callback, user_data, base::RetainedRef(this),
          static_cast<mn_websocket_message_type_t>(type), final,
          std::move(data)));
}

void WebSocket::ResumeReading() {
  if (terminal_.load()) {
    return;
  }
  if (pending_data_.fetch_sub(1, std::memory_order_acq_rel) == 1 &&
      channel_) {
    [[maybe_unused]] auto state = channel_->ReadFrames();
  }
}

void WebSocket::OnClosing() {
  closing_.store(true);
  if (!callbacks_.on_closing) {
    return;
  }
  auto callback = callbacks_.on_closing;
  void* user_data = callbacks_.user_data;
  callback_runner_->PostTask(
      FROM_HERE,
      base::BindOnce(
          [](mn_websocket_closing_fn callback, void* user_data,
             WebSocket* self) {
            if (!self->terminal_.load()) {
              InvokeClosingCallback(callback, user_data, self->public_handle_);
            }
          },
          callback, user_data, base::RetainedRef(this)));
}

void WebSocket::OnClosed(bool was_clean,
                         uint16_t code,
                         const std::string& reason) {
  if (terminal_.exchange(true)) {
    return;
  }
  timeout_timer_.Stop();
  opened_.store(false);
  closing_.store(true);
  pending_data_.store(0, std::memory_order_release);
  channel_.reset();
  auto callback = callbacks_.on_closed;
  void* user_data = callbacks_.user_data;
  scoped_refptr<WebSocket> keep_alive = std::move(keep_alive_);
  callback_runner_->PostTask(
      FROM_HERE,
      base::BindOnce(
          [](mn_websocket_closed_fn callback, void* user_data,
             WebSocket* self, bool was_clean, uint16_t code,
             std::string reason, scoped_refptr<WebSocket> keep_alive) {
            if (callback) {
              InvokeClosedCallback(callback, user_data, self->public_handle_,
                                   was_clean, code, reason);
            }
            self->ReleasePublicOwnership();
          },
          callback, user_data, base::RetainedRef(this), was_clean, code, reason,
          std::move(keep_alive)));
}

void WebSocket::OnFailure(const std::string& message,
                          int net_error,
                          int response_code) {
  CompleteFailure(net_error == net::ERR_ABORTED ? MN_ERROR_CANCELED
                                                 : MapNetError(net_error),
                  net_error, response_code, message);
}

void WebSocket::CompleteFailure(mn_result_t result,
                                int net_error,
                                int response_code,
                                std::string message) {
  if (terminal_.exchange(true)) {
    return;
  }
  timeout_timer_.Stop();
  opened_.store(false);
  closing_.store(true);
  pending_data_.store(0, std::memory_order_release);
  channel_.reset();
  auto callback = callbacks_.on_failure;
  void* user_data = callbacks_.user_data;
  scoped_refptr<WebSocket> keep_alive = std::move(keep_alive_);
  callback_runner_->PostTask(
      FROM_HERE,
      base::BindOnce(
          [](mn_websocket_failure_fn callback, void* user_data,
             WebSocket* self, mn_result_t result, int net_error,
             int response_code, std::string message,
             scoped_refptr<WebSocket> keep_alive) {
            if (callback) {
              InvokeFailureCallback(callback, user_data, self->public_handle_,
                                    result, net_error, response_code, message);
            }
            self->ReleasePublicOwnership();
          },
          callback, user_data, base::RetainedRef(this), result, net_error,
          response_code, std::move(message), std::move(keep_alive)));
}

void WebSocket::ReleasePublicOwnership() {
  mn_websocket_release(public_handle_);
}

}  // namespace minicronet
