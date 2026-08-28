#include "minicronet/profile_context.h"

namespace minicronet {

bool ProfileContext::Create(std::string profile_id,
                            std::string profile_namespace,
                            ProfileContext* out) {
  if (!out) {
    return false;
  }
  if (profile_id.empty()) {
    profile_id = "chromium_current";
  }
  const std::string expected = "minicronet/" + profile_id;
  if (profile_namespace.empty()) {
    profile_namespace = expected;
  }
  if (profile_namespace != expected) {
    return false;
  }
  const RuntimeProfileData* data = nullptr;
  if (profile_id != "chromium_current") {
    for (const RuntimeProfileData& candidate : kRuntimeProfiles) {
      if (profile_id == candidate.id) {
        data = &candidate;
        break;
      }
    }
    if (!data) {
      return false;
    }
  }
  const NetworkFeatureFlags flags =
      data ? data->network_feature_flags : kCurrentNetworkFeatureFlags;
  *out = ProfileContext(std::move(profile_id), std::move(profile_namespace),
                        data, flags);
  return true;
}

#if defined(MINICRONET_PROFILE_VERIFICATION)
bool ProfileContext::CreateWithFeatureFlags(
    std::string profile_id, std::string profile_namespace,
    NetworkFeatureFlags requested_flags, ProfileContext* out) {
  ProfileContext candidate;
  if (!Create(std::move(profile_id), std::move(profile_namespace),
              &candidate)) {
    return false;
  }
  if (candidate.feature_flags() != requested_flags) {
    return false;
  }
  *out = std::move(candidate);
  return true;
}
#endif

}  // namespace minicronet
