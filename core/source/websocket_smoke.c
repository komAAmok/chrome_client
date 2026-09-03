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

typedef struct websocket_state {
  atomic_int done;
  atomic_int open_count;
  atomic_int message_count;
  atomic_int closing_count;
  atomic_int terminal_count;
  atomic_int validation_errors;
  mn_result_t result;
  int net_error;
  int response_code;
  int was_clean;
  uint16_t close_code;
  int server_close;
} websocket_state_t;

static void MN_CALL on_open(void *user_data, mn_websocket_t *websocket,
                            const char *protocol, size_t protocol_length,
                            const char *extensions, size_t extensions_length) {
  websocket_state_t *state = user_data;
  atomic_fetch_add(&state->open_count, 1);
  if (protocol_length != 4 || memcmp(protocol, "chat", 4) != 0 ||
      extensions_length != 0) {
    atomic_fetch_add(&state->validation_errors, 1);
  }
  if (state->server_close) {
    return;
  }
  static const uint8_t text[] = "client-text";
  static const uint8_t binary[] = {0, 1, 2, 255};
  CHECK(mn_websocket_send(websocket, MN_WEBSOCKET_MESSAGE_TEXT, text,
                          sizeof(text) - 1) == MN_OK);
  CHECK(mn_websocket_send(websocket, MN_WEBSOCKET_MESSAGE_BINARY, binary,
                          sizeof(binary)) == MN_OK);
}

static void MN_CALL on_message(void *user_data, mn_websocket_t *websocket,
                               mn_websocket_message_type_t type, int final,
                               const uint8_t *data, size_t data_length) {
  websocket_state_t *state = user_data;
  int index = atomic_fetch_add(&state->message_count, 1);
  int valid = 0;
  if (index == 0) {
    valid = type == MN_WEBSOCKET_MESSAGE_TEXT && !final && data_length == 4 &&
            memcmp(data, "frag", 4) == 0;
  } else if (index == 1) {
    valid = type == MN_WEBSOCKET_MESSAGE_CONTINUATION && final &&
            data_length == 4 && memcmp(data, "ment", 4) == 0;
  } else if (index == 2) {
    static const uint8_t expected[] = {9, 8, 7, 0};
    valid = type == MN_WEBSOCKET_MESSAGE_BINARY && final &&
            data_length == sizeof(expected) &&
            memcmp(data, expected, sizeof(expected)) == 0;
  } else if (index == 3) {
    valid = type == MN_WEBSOCKET_MESSAGE_TEXT && final && data_length == 11 &&
            memcmp(data, "client-text", 11) == 0;
  } else if (index == 4) {
    static const uint8_t expected[] = {0, 1, 2, 255};
    valid = type == MN_WEBSOCKET_MESSAGE_BINARY && final &&
            data_length == sizeof(expected) &&
            memcmp(data, expected, sizeof(expected)) == 0;
  }
  if (!valid) {
    atomic_fetch_add(&state->validation_errors, 1);
  }
  if (index == 4) {
    CHECK(mn_websocket_close(websocket, 1000, "done", 4) == MN_OK);
  }
}

static void MN_CALL on_closing(void *user_data, mn_websocket_t *websocket) {
  websocket_state_t *state = user_data;
  atomic_fetch_add(&state->closing_count, 1);
}

static void MN_CALL on_closed(void *user_data, mn_websocket_t *websocket,
                              int was_clean, uint16_t code, const char *reason,
                              size_t reason_length) {
  websocket_state_t *state = user_data;
  state->was_clean = was_clean;
  state->close_code = code;
  const char *expected = state->server_close ? "server" : "done";
  size_t expected_length = state->server_close ? 6 : 4;
  if (reason_length != expected_length ||
      memcmp(reason, expected, expected_length) != 0) {
    atomic_fetch_add(&state->validation_errors, 1);
  }
  atomic_fetch_add(&state->terminal_count, 1);
  atomic_store(&state->done, 1);
}

static void MN_CALL on_failure(void *user_data, mn_websocket_t *websocket,
                               mn_result_t result, int net_error,
                               int response_code, const char *message,
                               size_t message_length) {
  websocket_state_t *state = user_data;
  state->result = result;
  state->net_error = net_error;
  state->response_code = response_code;
  atomic_fetch_add(&state->terminal_count, 1);
  atomic_store(&state->done, 1);
}

static void sleep_millisecond(void) {
#if defined(_WIN32)
  Sleep(1);
#else
  usleep(1000);
#endif
}

