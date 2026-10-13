/*
 * mic_adc - stream the three analog microphones to the PC.
 *
 * No DSP here by design: the ESP packs raw samples with an exact timebase and
 * the PC does bearing/TDOA. Two details make that possible at all.
 *
 * 1. SCAN ALIGNMENT. A DMA read can begin part-way through a round-robin
 *    scan. Appending each sample to its own channel array then lets the three
 *    channels drift a whole scan apart - invisible in the levels, but it adds
 *    +/-36 us to every delay the PC computes, which is ~7 deg of bearing.
 *    Samples are only accepted in slot order, resyncing on any gap.
 *
 * 2. TIMEBASE. `sample_index` is exact: it counts ADC scans, so the PC gets
 *    perfect RELATIVE timing from the ADC's own clock regardless of Wi-Fi
 *    jitter. `t_us` only anchors that index to wall time, and is approximate
 *    by roughly one frame. Use the index for timing, the stamp for alignment
 *    with other sensors.
 */
#include "mic_adc.h"

#include <inttypes.h>
#include <math.h>
#include <string.h>

#include "board.h"
#include "esp_adc/adc_continuous.h"
#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "net_link.h"
#include "proto.h"

static const char *TAG = "mic_adc";

#define N_CH        3
#define TOTAL_HZ    83333                    /* SOC_ADC_SAMPLE_FREQ_THRES_HIGH */
#define FS_PER_CH   (TOTAL_HZ / N_CH)        /* 27777                          */
#define FRAME_N     512                      /* samples per channel, ~18.4 ms  */
#define READ_BYTES  (N_CH * FRAME_N * SOC_ADC_DIGI_RESULT_BYTES)

static const int s_gpio[N_CH] = {PIN_MIC_A, PIN_MIC_B, PIN_MIC_C};
static adc_channel_t s_chan[N_CH];
static int s_chan_to_mic[ADC_CHANNEL_9 + 1];
static int32_t s_skew_ns[4];

static adc_continuous_handle_t s_adc;
static uint8_t *s_dma;
static int16_t *s_out;                       /* interleaved a,b,c            */
static volatile bool s_enabled = true;
static uint64_t s_index;
static mic_adc_stats_t s_stats;

/* MEASURED per-channel rate, not the configured one.
 *
 * The ESP32-S3 continuous ADC delivers about half the sample_freq_hz it is
 * asked for with a 3-channel pattern: requesting 83333 Hz total yields ~41670,
 * i.e. ~13890 Hz per channel instead of 27777. Measured against the chip's own
 * timer and confirmed independently by the PC from the stream timestamps.
 *
 * This is not cosmetic. The round-robin slot spacing is 1/total_rate, so the
 * inter-channel skew is ~24 us, not 12 - and reporting the wrong skew biases
 * every bearing the PC computes. Everything downstream therefore uses the
 * measured rate. */
static volatile float s_fs_meas;
static uint64_t s_meas_t0, s_meas_i0;

static const int32_t *mic_adc_skew_ns_live(void);
const int32_t *mic_adc_skew_ns(void) { return mic_adc_skew_ns_live(); }
uint32_t mic_adc_fs_per_ch(void)
{
    /* Measured once available; the nominal value is only a startup fallback. */
    const float m = s_fs_meas;
    return (m > 100.0f) ? (uint32_t)(m + 0.5f) : (uint32_t)FS_PER_CH;
}

static const int32_t *mic_adc_skew_ns_live(void)
{
    static int32_t sk[4];
    const uint32_t per_ch = mic_adc_fs_per_ch();
    const double total = (double)per_ch * N_CH;
    for (int i = 0; i < N_CH; i++) {
        sk[i] = (int32_t)(1e9 * i / total);
    }
    return sk;
}
void mic_adc_enable(bool on) { s_enabled = on; }

esp_err_t mic_adc_init(void)
{
    for (int i = 0; i <= ADC_CHANNEL_9; i++) s_chan_to_mic[i] = -1;

    adc_digi_pattern_config_t pat[N_CH] = {0};
    for (int i = 0; i < N_CH; i++) {
        adc_unit_t unit;
        ESP_RETURN_ON_ERROR(adc_continuous_io_to_channel(s_gpio[i], &unit, &s_chan[i]),
                            TAG, "GPIO%d not an ADC pin", s_gpio[i]);
        ESP_RETURN_ON_FALSE(unit == ADC_UNIT_1, ESP_ERR_INVALID_ARG, TAG,
                            "GPIO%d is on ADC2, unusable with Wi-Fi", s_gpio[i]);
        pat[i].atten = ADC_ATTEN_DB_12;
        pat[i].channel = s_chan[i] & 0x7;
        pat[i].unit = ADC_UNIT_1;
        pat[i].bit_width = SOC_ADC_DIGI_MAX_BITWIDTH;
        s_chan_to_mic[s_chan[i]] = i;
        s_skew_ns[i] = (int32_t)(1e9 * i / TOTAL_HZ);   /* 0, 12000, 24000 */
        ESP_LOGI(TAG, "mic %d: GPIO%d -> ADC1_CH%d, scan offset %" PRId32 " ns",
                 i, s_gpio[i], s_chan[i], s_skew_ns[i]);
    }

    adc_continuous_handle_cfg_t hc = {
        .max_store_buf_size = READ_BYTES * 4,
        .conv_frame_size = READ_BYTES,
    };
    ESP_RETURN_ON_ERROR(adc_continuous_new_handle(&hc, &s_adc), TAG, "handle");

    adc_continuous_config_t dc = {
        .pattern_num = N_CH,
        .adc_pattern = pat,
        .sample_freq_hz = TOTAL_HZ,
        .conv_mode = ADC_CONV_SINGLE_UNIT_1,
    };
    ESP_RETURN_ON_ERROR(adc_continuous_config(s_adc, &dc), TAG, "config");

    s_dma = heap_caps_malloc(READ_BYTES, MALLOC_CAP_DEFAULT);
    s_out = heap_caps_malloc(N_CH * FRAME_N * sizeof(int16_t), MALLOC_CAP_DEFAULT);
    return (s_dma && s_out) ? ESP_OK : ESP_ERR_NO_MEM;
}

