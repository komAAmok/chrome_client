#include "minicronet.h"
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#ifdef UNSAFE_BUFFERS_BUILD
#pragma allow_unsafe_buffers
#endif

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "check failed: %s\n", #x); return 1; } } while (0)
typedef struct { atomic_int done; mn_result_t result; int status; } state_t;
static void MN_CALL response(void *u, mn_request_t *r, int s, const char *h, size_t n) { ((state_t *)u)->status = s; }
static void MN_CALL complete(void *u, mn_request_t *r, mn_result_t e, int n) { state_t *s = u; s->result = e; atomic_store(&s->done, 1); }
static void wait_done(state_t *s) { for (int i = 0; i != 15000 && !atomic_load(&s->done); ++i) usleep(1000); }
typedef struct { atomic_int done; int opened; int closed; mn_result_t error; } ws_state_t;
static void MN_CALL open_cb(void *u, mn_websocket_t *w, const char *p, size_t pn, const char *e, size_t en) { ws_state_t *s = u; s->opened = (pn == 4 && !memcmp(p, "chat", 4)); static const uint8_t text[] = "client-text"; mn_websocket_send(w, MN_WEBSOCKET_MESSAGE_TEXT, text, sizeof(text) - 1); }
static void MN_CALL msg_cb(void *u, mn_websocket_t *w, mn_websocket_message_type_t t, int f, const uint8_t *d, size_t n) { if (t == MN_WEBSOCKET_MESSAGE_TEXT && f && n == 11 && !memcmp(d, "client-text", 11)) mn_websocket_close(w, 1000, "done", 4); }
static void MN_CALL close_cb(void *u, mn_websocket_t *w, int clean, uint16_t code, const char *r, size_t n) { ws_state_t *s = u; s->closed = clean && code == 1000; atomic_store(&s->done, 1); }
static void MN_CALL fail_cb(void *u, mn_websocket_t *w, mn_result_t e, int ne, int rc, const char *m, size_t n) { ws_state_t *s = u; s->error = e; atomic_store(&s->done, 1); }
int main(int argc, char **argv) {
  CHECK(argc == 2);
  const char *base = argv[1]; char warm[256], ws[256];
  snprintf(warm, sizeof(warm), "https://[::1]:%s/warmup", base);
  snprintf(ws, sizeof(ws), "wss://[::1]:%s/echo", base);
  mn_engine_config_t ec = { .size = sizeof(ec), .version = MN_ABI_VERSION, .accept_language = "en-US", .accept_language_length = 5,
    .profile_id = "chrome_151", .profile_id_length = 10, .profile_namespace = "minicronet/chrome_151", .profile_namespace_length = 21 };
  mn_engine_t *engine = NULL; CHECK(mn_engine_create(&ec, &engine) == MN_OK);
  state_t rs = {0}; mn_request_config_t rc = { .size = sizeof(rc), .version = MN_ABI_VERSION, .url = warm, .url_length = strlen(warm), .method = "GET", .method_length = 3,
    .callbacks = { .size = sizeof(rc.callbacks), .version = MN_ABI_VERSION, .user_data = &rs, .on_response = response, .on_complete = complete } };
  mn_request_t *req = NULL; CHECK(mn_request_create(engine, &rc, &req) == MN_OK); CHECK(mn_request_start(req) == MN_OK); wait_done(&rs); mn_request_release(req); CHECK(rs.result == MN_OK && rs.status == 200);
  ws_state_t ws_state = {0}; mn_string_t proto = {"chat", 4}; mn_websocket_config_t wc = { .size = sizeof(wc), .version = MN_ABI_VERSION, .url = ws, .url_length = strlen(ws), .origin = "https://example.test", .origin_length = 20, .protocols = &proto, .protocol_count = 1,
    .callbacks = { .size = sizeof(wc.callbacks), .version = MN_ABI_VERSION, .user_data = &ws_state, .on_open = open_cb, .on_message = msg_cb, .on_closed = close_cb, .on_failure = fail_cb } };
  mn_websocket_t *websocket = NULL; CHECK(mn_websocket_create(engine, &wc, &websocket) == MN_OK); CHECK(mn_websocket_start(websocket) == MN_OK); for (int i = 0; i != 15000 && !atomic_load(&ws_state.done); ++i) usleep(1000); mn_websocket_release(websocket); mn_engine_release(engine);
  CHECK(ws_state.opened && ws_state.closed); puts("Extended CONNECT wire probe passed"); return 0;
}
