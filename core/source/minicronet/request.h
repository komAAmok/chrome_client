#ifndef MINICRONET_CORE_REQUEST_H_
#define MINICRONET_CORE_REQUEST_H_

#include <atomic>
#include <memory>
#include <string>
#include <utility>
#include <vector>

#include "base/memory/ref_counted.h"
#include "base/memory/scoped_refptr.h"
#include "base/synchronization/lock.h"
#include "base/timer/timer.h"
#include "minicronet.h"
#include "net/base/chunked_upload_data_stream.h"
#include "net/base/io_buffer.h"
#include "net/url_request/url_request.h"

namespace minicronet {

class Engine;

class Request final : public base::RefCountedThreadSafe<Request>,
                      public net::URLRequest::Delegate {
public:
  Request(scoped_refptr<Engine> engine, std::string url, std::string method,
          std::vector<std::pair<std::string, std::string>> headers,
          std::vector<uint8_t> body, mn_upload_mode_t upload_mode,
          mn_cache_mode_t cache_mode, mn_redirect_mode_t redirect_mode,
          mn_request_priority_t priority,
          mn_request_callbacks_t callbacks, uint64_t timeout_ms,
          mn_request_t *public_handle);

  mn_result_t Start();
  mn_result_t Cancel();
  mn_result_t UploadWrite(std::vector<uint8_t> data, bool final_chunk);
  mn_result_t FollowRedirect();

  void OnReceivedRedirect(net::URLRequest *request,
                          const net::RedirectInfo &redirect_info,
                          bool *defer_redirect) override;
  void OnAuthRequired(net::URLRequest *request,
                      const net::AuthChallengeInfo &auth_info) override;
  void OnResponseStarted(net::URLRequest *request, int net_error) override;
  void OnReadCompleted(net::URLRequest *request, int bytes_read) override;

private:
  friend class base::RefCountedThreadSafe<Request>;
  ~Request() override;

  void StartOnNetworkThread();
  void CancelOnNetworkThread();
  void UploadWriteOnNetworkThread(std::vector<uint8_t> data, bool final_chunk);
  void FollowRedirectOnNetworkThread();
  void Complete(mn_result_t result, int net_error);
  void ReadMore();

  scoped_refptr<Engine> engine_;
  std::string url_;
  std::string method_;
  std::vector<std::pair<std::string, std::string>> headers_;
  std::vector<uint8_t> body_;
  mn_upload_mode_t upload_mode_ = MN_UPLOAD_NONE;
  mn_cache_mode_t cache_mode_ = MN_CACHE_DEFAULT;
  mn_redirect_mode_t redirect_mode_ = MN_REDIRECT_FOLLOW;
  mn_request_priority_t priority_ = MN_REQUEST_PRIORITY_DEFAULT;
  mn_request_callbacks_t callbacks_{};
  mn_request_t *public_handle_ = nullptr;
  uint64_t timeout_ms_ = 0;
  std::unique_ptr<net::URLRequest> request_;
  std::unique_ptr<net::ChunkedUploadDataStream::Writer> upload_writer_;
  scoped_refptr<net::IOBufferWithSize> read_buffer_;
  base::OneShotTimer timeout_timer_;
  scoped_refptr<Request> keep_alive_;
  std::atomic_bool started_{false};
  std::atomic_bool completed_{false};
  base::Lock upload_lock_;
  bool upload_finished_ = false;
  std::atomic_bool redirect_deferred_{false};
};

} // namespace minicronet

#endif // MINICRONET_CORE_REQUEST_H_
