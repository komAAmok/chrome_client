#include "minicronet.h"

#include <cassert>
#include <cstring>
#include <string>

namespace {

void MN_CALL Complete(void*, mn_request_t*, mn_result_t, int) {}

mn_engine_t* Create(const char* profile, const char* user_agent = nullptr) {
  mn_engine_config_t config{};
  config.size = sizeof(config);
  config.version = MN_ABI_VERSION;
  config.profile_id = profile;
  config.profile_id_length = std::strlen(profile);
  const std::string namespace_value = std::string("minicronet/") + profile;
  config.profile_namespace = namespace_value.data();
  config.profile_namespace_length = namespace_value.size();
  config.user_agent = user_agent;
  config.user_agent_length = user_agent ? std::strlen(user_agent) : 0;
  mn_engine_t* engine = nullptr;
  assert(mn_engine_create(&config, &engine) == MN_OK);
  assert(engine != nullptr);
  return engine;
}

}  // namespace

int main() {
  mn_engine_t* current = Create("chromium_current");
  mn_engine_retain(current);
  mn_engine_release(current);
  mn_engine_release(current);
  mn_engine_release(nullptr);

  mn_engine_t* historical = Create("chrome_151");
  mn_engine_release(historical);

  mn_engine_config_t conflict{};
  conflict.size = sizeof(conflict);
  conflict.version = MN_ABI_VERSION;
  conflict.profile_id = "chrome_151";
  conflict.profile_id_length = 10;
  conflict.profile_namespace = "minicronet/chrome_151";
  conflict.profile_namespace_length = 21;
  conflict.user_agent = "wrong";
  conflict.user_agent_length = 5;
  mn_engine_t* rejected = nullptr;
  assert(mn_engine_create(&conflict, &rejected) == MN_ERROR_PROFILE_CONFLICT);
  assert(rejected == nullptr);
  (void)rejected;

  mn_engine_t* engine = Create("chromium_current");
  mn_request_config_t request_config{};
  request_config.size = sizeof(request_config);
  request_config.version = MN_ABI_VERSION;
  request_config.url = "http://127.0.0.1/";
  request_config.url_length = std::strlen(request_config.url);
  request_config.method = "GET";
  request_config.method_length = 3;
  request_config.callbacks.size = sizeof(request_config.callbacks);
  request_config.callbacks.version = MN_ABI_VERSION;
  request_config.callbacks.on_complete = Complete;
  mn_request_t* request = nullptr;
  assert(mn_request_create(engine, &request_config, &request) == MN_OK);
  mn_request_retain(request);
  mn_request_release(request);
  mn_request_release(request);
  mn_engine_release(engine);
  return 0;
}
