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
  config.ech_enabled = profile_.ech_enabled;
  config.profile_use_new_alps_codepoint = profile_.use_new_alps_codepoint;
  // The trust_anchors (0xca34) payload arrives as the encoded sequence
  // SelectAllTrustAnchorIDs() would have built, so it is split back into the
  // per-ID entries that function iterates. An empty span leaves the field empty,
  // which is what makes ShouldAdvertiseTrustAnchorIDs() false and keeps the
  // extension off the wire for every profile before 152.
  //
  // Only the set is profile data. The wire order is deliberately not: the field
  // is an absl::flat_hash_set, whose iteration order absl seeds from ASLR and so
  // varies per process. Reproducing a captured order would make every instance
  // emit the same permutation, which real Chrome does not.
  config.trust_anchor_ids.clear();
  for (size_t offset = 0; offset < profile_.trust_anchor_ids.size();) {
    const size_t length = profile_.trust_anchor_ids[offset];
    // The generator rejects a payload whose prefixes do not tile it exactly;
    // stop rather than read past the end if that ever changes.
    if (length == 0 ||
        offset + 1 + length > profile_.trust_anchor_ids.size()) {
      config.trust_anchor_ids.clear();
      break;
    }
    const auto id = profile_.trust_anchor_ids.subspan(offset + 1, length);
    config.trust_anchor_ids.emplace(id.begin(), id.end());
    offset += 1 + length;
  }
  return config;
}

net::EchMode ProfileSSLConfigService::GetEchMode(
    std::string_view /*hostname*/) const {
  return profile_.ech_enabled ? net::EchMode::kOpportunistic
                              : net::EchMode::kDisabled;
}

}  // namespace minicronet
