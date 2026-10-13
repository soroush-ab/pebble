#ifndef PEBBLE_ENCODER_H_
#define PEBBLE_ENCODER_H_
#include "esp_err.h"
#include <stdbool.h>
#include <stdint.h>
esp_err_t encoder_init(void);
bool encoder_read(uint16_t *raw, float *deg, uint8_t *status);
/* Last three bytes read from STATUS/ANGLE_H/ANGLE_L, for bus diagnosis. */
void encoder_last_bytes(uint8_t out[3]);
/* Read `n` bytes from `reg` in a single transaction. For diagnosis: lets the
 * self-test poke individual AS5600 registers instead of trusting one burst. */
bool encoder_reg(uint8_t reg, uint8_t *buf, size_t n);
/* Probe every 7-bit address and print which ACK. The decisive wiring test. */
void encoder_scan(void);
/* True only if `status` is a combination the AS5600 can actually produce.
 * 0x3F sets ML and MH together - too weak AND too strong - which is the
 * signature of a floating bus, not a magnet. */
bool encoder_status_sane(uint8_t status);
#endif
