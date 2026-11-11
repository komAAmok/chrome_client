#ifndef MINICRONET_CORE_ENGINE_H_
#define MINICRONET_CORE_ENGINE_H_

#include <atomic>
#include <cstdint>
#include <deque>
#include <memory>
#include <string>
#include <string_view>
#include <vector>

#include "base/memory/ref_counted.h"
#include "base/memory/scoped_refptr.h"
#include "base/task/single_thread_task_runner.h"
#include "minicronet.h"
#include "net/proxy_resolution/proxy_config.h"
#include "minicronet/profile_context.h"

namespace net {
class URLRequestContext;
}

namespace minicronet {

bool ValidateCustomCaPem(std::string_view pem);

// The v9 engine options that arrive as strings or small enums. Grouped so the
// constructor stays readable and a future field cannot be added at one call
// site and forgotten at another.
struct EngineOptions {
  std::string host_mapping_rules;
  std::string dns_nameservers;
  std::string doh_template;
  std::vector<std::string> hsts_bypass_list;
  std::string proxy_chain;
  uint8_t dns_secure_mode = 0;
  uint8_t dns_over_tls = 0;
  uint8_t dns_rotate = 0;
  uint8_t dns_padding = 0;
  mn_platform_t platform = MN_PLATFORM_AUTO;
  // False suppresses the captured request-header set (mn_engine_config_t's
  // inject_client_hints == 0).
  bool inject_client_hints = true;
};

// One cookie as the Core hands it out: the owned bytes live in `storage` and
// the mn_cookie_t fields point into it. The ABI says a cookie's strings are
// valid only while the caller holds the result, so storage must outlive every
// mn_cookie_t read from it.
struct CookieView {
  std::vector<mn_cookie_t> cookies;
  // A deque, not a vector: the mn_cookie_t fields point into this container, and
  // a vector reallocation would invalidate every pointer already handed out.
  std::deque<std::string> storage;
};

class Engine final : public base::RefCountedThreadSafe<Engine> {
public:
  static scoped_refptr<Engine> Create(std::string user_agent,
                                      std::string accept_language,
                                      net::ProxyConfig proxy_config,
                                      std::string proxy_username,
                                      std::string proxy_password,
                                      bool http_cache_enabled,
                                      mn_protocol_mode_t protocol_mode,
                                      mn_tls_verify_mode_t tls_verify_mode,
                                      std::string custom_ca_pem,
                                      ProfileContext profile,
                                      EngineOptions options);
  scoped_refptr<base::SingleThreadTaskRunner> task_runner() const;
  net::URLRequestContext *context() const { return context_.get(); }
  const ProfileContext& profile() const { return profile_; }
  const std::string& user_agent() const { return user_agent_; }
  const std::string& proxy_username() const { return proxy_username_; }
  const std::string& proxy_password() const { return proxy_password_; }
  mn_protocol_mode_t protocol_mode() const { return protocol_mode_; }
  // sec-ch-ua-platform for this engine, derived from the same mn_platform_t as
  // the User-Agent group so the two cannot disagree.
  const char *platform_hint() const;
  bool inject_client_hints() const { return options_.inject_client_hints; }
  mn_tls_verify_mode_t tls_verify_mode() const { return tls_verify_mode_; }

  // v9. Each of these blocks until the network thread has finished, because a
  // caller that writes a cookie and then starts a request must not observe the
  // store from before the write.
  //
  // GetCookies fills *out with however many cookies the store holds, ignoring
  // any caller-supplied capacity; the ABI wrapper is what turns "more than the
  // caller asked for" into the documented count-only answer.
  //
  // The wait is on this engine's own runner and cannot deadlock with it: the
  // CookieMonster is built without a backing store, so DoCookieCallback() runs
  // the closure inline instead of reposting it
  // (net/cookies/cookie_monster.cc:2899-2915), and nothing here holds a lock
  // the network thread wants while it waits.
  mn_result_t GetCookies(const std::string *url, CookieView *out);
  mn_result_t SetCookie(const mn_cookie_t &cookie,
                        const std::string &source_url,
                        int64_t creation_unix_seconds);
  mn_result_t DeleteCookie(const std::string &domain,
                           const std::string &path,
                           const std::string &name, size_t *out_removed);
  mn_result_t ClearCookies();
  mn_result_t SetClientCertificate(const std::string &host, uint16_t port,
                                   const std::vector<uint8_t> &cert_pem,
                                   const std::vector<uint8_t> &key_pem);
  mn_result_t ClearClientCertificate(const std::string &host, uint16_t port);

private:
  friend class base::RefCountedThreadSafe<Engine>;

  Engine(std::string user_agent, std::string accept_language,
         net::ProxyConfig proxy_config, std::string proxy_username,
         std::string proxy_password, bool http_cache_enabled,
         mn_protocol_mode_t protocol_mode,
         mn_tls_verify_mode_t tls_verify_mode,
         std::string custom_ca_pem,
         ProfileContext profile, EngineOptions options);
  ~Engine();

  bool Start();
  // v9 cookie helpers. They run on the network thread; the public wrappers
  // post them there and wait, which is also what keeps the context alive.
  mn_result_t GetCookiesOnNetworkThread(const std::string *url, CookieView *out);
  mn_result_t SetCookieOnNetworkThread(const mn_cookie_t &cookie,
                                       const std::string &source_url,
                                       int64_t creation_unix_seconds);
  mn_result_t DeleteCookieOnNetworkThread(const std::string &domain,
                                          const std::string &path,
                                          const std::string &name,
                                          size_t *out_removed);
  mn_result_t ClearCookiesOnNetworkThread();
  mn_result_t SetClientCertificateOnNetworkThread(
      const std::string &host, uint16_t port,
      const std::vector<uint8_t> &cert_pem,
      const std::vector<uint8_t> &key_pem);
  mn_result_t ClearClientCertificateOnNetworkThread(const std::string &host,
                                                    uint16_t port);
  void InitializeOnNetworkThread();
  void ShutdownOnNetworkThread();

  std::string user_agent_;
  std::string accept_language_;
  net::ProxyConfig proxy_config_;
  const std::string proxy_username_;
  const std::string proxy_password_;
  bool http_cache_enabled_ = true;
  const mn_protocol_mode_t protocol_mode_;
  const mn_tls_verify_mode_t tls_verify_mode_;
  const std::string custom_ca_pem_;
  const ProfileContext profile_;
  const EngineOptions options_;
  //: Value of the Core's fork generation when this engine was created.  An
  //: engine whose generation no longer matches was created before a fork(), so
  //: its network thread is gone and must not be waited on; see engine.cc.
  const int fork_generation_;
  std::unique_ptr<net::URLRequestContext> context_;
};

class Request;

} // namespace minicronet

#endif // MINICRONET_CORE_ENGINE_H_
