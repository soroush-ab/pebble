#ifndef PEBBLE_DISP_H_
#define PEBBLE_DISP_H_
#include "esp_err.h"
#include <stdint.h>
esp_err_t disp_init(void);
/* which: 0 left, 1 right, 2 both. px is RGB565 little-endian, w*h entries. */
esp_err_t disp_blit(uint8_t which, uint16_t x, uint16_t y, uint16_t w, uint16_t h,
                    const uint16_t *px);
esp_err_t disp_fill(uint8_t which, uint16_t x, uint16_t y, uint16_t w, uint16_t h,
                    uint16_t color);
/* A full panel from RLE16 (see proto.h, MSG_FRAME_STORE). which: 0 or 1. */
esp_err_t disp_rle(uint8_t which, const uint8_t *rle, uint32_t len);
#endif
