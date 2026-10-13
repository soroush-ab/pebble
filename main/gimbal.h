#ifndef PEBBLE_GIMBAL_H_
#define PEBBLE_GIMBAL_H_

#include <stdbool.h>

#include "esp_err.h"

/*
 * gimbal - the robot's base, a BLDC motor run by the BGC board over UART.
 *
 * This replaces the stepper. The ESP32 sends setpoints in degrees; the BGC
 * board owns commutation and the position loop, for the same reason step
 * generation stayed local: it is hard real-time and cannot cross a network.
 *
 * Position here is MEASURED, reported by the AS5600 on the BGC board - unlike
 * the stepper, which could only ever report where it believed it was.
 */

esp_err_t gimbal_init(void);
void      gimbal_set(float deg);
void      gimbal_enable(bool on);
/* Last reported state. `fresh` is false if the board has gone quiet. */
void      gimbal_get(float *angle_deg, float *target_deg, bool *enabled,
                     bool *ready, bool *fresh);

#endif
