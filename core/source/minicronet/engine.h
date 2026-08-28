#ifndef MINICRONET_CORE_ENGINE_H_
#define MINICRONET_CORE_ENGINE_H_

#include <memory>
#include <string>
#include <string_view>
#include <vector>

#include "base/memory/ref_counted.h"
#include "base/memory/scoped_refptr.h"
#include "base/task/sequenced_task_runner.h"
#include "base/task/single_thread_task_runner.h"
#include "minicronet.h"
#include "net/proxy_resolution/proxy_config.h"
#include "minicronet/profile_context.h"

namespace net {
class URLRequestContext;
}

namespace minicronet {

bool ValidateCustomCaPem(std::string_view pem);

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
                                      ProfileContext profile);
  scoped_refptr<base::SingleThreadTaskRunner> task_runner() const;
  scoped_refptr<base::SequencedTaskRunner> callback_runner() const;
  net::URLRequestContext *context() const { return context_.get(); }
  const ProfileContext& profile() const { return profile_; }
  const std::string& user_agent() const { return user_agent_; }
  const std::string& proxy_username() const { return proxy_username_; }
  const std::string& proxy_password() const { return proxy_password_; }
  mn_protocol_mode_t protocol_mode() const { return protocol_mode_; }
  mn_tls_verify_mode_t tls_verify_mode() const { return tls_verify_mode_; }

private:
  friend class base::RefCountedThreadSafe<Engine>;

  Engine(std::string user_agent, std::string accept_language,
         net::ProxyConfig proxy_config, std::string proxy_username,
         std::string proxy_password, bool http_cache_enabled,
         mn_protocol_mode_t protocol_mode,
         mn_tls_verify_mode_t tls_verify_mode,
         std::string custom_ca_pem,
         ProfileContext profile);
  ~Engine();

  bool Start();
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
  std::unique_ptr<net::URLRequestContext> context_;
};

class Request;

} // namespace minicronet

#endif // MINICRONET_CORE_ENGINE_H_