static void test_validation(mn_engine_t *engine, mn_websocket_config_t config) {
  mn_websocket_t *websocket = NULL;
  mn_websocket_config_t invalid = config;
  invalid.size = sizeof(invalid) - 1;
  CHECK(mn_websocket_create(engine, &invalid, &websocket) ==
        MN_ERROR_INVALID_ARGUMENT);
  invalid = config;
  invalid.callbacks.size = sizeof(invalid.callbacks) - 1;
  CHECK(mn_websocket_create(engine, &invalid, &websocket) ==
        MN_ERROR_INVALID_ARGUMENT);
  invalid = config;
  invalid.version = MN_ABI_VERSION - 1;
  CHECK(mn_websocket_create(engine, &invalid, &websocket) ==
        MN_ERROR_UNSUPPORTED_ABI);
  invalid = config;
  invalid.callbacks.version = MN_ABI_VERSION - 1;
  CHECK(mn_websocket_create(engine, &invalid, &websocket) ==
        MN_ERROR_UNSUPPORTED_ABI);
  invalid = config;
  invalid.url = "https://example.test";
  invalid.url_length = strlen(invalid.url);
  CHECK(mn_websocket_create(engine, &invalid, &websocket) ==
        MN_ERROR_INVALID_ARGUMENT);
  invalid = config;
  invalid.url = "ws://example.test/#fragment";
  invalid.url_length = strlen(invalid.url);
  CHECK(mn_websocket_create(engine, &invalid, &websocket) ==
        MN_ERROR_INVALID_ARGUMENT);
  invalid = config;
  invalid.origin = "/relative";
  invalid.origin_length = strlen(invalid.origin);
  CHECK(mn_websocket_create(engine, &invalid, &websocket) ==
        MN_ERROR_INVALID_ARGUMENT);
  mn_string_t invalid_protocol = {"bad,protocol", 12};
  invalid = config;
  invalid.protocols = &invalid_protocol;
  CHECK(mn_websocket_create(engine, &invalid, &websocket) ==
        MN_ERROR_INVALID_ARGUMENT);
  mn_string_t duplicate_protocols[] = {{"chat", 4}, {"chat", 4}};
  invalid = config;
  invalid.protocols = duplicate_protocols;
  invalid.protocol_count = 2;
  CHECK(mn_websocket_create(engine, &invalid, &websocket) ==
        MN_ERROR_INVALID_ARGUMENT);
  mn_header_t invalid_header = {"Origin", 6, "https://other.test", 18};
  invalid = config;
  invalid.headers = &invalid_header;
  CHECK(mn_websocket_create(engine, &invalid, &websocket) ==
        MN_ERROR_INVALID_ARGUMENT);
  invalid_header.name = "User-Agent";
  invalid_header.name_length = 10;
  invalid_header.value = "override";
  invalid_header.value_length = 8;
  CHECK(mn_websocket_create(engine, &invalid, &websocket) ==
        MN_ERROR_INVALID_ARGUMENT);

  CHECK(mn_websocket_create(engine, &config, &websocket) == MN_OK);
  static const uint8_t invalid_utf8[] = {0xc0, 0x80};
  CHECK(mn_websocket_send(websocket, MN_WEBSOCKET_MESSAGE_TEXT, invalid_utf8,
                          sizeof(invalid_utf8)) == MN_ERROR_INVALID_ARGUMENT);
  CHECK(mn_websocket_close(websocket, 1004, NULL, 0) ==
        MN_ERROR_INVALID_ARGUMENT);
  CHECK(mn_websocket_close(websocket, 1000, "x", 124) ==
        MN_ERROR_INVALID_ARGUMENT);
  mn_websocket_release(websocket);
}

