/*
 * motion - the base (BLDC via the BGC board) and two servos.
 *
 * The base used to be a stepper whose pulses were generated here by a 20 kHz
 * timer. It is now a direct-drive gimbal motor run by the BGC controller over
 * UART (gimbal.c), chosen mainly because a stepper clicks and this robot's
 * job is listening to speech. The trajectory and commutation moved onto that
 * board; what stays here is the same contract as before - the PC sends
 * setpoints, something local and real-time executes them.
 *
 * Servos are still driven directly by LEDC.
 *
 * NOTE ON POWER: moving both servos simultaneously browns out the ESP32 when
 * they share its supply. Measured: either servo alone survives indefinitely,
 * both together reboot the board within two commands. Give the servos their
 * own 5-6 V rail with a common ground.
 */
#include "motion.h"

#include <math.h>

#include "board.h"
#include "driver/gpio.h"
#include "driver/ledc.h"
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "gimbal.h"

static const char *TAG = "motion";


static bool  s_base_en;
static bool  s_servo_en = true;
static float s_sa_deg, s_sb_deg, s_goal_deg;

/* ----------------------------------------------------------------- servos */

static void servo_write(ledc_channel_t ch, float deg)
{
    if (deg < -90.0f) deg = -90.0f;
    if (deg >  90.0f) deg =  90.0f;
    const float us = SERVO_MIN_US +
                     (deg + 90.0f) / 180.0f * (SERVO_MAX_US - SERVO_MIN_US);
    /* 14-bit at 50 Hz: one period is 20000 us across 16384 counts */
    const uint32_t duty = (uint32_t)(us / 20000.0f * 16383.0f);
    ledc_set_duty(LEDC_LOW_SPEED_MODE, ch, duty);
    ledc_update_duty(LEDC_LOW_SPEED_MODE, ch);
}

/* -------------------------------------------------------------------- api */

void motion_set(float base_deg, float sa, float sb, float max_vel)
{
    (void)max_vel;                       /* the BGC board owns the ramp now */
    s_goal_deg = fminf(BASE_MAX_DEG, fmaxf(BASE_MIN_DEG, base_deg));
    gimbal_set(s_goal_deg);
    s_sa_deg = sa;
    s_sb_deg = sb;
    if (s_servo_en) {
        servo_write(LEDC_CHANNEL_0, sa);
        servo_write(LEDC_CHANNEL_1, sb);
    }
}

void motion_enable(bool base, bool servos)
{
    s_base_en = base;
    s_servo_en = servos;
    gimbal_enable(base);
}

void motion_get(float *base_deg, float *sa, float *sb, bool *moving)
{
    /* MEASURED angle from the AS5600 on the BGC board - the stepper could
     * only ever report where it believed it was. */
    float ang = 0.0f;
    bool fresh = false;
    gimbal_get(&ang, NULL, NULL, NULL, &fresh);
    if (base_deg) *base_deg = fresh ? ang : 0.0f;
    if (sa) *sa = s_sa_deg;
    if (sb) *sb = s_sb_deg;
    if (moving) *moving = fresh && (fabsf(ang - s_goal_deg) > 0.5f);
}

esp_err_t motion_init(void)
{
    ledc_timer_config_t lt = {
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .duty_resolution = LEDC_TIMER_14_BIT,
        .timer_num = LEDC_TIMER_0,
        .freq_hz = SERVO_HZ,
        .clk_cfg = LEDC_AUTO_CLK,
    };
    ESP_RETURN_ON_ERROR(ledc_timer_config(&lt), TAG, "ledc timer");
    const int pins[2] = {PIN_SERVO_A, PIN_SERVO_B};
    for (int i = 0; i < 2; i++) {
        ledc_channel_config_t lc = {
            .gpio_num = pins[i],
            .speed_mode = LEDC_LOW_SPEED_MODE,
            .channel = (ledc_channel_t)i,
            .timer_sel = LEDC_TIMER_0,
            .duty = 0,
        };
        ESP_RETURN_ON_ERROR(ledc_channel_config(&lc), TAG, "ledc ch%d", i);
    }
    servo_write(LEDC_CHANNEL_0, 0.0f);
    servo_write(LEDC_CHANNEL_1, 0.0f);

    ESP_RETURN_ON_ERROR(gimbal_init(), TAG, "gimbal link");
    ESP_LOGI(TAG, "base: BLDC over the BGC board; servos on GPIO%d/%d",
             PIN_SERVO_A, PIN_SERVO_B);
    return ESP_OK;
}
