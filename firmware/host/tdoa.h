/*
 * tdoa - bearing from inter-microphone arrival-time differences.
 *
 * Pure C99, no ESP-IDF. Host-tested in test/host_test.c.
 *
 * Unlike the envelope path (mic_array.h), this uses the RAW AUDIO output and
 * real timing, so it does not depend on the body shadowing the mics.
 *
 * ---------------------------------------------------------------------------
 * THE TIMING BUDGET, for a 100 mm array at 343 m/s
 *
 *   max delay between a pair           291 us
 *   delay change per degree (broadside) 5.1 us/deg
 *   so +/-15 deg needs                  ~76 us of timing accuracy
 *
 * The ESP32-S3 ADC runs at 83333 Hz TOTAL (SOC_ADC_SAMPLE_FREQ_THRES_HIGH),
 * so three channels get 27.8 kHz each - a 36 us sample period. Parabolic
 * interpolation on the correlation peak reaches roughly 0.1-0.3 samples,
 * i.e. 4-11 us. That closes the budget with 3-5x margin.
 *
 * ---------------------------------------------------------------------------
 * THE CATCH: the SAR is multiplexed, not simultaneous
 *
 * Channels are converted round-robin, so channel j is sampled (j-i)/83333 s
 * after channel i - 12 us per slot, which is a sizeable fraction of the 76 us
 * budget. It is deterministic hardware timing, so it is CORRECTED, not
 * ignored: pass the per-channel sampling offsets to tdoa_solve() and it
 * subtracts them. Getting this wrong biases every bearing.
 *
 * ---------------------------------------------------------------------------
 * THE SOLVER
 *
 * For a far-field source in unit direction u, the arrival time at mic i is
 * (D - p_i.u)/c, so for each pair
 *
 *     (p_j - p_i) . u = c * tau_ij        where tau_ij = t_i - t_j
 *
 * Three pairs give three such rows; least squares solves the 2 unknowns in u.
 * |u| should come out near 1 for a consistent far-field solution, so the
 * deviation of |u| from 1 is a free quality metric - it rises with noise,
 * reverberation and near-field sources. That is `consistency` below.
 */

#ifndef TDOA_H
#define TDOA_H

#include <stdbool.h>

#include "mic_array.h"      /* MIC_COUNT */

#ifdef __cplusplus
extern "C" {
#endif

#define TDOA_PAIRS 3        /* (0,1) (0,2) (1,2) */

extern const int TDOA_PAIR_I[TDOA_PAIRS];
extern const int TDOA_PAIR_J[TDOA_PAIRS];

typedef struct {
    float pos_m[MIC_COUNT][2];   /* mic positions, robot frame, metres */
    float c_mps;                 /* speed of sound                     */
    float max_tau_s;             /* largest physically possible |tau|  */
} tdoa_array_t;

typedef struct {
    float tau_s[TDOA_PAIRS];     /* corrected pair delays, seconds     */
    float peak[TDOA_PAIRS];      /* normalised correlation peak, 0..1  */
    float bearing_deg;           /* (-180,180], 0 = front, +ve CCW     */
    float consistency;           /* |u|; 1.0 is ideal                  */
    float confidence;            /* 0..1 heuristic                     */
    bool  valid;
} tdoa_result_t;

/* angles_deg: mic azimuths; radius_m: array circumradius. */
void tdoa_init(tdoa_array_t *a, const float angles_deg[MIC_COUNT], float radius_m);

/* Normalised cross-correlation of two equal-length frames.
 *
 * Returns the lag, IN SAMPLES, that best aligns them, with parabolic
 * sub-sample interpolation. A positive return means `b` lags `a` (b is a
 * delayed copy), so t_b - t_a = +lag/fs.
 * `peak_out` receives the normalised peak height (0..1), a quality measure.
 * Searches |lag| <= max_lag. */
float tdoa_xcorr(const float *a, const float *b, int n, int max_lag, float *peak_out);

/* Solve for bearing.
 *
 * lag_s[p] is EXACTLY what tdoa_xcorr() returned for pair p, divided by the
 * per-channel sample rate - i.e. the lag by which mic j's stream trails mic
 * i's. Feeding the raw measurement straight in keeps the sign convention in
 * one place instead of scattered across call sites.
 *
 * sample_offset_s[m] is when mic m is converted within one round-robin scan
 * (e.g. {0, 12e-6, 24e-6}); pass NULL if sampling is simultaneous.
 * peak[] may be NULL. */
tdoa_result_t tdoa_solve(const tdoa_array_t *a, const float lag_s[TDOA_PAIRS],
                         const float sample_offset_s[MIC_COUNT],
                         const float peak[TDOA_PAIRS]);

#ifdef __cplusplus
}
#endif
#endif /* TDOA_H */
