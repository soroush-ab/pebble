/* mic_i2s - the digital microphone, streamed raw to the PC.
 *
 * NOTE ON "I2C MICROPHONE": digital MEMS mics in this class (INMP441,
 * SPH0645, ICS-43434) are I2S parts, not I2C. This driver assumes I2S. If
 * yours really is I2C, this is the only file that changes - add it to the
 * encoder's I2C bus and keep the same MSG_MIC_DIGITAL framing.
 *
 * This is the mic for speech: close-talking, clean, and the one the PC should
 * feed to STT. The analog array is for direction, not content.
 */
#include "mic_i2s.h"

#include <math.h>

#include "board.h"
#include "driver/i2s_std.h"
#include "esp_check.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "net_link.h"
#include "proto.h"

static const char *TAG = "mic_i2s";

#define FS_HZ     16000
#define FRAME_N   512

static i2s_chan_handle_t s_rx;
static volatile bool s_enabled = true;
static uint64_t s_index;
static float s_dc, s_rms, s_peak;
static unsigned s_frames;

void mic_i2s_enable(bool on) { s_enabled = on; }

esp_err_t mic_i2s_init(void)
{
    i2s_chan_config_t cc = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    ESP_RETURN_ON_ERROR(i2s_new_channel(&cc, NULL, &s_rx), TAG, "chan");

    i2s_std_config_t sc = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(FS_HZ),
        /* 32-bit slot, left channel: these mics put 24 bits MSB-first into a
         * 32-bit slot and leave L/R selected by the SEL pin. */
        .slot_cfg = I2S_STD_MSB_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT,
                                                    I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = PIN_I2S_BCLK,
            .ws   = PIN_I2S_WS,
            .dout = I2S_GPIO_UNUSED,
            .din  = PIN_I2S_DIN,
        },
    };
    ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(s_rx, &sc), TAG, "std");
    ESP_LOGI(TAG, "%d Hz mono on BCLK%d WS%d DIN%d",
             FS_HZ, PIN_I2S_BCLK, PIN_I2S_WS, PIN_I2S_DIN);
    return ESP_OK;
}

static void i2s_task(void *arg)
{
    (void)arg;
    static int32_t raw[FRAME_N];
    static int16_t out[FRAME_N];

    for (;;) {
        if (!s_enabled) {
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }
        size_t got = 0;
        if (i2s_channel_read(s_rx, raw, sizeof(raw), &got, 200) != ESP_OK || got == 0) {
            continue;
        }
        const int n = (int)(got / sizeof(int32_t));
        /* 24-bit left-justified in 32: >>16 keeps the top 16 bits */
        for (int i = 0; i < n; i++) out[i] = (int16_t)(raw[i] >> 16);

        double sum = 0.0;
        for (int i = 0; i < n; i++) sum += out[i];
        s_dc = (float)(sum / n);
        double e = 0.0; float pk = 0.0f;
        for (int i = 0; i < n; i++) {
            const float a = (float)out[i] - s_dc;
            e += (double)a * a;
            const float aa = a < 0 ? -a : a;
            if (aa > pk) pk = aa;
        }
        s_rms = (float)sqrt(e / n);
        s_peak = pk;
        s_frames++;

        if (!net_link_up()) {
            continue;
        }

        const uint64_t now = (uint64_t)esp_timer_get_time();
        audio_hdr_t h = {
            .t_us = now - (uint64_t)(1000000ULL * n / FS_HZ),
            .sample_index = s_index,
            .n_frames = (uint16_t)n,
            .n_ch = 1,
            .bits = 16,
        };
        s_index += (uint64_t)n;
        net_send(MSG_MIC_DIGITAL, &h, sizeof(h), out, (size_t)n * sizeof(int16_t));
    }
}

esp_err_t mic_i2s_start(void)
{
    ESP_RETURN_ON_ERROR(i2s_channel_enable(s_rx), TAG, "enable");
    return xTaskCreate(i2s_task, "mic_i2s", 4096, NULL, 6, NULL) == pdPASS
           ? ESP_OK : ESP_ERR_NO_MEM;
}

void mic_i2s_stats(float *dc, float *rms, float *peak, unsigned *frames)
{
    if (dc) *dc = s_dc;
    if (rms) *rms = s_rms;
    if (peak) *peak = s_peak;
    if (frames) *frames = s_frames;
}
