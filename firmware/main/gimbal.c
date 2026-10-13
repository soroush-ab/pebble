#include "gimbal.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "board.h"
#include "driver/uart.h"
#include "esp_check.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "gimbal";

#define UART_PORT   UART_NUM_1
#define RX_BUF      1024
#define STALE_US    1000000ULL      /* board silent for 1 s = not fresh */

static float s_angle, s_target, s_volt;
static bool  s_enabled, s_ready;
static uint64_t s_last_rx;

void gimbal_set(float deg)
{
    if (deg < BASE_MIN_DEG) deg = BASE_MIN_DEG;
    if (deg > BASE_MAX_DEG) deg = BASE_MAX_DEG;
    char buf[24];
    const int n = snprintf(buf, sizeof(buf), "T%.2f\n", deg);
    uart_write_bytes(UART_PORT, buf, n);
}

void gimbal_enable(bool on)
{
    const char *s = on ? "E1\n" : "E0\n";
    uart_write_bytes(UART_PORT, s, strlen(s));
}

void gimbal_get(float *angle_deg, float *target_deg, bool *enabled,
                bool *ready, bool *fresh)
{
    if (angle_deg)  *angle_deg = s_angle;
    if (target_deg) *target_deg = s_target;
    if (enabled)    *enabled = s_enabled;
    if (ready)      *ready = s_ready;
    if (fresh)      *fresh = ((uint64_t)esp_timer_get_time() - s_last_rx) < STALE_US;
}

/* Status lines look like:  A12.34 T15.00 V3.00 E1 R1
 * Notices look like:       !message                        */
static void parse(char *l)
{
    if (l[0] == '!') {
        ESP_LOGW(TAG, "board: %s", l + 1);
        return;
    }
    if (l[0] != 'A') {
        return;
    }
    float a = 0, t = 0, v = 0;
    int e = 0, r = 0;
    if (sscanf(l, "A%f T%f V%f E%d R%d", &a, &t, &v, &e, &r) == 5) {
        s_angle = a; s_target = t; s_volt = v;
        s_enabled = e != 0; s_ready = r != 0;
        s_last_rx = (uint64_t)esp_timer_get_time();
    }
}

static void rx_task(void *arg)
{
    (void)arg;
    static char line[96];
    int n = 0;
    uint8_t c;
    bool warned = false;

    for (;;) {
        if (uart_read_bytes(UART_PORT, &c, 1, pdMS_TO_TICKS(500)) == 1) {
            if (c == '\n' || c == '\r') {
                if (n) { line[n] = 0; parse(line); n = 0; }
            } else if (n < (int)sizeof(line) - 1) {
                line[n++] = (char)c;
            }
            warned = false;
        } else if (!warned) {
            /* Silence is meaningful: the base is unpowered, the level shifter
             * is wrong, or USB is still plugged into the BGC board and both
             * are driving its RX. */
            ESP_LOGW(TAG, "no status from the BGC board on GPIO%d "
                          "(power? level shifter? USB still attached?)",
                     PIN_GIMBAL_RX);
            warned = true;
        }
    }
}

esp_err_t gimbal_init(void)
{
    const uart_config_t cfg = {
        .baud_rate = GIMBAL_BAUD,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    ESP_RETURN_ON_ERROR(uart_driver_install(UART_PORT, RX_BUF, 0, 0, NULL, 0),
                        TAG, "driver");
    ESP_RETURN_ON_ERROR(uart_param_config(UART_PORT, &cfg), TAG, "config");
    ESP_RETURN_ON_ERROR(uart_set_pin(UART_PORT, PIN_GIMBAL_TX, PIN_GIMBAL_RX,
                                     UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE),
                        TAG, "pins");

    ESP_LOGI(TAG, "BGC link on TX%d/RX%d @ %d baud",
             PIN_GIMBAL_TX, PIN_GIMBAL_RX, GIMBAL_BAUD);

    gimbal_enable(false);           /* never come up energised */
    return xTaskCreate(rx_task, "gimbal", 3072, NULL, 5, NULL) == pdPASS
           ? ESP_OK : ESP_ERR_NO_MEM;
}
