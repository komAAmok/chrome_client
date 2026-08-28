#ifndef MINICRONET_CORE_PROFILE_CONTEXT_H_
#define MINICRONET_CORE_PROFILE_CONTEXT_H_

#include <string>
#include <utility>

#include "minicronet/profile_table_generated.h"

namespace minicronet {

// Immutable identity for all state owned by one URLRequestContext.
// Historical profiles are data/evidence until the matching Chromium revision
// is compiled; activating them against another revision is rejected.
class ProfileContext final {
 public:
  ProfileContext() = default;
  static bool Create(std::string profile_id, std::string profile_namespace,
                     ProfileContext* out);
#if defined(MINICRONET_PROFILE_VERIFICATION)
  // Verification-only constructor path used to reject a caller that tries to
  // combine a profile with a different network Feature snapshot.
  static bool CreateWithFeatureFlags(std::string profile_id,
                                     std::string profile_namespace,
                                     NetworkFeatureFlags requested_flags,
                                     ProfileContext* out);
#endif

  ProfileContext(const ProfileContext&) = default;
  ProfileContext& operator=(const ProfileContext&) = delete;
  ProfileContext(ProfileContext&&) = default;
  ProfileContext& operator=(ProfileContext&&) = default;

  const std::string& id() const { return id_; }
  const std::string& name_space() const { return namespace_; }
  bool is_current() const { return data_ == nullptr; }
  const RuntimeProfileData* data() const { return data_; }
  NetworkFeatureFlags feature_flags() const { return feature_flags_; }
#if defined(MINICRONET_PROFILE_VERIFICATION)
  bool has_feature(NetworkFeature feature) const {
    return HasNetworkFeature(feature_flags_, feature);
  }
#endif
  bool wire_verified() const {
#if defined(MINICRONET_PROFILE_VERIFICATION)
    return true;
#else
    return is_current() || data_->wire_verified;
#endif
  }

 private:
  ProfileContext(std::string id, std::string name_space,
                 const RuntimeProfileData* data,
                 NetworkFeatureFlags feature_flags)
      : id_(std::move(id)),
        namespace_(std::move(name_space)),
        data_(data),
        feature_flags_(feature_flags) {}

  std::string id_;
  std::string namespace_;
  const RuntimeProfileData* data_ = nullptr;
  NetworkFeatureFlags feature_flags_ = kCurrentNetworkFeatureFlags;
};

}  // namespace minicronet

#endif  // MINICRONET_CORE_PROFILE_CONTEXT_H_
