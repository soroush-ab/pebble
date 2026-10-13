/*
 * Host tests for mic_array. No ESP-IDF, no hardware.
 *
 *     ./test/run.sh
 *
 * Synthesises mic levels from the shadowing model a_i = g_i*(1 + k*cos(th-phi_i))
 * and checks the estimator recovers the bearing, measures k, survives channel
 * gain mismatch once calibrated, and that the analytic error predictor agrees
 * with Monte Carlo.
 */
#include "../mic_array.h"
#include "../tdoa.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

static int failures = 0;
static int checks = 0;

static void ck(const char *label, int ok, const char *fmt, ...)
{
    va_list ap;
    checks++;
    if (!ok) failures++;
    printf("  [%s] %-52s ", ok ? "PASS" : "FAIL", label);
    va_start(ap, fmt);
    vprintf(fmt, ap);
    va_end(ap);
    printf("\n");
}

static const float ANGLES[MIC_COUNT] = {90.0f, 210.0f, 330.0f};

/* Synthesise envelope levels in mV for a source at `bearing`, directivity k,
 * per-channel hardware gains g, noise floor, and optional relative noise. */
static void synth(float bearing, float k, float loud_mv, const float g[3],
                  const float noise[3], float rel_noise, unsigned *seed,
                  float out_mv[3])
{
    for (int i = 0; i < MIC_COUNT; i++) {
        float th = (bearing - ANGLES[i]) * (float)(M_PI / 180.0);
        float a = loud_mv * (1.0f + k * cosf(th));
        if (rel_noise > 0.0f) {
            /* Sum of 4 uniforms, scaled to UNIT variance. Var(sum of 4
             * U(-0.5,0.5)) = 4/12 = 1/3, so multiply by sqrt(3). Getting this
             * scale wrong silently changes the injected noise and makes the
             * error predictor look miscalibrated. */
            float n = 0.0f;
            for (int j = 0; j < 4; j++) n += (float)rand_r(seed) / RAND_MAX - 0.5f;
            a *= 1.0f + rel_noise * n * 1.73205081f;
        }
        out_mv[i] = a * g[i] + noise[i];
    }
}

static float angdiff(float a, float b) { return fabsf(mic_array_wrap180(a - b)); }

