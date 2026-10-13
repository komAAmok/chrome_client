#include "minicronet/profile_ssl_config_service.h"

#include <utility>

#include "net/base/ech_mode.h"

namespace minicronet {

net::SSLContextConfig ProfileSSLConfigService::GetSSLContextConfig() {
  net::SSLContextConfig config =
      net::SSLConfigServiceDefaults::GetSSLContextConfig();
  config.profile_cipher_suites.assign(profile_.cipher_suites.begin(),
                                      profile_.cipher_suites.end());
  config.profile_signature_algorithms.assign(
      profile_.signature_algorithms.begin(),
      profile_.signature_algorithms.end());
  config.profile_tls_extension_ids.assign(profile_.tls_extension_ids.begin(),
                                          profile_.tls_extension_ids.end());
  config.profile_tls_extension_order_policy =
      static_cast<uint8_t>(profile_.tls_extension_order_policy);
  config.profile_tls_grease_enabled = profile_.tls_grease_enabled;
  config.profile_tls_grease_per_connection =
      profile_.tls_grease_per_connection;
  config.profile_grease_signature_algorithms =
      profile_.grease_signature_algorithms;
  config.profile_client_hello_padding_enabled =
      profile_.client_hello_padding_enabled;
  config.profile_client_hello_padding_length =
      profile_.client_hello_padding_length;
  config.supported_named_groups.clear();
  for (size_t i = 0; i < profile_.curves.size(); ++i) {
    config.supported_named_groups.push_back(
        net::SSLNamedGroupInfo{profile_.curves[i],
                               i < profile_.key_share_count});
  }
  config.profile_use_new_alps_codepoint = profile_.use_new_alps_codepoint;
  // net::SSLContextConfig::trust_anchor_ids is the encoded
  // RequestedTrustAnchorList itself (a series of non-empty 8-bit
  // length-prefixed IDs, without the outer 16-bit length), which is exactly the
  // form the profile table stores, so it is copied through unchanged. Leaving
  // the field unset keeps ShouldAdvertiseTrustAnchorIDs() false and the
  // extension off the wire for every profile before 152.
  //
  // The stored order is the capture's observed order and is not significant:
  // upstream sorts the list inside EncodeTlsRequestedTrustAnchorIDList, and the
  // real browser's order comes from an absl::flat_hash_set whose iteration order
  // varies per process.
  if (!profile_.trust_anchor_ids.empty()) {
    config.trust_anchor_ids =
        std::vector<uint8_t>(profile_.trust_anchor_ids.begin(),
                             profile_.trust_anchor_ids.end());
  }
  return config;
}

net::EchMode ProfileSSLConfigService::GetEchMode(
    std::string_view /*hostname*/) const {
  return profile_.ech_enabled ? net::EchMode::kOpportunistic
                              : net::EchMode::kDisabled;
}

}  // namespace minicronet
