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
#include "base/task/sequenced_task_runner.h"
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

  // ABI 12. Must be called before Start, while the request is still owned
  // by the creating thread; it is not synchronised with the network side.
  void set_strip_auth_on_cross_origin_redirect(bool value) {
    strip_auth_on_cross_origin_redirect = value;
  }

  mn_result_t Start();
  mn_result_t Cancel();
  mn_result_t UploadWrite(std::vector<uint8_t> data, bool final_chunk);
  mn_result_t FollowRedirect();
  mn_result_t ResumeRead();
  // ABI 12. Hands back a buffer taken with MN_READ_TAKE_BUFFER.
  mn_result_t ReleaseBuffer(const uint8_t *data);
  // v9. Both report MN_ERROR_INVALID_STATE until a response has started, and
  // both keep working after the request completes, which is why they read state
  // captured on the network thread rather than request_ itself.
  mn_result_t NegotiatedProtocol(mn_http_version_t *out_version) const;
  mn_result_t EchStatus(mn_ech_status_t *out_status) const;

  void OnReceivedRedirect(net::URLRequest *request,
                          const net::RedirectInfo &redirect_info,
                          bool *defer_redirect) override;
  void OnAuthRequired(net::URLRequest *request,
                      const net::AuthChallengeInfo &auth_info) override;
  // Overridden so a rejected certificate reports its own error code; the base
  // implementation cancels with ERR_ABORTED and loses it.
  void OnSSLCertificateError(net::URLRequest *request, int net_error,
                             const net::SSLInfo &ssl_info, bool fatal) override;
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
  void PostReadMore();
  // Returns the read buffer to the network thread and continues or pauses.
  // Separate from PostReadMore because the buffer has to be stored back on the
  // thread that will refill it, not on the callback thread that just read it.
  void PostReturnBuffer(scoped_refptr<net::IOBufferWithSize> buffer,
                        mn_read_disposition_t disposition);
  void ReturnBufferAndContinue(scoped_refptr<net::IOBufferWithSize> buffer,
                               mn_read_disposition_t disposition);
  // Runs on the callback runner once on_body has returned MN_READ_PAUSE.
  void PauseRead();

  scoped_refptr<Engine> engine_;
  scoped_refptr<base::SequencedTaskRunner> callback_runner_;
  std::string url_;
  std::string method_;
  std::vector<std::pair<std::string, std::string>> headers_;
  std::vector<uint8_t> body_;
  mn_upload_mode_t upload_mode_ = MN_UPLOAD_NONE;
  mn_cache_mode_t cache_mode_ = MN_CACHE_DEFAULT;
  mn_redirect_mode_t redirect_mode_ = MN_REDIRECT_FOLLOW;
  // Set from mn_request_config_t.abi 12. ABI 11 callers leave it
  // false, so their behaviour is untouched.
  bool strip_auth_on_cross_origin_redirect = false;
  mn_request_priority_t priority_ = MN_REQUEST_PRIORITY_DEFAULT;
  mn_request_callbacks_t callbacks_{};
  mn_request_t *public_handle_ = nullptr;
  uint64_t timeout_ms_ = 0;
  std::unique_ptr<net::URLRequest> request_;
  std::unique_ptr<net::ChunkedUploadDataStream::Writer> upload_writer_;
  scoped_refptr<net::IOBufferWithSize> read_buffer_;
  // ABI 12: buffers handed to the caller by returning MN_READ_TAKE_BUFFER.
  // Keyed by the data pointer the callback received, so release can prove the
  // pointer came from this request and refuse a double release.
  std::map<const uint8_t *, scoped_refptr<net::IOBufferWithSize>>
      taken_buffers_;
  // Guards taken_buffers_: the callback task runs on callback_runner_ while
  // release may arrive from any thread.
  base::Lock taken_buffers_lock_;
  // Set when a chunk was delivered with MN_READ_PAUSE and the buffer came back:
  // that chunk is consumed but the next read must not start until resume_read,
  // so the buffer waits here. Without it the pause would either drop the buffer
  // (back to one allocation per chunk) or lose the pause.
  bool buffer_held_paused_ = false;
  base::OneShotTimer timeout_timer_;
  scoped_refptr<Request> keep_alive_;
  std::atomic_bool started_{false};
  std::atomic_bool completed_{false};
  // Set on the network thread once a response header block has arrived. The v9
  // getters read these instead of response_info(), because request_ is reset
  // when the request completes and a caller may still ask afterwards.
  std::atomic_bool response_started_{false};
  std::atomic<int> negotiated_protocol_{MN_HTTP_VERSION_UNKNOWN};
  std::atomic<int> ech_status_{MN_ECH_NOT_ATTEMPTED};
  base::Lock upload_lock_;
  bool upload_finished_ = false;
  std::atomic_bool redirect_deferred_{false};
  // Read flow control. PauseRead and ResumeRead run on different threads, so
  // the third state records a resume that arrived before the pause landed;
  // without it that resume would be dropped and the request would stall.
  enum ReadState : int {
    kReadReading = 0,
    kReadPaused = 1,
    kReadResumeRequested = 2,
  };
  std::atomic<int> read_state_{kReadReading};
};

} // namespace minicronet

#endif // MINICRONET_CORE_REQUEST_H_
