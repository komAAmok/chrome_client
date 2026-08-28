#ifndef MINICRONET_CORE_WEBSOCKET_H_
#define MINICRONET_CORE_WEBSOCKET_H_

#include <atomic>
#include <memory>
#include <string>
#include <utility>
#include <vector>

#include "base/memory/ref_counted.h"
#include "base/memory/scoped_refptr.h"
#include "base/timer/timer.h"
#include "minicronet.h"

namespace net {
class WebSocketChannel;
}

namespace minicronet {

class Engine;

class WebSocket final : public base::RefCountedThreadSafe<WebSocket> {
 public:
  WebSocket(scoped_refptr<Engine> engine,
            std::string url,
            std::string origin,
            std::vector<std::string> protocols,
            std::vector<std::pair<std::string, std::string>> headers,
            mn_websocket_callbacks_t callbacks,
            uint64_t timeout_ms,
            mn_websocket_t* public_handle);

  mn_result_t Start();
  mn_result_t Send(mn_websocket_message_type_t type,
                   std::vector<uint8_t> data);
  mn_result_t Close(uint16_t code, std::string reason);
  mn_result_t Cancel();

 private:
  friend class base::RefCountedThreadSafe<WebSocket>;
  class EventHandler;

  ~WebSocket();

  void StartOnNetworkThread();
  void SendOnNetworkThread(mn_websocket_message_type_t type,
                           std::vector<uint8_t> data);
  void CloseOnNetworkThread(uint16_t code, std::string reason);
  void CancelOnNetworkThread();
  void TimeoutOnNetworkThread();
  void OnOpen(const std::string& protocol, const std::string& extensions);
  void StartReading();
  void OnMessage(bool final, int type, base::span<const char> payload);
  void ResumeReading();
  void OnClosing();
  void OnClosed(bool was_clean, uint16_t code, const std::string& reason);
  void OnFailure(const std::string& message,
                 int net_error,
                 int response_code);
  void CompleteFailure(mn_result_t result,
                       int net_error,
                       int response_code,
                       std::string message);
  void ReleasePublicOwnership();

  scoped_refptr<Engine> engine_;
  std::string url_;
  std::string origin_;
  std::vector<std::string> protocols_;
  std::vector<std::pair<std::string, std::string>> headers_;
  mn_websocket_callbacks_t callbacks_{};
  uint64_t timeout_ms_ = 0;
  mn_websocket_t* public_handle_ = nullptr;
  std::unique_ptr<net::WebSocketChannel> channel_;
  scoped_refptr<WebSocket> keep_alive_;
  std::atomic_bool started_{false};
  std::atomic_bool opened_{false};
  std::atomic_bool closing_{false};
  std::atomic_bool terminal_{false};
  std::atomic_uint32_t pending_data_{0};
  base::OneShotTimer timeout_timer_;
};

}  // namespace minicronet

#endif  // MINICRONET_CORE_WEBSOCKET_H_
