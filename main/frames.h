#ifndef PEBBLE_FRAMES_H_
#define PEBBLE_FRAMES_H_
/* Eye frames stored in PSRAM and played locally (see proto.h, MSG_FRAME_*). */
#include <stdint.h>
#include "esp_err.h"
#include "proto.h"

esp_err_t frames_init(void);
/* One MSG_FRAME_STORE payload (header + chunk). */
void frames_store(const uint8_t *p, uint32_t len);
/* Show a pair now; cancels any running animation. */
void frames_show(uint16_t left, uint16_t right);
/* Play n pairs, frame_ms each; cancels any running animation. */
void frames_anim(const frame_pair_t *steps, uint8_t n, uint16_t frame_ms);
void frames_clear(void);
void frames_info(frame_info_t *out);
#endif
