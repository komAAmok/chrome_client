#include "minicronet.h"

#include <stdio.h>
#include <string.h>
#include <stdlib.h>

struct engine_node {
  mn_engine_t *engine;
  struct engine_node *next;
};

static mn_engine_t *create_engine(const char *profile_id) {
  char profile_namespace[64];
  int length = snprintf(profile_namespace, sizeof(profile_namespace),
                        "minicronet/%s", profile_id);
  if (length < 0 || (size_t)length >= sizeof(profile_namespace)) {
    return NULL;
  }
  mn_engine_config_t config = {0};
  config.size = sizeof(config);
  config.version = MN_ABI_VERSION;
  config.profile_id = profile_id;
  config.profile_id_length = strlen(profile_id);
  config.profile_namespace = profile_namespace;
  config.profile_namespace_length = (size_t)length;
  mn_engine_t *engine = NULL;
  return mn_engine_create(&config, &engine) == MN_OK ? engine : NULL;
}

int main(void) {
  struct engine_node *head = NULL;
  for (int major = 99; major <= 151; ++major) {
    char profile_id[16];
    snprintf(profile_id, sizeof(profile_id), "chrome_%d", major);
    mn_engine_t *engine = create_engine(profile_id);
    if (!engine) {
      while (head) {
        struct engine_node *node = head;
        head = head->next;
        mn_engine_release(node->engine);
        free(node);
      }
      return 1;
    }
    struct engine_node *node = (struct engine_node *)malloc(sizeof(*node));
    if (!node) {
      mn_engine_release(engine);
      while (head) {
        struct engine_node *old = head;
        head = head->next;
        mn_engine_release(old->engine);
        free(old);
      }
      return 1;
    }
    node->engine = engine;
    node->next = head;
    head = node;
  }
  while (head) {
    struct engine_node *node = head;
    head = head->next;
    mn_engine_release(node->engine);
    free(node);
  }
  puts("Chrome 99-151 profile isolation passed");
  return 0;
}
