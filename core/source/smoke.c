#include "minicronet.h"

#ifdef UNSAFE_BUFFERS_BUILD
#pragma allow_unsafe_buffers
#endif

#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#if defined(_WIN32)
#include <windows.h>
#else
#include <unistd.h>
#endif

#define CHECK(condition)                                                       \
  do {                                                                         \
    if (!(condition)) {                                                        \
      fprintf(stderr, "CHECK failed at %s:%d: %s\n", __FILE__, __LINE__,       \
              #condition);                                                     \
      abort();                                                                 \
    }                                                                          \
  } while (0)

typedef struct request_state {
  atomic_int done;
  atomic_int complete_count;
  int status_code;
  size_t body_bytes;
  size_t body_stored;
  char body[16385];
  size_t headers_stored;
  char headers[4097];
  mn_result_t result;
  int net_error;
  int redirect_count;
  int follow_redirect;
  int release_callback_ref;
} request_state_t;

static void MN_CALL on_response(void *user_data, mn_request_t *request,
                                int status_code, const char *headers,
                                size_t headers_length) {
  request_state_t *state = user_data;
  state->status_code = status_code;
  CHECK(headers || headers_length == 0);
  size_t copied = headers_length < sizeof(state->headers) - 1
                      ? headers_length
                      : sizeof(state->headers) - 1;
  memcpy(state->headers, headers, copied);
  state->headers[copied] = '\0';
  state->headers_stored = copied;
}

static mn_read_disposition_t MN_CALL on_body(void *user_data,
                                            mn_request_t *request,
                                            const uint8_t *data,
                                            size_t data_length) {
  request_state_t *state = user_data;
  CHECK(data || data_length == 0);
  state->body_bytes += data_length;
  size_t available = sizeof(state->body) - 1 - state->body_stored;
  size_t copied = data_length < available ? data_length : available;
  memcpy(state->body + state->body_stored, data, copied);
  state->body_stored += copied;
  state->body[state->body_stored] = '\0';
  /* This smoke consumes the body inline, so reads never need pausing. */
  return MN_READ_CONTINUE;
}

static void MN_CALL on_complete(void *user_data, mn_request_t *request,
                                mn_result_t result, int net_error) {
  request_state_t *state = user_data;
  state->result = result;
  state->net_error = net_error;
  if (state->release_callback_ref) {
    state->release_callback_ref = 0;
    mn_request_release(request);
  }
  atomic_fetch_add(&state->complete_count, 1);
  atomic_store(&state->done, 1);
}

static void MN_CALL on_redirect(void *user_data, mn_request_t *request,
                                int status_code, const char *headers,
                                size_t headers_length, const char *new_url,
                                size_t new_url_length, const char *new_method,
                                size_t new_method_length) {
  request_state_t *state = user_data;
  (void)status_code;
  (void)headers;
  (void)headers_length;
  (void)new_url;
  (void)new_url_length;
  (void)new_method;
  (void)new_method_length;
  ++state->redirect_count;
  if (state->follow_redirect) {
    CHECK(mn_request_follow_redirect(request) == MN_OK);
  }
}

static void sleep_millisecond(void) {
#if defined(_WIN32)
  Sleep(1);
#else
  usleep(1000);
#endif
}

static request_state_t run_request_ex(mn_engine_t *engine, const char *url,
                                      const char *method, const uint8_t *body,
                                      size_t body_length,
                                      mn_upload_mode_t upload_mode,
                                      mn_cache_mode_t cache_mode,
                                      mn_redirect_mode_t redirect_mode,
                                      uint64_t timeout_ms, int cancel) {
  request_state_t state = {0};
  state.release_callback_ref = 1;
  state.follow_redirect = redirect_mode == MN_REDIRECT_MANUAL;
  mn_request_config_t config = {0};
  config.size = sizeof(config);
  config.version = MN_ABI_VERSION;
  config.url = url;
  config.url_length = strlen(url);
  config.method = method;
  config.method_length = strlen(method);
  config.timeout_ms = timeout_ms;
  if (upload_mode == MN_UPLOAD_FIXED) {
    config.body = body;
    config.body_length = body_length;
  }
  config.upload_mode = upload_mode;
  config.cache_mode = cache_mode;
  config.redirect_mode = redirect_mode;
  config.callbacks.size = sizeof(config.callbacks);
  config.callbacks.version = MN_ABI_VERSION;
  config.callbacks.user_data = &state;
  config.callbacks.on_response = on_response;
  config.callbacks.on_body = on_body;
  config.callbacks.on_complete = on_complete;
  config.callbacks.on_redirect = on_redirect;

  mn_request_t *request = NULL;
  CHECK(mn_request_create(engine, &config, &request) == MN_OK);
  if (state.release_callback_ref) {
    mn_request_retain(request);
  }
  CHECK(mn_request_start(request) == MN_OK);
  CHECK(mn_request_start(request) == MN_ERROR_INVALID_STATE);
  if (upload_mode == MN_UPLOAD_CHUNKED) {
    size_t split = body_length / 2;
    CHECK(mn_request_upload_write(request, body, split, 0) == MN_OK);
    CHECK(mn_request_upload_write(request, body + split, body_length - split,
                                  1) == MN_OK);
  }
  if (cancel) {
    const char *delay_value = getenv("MINICRONET_CANCEL_DELAY_MS");
    int delay_ms = delay_value ? atoi(delay_value) : 0;
    CHECK(delay_ms >= 0 && delay_ms <= 1000);
    for (int i = 0; i < delay_ms; ++i) {
      sleep_millisecond();
    }
    CHECK(mn_request_cancel(request) == MN_OK);
  }
  mn_request_release(request);

  for (int i = 0; i < 10000 && !atomic_load(&state.done); ++i) {
    sleep_millisecond();
  }
  CHECK(atomic_load(&state.done));
  sleep_millisecond();
  CHECK(atomic_load(&state.complete_count) == 1);
  return state;
}

static request_state_t run_request(mn_engine_t *engine, const char *url,
                                   uint64_t timeout_ms, int cancel) {
  return run_request_ex(engine, url, "GET", NULL, 0, MN_UPLOAD_NONE,
                        MN_CACHE_DEFAULT, MN_REDIRECT_FOLLOW, timeout_ms,
                        cancel);
}

static void run_concurrent(mn_engine_t *engine, const char *url, int count) {
  request_state_t *states = calloc((size_t)count, sizeof(*states));
  mn_request_t **requests = calloc((size_t)count, sizeof(*requests));
  CHECK(states && requests);
  for (int i = 0; i < count; ++i) {
    mn_request_config_t config = {0};
    config.size = sizeof(config);
    config.version = MN_ABI_VERSION;
    config.url = url;
    config.url_length = strlen(url);
    config.method = "GET";
    config.method_length = 3;
    config.cache_mode = MN_CACHE_BYPASS;
    config.callbacks.size = sizeof(config.callbacks);
    config.callbacks.version = MN_ABI_VERSION;
    config.callbacks.user_data = &states[i];
    config.callbacks.on_response = on_response;
    config.callbacks.on_body = on_body;
    config.callbacks.on_complete = on_complete;
    CHECK(mn_request_create(engine, &config, &requests[i]) == MN_OK);
    CHECK(mn_request_start(requests[i]) == MN_OK);
    mn_request_retain(requests[i]);
  }
  for (int i = 0; i < count; ++i) {
    mn_request_release(requests[i]);
    mn_request_release(requests[i]);
  }
  for (int tick = 0; tick < 10000; ++tick) {
    int complete = 0;
    for (int i = 0; i < count; ++i) {
      complete += atomic_load(&states[i].done) != 0;
    }
    if (complete == count) {
      break;
    }
    sleep_millisecond();
  }
  for (int i = 0; i < count; ++i) {
    CHECK(atomic_load(&states[i].done));
    CHECK(atomic_load(&states[i].complete_count) == 1);
    CHECK(states[i].result == MN_OK);
    CHECK(states[i].status_code == 200);
    CHECK(states[i].body_bytes > 0);
  }
  free(requests);
  free(states);
}

static void test_validation(mn_engine_t *engine) {
  mn_request_t *request = NULL;
  mn_request_config_t config = {0};
  config.size = sizeof(config);
  config.version = MN_ABI_VERSION;
  config.url = "http://127.0.0.1/";
  config.url_length = strlen(config.url);
  config.method = "BAD:METHOD";
  config.method_length = strlen(config.method);
  config.callbacks.size = sizeof(config.callbacks);
  config.callbacks.version = MN_ABI_VERSION;
  config.callbacks.on_complete = on_complete;
  CHECK(mn_request_create(engine, &config, &request) ==
        MN_ERROR_INVALID_ARGUMENT);
  CHECK(request == NULL);

  config.method = "GET";
  config.method_length = 3;
  config.priority = (mn_request_priority_t)99;
  CHECK(mn_request_create(engine, &config, &request) ==
        MN_ERROR_INVALID_ARGUMENT);
  CHECK(request == NULL);
  config.priority = MN_REQUEST_PRIORITY_DEFAULT;

  config.size = sizeof(config) - 1;
  CHECK(mn_request_create(engine, &config, &request) ==
        MN_ERROR_INVALID_ARGUMENT);
  config.size = sizeof(config);
  config.callbacks.size = sizeof(config.callbacks) - 1;
  CHECK(mn_request_create(engine, &config, &request) ==
        MN_ERROR_INVALID_ARGUMENT);
  config.callbacks.size = sizeof(config.callbacks);
  config.version = MN_ABI_VERSION - 1;
  CHECK(mn_request_create(engine, &config, &request) ==
        MN_ERROR_UNSUPPORTED_ABI);
  config.version = MN_ABI_VERSION;
  config.callbacks.version = MN_ABI_VERSION - 1;
  CHECK(mn_request_create(engine, &config, &request) ==
        MN_ERROR_UNSUPPORTED_ABI);

  config.callbacks.version = MN_ABI_VERSION;
  CHECK(mn_request_create(engine, &config, &request) == MN_OK);
  mn_request_retain(request);
  mn_request_release(request);
  mn_request_release(request);
  request = NULL;
}

static void test_profile_gate(void) {
  mn_engine_config_t config = {0};
  config.size = sizeof(config);
  config.version = MN_ABI_VERSION;
  config.profile_id = "chrome_98";
  config.profile_id_length = strlen(config.profile_id);
  config.profile_namespace = "minicronet/chrome_98";
  config.profile_namespace_length = strlen(config.profile_namespace);

  mn_engine_t *engine = NULL;
  CHECK(mn_engine_create(&config, &engine) == MN_ERROR_PROFILE_UNSUPPORTED);
  CHECK(engine == NULL);
}

int main(int argc, char **argv) {
  CHECK(mn_abi_version() == MN_ABI_VERSION);
  CHECK(strlen(mn_version_string()) > 0);
  test_profile_gate();

  mn_engine_config_t historical = {0};
  historical.size = sizeof(historical);
  historical.version = MN_ABI_VERSION;
  historical.profile_id = "chrome_151";
  historical.profile_id_length = strlen(historical.profile_id);
  historical.profile_namespace = "minicronet/chrome_151";
  historical.profile_namespace_length = strlen(historical.profile_namespace);
  mn_engine_t *historical_engine = NULL;
  CHECK(mn_engine_create(&historical, &historical_engine) == MN_OK);
  CHECK(historical_engine != NULL);
  mn_engine_release(historical_engine);
  historical.user_agent =
      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36";
  historical.user_agent_length = strlen(historical.user_agent);
  CHECK(mn_engine_create(&historical, &historical_engine) == MN_OK);
  CHECK(historical_engine != NULL);
  mn_engine_release(historical_engine);
  historical.user_agent = "wrong-agent";
  historical.user_agent_length = strlen(historical.user_agent);
  CHECK(mn_engine_create(&historical, &historical_engine) ==
        MN_ERROR_PROFILE_CONFLICT);
  CHECK(historical_engine == NULL);

  mn_engine_config_t config = {0};
  config.size = sizeof(config);
  config.version = MN_ABI_VERSION;
  config.user_agent = getenv("MINICRONET_USER_AGENT");
  if (!config.user_agent) {
    config.user_agent = "minicronet-smoke/0.1";
  }
  config.user_agent_length = strlen(config.user_agent);
  config.accept_language = "en-US,en;q=0.9";
  config.accept_language_length = strlen(config.accept_language);
  const char *profile_id = getenv("MINICRONET_PROFILE_ID");
  char profile_namespace[128];
  if (profile_id) {
    int length = snprintf(profile_namespace, sizeof(profile_namespace),
                          "minicronet/%s", profile_id);
    CHECK(length > 0 && (size_t)length < sizeof(profile_namespace));
    config.profile_id = profile_id;
    config.profile_id_length = strlen(profile_id);
    config.profile_namespace = profile_namespace;
    config.profile_namespace_length = (size_t)length;
  }
  config.proxy_rules = getenv("MINICRONET_PROXY_RULES");
  if (config.proxy_rules) {
    config.proxy_rules_length = strlen(config.proxy_rules);
  }
  config.proxy_username = getenv("MINICRONET_PROXY_USERNAME");
  if (config.proxy_username) {
    config.proxy_username_length = strlen(config.proxy_username);
  }
  config.proxy_password = getenv("MINICRONET_PROXY_PASSWORD");
  if (config.proxy_password) {
    config.proxy_password_length = strlen(config.proxy_password);
  }
  mn_protocol_mode_t requested_protocol_mode = MN_PROTOCOL_NATIVE;
  const char *protocol_mode = getenv("MINICRONET_PROTOCOL_MODE");
  if (protocol_mode) {
    if (strcmp(protocol_mode, "h1") == 0) {
      requested_protocol_mode = MN_PROTOCOL_FORCE_H1;
    } else if (strcmp(protocol_mode, "h2") == 0) {
      requested_protocol_mode = MN_PROTOCOL_FORCE_H2;
    } else if (strcmp(protocol_mode, "h3") == 0) {
      requested_protocol_mode = MN_PROTOCOL_FORCE_H3;
    } else {
      CHECK(strcmp(protocol_mode, "native") == 0);
    }
  }
  config.protocol_mode = requested_protocol_mode;

  mn_engine_t *engine = NULL;
  mn_engine_config_t invalid_config = config;
  invalid_config.user_agent = "bad\nagent";
  invalid_config.user_agent_length = strlen(invalid_config.user_agent);
  CHECK(mn_engine_create(&invalid_config, &engine) ==
        MN_ERROR_INVALID_ARGUMENT);
  CHECK(engine == NULL);
  invalid_config = config;
  invalid_config.accept_language = "bad\nlanguage";
  invalid_config.accept_language_length =
      strlen(invalid_config.accept_language);
  CHECK(mn_engine_create(&invalid_config, &engine) ==
        MN_ERROR_INVALID_ARGUMENT);
  CHECK(engine == NULL);
  invalid_config = config;
  invalid_config.http_cache_mode = (mn_http_cache_mode_t)99;
  CHECK(mn_engine_create(&invalid_config, &engine) ==
        MN_ERROR_INVALID_ARGUMENT);
  CHECK(engine == NULL);
  invalid_config = config;
  invalid_config.protocol_mode = (mn_protocol_mode_t)99;
  CHECK(mn_engine_create(&invalid_config, &engine) ==
        MN_ERROR_INVALID_ARGUMENT);
  CHECK(engine == NULL);
  invalid_config = config;
  invalid_config.tls_verify_mode = (mn_tls_verify_mode_t)99;
  CHECK(mn_engine_create(&invalid_config, &engine) ==
        MN_ERROR_INVALID_ARGUMENT);
  CHECK(engine == NULL);
  invalid_config = config;
  invalid_config.tls_verify_mode = MN_TLS_VERIFY_CUSTOM_CA;
  CHECK(mn_engine_create(&invalid_config, &engine) ==
        MN_ERROR_INVALID_ARGUMENT);
  CHECK(engine == NULL);
  invalid_config.custom_ca_pem = (const uint8_t *)"not a certificate";
  invalid_config.custom_ca_pem_length = strlen((const char *)invalid_config.custom_ca_pem);
  CHECK(mn_engine_create(&invalid_config, &engine) ==
        MN_ERROR_INVALID_ARGUMENT);
  CHECK(engine == NULL);
  invalid_config = config;
  invalid_config.tls_verify_mode = MN_TLS_VERIFY_INSECURE;
  invalid_config.custom_ca_pem = (const uint8_t *)"x";
  invalid_config.custom_ca_pem_length = 1;
  CHECK(mn_engine_create(&invalid_config, &engine) ==
        MN_ERROR_INVALID_ARGUMENT);
  CHECK(engine == NULL);
  invalid_config = config;
  invalid_config.profile_id = "chromium_current";
  invalid_config.profile_id_length = strlen(invalid_config.profile_id);
  invalid_config.profile_namespace = "minicronet/wrong";
  invalid_config.profile_namespace_length = strlen(invalid_config.profile_namespace);
  CHECK(mn_engine_create(&invalid_config, &engine) ==
        MN_ERROR_PROFILE_CONFLICT);
  CHECK(engine == NULL);
  invalid_config.profile_namespace = "minicronet/chromium_current";
  invalid_config.profile_namespace_length = strlen(invalid_config.profile_namespace);
  invalid_config.profile_id = "chrome_151";
  invalid_config.profile_id_length = strlen(invalid_config.profile_id);
  invalid_config.profile_namespace = "minicronet/chrome_151";
  invalid_config.profile_namespace_length = strlen(invalid_config.profile_namespace);
  CHECK(mn_engine_create(&invalid_config, &engine) ==
        MN_ERROR_PROFILE_CONFLICT);
  CHECK(engine == NULL);
  invalid_config.user_agent = NULL;
  invalid_config.user_agent_length = 0;
  CHECK(mn_engine_create(&invalid_config, &engine) == MN_OK);
  CHECK(engine != NULL);
  mn_engine_release(engine);
  engine = NULL;
  invalid_config = config;
  invalid_config.profile_id = "chromium_current";
  invalid_config.profile_id_length = strlen(invalid_config.profile_id);
  invalid_config.profile_namespace = "minicronet/chromium_current";
  invalid_config.profile_namespace_length = strlen(invalid_config.profile_namespace);
  CHECK(mn_engine_create(&invalid_config, &engine) == MN_OK);
  CHECK(engine != NULL);
  mn_engine_release(engine);
  engine = NULL;
  invalid_config = config;
  invalid_config.version = MN_ABI_VERSION - 1;
  CHECK(mn_engine_create(&invalid_config, &engine) == MN_ERROR_UNSUPPORTED_ABI);
  CHECK(engine == NULL);
  config.size = sizeof(config) - 1;
  CHECK(mn_engine_create(&config, &engine) == MN_ERROR_INVALID_ARGUMENT);
  CHECK(engine == NULL);
  config.size = sizeof(config);
  CHECK(mn_engine_create(&config, &engine) == MN_OK);
  CHECK(engine != NULL);
  mn_engine_t *insecure_engine = NULL;
  mn_engine_config_t insecure_config = config;
  insecure_config.tls_verify_mode = MN_TLS_VERIFY_INSECURE;
  CHECK(mn_engine_create(&insecure_config, &insecure_engine) == MN_OK);
  CHECK(insecure_engine != NULL);
  mn_engine_release(insecure_engine);
  for (mn_protocol_mode_t mode = MN_PROTOCOL_FORCE_H1;
       mode <= MN_PROTOCOL_FORCE_H3; ++mode) {
    mn_engine_t *forced = NULL;
    config.protocol_mode = mode;
    CHECK(mn_engine_create(&config, &forced) == MN_OK);
    CHECK(forced != NULL);
    mn_engine_release(forced);
  }
  config.protocol_mode = requested_protocol_mode;
  mn_engine_retain(engine);
  mn_engine_release(engine);
  mn_engine_release(NULL);
  test_validation(engine);

  if (argc > 1) {
    const char *mode = argc > 2 ? argv[1] : "success";
    const char *url = argc > 2 ? argv[2] : argv[1];
    if (strcmp(mode, "cache-disabled") == 0) {
      mn_engine_release(engine);
      engine = NULL;
      config.http_cache_mode = MN_HTTP_CACHE_DISABLED;
      CHECK(mn_engine_create(&config, &engine) == MN_OK);
      request_state_t first = run_request(engine, url, 5000, 0);
      request_state_t second = run_request(engine, url, 5000, 0);
      CHECK(first.result == MN_OK);
      CHECK(second.result == MN_OK);
    } else if (strcmp(mode, "method") == 0 || strcmp(mode, "chunked") == 0) {
      CHECK(argc > 4);
      const char *method = argv[3];
      const uint8_t *body = (const uint8_t *)argv[4];
      size_t body_length = strlen(argv[4]);
      request_state_t state = run_request_ex(
          engine, url, method, body, body_length,
          strcmp(mode, "chunked") == 0 ? MN_UPLOAD_CHUNKED : MN_UPLOAD_FIXED,
          MN_CACHE_NO_STORE, MN_REDIRECT_FOLLOW, 5000, 0);
      CHECK(state.result == MN_OK);
      CHECK(state.status_code == 200);
      CHECK(strstr(state.body, method) != NULL);
      CHECK(strstr(state.body, argv[4]) != NULL);
    } else if (strcmp(mode, "redirect-manual") == 0) {
      request_state_t state =
          run_request_ex(engine, url, "GET", NULL, 0, MN_UPLOAD_NONE,
                         MN_CACHE_NO_STORE, MN_REDIRECT_MANUAL, 5000, 0);
      CHECK(state.result == MN_OK);
      CHECK(state.redirect_count > 0);
    } else if (strcmp(mode, "redirect-error") == 0) {
      request_state_t state =
          run_request_ex(engine, url, "GET", NULL, 0, MN_UPLOAD_NONE,
                         MN_CACHE_NO_STORE, MN_REDIRECT_ERROR, 5000, 0);
      CHECK(state.result == MN_ERROR_REDIRECT);
      CHECK(state.net_error != 0);
    } else if (strcmp(mode, "redirect-upload") == 0) {
      const uint8_t body[] = "redirect-payload";
      request_state_t state = run_request_ex(
          engine, url, "POST", body, sizeof(body) - 1, MN_UPLOAD_FIXED,
          MN_CACHE_NO_STORE, MN_REDIRECT_MANUAL, 5000, 0);
      CHECK(state.result == MN_OK);
      CHECK(state.redirect_count == 1);
      CHECK(strstr(state.body, "POST:redirect-payload") != NULL);
    } else if (strcmp(mode, "cookie") == 0) {
      char set_url[1024];
      char read_url[1024];
      CHECK(strlen(url) + 12 < sizeof(set_url));
      snprintf(set_url, sizeof(set_url), "%s/cookie-set", url);
      snprintf(read_url, sizeof(read_url), "%s/cookie-read", url);
      request_state_t set_state = run_request(engine, set_url, 5000, 0);
      CHECK(set_state.result == MN_OK);
      request_state_t read_state = run_request(engine, read_url, 5000, 0);
      CHECK(read_state.result == MN_OK);
      CHECK(strstr(read_state.body, "mn_cookie=1") != NULL);
    } else if (strcmp(mode, "cache-modes") == 0) {
      request_state_t state =
          run_request_ex(engine, url, "GET", NULL, 0, MN_UPLOAD_NONE,
                         MN_CACHE_DEFAULT, MN_REDIRECT_FOLLOW, 5000, 0);
      CHECK(state.result == MN_OK);
      state = run_request_ex(engine, url, "GET", NULL, 0, MN_UPLOAD_NONE,
                             MN_CACHE_VALIDATE, MN_REDIRECT_FOLLOW, 5000, 0);
      CHECK(state.result == MN_OK);
      state = run_request_ex(engine, url, "GET", NULL, 0, MN_UPLOAD_NONE,
                             MN_CACHE_BYPASS, MN_REDIRECT_FOLLOW, 5000, 0);
      CHECK(state.result == MN_OK);
      state =
          run_request_ex(engine, url, "GET", NULL, 0, MN_UPLOAD_NONE,
                         MN_CACHE_ONLY_IF_CACHED, MN_REDIRECT_FOLLOW, 5000, 0);
      CHECK(state.result == MN_OK);
    } else if (strcmp(mode, "cache-miss") == 0) {
      request_state_t state =
          run_request_ex(engine, url, "GET", NULL, 0, MN_UPLOAD_NONE,
                         MN_CACHE_ONLY_IF_CACHED, MN_REDIRECT_FOLLOW, 5000, 0);
      CHECK(state.result == MN_ERROR_CACHE_MISS);
      CHECK(state.net_error != 0);
    } else if (strcmp(mode, "concurrent") == 0) {
      CHECK(argc > 3);
      int count = atoi(argv[3]);
      CHECK(count > 0 && count <= 128);
      run_concurrent(engine, url, count);
    } else {
      int repetitions =
          (strcmp(mode, "protocol") == 0 || strcmp(mode, "header") == 0 ||
           strcmp(mode, "headers") == 0 || strcmp(mode, "dump") == 0) &&
                  argc > 4
              ? atoi(argv[4])
              : 1;
      CHECK(repetitions > 0 && repetitions <= 3);
      request_state_t state = {0};
      for (int i = 0; i < repetitions; ++i) {
        state =
            run_request(engine, url, strcmp(mode, "timeout") == 0 ? 25 : 5000,
                        strcmp(mode, "cancel") == 0);
      }
      if (strcmp(mode, "success") == 0) {
        if (requested_protocol_mode != MN_PROTOCOL_NATIVE &&
            requested_protocol_mode != MN_PROTOCOL_FORCE_H1 &&
            strncmp(url, "http://", 7) == 0) {
          CHECK(state.result == MN_ERROR_PROTOCOL);
        } else {
          CHECK(state.result == MN_OK);
          CHECK(state.net_error == 0);
          CHECK(state.status_code == 200);
          CHECK(state.body_bytes > 0);
        }
      } else if (strcmp(mode, "timeout") == 0) {
        CHECK(state.result == MN_ERROR_TIMEOUT);
        CHECK(state.net_error != 0);
        puts("request timeout smoke passed");
      } else if (strcmp(mode, "cancel") == 0) {
        CHECK(state.result == MN_ERROR_CANCELED);
        CHECK(state.net_error != 0);
        puts("request cancel smoke passed");
      } else if (strcmp(mode, "network-error") == 0) {
        CHECK(state.result != MN_OK);
        CHECK(state.net_error != 0);
      } else if (strcmp(mode, "protocol") == 0) {
        CHECK(argc > 3);
        CHECK(state.result == MN_OK);
        CHECK(strstr(state.body, argv[3]) != NULL);
      } else if (strcmp(mode, "header") == 0) {
        CHECK(argc > 3);
        CHECK(state.result == MN_OK);
        CHECK(strstr(state.headers, argv[3]) != NULL);
      } else if (strcmp(mode, "headers") == 0) {
        CHECK(state.result == MN_OK);
        fwrite(state.headers, 1, state.headers_stored, stdout);
      } else if (strcmp(mode, "dump") == 0) {
        CHECK(state.result == MN_OK);
        fwrite(state.body, 1, state.body_stored, stdout);
        fputc('\n', stdout);
      } else {
        CHECK(0 && "unknown mode");
      }
    }
  }

  mn_engine_release(engine);
  return 0;
}
