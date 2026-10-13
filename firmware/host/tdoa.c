#include "tdoa.h"

#include <math.h>
#include <string.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#define DEG2RAD (float)(M_PI / 180.0)
#define RAD2DEG (float)(180.0 / M_PI)

const int TDOA_PAIR_I[TDOA_PAIRS] = {0, 0, 1};
const int TDOA_PAIR_J[TDOA_PAIRS] = {1, 2, 2};

void tdoa_init(tdoa_array_t *a, const float angles_deg[MIC_COUNT], float radius_m)
{
    memset(a, 0, sizeof(*a));
    a->c_mps = 343.0f;
    for (int i = 0; i < MIC_COUNT; i++) {
        const float th = angles_deg[i] * DEG2RAD;
        a->pos_m[i][0] = radius_m * cosf(th);
        a->pos_m[i][1] = radius_m * sinf(th);
    }

    /* Largest baseline sets the largest physically possible delay. */
    float dmax = 0.0f;
    for (int p = 0; p < TDOA_PAIRS; p++) {
        const int i = TDOA_PAIR_I[p], j = TDOA_PAIR_J[p];
        const float dx = a->pos_m[j][0] - a->pos_m[i][0];
        const float dy = a->pos_m[j][1] - a->pos_m[i][1];
        const float d = sqrtf(dx * dx + dy * dy);
        if (d > dmax) dmax = d;
    }
    a->max_tau_s = dmax / a->c_mps;
}

float tdoa_xcorr(const float *a, const float *b, int n, int max_lag, float *peak_out)
{
    if (max_lag < 1 || n <= 2 * max_lag) {
        if (peak_out) *peak_out = 0.0f;
        return 0.0f;
    }

    /* Energy over the overlapped region, for normalisation. */
    double ea = 0.0, eb = 0.0;
    for (int k = max_lag; k < n - max_lag; k++) {
        ea += (double)a[k] * a[k];
        eb += (double)b[k] * b[k];
    }
    const double norm = sqrt(ea * eb);
    if (norm < 1e-20) {
        if (peak_out) *peak_out = 0.0f;
        return 0.0f;
    }

    int best = 0;
    double best_r = -1e30;
    double rm1 = 0.0, rp1 = 0.0;        /* neighbours of the peak */

    /* r[L] = sum a[k] * b[k+L]; maximised when b is a copy of a delayed by L */
    double prev = 0.0, cur = 0.0;
    for (int L = -max_lag; L <= max_lag; L++) {
        double r = 0.0;
        for (int k = max_lag; k < n - max_lag; k++) {
            r += (double)a[k] * b[k + L];
        }
        if (r > best_r) {
            best_r = r;
            best = L;
            rm1 = prev;                 /* r[L-1], filled in below if needed */
        }
        prev = cur;
        cur = r;
        (void)rp1;
    }

    /* Re-evaluate the two neighbours cleanly rather than tracking them in the
     * loop above, which is easy to get subtly wrong. */
    double rl[3] = {0.0, best_r, 0.0};
    for (int s = 0; s < 3; s += 2) {
        const int L = best + (s - 1);
        if (L < -max_lag || L > max_lag) {
            rl[s] = best_r - 1.0;       /* clamp: no interpolation at the edge */
            continue;
        }
        double r = 0.0;
        for (int k = max_lag; k < n - max_lag; k++) {
            r += (double)a[k] * b[k + L];
        }
        rl[s] = r;
    }
    rm1 = rl[0];
    rp1 = rl[2];

    /* Parabolic interpolation around the integer peak. */
    float frac = 0.0f;
    const double denom = (rm1 - 2.0 * best_r + rp1);
    if (fabs(denom) > 1e-20) {
        frac = (float)(0.5 * (rm1 - rp1) / denom);
        if (frac > 1.0f) frac = 1.0f;
        if (frac < -1.0f) frac = -1.0f;
    }

    if (peak_out) {
        double pk = best_r / norm;
        if (pk < 0.0) pk = 0.0;
        if (pk > 1.0) pk = 1.0;
        *peak_out = (float)pk;
    }
    return (float)best + frac;
}

tdoa_result_t tdoa_solve(const tdoa_array_t *a, const float lag_s[TDOA_PAIRS],
                         const float sample_offset_s[MIC_COUNT],
                         const float peak[TDOA_PAIRS])
{
    tdoa_result_t r;
    memset(&r, 0, sizeof(r));

    /* Normal equations for  A u = b,  A row = (p_j - p_i), b = c * tau_ij. */
    float ata[2][2] = {{0, 0}, {0, 0}};
    float atb[2] = {0, 0};
    int used = 0;

    for (int p = 0; p < TDOA_PAIRS; p++) {
        const int i = TDOA_PAIR_I[p], j = TDOA_PAIR_J[p];

        /* xcorr gives  lag = (t_j - off_j) - (t_i - off_i), so the acoustic
         * difference the geometry cares about is
         *     tau_ij = t_i - t_j = (off_i - off_j) - lag
         * Undoing the round-robin skew here is not optional: at 12 us per slot
         * it is a sixth of the whole +/-15 deg timing budget. */
        float tau = -lag_s[p];
        if (sample_offset_s) {
            tau += (sample_offset_s[i] - sample_offset_s[j]);
        }

        /* Reject delays the geometry cannot produce - they are correlation
         * peaks picked off noise or a reflection. */
        if (fabsf(tau) > a->max_tau_s * 1.25f) {
            r.tau_s[p] = tau;
            r.peak[p] = peak ? peak[p] : 0.0f;
            continue;
        }
        r.tau_s[p] = tau;
        r.peak[p] = peak ? peak[p] : 0.0f;

        const float ax = a->pos_m[j][0] - a->pos_m[i][0];
        const float ay = a->pos_m[j][1] - a->pos_m[i][1];
        const float bb = a->c_mps * tau;

        ata[0][0] += ax * ax; ata[0][1] += ax * ay;
        ata[1][0] += ay * ax; ata[1][1] += ay * ay;
        atb[0] += ax * bb;    atb[1] += ay * bb;
        used++;
    }

    if (used < 2) {
        r.valid = false;
        return r;
    }

    const float det = ata[0][0] * ata[1][1] - ata[0][1] * ata[1][0];
    if (fabsf(det) < 1e-12f) {
        r.valid = false;
        return r;
    }

    const float ux = ( ata[1][1] * atb[0] - ata[0][1] * atb[1]) / det;
    const float uy = (-ata[1][0] * atb[0] + ata[0][0] * atb[1]) / det;

    const float mag = sqrtf(ux * ux + uy * uy);
    r.consistency = mag;
    r.bearing_deg = mic_array_wrap180(atan2f(uy, ux) * RAD2DEG);

    /* |u| should be 1 for a clean far-field solution; punish both directions. */
    float cons = 1.0f - fabsf(mag - 1.0f);
    if (cons < 0.0f) cons = 0.0f;

    float pk = 1.0f;
    if (peak) {
        pk = 0.0f;
        for (int p = 0; p < TDOA_PAIRS; p++) pk += r.peak[p];
        pk /= (float)TDOA_PAIRS;
    }

    r.confidence = cons * pk;
    r.valid = (used == TDOA_PAIRS) && (mag > 0.45f) && (mag < 2.2f);
    return r;
}
