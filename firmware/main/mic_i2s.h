#ifndef PEBBLE_MIC_I2S_H_
#define PEBBLE_MIC_I2S_H_
#include "esp_err.h"
#include <stdbool.h>
esp_err_t mic_i2s_init(void);
esp_err_t mic_i2s_start(void);
void      mic_i2s_enable(bool on);
/* Bring-up only; see mic_adc.h for why the MCU measures anything at all. */
void      mic_i2s_stats(float *dc, float *rms, float *peak, unsigned *frames);
#endif
