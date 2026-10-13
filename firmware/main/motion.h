#ifndef PEBBLE_MOTION_H_
#define PEBBLE_MOTION_H_
#include "esp_err.h"
#include <stdbool.h>
esp_err_t motion_init(void);
/* Setpoints from the PC. The ESP interpolates under its own limits. */
void motion_set(float base_deg, float servo_a_deg, float servo_b_deg, float max_vel_dps);
void motion_enable(bool base, bool servos);   /* base = BLDC via BGC */
void motion_get(float *base_deg, float *sa_deg, float *sb_deg, bool *moving);
#endif