int main(int argc, char **argv) {
  CHECK(argc == 3);
  const char *mode = argv[1];
  const char *url = argv[2];
  websocket_state_t state = {0};

  mn_engine_config_t engine_config = {0};
  engine_config.size = sizeof(engine_config);
  engine_config.version = MN_ABI_VERSION;
  engine_config.user_agent = getenv("MINICRONET_USER_AGENT");
  if (engine_config.user_agent) {
    engine_config.user_agent_length = strlen(engine_config.user_agent);
  }
  engine_config.accept_language = "en-US,en;q=0.9";
  engine_config.accept_language_length = strlen(engine_config.accept_language);
  engine_config.proxy_rules = getenv("MINICRONET_PROXY_RULES");
  if (engine_config.proxy_rules) {
    engine_config.proxy_rules_length = strlen(engine_config.proxy_rules);
  }
  engine_config.proxy_username = getenv("MINICRONET_PROXY_USERNAME");
  if (engine_config.proxy_username) {
    engine_config.proxy_username_length = strlen(engine_config.proxy_username);
  }
  engine_config.proxy_password = getenv("MINICRONET_PROXY_PASSWORD");
  if (engine_config.proxy_password) {
    engine_config.proxy_password_length = strlen(engine_config.proxy_password);
  }
  char profile_namespace[64] = {0};
  engine_config.profile_id = getenv("MINICRONET_PROFILE_ID");
  if (engine_config.profile_id) {
    engine_config.profile_id_length = strlen(engine_config.profile_id);
    CHECK(snprintf(profile_namespace, sizeof(profile_namespace),
                   "minicronet/%s", engine_config.profile_id) > 0);
    engine_config.profile_namespace = profile_namespace;
    engine_config.profile_namespace_length = strlen(profile_namespace);
  }
  mn_engine_t *engine = NULL;
  CHECK(mn_engine_create(&engine_config, &engine) == MN_OK);

  mn_string_t protocol = {"chat", 4};
  mn_header_t header = {"X-Test", 6, "minicronet", 10};
  mn_websocket_config_t config = {0};
  config.size = sizeof(config);
  config.version = MN_ABI_VERSION;
  config.url = url;
  config.url_length = strlen(url);
  config.origin = "https://example.test";
  config.origin_length = strlen(config.origin);
  config.protocols = &protocol;
  config.protocol_count = 1;
  config.headers = &header;
  config.header_count = 1;
  config.callbacks.size = sizeof(config.callbacks);
  config.callbacks.version = MN_ABI_VERSION;
  config.callbacks.user_data = &state;
  config.callbacks.on_open = on_open;
  config.callbacks.on_message = on_message;
  config.callbacks.on_closing = on_closing;
  config.callbacks.on_closed = on_closed;
  config.callbacks.on_failure = on_failure;
  if (strcmp(mode, "timeout") == 0) {
    config.timeout_ms = 25;
  }

  test_validation(engine, config);
  state.server_close = strcmp(mode, "server-close") == 0;

  mn_websocket_t *websocket = NULL;
  CHECK(mn_websocket_create(engine, &config, &websocket) == MN_OK);
  CHECK(mn_websocket_send(websocket, MN_WEBSOCKET_MESSAGE_TEXT, NULL, 0) ==
        MN_ERROR_INVALID_STATE);
  CHECK(mn_websocket_start(websocket) == MN_OK);
  CHECK(mn_websocket_start(websocket) == MN_ERROR_INVALID_STATE);
  if (strcmp(mode, "cancel") == 0) {
    CHECK(mn_websocket_cancel(websocket) == MN_OK);
  }
  mn_websocket_retain(websocket);
  mn_websocket_release(websocket);
  mn_websocket_release(websocket);
  mn_websocket_release(NULL);

  for (int i = 0; i < 15000 && !atomic_load(&state.done); ++i) {
    sleep_millisecond();
  }
  CHECK(atomic_load(&state.done));
  sleep_millisecond();
  CHECK(atomic_load(&state.terminal_count) == 1);
  if (strcmp(mode, "success") == 0) {
    CHECK(atomic_load(&state.open_count) == 1);
    CHECK(atomic_load(&state.message_count) == 5);
    CHECK(atomic_load(&state.closing_count) == 0);
    CHECK(atomic_load(&state.validation_errors) == 0);
    CHECK(state.was_clean);
    CHECK(state.close_code == 1000);
  } else if (strcmp(mode, "server-close") == 0) {
    CHECK(atomic_load(&state.open_count) == 1);
    CHECK(atomic_load(&state.message_count) == 0);
    /* Chromium may omit OnClosingHandshake before a remote OnDropChannel. */
    CHECK(atomic_load(&state.closing_count) <= 1);
    CHECK(atomic_load(&state.validation_errors) == 0);
    CHECK(state.was_clean);
    CHECK(state.close_code == 1000);
  } else if (strcmp(mode, "cancel") == 0) {
    CHECK(state.result == MN_ERROR_CANCELED);
    CHECK(state.net_error != 0);
  } else if (strcmp(mode, "failure") == 0) {
    CHECK(state.result == MN_ERROR_NETWORK);
    CHECK(state.net_error != 0 || state.response_code >= 400);
  } else if (strcmp(mode, "timeout") == 0) {
    CHECK(state.result == MN_ERROR_TIMEOUT);
    CHECK(state.net_error != 0);
  } else {
    CHECK(0 && "unknown mode");
  }

  mn_engine_release(engine);
  return 0;
}
