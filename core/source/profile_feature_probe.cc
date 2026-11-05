#include "minicronet/profile_context.h"

#include <cstdio>
#include <string>

namespace {

bool Create(const char* id, minicronet::ProfileContext* out) {
  return minicronet::ProfileContext::Create(
      id, std::string("minicronet/") + id, out);
}

}  // namespace

int main() {
  minicronet::ProfileContext chrome122;
  minicronet::ProfileContext chrome123;
  if (!Create("chrome_122", &chrome122) ||
      !Create("chrome_123", &chrome123)) {
    return 1;
  }
  // kMaintainConnectionsOnIpv6TempAddrChange: upstream added the feature in
  // 142 (DISABLED) and flipped it to ENABLED at 148. Assert on the profiles
  // either side of that boundary so the table cannot drift silently.
  {
    minicronet::ProfileContext c147;
    minicronet::ProfileContext c148;
    minicronet::ProfileContext c157;
    if (!Create("chrome_147", &c147) || !Create("chrome_148", &c148) ||
        !Create("chrome_157", &c157)) {
      return 5;
    }
    if (c147.data()->maintain_ipv6_temp_addr ||
        !c148.data()->maintain_ipv6_temp_addr ||
        !c157.data()->maintain_ipv6_temp_addr) {
      return 6;
    }
  }
  if (!chrome122.has_feature(minicronet::NetworkFeature::kAlpsForHttp2) ||
      chrome122.has_feature(minicronet::NetworkFeature::kTryQuicByDefault) ||
      !chrome123.has_feature(minicronet::NetworkFeature::kTryQuicByDefault) ||
      !chrome122.data() || !chrome122.data()->enable_quic ||
      chrome122.data()->try_quic_by_default ||
      !chrome123.data() || !chrome123.data()->enable_quic ||
      !chrome123.data()->try_quic_by_default ||
      chrome122.feature_flags() == chrome123.feature_flags()) {
    return 2;
  }

  minicronet::ProfileContext rejected;
  if (minicronet::ProfileContext::CreateWithFeatureFlags(
          "chrome_122", "minicronet/chrome_122",
          chrome122.feature_flags() |
              static_cast<minicronet::NetworkFeatureFlags>(
                  minicronet::NetworkFeature::kTryQuicByDefault),
          &rejected)) {
    return 3;
  }
  if (!minicronet::ProfileContext::CreateWithFeatureFlags(
          "chrome_122", "minicronet/chrome_122", chrome122.feature_flags(),
          &rejected)) {
    return 4;
  }
  std::puts("network feature snapshot isolation passed");
  return 0;
}
