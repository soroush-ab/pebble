#include "mic_array.h"

#include <math.h>
#include <string.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#define DEG2RAD (float)(M_PI / 180.0)
#define RAD2DEG (float)(180.0 / M_PI)

/* sigma_theta ~= SIGMA_K * eps / k  radians. Derived in mic_array.h; the
 * constant is 1/sqrt(3/2) for a symmetric three-mic array. */
#define SIGMA_K 0.81649658f

#define DEFAULT_MIN_LEVEL_MV     8.0f
#define DEFAULT_MIN_DIRECTIVITY  0.02f

float mic_array_wrap180(float deg)
{
    /* Folds to (-180, 180], matching wrap180() in twin/kinematics.py exactly -
     * including the endpoint, where +180 stays +180. C's fmodf keeps the sign
     * of the dividend, so the result needs normalising before the fold. */
    float x = fmodf(-deg + 180.0f, 360.0f);
    if (x < 0.0f) {
        x += 360.0f;
    }
    return -(x - 180.0f);
}

void mic_array_init(mic_array_t *m, const float angles_deg[MIC_COUNT])
{
    memset(m, 0, sizeof(*m));
    for (int i = 0; i < MIC_COUNT; i++) {
        m->angle_deg[i] = angles_deg ? angles_deg[i] : (float)(120 * i);
        m->gain[i] = 1.0f;
        m->noise_mv[i] = 0.0f;
    }
    m->min_level_mv = DEFAULT_MIN_LEVEL_MV;
    m->min_directivity = DEFAULT_MIN_DIRECTIVITY;
    m->calibrated = false;
    m->tracking = false;
}

void mic_array_set_noise(mic_array_t *m, const float noise_mv[MIC_COUNT])
{
    for (int i = 0; i < MIC_COUNT; i++) {
        m->noise_mv[i] = noise_mv[i];
    }
}

bool mic_array_calibrate(mic_array_t *m, const float level_mv[MIC_COUNT])
{
    float a[MIC_COUNT];
    float sum = 0.0f;

    for (int i = 0; i < MIC_COUNT; i++) {
        a[i] = level_mv[i] - m->noise_mv[i];
        if (a[i] < m->min_level_mv) {
            return false;               /* too quiet to equalise against */
        }
        sum += a[i];
    }

    const float mean = sum / (float)MIC_COUNT;
    for (int i = 0; i < MIC_COUNT; i++) {
        m->gain[i] = mean / a[i];
    }
    m->calibrated = true;
    return true;
}

float mic_array_expected_error_deg(float directivity, float level_noise_frac)
{
    if (directivity <= 1e-4f) {
        return 180.0f;                  /* no usable shadowing */
    }
    const float rad = SIGMA_K * level_noise_frac / directivity;
    const float deg = rad * RAD2DEG;
    return deg > 180.0f ? 180.0f : deg;
}

/* Shared core: builds the level vector and the raw sum. */
static void mic_vector(const mic_array_t *m, const float raw_mv[MIC_COUNT],
                       float *vx, float *vy, float *sum, float levels[MIC_COUNT])
{
    *vx = 0.0f;
    *vy = 0.0f;
    *sum = 0.0f;

    for (int i = 0; i < MIC_COUNT; i++) {
        float a = (raw_mv[i] - m->noise_mv[i]) * m->gain[i];
        if (a < 0.0f) {
            a = 0.0f;                   /* below the floor: contributes nothing */
        }
        levels[i] = a;
        *sum += a;

        const float th = m->angle_deg[i] * DEG2RAD;
        *vx += a * cosf(th);
        *vy += a * sinf(th);
    }
}

/* Turn a level vector into a result. */
static mic_bearing_t mic_resolve(const mic_array_t *m, float vx, float vy,
                                 float sum, const float levels[MIC_COUNT])
{
    mic_bearing_t r;
    memset(&r, 0, sizeof(r));

    for (int i = 0; i < MIC_COUNT; i++) {
        r.level_mv[i] = levels[i];
    }
    r.total_mv = sum;

    if (sum < m->min_level_mv * (float)MIC_COUNT) {
        r.valid = false;                /* silence; leave bearing at 0 */
        r.expected_err_deg = 180.0f;
        return r;
    }

    const float mag = sqrtf(vx * vx + vy * vy);
    r.directivity = 2.0f * mag / sum;
    r.bearing_deg = mic_array_wrap180(atan2f(vy, vx) * RAD2DEG);

    /* Heuristic: scale with how shadowed the array is and how far above the
     * floor the signal sits. 0.3 is a well-shadowed array; 6x the minimum
     * level is a comfortably loud talker. */
    float c_dir = r.directivity / 0.3f;
    float c_lvl = sum / (m->min_level_mv * 6.0f);
    if (c_dir > 1.0f) c_dir = 1.0f;
    if (c_lvl > 1.0f) c_lvl = 1.0f;
    r.confidence = c_dir * c_lvl;

    /* 3% is a realistic per-window level noise for an RC envelope sampled over
     * tens of ms; measure yours with the `noise` report and adjust. */
    r.expected_err_deg = mic_array_expected_error_deg(r.directivity, 0.03f);

    r.valid = (r.directivity >= m->min_directivity);
    return r;
}

mic_bearing_t mic_array_bearing(const mic_array_t *m, const float raw_mv[MIC_COUNT])
{
    float vx, vy, sum, levels[MIC_COUNT];
    mic_vector(m, raw_mv, &vx, &vy, &sum, levels);
    return mic_resolve(m, vx, vy, sum, levels);
}

mic_bearing_t mic_array_track(mic_array_t *m, const float raw_mv[MIC_COUNT], float alpha)
{
    float vx, vy, sum, levels[MIC_COUNT];
    mic_vector(m, raw_mv, &vx, &vy, &sum, levels);

    if (alpha <= 0.0f) alpha = 0.01f;
    if (alpha > 1.0f)  alpha = 1.0f;

    /* Only fold in windows that actually contain sound, otherwise silence
     * drags the tracked vector toward zero and the bearing wanders. */
    const bool active = sum >= m->min_level_mv * (float)MIC_COUNT;
    if (active) {
        if (!m->tracking) {
            m->vx = vx;
            m->vy = vy;
            m->tracking = true;
        } else {
            m->vx += alpha * (vx - m->vx);
            m->vy += alpha * (vy - m->vy);
        }
    }

    if (!m->tracking) {
        return mic_resolve(m, vx, vy, sum, levels);
    }

    /* Report the smoothed direction, but keep the instantaneous magnitudes so
     * directivity and confidence still describe the current window. */
    const float smag = sqrtf(m->vx * m->vx + m->vy * m->vy);
    const float imag = sqrtf(vx * vx + vy * vy);
    float sx = m->vx, sy = m->vy;
    if (smag > 1e-9f) {
        sx = m->vx * (imag / smag);
        sy = m->vy * (imag / smag);
    }
    return mic_resolve(m, sx, sy, sum, levels);
}