static void mic_task(void *arg)
{
    (void)arg;
    for (;;) {
        if (!s_enabled) {
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }

        int filled = 0, slot = 0;
        while (filled < FRAME_N) {
            uint32_t got = 0;
            if (adc_continuous_read(s_adc, s_dma, READ_BYTES, &got, 200) != ESP_OK
                || got == 0) {
                break;
            }
            for (uint32_t off = 0; off < got && filled < FRAME_N;
                 off += SOC_ADC_DIGI_RESULT_BYTES) {
                const adc_digi_output_data_t *d =
                    (const adc_digi_output_data_t *)&s_dma[off];
                const uint32_t ch = d->type2.channel;
                if (ch > ADC_CHANNEL_9) continue;
                const int m = s_chan_to_mic[ch];
                if (m < 0) continue;

                if (m != slot) {              /* lost a sample: resync to slot 0 */
                    slot = 0;
                    if (m != 0) continue;
                }
                s_out[filled * N_CH + m] = (int16_t)d->type2.data;
                slot++;
                if (slot == N_CH) { slot = 0; filled++; }
            }
        }
        if (filled == 0) continue;

        /* Per-channel stats for the bring-up test. One pass over the frame,
         * ~1.5k operations per 18 ms - free, and it means the array can be
         * checked before the network exists. */
        for (int c = 0; c < N_CH; c++) {
            double sum = 0.0;
            for (int i = 0; i < filled; i++) sum += s_out[i * N_CH + c];
            const float dc = (float)(sum / filled);
            double e = 0.0; float pk = 0.0f; uint32_t clip = 0;
            for (int i = 0; i < filled; i++) {
                const int v = s_out[i * N_CH + c];
                if (v >= 4080 || v <= 15) clip++;
                const float a = (float)v - dc;
                e += (double)a * a;
                const float aa = a < 0 ? -a : a;
                if (aa > pk) pk = aa;
            }
            s_stats.dc[c] = dc;
            s_stats.rms[c] = (float)sqrt(e / filled);
            s_stats.peak[c] = pk;
            s_stats.clipped[c] = clip;
        }
        s_stats.frames++;

        /* Rate measurement over a 2 s window, restarted each time so it
         * tracks reality rather than averaging over a startup transient. */
        {
            const uint64_t tnow = (uint64_t)esp_timer_get_time();
            if (s_meas_t0 == 0) {
                s_meas_t0 = tnow;
                s_meas_i0 = s_index;
            } else if (tnow - s_meas_t0 >= 3000000ULL) {   /* 3 s: short windows track load, not the clock */
                s_fs_meas = (float)((s_index - s_meas_i0) * 1000000.0
                                    / (double)(tnow - s_meas_t0));
                s_meas_t0 = tnow;
                s_meas_i0 = s_index;
            }
        }

        if (!net_link_up()) {
            continue;                  /* sampled and measured; nothing to send */
        }

        const uint64_t now = (uint64_t)esp_timer_get_time();
        audio_hdr_t h = {
            /* back-date to the first sample; exact relative timing comes from
             * sample_index, this only anchors to wall time */
            .t_us = now - (uint64_t)(1000000ULL * filled / FS_PER_CH),
            .sample_index = s_index,
            .n_frames = (uint16_t)filled,
            .n_ch = N_CH,
            .bits = 16,
        };
        s_index += (uint64_t)filled;
        net_send(MSG_MIC_ANALOG, &h, sizeof(h), s_out,
                 (size_t)filled * N_CH * sizeof(int16_t));
    }
}

esp_err_t mic_adc_start(void)
{
    ESP_RETURN_ON_ERROR(adc_continuous_start(s_adc), TAG, "start");
    return xTaskCreate(mic_task, "mic_adc", 4096, NULL, 6, NULL) == pdPASS
           ? ESP_OK : ESP_ERR_NO_MEM;
}

void mic_adc_stats(mic_adc_stats_t *out) { *out = s_stats; }
