#ifndef MINICRONET_CORE_PROFILE_SSL_CONFIG_SERVICE_H_
#define MINICRONET_CORE_PROFILE_SSL_CONFIG_SERVICE_H_

#include "minicronet/profile_table_generated.h"
#include "net/ssl/ssl_config_service_defaults.h"

namespace minicronet {

class ProfileSSLConfigService final : public net::SSLConfigServiceDefaults {
 public:
  explicit ProfileSSLConfigService(const RuntimeProfileData& profile)
      : profile_(profile) {}

  net::SSLContextConfig GetSSLContextConfig() override;
  net::EchMode GetEchMode(std::string_view hostname) const override;

 private:
  const RuntimeProfileData& profile_;
};

}  // namespace minicronet

#endif  // MINICRONET_CORE_PROFILE_SSL_CONFIG_SERVICE_H_
