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
static void sleep_millisecond(void) { Sleep(1); }
#else
#include <time.h>
static void sleep_millisecond(void) {
  struct timespec delay = {0, 1000000};
  nanosleep(&delay, NULL);
}
#endif

typedef struct probe_state {
  atomic_int done;
  mn_result_t result;
  int net_error;
  int status_code;
  uint8_t *body;
  size_t body_length;
  size_t body_capacity;
} probe_state_t;

static void MN_CALL on_response(void *user_data, mn_request_t *request,
                                int status_code, const char *headers,
                                size_t headers_length) {
  (void)request;
  (void)headers;
  (void)headers_length;
  ((probe_state_t *)user_data)->status_code = status_code;
}

static mn_read_disposition_t MN_CALL on_body(void *user_data,
                                            mn_request_t *request,
                                            const uint8_t *data,
                                            size_t data_length) {
  (void)request;
  probe_state_t *state = user_data;
  if (data_length == 0) {
    return MN_READ_CONTINUE;
  }
  if (state->body_length > SIZE_MAX - data_length) {
    return MN_READ_CONTINUE;
  }
  size_t required = state->body_length + data_length;
  if (required > state->body_capacity) {
    size_t capacity = state->body_capacity ? state->body_capacity : 4096;
    while (capacity < required) {
      if (capacity > SIZE_MAX / 2) {
        capacity = required;
        break;
      }
      capacity *= 2;
    }
    uint8_t *body = realloc(state->body, capacity);
    if (!body) {
      return MN_READ_CONTINUE;
    }
    state->body = body;
    state->body_capacity = capacity;
  }
  memcpy(state->body + state->body_length, data, data_length);
  state->body_length = required;
  return MN_READ_CONTINUE;
}

static void MN_CALL on_complete(void *user_data, mn_request_t *request,
                                mn_result_t result, int net_error) {
  (void)request;
  probe_state_t *state = user_data;
  state->result = result;
  state->net_error = net_error;
  atomic_store_explicit(&state->done, 1, memory_order_release);
}

static int write_body(const char *path, const probe_state_t *state) {
  FILE *file = fopen(path, "wb");
  if (!file) {
    return 0;
  }
  size_t written = fwrite(state->body, 1, state->body_length, file);
  int ok = written == state->body_length;
  if (fclose(file) != 0) {
    ok = 0;
  }
  return ok;
}