int main(void)
{
    const float unity[3] = {1.0f, 1.0f, 1.0f};
    const float nonoise[3] = {0.0f, 0.0f, 0.0f};
    float mv[3];
    unsigned seed = 12345;

    printf("mic_array host tests\n\n1. wrap180\n");
    ck("wrap180(190)", fabsf(mic_array_wrap180(190.0f) + 170.0f) < 1e-3f, "%.1f", mic_array_wrap180(190.0f));
    ck("wrap180(-200)", fabsf(mic_array_wrap180(-200.0f) - 160.0f) < 1e-3f, "%.1f", mic_array_wrap180(-200.0f));
    ck("wrap180(180)", fabsf(mic_array_wrap180(180.0f) - 180.0f) < 1e-3f, "%.1f", mic_array_wrap180(180.0f));

    printf("\n2. bearing is exact with clean levels (all k)\n");
    for (int ki = 0; ki < 3; ki++) {
        float k = 0.1f + 0.15f * ki;
        mic_array_t m; mic_array_init(&m, ANGLES);
        float worst = 0.0f;
        for (int d = 0; d < 360; d += 3) {
            synth((float)d, k, 300.0f, unity, nonoise, 0.0f, &seed, mv);
            mic_bearing_t r = mic_array_bearing(&m, mv);
            float e = angdiff(r.bearing_deg, mic_array_wrap180((float)d));
            if (e > worst) worst = e;
        }
        ck("max error over full circle", worst < 0.05f, "k=%.2f -> %.4f deg", k, worst);
    }

    printf("\n3. directivity k is recovered\n");
    for (int ki = 0; ki < 3; ki++) {
        float k = 0.1f + 0.15f * ki;
        mic_array_t m; mic_array_init(&m, ANGLES);
        synth(37.0f, k, 300.0f, unity, nonoise, 0.0f, &seed, mv);
        mic_bearing_t r = mic_array_bearing(&m, mv);
        ck("measured directivity matches model", fabsf(r.directivity - k) < 0.005f,
           "set k=%.2f -> measured %.4f", k, r.directivity);
    }

    printf("\n4. bearing is independent of loudness\n");
    {
        mic_array_t m; mic_array_init(&m, ANGLES);
        float b[3];
        float loud[3] = {60.0f, 300.0f, 1200.0f};
        for (int i = 0; i < 3; i++) {
            synth(-115.0f, 0.2f, loud[i], unity, nonoise, 0.0f, &seed, mv);
            b[i] = mic_array_bearing(&m, mv).bearing_deg;
        }
        ck("same bearing across 20x level range",
           angdiff(b[0], b[2]) < 0.05f, "%.2f / %.2f / %.2f deg", b[0], b[1], b[2]);
    }

    printf("\n5. uncalibrated gain mismatch wrecks it; calibration fixes it\n");
    {
        /* +/-2.5 dB spread, i.e. a normal electret batch */
        const float g[3] = {1.33f, 0.79f, 1.00f};
        mic_array_t raw, cal;
        mic_array_init(&raw, ANGLES);
        mic_array_init(&cal, ANGLES);

        /* calibrate from an equidistant source: equal acoustic level, so only
         * the hardware gains differ */
        float flat[3];
        for (int i = 0; i < 3; i++) flat[i] = 400.0f * g[i];
        bool okc = mic_array_calibrate(&cal, flat);
        ck("calibration accepted", okc, "gains %.3f %.3f %.3f",
           cal.gain[0], cal.gain[1], cal.gain[2]);

        float worst_raw = 0.0f, worst_cal = 0.0f;
        for (int d = 0; d < 360; d += 5) {
            synth((float)d, 0.2f, 300.0f, g, nonoise, 0.0f, &seed, mv);
            float er = angdiff(mic_array_bearing(&raw, mv).bearing_deg, mic_array_wrap180((float)d));
            float ec = angdiff(mic_array_bearing(&cal, mv).bearing_deg, mic_array_wrap180((float)d));
            if (er > worst_raw) worst_raw = er;
            if (ec > worst_cal) worst_cal = ec;
        }
        ck("uncalibrated error is large", worst_raw > 30.0f, "%.1f deg worst", worst_raw);
        ck("calibrated error is small", worst_cal < 1.0f, "%.2f deg worst", worst_cal);
    }

    printf("\n6. noise floor is subtracted\n");
    {
        const float floor_mv[3] = {120.0f, 95.0f, 140.0f};
        mic_array_t m; mic_array_init(&m, ANGLES);
        mic_array_set_noise(&m, floor_mv);
        float worst = 0.0f;
        for (int d = 0; d < 360; d += 5) {
            synth((float)d, 0.25f, 300.0f, unity, floor_mv, 0.0f, &seed, mv);
            float e = angdiff(mic_array_bearing(&m, mv).bearing_deg, mic_array_wrap180((float)d));
            if (e > worst) worst = e;
        }
        ck("bearing correct with offsets removed", worst < 1.0f, "%.3f deg worst", worst);

        mic_array_t bad; mic_array_init(&bad, ANGLES);   /* floor NOT told */
        synth(45.0f, 0.25f, 300.0f, unity, floor_mv, 0.0f, &seed, mv);
        float e = angdiff(mic_array_bearing(&bad, mv).bearing_deg, 45.0f);
        ck("ignoring the floor degrades it", e > 2.0f, "%.1f deg error", e);
    }

    printf("\n7. silence is reported invalid, not as a bearing\n");
    {
        mic_array_t m; mic_array_init(&m, ANGLES);
        float quiet[3] = {1.0f, 0.5f, 0.8f};
        mic_bearing_t r = mic_array_bearing(&m, quiet);
        ck("valid == false below the level gate", !r.valid, "total %.1f mV", r.total_mv);

        mic_array_t m2; mic_array_init(&m2, ANGLES);
        synth(10.0f, 0.0f, 300.0f, unity, nonoise, 0.0f, &seed, mv);  /* k = 0 */
        mic_bearing_t r2 = mic_array_bearing(&m2, mv);
        ck("valid == false with no shadowing (k=0)", !r2.valid,
           "directivity %.4f", r2.directivity);
    }

    printf("\n8. analytic error predictor vs Monte Carlo\n");
    for (int ki = 0; ki < 3; ki++) {
        float k = 0.1f + 0.15f * ki;
        for (int ei = 0; ei < 2; ei++) {
            float eps = (ei == 0) ? 0.03f : 0.10f;
            mic_array_t m; mic_array_init(&m, ANGLES);
            m.min_directivity = 0.0f;
            int n = 20000, inside = 0;
            float pred = mic_array_expected_error_deg(k, eps);
            for (int t = 0; t < n; t++) {
                float truth = (float)(rand_r(&seed) % 3600) / 10.0f - 180.0f;
                synth(truth, k, 300.0f, unity, nonoise, eps, &seed, mv);
                if (angdiff(mic_array_bearing(&m, mv).bearing_deg, truth) <= pred) inside++;
            }
            float frac = (float)inside / n;
            /* a 1-sigma band on a roughly gaussian angle error holds ~68% */
            ck("1-sigma band contains ~68% of trials", frac > 0.60f && frac < 0.76f,
               "k=%.2f eps=%2.0f%% pred=%5.1f deg -> %.1f%% inside", k, eps * 100, pred, frac * 100);
        }
    }

    printf("\n9. tracking smooths without the angle-wrap bug\n");
    {
        mic_array_t m; mic_array_init(&m, ANGLES);
        m.min_directivity = 0.0f;
        /* true bearing sits on the +/-180 discontinuity, where averaging the
         * ANGLE instead of the vector would give ~0 deg - the classic bug */
        for (int t = 0; t < 400; t++) {
            synth(178.0f, 0.25f, 300.0f, unity, nonoise, 0.08f, &seed, mv);
            mic_array_track(&m, mv, 0.2f);
        }
        synth(178.0f, 0.25f, 300.0f, unity, nonoise, 0.0f, &seed, mv);
        mic_bearing_t r = mic_array_track(&m, mv, 0.2f);
        ck("tracked bearing stable across the wrap", angdiff(r.bearing_deg, 178.0f) < 6.0f,
           "%.1f deg (truth 178)", r.bearing_deg);

        /* smoothing must actually reduce jitter */
        mic_array_t a, b;
        mic_array_init(&a, ANGLES); a.min_directivity = 0.0f;
        mic_array_init(&b, ANGLES); b.min_directivity = 0.0f;
        float sa = 0, sb = 0;
        for (int t = 0; t < 2000; t++) {
            synth(40.0f, 0.2f, 300.0f, unity, nonoise, 0.10f, &seed, mv);
            float ea = angdiff(mic_array_bearing(&a, mv).bearing_deg, 40.0f);
            float eb = angdiff(mic_array_track(&b, mv, 0.05f).bearing_deg, 40.0f);
            sa += ea * ea; sb += eb * eb;
        }
        ck("tracked RMS error < instantaneous", sqrtf(sb / 2000) < sqrtf(sa / 2000),
           "inst %.1f deg -> tracked %.1f deg", sqrtf(sa / 2000), sqrtf(sb / 2000));
    }

    /* ---------------------------------------------------------------- TDOA */
    printf("\n10. TDOA: geometry and the solver\n");
    {
        tdoa_array_t arr;
        tdoa_init(&arr, ANGLES, 0.058f);
        ck("max delay matches a 100 mm baseline",
           fabsf(arr.max_tau_s - 0.1004f / 343.0f) < 3e-6f,
           "%.1f us", arr.max_tau_s * 1e6f);
    }

    printf("\n11. TDOA: xcorr recovers a known fractional delay\n");
    {
        /* Band-limited broadband signal, sampled continuously so the delay can
         * be fractional without any interpolation artefacts of our own. */
        const int N = 512;
        const float fs = 27777.0f;
        float a[512], b[512];
        float worst = 0.0f;
        for (int t = 0; t < 12; t++) {
            float D = -6.0f + t * 1.1f;        /* samples */
            unsigned sd = 999 + t;
            float ph[24], fr[24];
            for (int k = 0; k < 24; k++) {
                ph[k] = (float)rand_r(&sd) / RAND_MAX * 6.2831853f;
                fr[k] = 300.0f + 9000.0f * (float)rand_r(&sd) / RAND_MAX;
            }
            for (int n = 0; n < N; n++) {
                float ta = n / fs, tb = (n - D) / fs;
                float va = 0, vb = 0;
                for (int k = 0; k < 24; k++) {
                    va += sinf(6.2831853f * fr[k] * ta + ph[k]);
                    vb += sinf(6.2831853f * fr[k] * tb + ph[k]);
                }
                a[n] = va; b[n] = vb;
            }
            float pk = 0;
            float L = tdoa_xcorr(a, b, N, 10, &pk);
            float e = fabsf(L - D);
            if (e > worst) worst = e;
        }
        ck("sub-sample lag error over +/-6 samples", worst < 0.25f,
           "worst %.3f samples = %.1f us", worst, worst / 27777.0f * 1e6f);
    }

    printf("\n12. TDOA: bearing from exact geometric delays\n");
    {
        tdoa_array_t arr;
        tdoa_init(&arr, ANGLES, 0.058f);
        float worst = 0.0f;
        for (int d = 0; d < 360; d += 7) {
            float th = (float)d * (float)(M_PI / 180.0);
            float ux = cosf(th), uy = sinf(th);
            float t[MIC_COUNT];
            for (int m = 0; m < MIC_COUNT; m++) {
                t[m] = -(arr.pos_m[m][0] * ux + arr.pos_m[m][1] * uy) / arr.c_mps;
            }
            float lag[TDOA_PAIRS];
            for (int p = 0; p < TDOA_PAIRS; p++) {
                lag[p] = t[TDOA_PAIR_J[p]] - t[TDOA_PAIR_I[p]];
            }
            tdoa_result_t r = tdoa_solve(&arr, lag, NULL, NULL);
            float e = angdiff(r.bearing_deg, mic_array_wrap180((float)d));
            if (e > worst) worst = e;
            if (!r.valid) { worst = 999.0f; break; }
        }
        ck("bearing exact over the full circle", worst < 0.05f, "%.4f deg worst", worst);
    }

    printf("\n13. TDOA: the 12 us round-robin skew must be corrected\n");
    {
        tdoa_array_t arr;
        tdoa_init(&arr, ANGLES, 0.058f);
        const float off[MIC_COUNT] = {0.0f, 12.0e-6f, 24.0e-6f};
        float worst_corr = 0.0f, worst_raw = 0.0f;
        for (int d = 0; d < 360; d += 7) {
            float th = (float)d * (float)(M_PI / 180.0);
            float ux = cosf(th), uy = sinf(th);
            float t[MIC_COUNT];
            for (int m = 0; m < MIC_COUNT; m++) {
                t[m] = -(arr.pos_m[m][0] * ux + arr.pos_m[m][1] * uy) / arr.c_mps;
            }
            float lag[TDOA_PAIRS];
            for (int p = 0; p < TDOA_PAIRS; p++) {
                const int i = TDOA_PAIR_I[p], j = TDOA_PAIR_J[p];
                lag[p] = (t[j] - off[j]) - (t[i] - off[i]);   /* what the ADC gives */
            }
            float ec = angdiff(tdoa_solve(&arr, lag, off, NULL).bearing_deg,
                               mic_array_wrap180((float)d));
            float er = angdiff(tdoa_solve(&arr, lag, NULL, NULL).bearing_deg,
                               mic_array_wrap180((float)d));
            if (ec > worst_corr) worst_corr = ec;
            if (er > worst_raw) worst_raw = er;
        }
        ck("corrected bearing is exact", worst_corr < 0.05f, "%.4f deg worst", worst_corr);
        /* Measured: ~4.7 deg worst case. Less than first assumed, because the
         * least-squares fit partially absorbs a consistent skew - but it is
         * still a third of the whole +/-15 deg budget, for free. Correct it. */
        ck("ignoring the skew costs a real bias", worst_raw > 2.0f,
           "%.1f deg worst (vs %.4f corrected)", worst_raw, worst_corr);
    }

    printf("\n14. TDOA: timing noise -> bearing error\n");
    {
        tdoa_array_t arr;
        tdoa_init(&arr, ANGLES, 0.058f);
        unsigned sd = 4242;
        printf("     jitter    68%% bearing error\n");
        for (int ji = 0; ji < 3; ji++) {
            float jit = (ji == 0) ? 4e-6f : (ji == 1) ? 11e-6f : 36e-6f;
            float errs[1500];
            for (int t = 0; t < 1500; t++) {
                float truth = (float)(rand_r(&sd) % 3600) / 10.0f - 180.0f;
                float th = truth * (float)(M_PI / 180.0);
                float ux = cosf(th), uy = sinf(th);
                float ti[MIC_COUNT];
                for (int m = 0; m < MIC_COUNT; m++) {
                    ti[m] = -(arr.pos_m[m][0] * ux + arr.pos_m[m][1] * uy) / arr.c_mps;
                }
                float lag[TDOA_PAIRS];
                for (int p = 0; p < TDOA_PAIRS; p++) {
                    float n = 0; for (int q = 0; q < 4; q++)
                        n += (float)rand_r(&sd) / RAND_MAX - 0.5f;
                    lag[p] = ti[TDOA_PAIR_J[p]] - ti[TDOA_PAIR_I[p]] + n * 1.73205081f * jit;
                }
                errs[t] = angdiff(tdoa_solve(&arr, lag, NULL, NULL).bearing_deg, truth);
            }
            /* crude 68th percentile */
            for (int x = 0; x < 1500; x++)
                for (int y = x + 1; y < 1500; y++)
                    if (errs[y] < errs[x]) { float s2 = errs[x]; errs[x] = errs[y]; errs[y] = s2; }
            float p68 = errs[(int)(1500 * 0.68f)];
            printf("     %5.1f us   %6.1f deg\n", jit * 1e6f, p68);
            if (ji == 1) {
                ck("11 us jitter (realistic interpolation) meets +/-15 deg",
                   p68 < 15.0f, "%.1f deg", p68);
            }
        }
    }

    printf("\n%d checks, %d failures\n", checks, failures);
    printf("%s\n", failures ? "SOME TESTS FAILED" : "ALL TESTS PASSED");
    return failures ? 1 : 0;
}
