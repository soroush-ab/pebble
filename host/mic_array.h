/*
 * mic_array - bearing estimation from three sound-detector ENVELOPE levels.
 *
 * Pure C99, no ESP-IDF dependencies, so it builds and is tested on the host
 * (see test/host_test.c). Keep it that way: this is the part worth testing.
 *
 * ---------------------------------------------------------------------------
 * WHAT THIS CAN AND CANNOT DO
 *
 * It CANNOT do TDOA. The SparkFun Sound Detector's envelope output is a
 * rectified, RC-smoothed amplitude follower with a time constant of roughly
 * 5-30 ms. The inter-mic delay this array needs to resolve is at most 338 us
 * (2 x 58 mm circumradius / 343 m/s) - about 15x faster than the envelope can
 * respond. The arrival-time information is gone before it reaches the pin.
 * GCC-PHAT on these signals measures nothing. For real TDOA you need the raw
 * audio, sample-synchronous across channels (see BUILD.md 2.1: TDM mics).
 *
 * It CAN do amplitude (level-difference) bearing, with caveats:
 *
 *   - Inverse-square path differences across a 58 mm array are tiny:
 *       0.5 m -> 2.0 dB,  1 m -> 1.0 dB,  2 m -> 0.50 dB,  3 m -> 0.34 dB
 *     Electret capsules vary +/- 2-3 dB unit to unit, so the gain mismatch is
 *     5-10x larger than the cue. CALIBRATION IS MANDATORY, not optional.
 *
 *   - What actually carries the bearing is ACOUSTIC SHADOWING by the robot
 *     body. Model each mic as  a_i = 1 + k*cos(theta - phi_i), where k is the
 *     directivity contrast. k depends on your body geometry and mic mounting,
 *     so this code MEASURES k at runtime (mic_bearing_t.directivity) instead of
 *     assuming it. If k comes out near zero, the mics are not shadowed and no
 *     amount of signal processing will produce a bearing.
 *
 * ---------------------------------------------------------------------------
 * THE ESTIMATOR
 *
 * Vector sum:  V = sum_i a_i * (cos phi_i, sin phi_i),  bearing = atan2(Vy,Vx)
 *
 * For a symmetric array, sum_i (cos phi_i, sin phi_i) = 0, so substituting the
 * shadowing model gives exactly
 *
 *     V = (3/2) * k * (cos theta, sin theta)
 *
 * The omnidirectional part cancels and the bearing is recovered EXACTLY,
 * independent of k and of source loudness. k is then recovered as
 *
 *     k = 2 * |V| / sum_i a_i
 *
 * All real-world error therefore comes from level measurement, not from the
 * estimator. Propagating per-mic relative level noise eps gives
 *
 *     sigma_theta ~= 0.8165 * eps / k   radians
 *
 * which matches Monte Carlo to better than 2%. mic_array_expected_error_deg()
 * exposes this so the firmware can report its own achievable accuracy live.
 * Worked examples: k=0.2 with 3% level noise -> ~7 deg; k=0.1 with 10% -> ~47 deg.
 */

#ifndef MIC_ARRAY_H
#define MIC_ARRAY_H

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

#define MIC_COUNT 3

typedef struct {
    float angle_deg[MIC_COUNT];   /* mic mounting azimuths, robot frame        */
    float gain[MIC_COUNT];        /* calibration multipliers, 1.0 = uncalibrated */
    float noise_mv[MIC_COUNT];    /* measured per-channel noise floor          */
    bool  calibrated;

    float min_level_mv;           /* below this the bearing is meaningless     */
    float min_directivity;        /* below this the array is not shadowed      */

    /* smoothing state for mic_array_track(); the VECTOR is smoothed, never the
     * angle - averaging angles across the +/-180 wrap is simply wrong */
    float vx, vy;
    bool  tracking;
} mic_array_t;

typedef struct {
    float level_mv[MIC_COUNT];    /* calibrated level above noise, per mic     */
    float total_mv;               /* sum of the above                          */
    float bearing_deg;            /* (-180, 180], 0 = robot front, +ve = CCW   */
    float directivity;            /* measured k; the headline diagnostic       */
    float confidence;             /* 0..1 heuristic                            */
    float expected_err_deg;       /* predicted 1-sigma error at this k         */
    bool  valid;
} mic_bearing_t;

/* angles_deg: mounting azimuths, e.g. {90, 210, 330} from config/robot.yaml */
void mic_array_init(mic_array_t *m, const float angles_deg[MIC_COUNT]);

/* Record the per-channel noise floor, measured in silence. */
void mic_array_set_noise(mic_array_t *m, const float noise_mv[MIC_COUNT]);

/* Equalise channel gains from a source EQUIDISTANT to all three mics - hold a
 * steady noise source directly above the array centre. Returns false if any
 * channel is too quiet to calibrate against. */
bool mic_array_calibrate(mic_array_t *m, const float level_mv[MIC_COUNT]);

/* Stateless instantaneous estimate. */
mic_bearing_t mic_array_bearing(const mic_array_t *m, const float raw_mv[MIC_COUNT]);

/* Smoothed estimate. alpha is the per-update weight of the new sample
 * (0 < alpha <= 1); smaller is steadier and slower. */
mic_bearing_t mic_array_track(mic_array_t *m, const float raw_mv[MIC_COUNT], float alpha);

/* Predicted 1-sigma bearing error, degrees, for a measured directivity and a
 * relative level noise (e.g. 0.03 for 3%). Returns a large value as k -> 0. */
float mic_array_expected_error_deg(float directivity, float level_noise_frac);

/* Fold an angle into (-180, 180]. */
float mic_array_wrap180(float deg);

#ifdef __cplusplus
}
#endif
#endif /* MIC_ARRAY_H */