int main(int argc, char **argv) {
  if (argc < 4 || argc > 5) {
    fprintf(stderr, "usage: %s PROFILE_ID URL BODY_FILE [REQUEST_COUNT]\n",
            argv[0]);
    return 2;
  }

  const char *profile_id = argv[1];
  const char *url = argv[2];
  const char *body_file = argv[3];
  int request_count = argc == 5 ? atoi(argv[4]) : 1;
  if (request_count < 1 || request_count > 3) {
    fprintf(stderr, "request count must be 1..3\n");
    return 2;
  }
  char profile_namespace[128];
  int namespace_length = snprintf(profile_namespace, sizeof(profile_namespace),
                                  "minicronet/%s", profile_id);
  if (namespace_length < 0 ||
      (size_t)namespace_length >= sizeof(profile_namespace)) {
    fprintf(stderr, "profile id is too long\n");
    return 2;
  }

  mn_engine_config_t engine_config = {0};
  engine_config.size = sizeof(engine_config);
  engine_config.version = MN_ABI_VERSION;
  engine_config.profile_id = profile_id;
  engine_config.profile_id_length = strlen(profile_id);
  engine_config.profile_namespace = profile_namespace;
  engine_config.profile_namespace_length = (size_t)namespace_length;
  static const char chrome_140_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36";
  static const char chrome_139_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36";
  static const char chrome_138_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36";
  static const char chrome_137_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36";
  static const char chrome_136_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36";
  static const char chrome_135_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36";
  static const char chrome_134_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36";
  static const char chrome_133_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36";
  static const char chrome_132_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36";
  static const char chrome_131_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";
  static const char chrome_130_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36";
  static const char chrome_129_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36";
  static const char chrome_128_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36";
  static const char chrome_127_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36";
  static const char chrome_126_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";
  static const char chrome_125_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36";
  static const char chrome_124_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36";
  static const char chrome_123_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36";
  static const char chrome_122_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36";
  static const char chrome_121_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36";
  static const char chrome_120_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36";
  static const char chrome_119_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36";
  static const char chrome_118_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36";
  static const char chrome_117_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36";
  static const char chrome_116_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36";
  static const char chrome_115_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36";
  static const char chrome_114_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36";
  static const char chrome_113_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36";
  static const char chrome_112_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/112.0.0.0 Safari/537.36";
  static const char chrome_111_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/111.0.0.0 Safari/537.36";
  static const char chrome_110_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36";
  static const char chrome_109_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36";
  static const char chrome_108_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36";
  static const char chrome_107_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36";
  static const char chrome_106_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/106.0.0.0 Safari/537.36";
  static const char chrome_105_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/105.0.0.0 Safari/537.36";
  static const char chrome_104_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/104.0.5112.101 Safari/537.36";
  static const char chrome_103_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/103.0.5060.134 Safari/537.36";
  static const char chrome_102_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/102.0.5005.115 Safari/537.36";
  static const char chrome_101_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/101.0.4951.64 Safari/537.36";
  static const char chrome_100_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36";
  static const char chrome_99_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/99.0.4844.84 Safari/537.36";
  static const char chrome_141_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36";
  static const char chrome_142_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36";
  static const char chrome_145_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36";
  static const char chrome_144_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36";
  static const char chrome_143_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36";
  static const char chrome_146_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36";
  static const char chrome_147_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36";
  static const char chrome_148_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36";
  static const char chrome_150_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36";
  static const char chrome_149_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36";
  static const char chrome_151_user_agent[] =
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36";
  static const char accept_language[] = "zh-CN,zh;q=0.9";
#define MN_PROBE_HEADER(name, value) \
  {name, sizeof(name) - 1, value, sizeof(value) - 1}
  static const mn_header_t chrome_105_headers[] = {
      MN_PROBE_HEADER("sec-ch-ua",
                      "\"Google Chrome\";v=\"105\", \"Not)A;Brand\";v=\"8\", "
                      "\"Chromium\";v=\"105\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/105.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.9"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
  };
  static const mn_header_t chrome_104_headers[] = {
      MN_PROBE_HEADER("sec-ch-ua",
                      "\"Chromium\";v=\"104\", \" Not A;Brand\";v=\"99\", "
                      "\"Google Chrome\";v=\"104\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/104.0.5112.101 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.9"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
  };
  static const mn_header_t chrome_103_headers[] = {
      MN_PROBE_HEADER("sec-ch-ua",
                      "\".Not/A)Brand\";v=\"99\", \"Google Chrome\";v=\"103\", "
                      "\"Chromium\";v=\"103\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/103.0.5060.134 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.9"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
  };
  static const mn_header_t chrome_102_headers[] = {
      MN_PROBE_HEADER("sec-ch-ua",
                      "\" Not A;Brand\";v=\"99\", \"Chromium\";v=\"102\", "
                      "\"Google Chrome\";v=\"102\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/102.0.5005.115 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.9"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
  };
  static const mn_header_t chrome_101_headers[] = {
      MN_PROBE_HEADER("sec-ch-ua",
                      "\" Not A;Brand\";v=\"99\", \"Chromium\";v=\"101\", "
                      "\"Google Chrome\";v=\"101\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/101.0.4951.64 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.9"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
  };
  static const mn_header_t chrome_100_headers[] = {
      MN_PROBE_HEADER("sec-ch-ua",
                      "\" Not A;Brand\";v=\"99\", \"Chromium\";v=\"100\", "
                      "\"Google Chrome\";v=\"100\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.9"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
  };
  static const mn_header_t chrome_99_headers[] = {
      MN_PROBE_HEADER("sec-ch-ua",
                      "\" Not A;Brand\";v=\"99\", \"Chromium\";v=\"99\", "
                      "\"Google Chrome\";v=\"99\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/99.0.4844.84 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.9"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
  };
  if (strcmp(profile_id, "chrome_99") == 0 ||
      strcmp(profile_id, "chrome_100") == 0 ||
      strcmp(profile_id, "chrome_101") == 0 ||
      strcmp(profile_id, "chrome_102") == 0 ||
      strcmp(profile_id, "chrome_103") == 0 ||
      strcmp(profile_id, "chrome_104") == 0 ||
      strcmp(profile_id, "chrome_105") == 0 ||
      strcmp(profile_id, "chrome_106") == 0 ||
      strcmp(profile_id, "chrome_107") == 0 ||
      strcmp(profile_id, "chrome_108") == 0 ||
      strcmp(profile_id, "chrome_109") == 0 ||
      strcmp(profile_id, "chrome_110") == 0 ||
      strcmp(profile_id, "chrome_111") == 0 ||
      strcmp(profile_id, "chrome_112") == 0 ||
      strcmp(profile_id, "chrome_113") == 0 ||
      strcmp(profile_id, "chrome_114") == 0 ||
      strcmp(profile_id, "chrome_115") == 0 ||
      strcmp(profile_id, "chrome_116") == 0 ||
      strcmp(profile_id, "chrome_117") == 0 ||
      strcmp(profile_id, "chrome_118") == 0 ||
      strcmp(profile_id, "chrome_119") == 0 ||
      strcmp(profile_id, "chrome_120") == 0 ||
      strcmp(profile_id, "chrome_121") == 0 ||
      strcmp(profile_id, "chrome_122") == 0 ||
      strcmp(profile_id, "chrome_123") == 0 ||
      strcmp(profile_id, "chrome_124") == 0 ||
      strcmp(profile_id, "chrome_125") == 0 ||
      strcmp(profile_id, "chrome_126") == 0 ||
      strcmp(profile_id, "chrome_127") == 0 ||
      strcmp(profile_id, "chrome_128") == 0 ||
      strcmp(profile_id, "chrome_129") == 0 ||
      strcmp(profile_id, "chrome_130") == 0 ||
      strcmp(profile_id, "chrome_131") == 0 ||
      strcmp(profile_id, "chrome_132") == 0 ||
      strcmp(profile_id, "chrome_133") == 0 ||
      strcmp(profile_id, "chrome_134") == 0 ||
      strcmp(profile_id, "chrome_135") == 0 ||
      strcmp(profile_id, "chrome_136") == 0 ||
      strcmp(profile_id, "chrome_137") == 0 ||
      strcmp(profile_id, "chrome_138") == 0 ||
      strcmp(profile_id, "chrome_139") == 0 ||
      strcmp(profile_id, "chrome_140") == 0 ||
      strcmp(profile_id, "chrome_141") == 0 ||
      strcmp(profile_id, "chrome_142") == 0 ||
      strcmp(profile_id, "chrome_143") == 0 ||
      strcmp(profile_id, "chrome_144") == 0 ||
      strcmp(profile_id, "chrome_145") == 0 ||
      strcmp(profile_id, "chrome_146") == 0 ||
      strcmp(profile_id, "chrome_147") == 0 ||
      strcmp(profile_id, "chrome_148") == 0 ||
      strcmp(profile_id, "chrome_149") == 0 ||
      strcmp(profile_id, "chrome_150") == 0 ||
      strcmp(profile_id, "chrome_151") == 0) {
    const char *user_agent = chrome_151_user_agent;
    if (strcmp(profile_id, "chrome_99") == 0) {
      user_agent = chrome_99_user_agent;
    } else if (strcmp(profile_id, "chrome_100") == 0) {
      user_agent = chrome_100_user_agent;
    } else if (strcmp(profile_id, "chrome_101") == 0) {
      user_agent = chrome_101_user_agent;
    } else if (strcmp(profile_id, "chrome_102") == 0) {
      user_agent = chrome_102_user_agent;
    } else if (strcmp(profile_id, "chrome_103") == 0) {
      user_agent = chrome_103_user_agent;
    } else if (strcmp(profile_id, "chrome_104") == 0) {
      user_agent = chrome_104_user_agent;
    } else if (strcmp(profile_id, "chrome_105") == 0) {
      user_agent = chrome_105_user_agent;
    } else if (strcmp(profile_id, "chrome_106") == 0) {
      user_agent = chrome_106_user_agent;
    } else if (strcmp(profile_id, "chrome_107") == 0) {
      user_agent = chrome_107_user_agent;
    } else if (strcmp(profile_id, "chrome_108") == 0) {
      user_agent = chrome_108_user_agent;
    } else if (strcmp(profile_id, "chrome_109") == 0) {
      user_agent = chrome_109_user_agent;
    } else if (strcmp(profile_id, "chrome_110") == 0) {
      user_agent = chrome_110_user_agent;
    } else if (strcmp(profile_id, "chrome_111") == 0) {
      user_agent = chrome_111_user_agent;
    } else if (strcmp(profile_id, "chrome_112") == 0) {
      user_agent = chrome_112_user_agent;
    } else if (strcmp(profile_id, "chrome_113") == 0) {
      user_agent = chrome_113_user_agent;
    } else if (strcmp(profile_id, "chrome_114") == 0) {
      user_agent = chrome_114_user_agent;
    } else if (strcmp(profile_id, "chrome_115") == 0) {
      user_agent = chrome_115_user_agent;
    } else if (strcmp(profile_id, "chrome_116") == 0) {
      user_agent = chrome_116_user_agent;
    } else if (strcmp(profile_id, "chrome_117") == 0) {
      user_agent = chrome_117_user_agent;
    } else if (strcmp(profile_id, "chrome_118") == 0) {
      user_agent = chrome_118_user_agent;
    } else if (strcmp(profile_id, "chrome_119") == 0) {
      user_agent = chrome_119_user_agent;
    } else if (strcmp(profile_id, "chrome_120") == 0) {
      user_agent = chrome_120_user_agent;
    } else if (strcmp(profile_id, "chrome_121") == 0) {
      user_agent = chrome_121_user_agent;
    } else if (strcmp(profile_id, "chrome_122") == 0) {
      user_agent = chrome_122_user_agent;
    } else if (strcmp(profile_id, "chrome_123") == 0) {
      user_agent = chrome_123_user_agent;
    } else if (strcmp(profile_id, "chrome_124") == 0) {
      user_agent = chrome_124_user_agent;
    } else if (strcmp(profile_id, "chrome_125") == 0) {
      user_agent = chrome_125_user_agent;
    } else if (strcmp(profile_id, "chrome_126") == 0) {
      user_agent = chrome_126_user_agent;
    } else if (strcmp(profile_id, "chrome_127") == 0) {
      user_agent = chrome_127_user_agent;
    } else if (strcmp(profile_id, "chrome_128") == 0) {
      user_agent = chrome_128_user_agent;
    } else if (strcmp(profile_id, "chrome_129") == 0) {
      user_agent = chrome_129_user_agent;
    } else if (strcmp(profile_id, "chrome_130") == 0) {
      user_agent = chrome_130_user_agent;
    } else if (strcmp(profile_id, "chrome_131") == 0) {
      user_agent = chrome_131_user_agent;
    } else if (strcmp(profile_id, "chrome_132") == 0) {
      user_agent = chrome_132_user_agent;
    } else if (strcmp(profile_id, "chrome_133") == 0) {
      user_agent = chrome_133_user_agent;
    } else if (strcmp(profile_id, "chrome_134") == 0) {
      user_agent = chrome_134_user_agent;
    } else if (strcmp(profile_id, "chrome_135") == 0) {
      user_agent = chrome_135_user_agent;
    } else if (strcmp(profile_id, "chrome_136") == 0) {
      user_agent = chrome_136_user_agent;
    } else if (strcmp(profile_id, "chrome_137") == 0) {
      user_agent = chrome_137_user_agent;
    } else if (strcmp(profile_id, "chrome_138") == 0) {
      user_agent = chrome_138_user_agent;
    } else if (strcmp(profile_id, "chrome_139") == 0) {
      user_agent = chrome_139_user_agent;
    } else if (strcmp(profile_id, "chrome_140") == 0) {
      user_agent = chrome_140_user_agent;
    } else if (strcmp(profile_id, "chrome_141") == 0) {
      user_agent = chrome_141_user_agent;
    } else if (strcmp(profile_id, "chrome_142") == 0) {
      user_agent = chrome_142_user_agent;
    } else if (strcmp(profile_id, "chrome_143") == 0) {
      user_agent = chrome_143_user_agent;
    } else if (strcmp(profile_id, "chrome_144") == 0) {
      user_agent = chrome_144_user_agent;
    } else if (strcmp(profile_id, "chrome_145") == 0) {
      user_agent = chrome_145_user_agent;
    } else if (strcmp(profile_id, "chrome_146") == 0) {
      user_agent = chrome_146_user_agent;
    } else if (strcmp(profile_id, "chrome_147") == 0) {
      user_agent = chrome_147_user_agent;
    } else if (strcmp(profile_id, "chrome_148") == 0) {
      user_agent = chrome_148_user_agent;
    } else if (strcmp(profile_id, "chrome_149") == 0) {
      user_agent = chrome_149_user_agent;
    } else if (strcmp(profile_id, "chrome_150") == 0) {
      user_agent = chrome_150_user_agent;
    }
    engine_config.user_agent = user_agent;
    engine_config.user_agent_length = strlen(engine_config.user_agent);
    engine_config.accept_language = accept_language;
    engine_config.accept_language_length = sizeof(accept_language) - 1;
  }
  const char *proxy_rules = getenv("MINICRONET_PROXY_RULES");
  if (proxy_rules) {
    engine_config.proxy_rules = proxy_rules;
    engine_config.proxy_rules_length = strlen(proxy_rules);
  }

  mn_engine_t *engine = NULL;
  mn_result_t result = mn_engine_create(&engine_config, &engine);
  if (result != MN_OK) {
    printf("{\"profile_id\":\"%s\",\"stage\":\"engine\",\"result\":%d}\n",
           profile_id, result);
    return 0;
  }

  probe_state_t state = {0};
  mn_request_config_t request_config = {0};
  request_config.size = sizeof(request_config);
  request_config.version = MN_ABI_VERSION;
  request_config.url = url;
  request_config.url_length = strlen(url);
  request_config.method = "GET";
  request_config.method_length = 3;
  static const mn_header_t chrome_145_headers[] = {
      MN_PROBE_HEADER("sec-ch-ua",
                      "\"Not:A-Brand\";v=\"99\", \"Google Chrome\";v=\"145\", "
                      "\"Chromium\";v=\"145\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br, zstd"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
      MN_PROBE_HEADER("priority", "u=0, i"),
  };
  static const mn_header_t chrome_144_headers[] = {
      MN_PROBE_HEADER(
          "sec-ch-ua",
          "\"Not(A:Brand\";v=\"8\", \"Chromium\";v=\"144\", "
          "\"Google Chrome\";v=\"144\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br, zstd"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
      MN_PROBE_HEADER("priority", "u=0, i"),
  };
  static const mn_header_t chrome_143_headers[] = {
      MN_PROBE_HEADER(
          "sec-ch-ua",
          "\"Google Chrome\";v=\"143\", \"Chromium\";v=\"143\", "
          "\"Not A(Brand\";v=\"24\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br, zstd"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
      MN_PROBE_HEADER("priority", "u=0, i"),
  };
  static const mn_header_t chrome_142_headers[] = {
      MN_PROBE_HEADER(
          "sec-ch-ua",
          "\"Chromium\";v=\"142\", \"Google Chrome\";v=\"142\", "
          "\"Not_A Brand\";v=\"99\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br, zstd"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
      MN_PROBE_HEADER("priority", "u=0, i"),
  };
  static const mn_header_t chrome_141_headers[] = {
      MN_PROBE_HEADER(
          "sec-ch-ua",
          "\"Google Chrome\";v=\"141\", \"Not?A_Brand\";v=\"8\", "
          "\"Chromium\";v=\"141\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br, zstd"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
      MN_PROBE_HEADER("priority", "u=0, i"),
  };
  static const mn_header_t chrome_140_headers[] = {
      MN_PROBE_HEADER(
          "sec-ch-ua",
          "\"Chromium\";v=\"140\", \"Not=A?Brand\";v=\"24\", "
          "\"Google Chrome\";v=\"140\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br, zstd"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
      MN_PROBE_HEADER("priority", "u=0, i"),
  };
  static const mn_header_t chrome_139_headers[] = {
      MN_PROBE_HEADER(
          "sec-ch-ua",
          "\"Not;A=Brand\";v=\"99\", \"Google Chrome\";v=\"139\", "
          "\"Chromium\";v=\"139\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br, zstd"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
      MN_PROBE_HEADER("priority", "u=0, i"),
  };
  static const mn_header_t chrome_138_headers[] = {
      MN_PROBE_HEADER(
          "sec-ch-ua",
          "\"Not)A;Brand\";v=\"8\", \"Chromium\";v=\"138\", "
          "\"Google Chrome\";v=\"138\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br, zstd"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
      MN_PROBE_HEADER("priority", "u=0, i"),
  };
  static const mn_header_t chrome_137_headers[] = {
      MN_PROBE_HEADER(
          "sec-ch-ua",
          "\"Google Chrome\";v=\"137\", \"Chromium\";v=\"137\", "
          "\"Not/A)Brand\";v=\"24\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br, zstd"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
      MN_PROBE_HEADER("priority", "u=0, i"),
  };
  static const mn_header_t chrome_136_headers[] = {
      MN_PROBE_HEADER(
          "sec-ch-ua",
          "\"Chromium\";v=\"136\", \"Google Chrome\";v=\"136\", "
          "\"Not.A/Brand\";v=\"99\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br, zstd"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
      MN_PROBE_HEADER("priority", "u=0, i"),
  };
  static const mn_header_t chrome_135_headers[] = {
      MN_PROBE_HEADER(
          "sec-ch-ua",
          "\"Google Chrome\";v=\"135\", \"Not-A.Brand\";v=\"8\", "
          "\"Chromium\";v=\"135\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br, zstd"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
      MN_PROBE_HEADER("priority", "u=0, i"),
  };
  static const mn_header_t chrome_134_headers[] = {
      MN_PROBE_HEADER(
          "sec-ch-ua",
          "\"Chromium\";v=\"134\", \"Not:A-Brand\";v=\"24\", "
          "\"Google Chrome\";v=\"134\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br, zstd"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
      MN_PROBE_HEADER("priority", "u=0, i"),
  };
  static const mn_header_t chrome_133_headers[] = {
      MN_PROBE_HEADER(
          "sec-ch-ua",
          "\"Not(A:Brand\";v=\"99\", \"Google Chrome\";v=\"133\", "
          "\"Chromium\";v=\"133\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br, zstd"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
      MN_PROBE_HEADER("priority", "u=0, i"),
  };
  static const mn_header_t chrome_132_headers[] = {
      MN_PROBE_HEADER(
          "sec-ch-ua",
          "\"Not A(Brand\";v=\"8\", \"Chromium\";v=\"132\", "
          "\"Google Chrome\";v=\"132\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br, zstd"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
      MN_PROBE_HEADER("priority", "u=0, i"),
  };
  static const mn_header_t chrome_131_headers[] = {
      MN_PROBE_HEADER(
          "sec-ch-ua",
          "\"Google Chrome\";v=\"131\", \"Chromium\";v=\"131\", "
          "\"Not_A Brand\";v=\"24\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br, zstd"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
      MN_PROBE_HEADER("priority", "u=0, i"),
  };
  static const mn_header_t chrome_130_headers[] = {
      MN_PROBE_HEADER(
          "sec-ch-ua",
          "\"Chromium\";v=\"130\", \"Google Chrome\";v=\"130\", "
          "\"Not?A_Brand\";v=\"99\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br, zstd"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
      MN_PROBE_HEADER("priority", "u=0, i"),
  };
  static const mn_header_t chrome_129_headers[] = {
      MN_PROBE_HEADER(
          "sec-ch-ua",
          "\"Google Chrome\";v=\"129\", \"Not=A?Brand\";v=\"8\", "
          "\"Chromium\";v=\"129\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br, zstd"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
      MN_PROBE_HEADER("priority", "u=0, i"),
  };
  static const mn_header_t chrome_128_headers[] = {
      MN_PROBE_HEADER(
          "sec-ch-ua",
          "\"Chromium\";v=\"128\", \"Not;A=Brand\";v=\"24\", "
          "\"Google Chrome\";v=\"128\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br, zstd"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
      MN_PROBE_HEADER("priority", "u=0, i"),
  };
  static const mn_header_t chrome_127_headers[] = {
      MN_PROBE_HEADER(
          "sec-ch-ua",
          "\"Not)A;Brand\";v=\"99\", \"Google Chrome\";v=\"127\", "
          "\"Chromium\";v=\"127\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br, zstd"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
      MN_PROBE_HEADER("priority", "u=0, i"),
  };
  static const mn_header_t chrome_126_headers[] = {
      MN_PROBE_HEADER(
          "sec-ch-ua",
          "\"Not/A)Brand\";v=\"8\", \"Chromium\";v=\"126\", "
          "\"Google Chrome\";v=\"126\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br, zstd"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
      MN_PROBE_HEADER("priority", "u=0, i"),
  };
  static const mn_header_t chrome_125_headers[] = {
      MN_PROBE_HEADER(
          "sec-ch-ua",
          "\"Google Chrome\";v=\"125\", \"Chromium\";v=\"125\", "
          "\"Not.A/Brand\";v=\"24\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br, zstd"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
      MN_PROBE_HEADER("priority", "u=0, i"),
  };
  static const mn_header_t chrome_124_headers[] = {
      MN_PROBE_HEADER(
          "sec-ch-ua",
          "\"Chromium\";v=\"124\", \"Google Chrome\";v=\"124\", "
          "\"Not-A.Brand\";v=\"99\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br, zstd"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
      MN_PROBE_HEADER("priority", "u=0, i"),
  };
  static const mn_header_t chrome_123_headers[] = {
      MN_PROBE_HEADER(
          "sec-ch-ua",
          "\"Google Chrome\";v=\"123\", \"Not:A-Brand\";v=\"8\", "
          "\"Chromium\";v=\"123\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br, zstd"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
  };
  static const mn_header_t chrome_122_headers[] = {
      MN_PROBE_HEADER(
          "sec-ch-ua",
          "\"Chromium\";v=\"122\", \"Not(A:Brand\";v=\"24\", "
          "\"Google Chrome\";v=\"122\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
  };
  static const mn_header_t chrome_121_headers[] = {
      MN_PROBE_HEADER(
          "sec-ch-ua",
          "\"Not A(Brand\";v=\"99\", \"Google Chrome\";v=\"121\", "
          "\"Chromium\";v=\"121\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
  };
  static const mn_header_t chrome_120_headers[] = {
      MN_PROBE_HEADER("sec-ch-ua",
                      "\"Not_A Brand\";v=\"8\", \"Chromium\";v=\"120\", "
                      "\"Google Chrome\";v=\"120\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
  };
  static const mn_header_t chrome_119_headers[] = {
      MN_PROBE_HEADER("sec-ch-ua",
                      "\"Google Chrome\";v=\"119\", \"Chromium\";v=\"119\", "
                      "\"Not?A_Brand\";v=\"24\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
  };
  static const mn_header_t chrome_118_headers[] = {
      MN_PROBE_HEADER("sec-ch-ua",
                      "\"Chromium\";v=\"118\", \"Google Chrome\";v=\"118\", "
                      "\"Not=A?Brand\";v=\"99\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
  };
  static const mn_header_t chrome_117_headers[] = {
      MN_PROBE_HEADER("sec-ch-ua",
                      "\"Google Chrome\";v=\"117\", \"Not;A=Brand\";v=\"8\", "
                      "\"Chromium\";v=\"117\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
  };
  static const mn_header_t chrome_116_headers[] = {
      MN_PROBE_HEADER("sec-ch-ua",
                      "\"Chromium\";v=\"116\", \"Not)A;Brand\";v=\"24\", "
                      "\"Google Chrome\";v=\"116\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
  };
  static const mn_header_t chrome_115_headers[] = {
      MN_PROBE_HEADER("sec-ch-ua",
                      "\"Not/A)Brand\";v=\"99\", \"Google Chrome\";v=\"115\", "
                      "\"Chromium\";v=\"115\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
  };
  static const mn_header_t chrome_114_headers[] = {
      MN_PROBE_HEADER("sec-ch-ua",
                      "\"Not.A/Brand\";v=\"8\", \"Chromium\";v=\"114\", "
                      "\"Google Chrome\";v=\"114\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
  };
  static const mn_header_t chrome_113_headers[] = {
      MN_PROBE_HEADER("sec-ch-ua",
                      "\"Google Chrome\";v=\"113\", \"Chromium\";v=\"113\", "
                      "\"Not-A.Brand\";v=\"24\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
  };
  static const mn_header_t chrome_112_headers[] = {
      MN_PROBE_HEADER("sec-ch-ua",
                      "\"Chromium\";v=\"112\", \"Google Chrome\";v=\"112\", "
                      "\"Not:A-Brand\";v=\"99\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/112.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
  };
  static const mn_header_t chrome_111_headers[] = {
      MN_PROBE_HEADER("sec-ch-ua",
                      "\"Google Chrome\";v=\"111\", \"Not(A:Brand\";v=\"8\", "
                      "\"Chromium\";v=\"111\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/111.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
  };
  static const mn_header_t chrome_110_headers[] = {
      MN_PROBE_HEADER("sec-ch-ua",
                      "\"Chromium\";v=\"110\", \"Not A(Brand\";v=\"24\", "
                      "\"Google Chrome\";v=\"110\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
  };
  static const mn_header_t chrome_109_headers[] = {
      MN_PROBE_HEADER("sec-ch-ua",
                      "\"Not_A Brand\";v=\"99\", \"Google Chrome\";v=\"109\", "
                      "\"Chromium\";v=\"109\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.9"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
  };
  static const mn_header_t chrome_108_headers[] = {
      MN_PROBE_HEADER("sec-ch-ua",
                      "\"Not?A_Brand\";v=\"8\", \"Chromium\";v=\"108\", "
                      "\"Google Chrome\";v=\"108\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.9"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
  };
  static const mn_header_t chrome_107_headers[] = {
      MN_PROBE_HEADER("sec-ch-ua",
                      "\"Google Chrome\";v=\"107\", \"Chromium\";v=\"107\", "
                      "\"Not=A?Brand\";v=\"24\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.9"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
  };
  static const mn_header_t chrome_106_headers[] = {
      MN_PROBE_HEADER("sec-ch-ua",
                      "\"Chromium\";v=\"106\", \"Google Chrome\";v=\"106\", "
                      "\"Not;A=Brand\";v=\"99\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/106.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.9"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
  };
  static const mn_header_t chrome_146_headers[] = {
      MN_PROBE_HEADER(
          "sec-ch-ua",
          "\"Chromium\";v=\"146\", \"Not-A.Brand\";v=\"24\", "
          "\"Google Chrome\";v=\"146\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br, zstd"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
      MN_PROBE_HEADER("priority", "u=0, i"),
  };
  static const mn_header_t chrome_147_headers[] = {
      MN_PROBE_HEADER(
          "sec-ch-ua",
          "\"Google Chrome\";v=\"147\", \"Not.A/Brand\";v=\"8\", "
          "\"Chromium\";v=\"147\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br, zstd"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
      MN_PROBE_HEADER("priority", "u=0, i"),
  };
  static const mn_header_t chrome_148_headers[] = {
      MN_PROBE_HEADER(
          "sec-ch-ua",
          "\"Chromium\";v=\"148\", \"Google Chrome\";v=\"148\", "
          "\"Not/A)Brand\";v=\"99\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br, zstd"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
      MN_PROBE_HEADER("priority", "u=0, i"),
  };
  static const mn_header_t chrome_149_headers[] = {
      MN_PROBE_HEADER("sec-ch-ua",
                      "\"Google Chrome\";v=\"149\", \"Chromium\";v=\"149\", "
                      "\"Not)A;Brand\";v=\"24\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br, zstd"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
      MN_PROBE_HEADER("priority", "u=0, i"),
  };
  static const mn_header_t chrome_150_headers[] = {
      MN_PROBE_HEADER("sec-ch-ua",
                      "\"Not;A=Brand\";v=\"8\", \"Chromium\";v=\"150\", "
                      "\"Google Chrome\";v=\"150\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br, zstd"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
      MN_PROBE_HEADER("priority", "u=0, i"),
  };
  static const mn_header_t chrome_151_headers[] = {
      MN_PROBE_HEADER("sec-ch-ua",
                      "\"Not=A?Brand\";v=\"99\", \"Google Chrome\";v=\"151\", "
                      "\"Chromium\";v=\"151\""),
      MN_PROBE_HEADER("sec-ch-ua-mobile", "?0"),
      MN_PROBE_HEADER("sec-ch-ua-platform", "\"Windows\""),
      MN_PROBE_HEADER("upgrade-insecure-requests", "1"),
      MN_PROBE_HEADER(
          "user-agent",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"),
      MN_PROBE_HEADER(
          "accept",
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
          "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;"
          "q=0.7"),
      MN_PROBE_HEADER("sec-fetch-site", "none"),
      MN_PROBE_HEADER("sec-fetch-mode", "navigate"),
      MN_PROBE_HEADER("sec-fetch-user", "?1"),
      MN_PROBE_HEADER("sec-fetch-dest", "document"),
      MN_PROBE_HEADER("accept-encoding", "gzip, deflate, br, zstd"),
      MN_PROBE_HEADER("accept-language", "zh-CN,zh;q=0.9"),
      MN_PROBE_HEADER("priority", "u=0, i"),
  };
#undef MN_PROBE_HEADER
  if (strcmp(profile_id, "chrome_99") == 0 ||
      strcmp(profile_id, "chrome_100") == 0 ||
      strcmp(profile_id, "chrome_101") == 0 ||
      strcmp(profile_id, "chrome_102") == 0 ||
      strcmp(profile_id, "chrome_103") == 0 ||
      strcmp(profile_id, "chrome_104") == 0 ||
      strcmp(profile_id, "chrome_105") == 0 ||
      strcmp(profile_id, "chrome_106") == 0 ||
      strcmp(profile_id, "chrome_107") == 0 ||
      strcmp(profile_id, "chrome_108") == 0 ||
      strcmp(profile_id, "chrome_109") == 0 ||
      strcmp(profile_id, "chrome_110") == 0 ||
      strcmp(profile_id, "chrome_111") == 0 ||
      strcmp(profile_id, "chrome_112") == 0 ||
      strcmp(profile_id, "chrome_113") == 0 ||
      strcmp(profile_id, "chrome_114") == 0 ||
      strcmp(profile_id, "chrome_115") == 0 ||
      strcmp(profile_id, "chrome_116") == 0 ||
      strcmp(profile_id, "chrome_117") == 0 ||
      strcmp(profile_id, "chrome_118") == 0 ||
      strcmp(profile_id, "chrome_119") == 0 ||
      strcmp(profile_id, "chrome_120") == 0 ||
      strcmp(profile_id, "chrome_121") == 0 ||
      strcmp(profile_id, "chrome_122") == 0 ||
      strcmp(profile_id, "chrome_123") == 0 ||
      strcmp(profile_id, "chrome_124") == 0 ||
      strcmp(profile_id, "chrome_125") == 0 ||
      strcmp(profile_id, "chrome_126") == 0 ||
      strcmp(profile_id, "chrome_127") == 0 ||
      strcmp(profile_id, "chrome_128") == 0 ||
      strcmp(profile_id, "chrome_129") == 0 ||
      strcmp(profile_id, "chrome_130") == 0 ||
      strcmp(profile_id, "chrome_131") == 0 ||
      strcmp(profile_id, "chrome_132") == 0 ||
      strcmp(profile_id, "chrome_133") == 0 ||
      strcmp(profile_id, "chrome_134") == 0 ||
      strcmp(profile_id, "chrome_135") == 0 ||
      strcmp(profile_id, "chrome_136") == 0 ||
      strcmp(profile_id, "chrome_137") == 0 ||
      strcmp(profile_id, "chrome_138") == 0 ||
      strcmp(profile_id, "chrome_139") == 0 ||
      strcmp(profile_id, "chrome_140") == 0 ||
      strcmp(profile_id, "chrome_141") == 0 ||
      strcmp(profile_id, "chrome_142") == 0 ||
      strcmp(profile_id, "chrome_143") == 0 ||
      strcmp(profile_id, "chrome_144") == 0 ||
      strcmp(profile_id, "chrome_145") == 0 ||
      strcmp(profile_id, "chrome_146") == 0 ||
      strcmp(profile_id, "chrome_147") == 0 ||
      strcmp(profile_id, "chrome_148") == 0 ||
      strcmp(profile_id, "chrome_149") == 0 ||
      strcmp(profile_id, "chrome_150") == 0 ||
      strcmp(profile_id, "chrome_151") == 0) {
    if (strcmp(profile_id, "chrome_99") == 0) {
      request_config.headers = chrome_99_headers;
      request_config.header_count =
          sizeof(chrome_99_headers) / sizeof(chrome_99_headers[0]);
    } else if (strcmp(profile_id, "chrome_100") == 0) {
      request_config.headers = chrome_100_headers;
      request_config.header_count =
          sizeof(chrome_100_headers) / sizeof(chrome_100_headers[0]);
    } else if (strcmp(profile_id, "chrome_101") == 0) {
      request_config.headers = chrome_101_headers;
      request_config.header_count =
          sizeof(chrome_101_headers) / sizeof(chrome_101_headers[0]);
    } else if (strcmp(profile_id, "chrome_102") == 0) {
      request_config.headers = chrome_102_headers;
      request_config.header_count =
          sizeof(chrome_102_headers) / sizeof(chrome_102_headers[0]);
    } else if (strcmp(profile_id, "chrome_103") == 0) {
      request_config.headers = chrome_103_headers;
      request_config.header_count =
          sizeof(chrome_103_headers) / sizeof(chrome_103_headers[0]);
    } else if (strcmp(profile_id, "chrome_104") == 0) {
      request_config.headers = chrome_104_headers;
      request_config.header_count =
          sizeof(chrome_104_headers) / sizeof(chrome_104_headers[0]);
    } else if (strcmp(profile_id, "chrome_105") == 0) {
      request_config.headers = chrome_105_headers;
      request_config.header_count =
          sizeof(chrome_105_headers) / sizeof(chrome_105_headers[0]);
    } else if (strcmp(profile_id, "chrome_106") == 0) {
      request_config.headers = chrome_106_headers;
      request_config.header_count =
          sizeof(chrome_106_headers) / sizeof(chrome_106_headers[0]);
    } else if (strcmp(profile_id, "chrome_107") == 0) {
      request_config.headers = chrome_107_headers;
      request_config.header_count =
          sizeof(chrome_107_headers) / sizeof(chrome_107_headers[0]);
    } else if (strcmp(profile_id, "chrome_108") == 0) {
      request_config.headers = chrome_108_headers;
      request_config.header_count =
          sizeof(chrome_108_headers) / sizeof(chrome_108_headers[0]);
    } else if (strcmp(profile_id, "chrome_109") == 0) {
      request_config.headers = chrome_109_headers;
      request_config.header_count =
          sizeof(chrome_109_headers) / sizeof(chrome_109_headers[0]);
    } else if (strcmp(profile_id, "chrome_110") == 0) {
      request_config.headers = chrome_110_headers;
      request_config.header_count =
          sizeof(chrome_110_headers) / sizeof(chrome_110_headers[0]);
    } else if (strcmp(profile_id, "chrome_111") == 0) {
      request_config.headers = chrome_111_headers;
      request_config.header_count =
          sizeof(chrome_111_headers) / sizeof(chrome_111_headers[0]);
    } else if (strcmp(profile_id, "chrome_112") == 0) {
      request_config.headers = chrome_112_headers;
      request_config.header_count =
          sizeof(chrome_112_headers) / sizeof(chrome_112_headers[0]);
    } else if (strcmp(profile_id, "chrome_113") == 0) {
      request_config.headers = chrome_113_headers;
      request_config.header_count =
          sizeof(chrome_113_headers) / sizeof(chrome_113_headers[0]);
    } else if (strcmp(profile_id, "chrome_114") == 0) {
      request_config.headers = chrome_114_headers;
      request_config.header_count =
          sizeof(chrome_114_headers) / sizeof(chrome_114_headers[0]);
    } else if (strcmp(profile_id, "chrome_115") == 0) {
      request_config.headers = chrome_115_headers;
      request_config.header_count =
          sizeof(chrome_115_headers) / sizeof(chrome_115_headers[0]);
    } else if (strcmp(profile_id, "chrome_116") == 0) {
      request_config.headers = chrome_116_headers;
      request_config.header_count =
          sizeof(chrome_116_headers) / sizeof(chrome_116_headers[0]);
    } else if (strcmp(profile_id, "chrome_117") == 0) {
      request_config.headers = chrome_117_headers;
      request_config.header_count =
          sizeof(chrome_117_headers) / sizeof(chrome_117_headers[0]);
    } else if (strcmp(profile_id, "chrome_118") == 0) {
      request_config.headers = chrome_118_headers;
      request_config.header_count =
          sizeof(chrome_118_headers) / sizeof(chrome_118_headers[0]);
    } else if (strcmp(profile_id, "chrome_119") == 0) {
      request_config.headers = chrome_119_headers;
      request_config.header_count =
          sizeof(chrome_119_headers) / sizeof(chrome_119_headers[0]);
    } else if (strcmp(profile_id, "chrome_120") == 0) {
      request_config.headers = chrome_120_headers;
      request_config.header_count =
          sizeof(chrome_120_headers) / sizeof(chrome_120_headers[0]);
    } else if (strcmp(profile_id, "chrome_121") == 0) {
      request_config.headers = chrome_121_headers;
      request_config.header_count =
          sizeof(chrome_121_headers) / sizeof(chrome_121_headers[0]);
    } else if (strcmp(profile_id, "chrome_122") == 0) {
      request_config.headers = chrome_122_headers;
      request_config.header_count =
          sizeof(chrome_122_headers) / sizeof(chrome_122_headers[0]);
    } else if (strcmp(profile_id, "chrome_123") == 0) {
      request_config.headers = chrome_123_headers;
      request_config.header_count =
          sizeof(chrome_123_headers) / sizeof(chrome_123_headers[0]);
    } else if (strcmp(profile_id, "chrome_124") == 0) {
      request_config.headers = chrome_124_headers;
      request_config.header_count =
          sizeof(chrome_124_headers) / sizeof(chrome_124_headers[0]);
    } else if (strcmp(profile_id, "chrome_125") == 0) {
      request_config.headers = chrome_125_headers;
      request_config.header_count =
          sizeof(chrome_125_headers) / sizeof(chrome_125_headers[0]);
    } else if (strcmp(profile_id, "chrome_126") == 0) {
      request_config.headers = chrome_126_headers;
      request_config.header_count =
          sizeof(chrome_126_headers) / sizeof(chrome_126_headers[0]);
    } else if (strcmp(profile_id, "chrome_127") == 0) {
      request_config.headers = chrome_127_headers;
      request_config.header_count =
          sizeof(chrome_127_headers) / sizeof(chrome_127_headers[0]);
    } else if (strcmp(profile_id, "chrome_128") == 0) {
      request_config.headers = chrome_128_headers;
      request_config.header_count =
          sizeof(chrome_128_headers) / sizeof(chrome_128_headers[0]);
    } else if (strcmp(profile_id, "chrome_129") == 0) {
      request_config.headers = chrome_129_headers;
      request_config.header_count =
          sizeof(chrome_129_headers) / sizeof(chrome_129_headers[0]);
    } else if (strcmp(profile_id, "chrome_130") == 0) {
      request_config.headers = chrome_130_headers;
      request_config.header_count =
          sizeof(chrome_130_headers) / sizeof(chrome_130_headers[0]);
    } else if (strcmp(profile_id, "chrome_131") == 0) {
      request_config.headers = chrome_131_headers;
      request_config.header_count =
          sizeof(chrome_131_headers) / sizeof(chrome_131_headers[0]);
    } else if (strcmp(profile_id, "chrome_132") == 0) {
      request_config.headers = chrome_132_headers;
      request_config.header_count =
          sizeof(chrome_132_headers) / sizeof(chrome_132_headers[0]);
    } else if (strcmp(profile_id, "chrome_133") == 0) {
      request_config.headers = chrome_133_headers;
      request_config.header_count =
          sizeof(chrome_133_headers) / sizeof(chrome_133_headers[0]);
    } else if (strcmp(profile_id, "chrome_134") == 0) {
      request_config.headers = chrome_134_headers;
      request_config.header_count =
          sizeof(chrome_134_headers) / sizeof(chrome_134_headers[0]);
    } else if (strcmp(profile_id, "chrome_135") == 0) {
      request_config.headers = chrome_135_headers;
      request_config.header_count =
          sizeof(chrome_135_headers) / sizeof(chrome_135_headers[0]);
    } else if (strcmp(profile_id, "chrome_136") == 0) {
      request_config.headers = chrome_136_headers;
      request_config.header_count =
          sizeof(chrome_136_headers) / sizeof(chrome_136_headers[0]);
    } else if (strcmp(profile_id, "chrome_137") == 0) {
      request_config.headers = chrome_137_headers;
      request_config.header_count =
          sizeof(chrome_137_headers) / sizeof(chrome_137_headers[0]);
    } else if (strcmp(profile_id, "chrome_138") == 0) {
      request_config.headers = chrome_138_headers;
      request_config.header_count =
          sizeof(chrome_138_headers) / sizeof(chrome_138_headers[0]);
    } else if (strcmp(profile_id, "chrome_139") == 0) {
      request_config.headers = chrome_139_headers;
      request_config.header_count =
          sizeof(chrome_139_headers) / sizeof(chrome_139_headers[0]);
    } else if (strcmp(profile_id, "chrome_140") == 0) {
      request_config.headers = chrome_140_headers;
      request_config.header_count =
          sizeof(chrome_140_headers) / sizeof(chrome_140_headers[0]);
    } else if (strcmp(profile_id, "chrome_141") == 0) {
      request_config.headers = chrome_141_headers;
      request_config.header_count =
          sizeof(chrome_141_headers) / sizeof(chrome_141_headers[0]);
    } else if (strcmp(profile_id, "chrome_142") == 0) {
      request_config.headers = chrome_142_headers;
      request_config.header_count =
          sizeof(chrome_142_headers) / sizeof(chrome_142_headers[0]);
    } else if (strcmp(profile_id, "chrome_143") == 0) {
      request_config.headers = chrome_143_headers;
      request_config.header_count =
          sizeof(chrome_143_headers) / sizeof(chrome_143_headers[0]);
    } else if (strcmp(profile_id, "chrome_144") == 0) {
      request_config.headers = chrome_144_headers;
      request_config.header_count =
          sizeof(chrome_144_headers) / sizeof(chrome_144_headers[0]);
    } else if (strcmp(profile_id, "chrome_145") == 0) {
      request_config.headers = chrome_145_headers;
      request_config.header_count =
          sizeof(chrome_145_headers) / sizeof(chrome_145_headers[0]);
    } else if (strcmp(profile_id, "chrome_146") == 0) {
      request_config.headers = chrome_146_headers;
      request_config.header_count =
          sizeof(chrome_146_headers) / sizeof(chrome_146_headers[0]);
    } else if (strcmp(profile_id, "chrome_147") == 0) {
      request_config.headers = chrome_147_headers;
      request_config.header_count =
          sizeof(chrome_147_headers) / sizeof(chrome_147_headers[0]);
    } else if (strcmp(profile_id, "chrome_148") == 0) {
      request_config.headers = chrome_148_headers;
      request_config.header_count =
          sizeof(chrome_148_headers) / sizeof(chrome_148_headers[0]);
    } else if (strcmp(profile_id, "chrome_149") == 0) {
      request_config.headers = chrome_149_headers;
      request_config.header_count =
          sizeof(chrome_149_headers) / sizeof(chrome_149_headers[0]);
    } else if (strcmp(profile_id, "chrome_150") == 0) {
      request_config.headers = chrome_150_headers;
      request_config.header_count =
          sizeof(chrome_150_headers) / sizeof(chrome_150_headers[0]);
    } else {
      request_config.headers = chrome_151_headers;
      request_config.header_count =
          sizeof(chrome_151_headers) / sizeof(chrome_151_headers[0]);
    }
    request_config.priority = MN_REQUEST_PRIORITY_HIGHEST;
  }
  request_config.timeout_ms = 20000;
  request_config.callbacks.size = sizeof(request_config.callbacks);
  request_config.callbacks.version = MN_ABI_VERSION;
  request_config.callbacks.user_data = &state;
  request_config.callbacks.on_response = on_response;
  request_config.callbacks.on_body = on_body;
  request_config.callbacks.on_complete = on_complete;

  for (int request_index = 0; request_index < request_count; ++request_index) {
    result = MN_OK;
    atomic_store_explicit(&state.done, 0, memory_order_relaxed);
    state.result = MN_OK;
    state.net_error = 0;
    state.status_code = 0;
    state.body_length = 0;

    mn_request_t *request = NULL;
    result = mn_request_create(engine, &request_config, &request);
    if (result == MN_OK) {
      result = mn_request_start(request);
    }
    if (result == MN_OK) {
      for (int i = 0; i < 20000 &&
                      !atomic_load_explicit(&state.done, memory_order_acquire);
           ++i) {
        sleep_millisecond();
      }
      if (!atomic_load_explicit(&state.done, memory_order_acquire)) {
        mn_request_cancel(request);
        for (int i = 0; i < 5000 && !atomic_load_explicit(&state.done,
                                                          memory_order_acquire);
             ++i) {
          sleep_millisecond();
        }
        result = MN_ERROR_TIMEOUT;
      } else {
        result = state.result;
      }
    }
    if (request) {
      mn_request_release(request);
    }
    if (request_index + 1 < request_count) {
      const char *delay_value = getenv("MINICRONET_REQUEST_DELAY_MS");
      long delay_ms = delay_value ? strtol(delay_value, NULL, 10) : 0;
      if (delay_ms < 0 || delay_ms > 60000) {
        delay_ms = 0;
      }
      for (long i = 0; i < delay_ms; ++i) {
        sleep_millisecond();
      }
    }
  }
  mn_engine_release(engine);
  if (getenv("MINICRONET_SSL_KEY_LOG_FILE")) {
    for (int i = 0; i < 1000; ++i) {
      sleep_millisecond();
    }
  }

  int body_written = result == MN_OK && write_body(body_file, &state);
  printf("{\"profile_id\":\"%s\",\"stage\":\"request\",\"result\":%d,"
         "\"net_error\":%d,\"status_code\":%d,\"body_bytes\":%zu,"
         "\"body_written\":%s}\n",
         profile_id, result, state.net_error, state.status_code,
         state.body_length, body_written ? "true" : "false");
  free(state.body);
  return 0;
}
