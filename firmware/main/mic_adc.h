#ifndef PEBBLE_MIC_ADC_H_
#define PEBBLE_MIC_ADC_H_
#include "esp_err.h"
#include <stdbool.h>
#include <stdint.h>
esp_err_t mic_adc_init(void);
esp_err_t mic_adc_start(void);
void      mic_adc_enable(bool on);
/* Per-channel sampling offsets within one SAR scan, nanoseconds.
 * Derived from the MEASURED conversion rate: the S3 delivers about half the
 * rate it is configured for, which doubles the real skew. */
const int32_t *mic_adc_skew_ns(void);
uint32_t  mic_adc_fs_per_ch(void);

/* Bring-up statistics, computed on-device.
 *
 * The MCU does no DSP in normal operation - the PC does. These exist purely so
 * the array can be verified with no Wi-Fi and no PC, which is what you need
 * when the question is "are all three mics wired". */
typedef struct {
    float    dc[3];        /* mean, ADC counts (VCC/2 on the AUDIO pin)  */
    float    rms[3];       /* AC RMS about that mean                     */
    float    peak[3];      /* max |AC|                                   */
    uint32_t clipped[3];
    uint32_t frames;
} mic_adc_stats_t;
void mic_adc_stats(mic_adc_stats_t *out);
#endif
