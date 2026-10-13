/*
 * selftest - bring up one peripheral at a time.
 *
 * Deliberately needs neither Wi-Fi nor the PC: when a board is new, the first
 * question is "is this one device wired correctly", and making that depend on
 * a working network link is how bring-up turns into guesswork.
 *
 * Each test prints what a GOOD result looks like, because a number on its own
 * does not tell you whether the hardware is right.
 */
#include "selftest.h"

#include <stdbool.h>
#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include <stdio.h>

#include "board.h"
#include "driver/usb_serial_jtag.h"
#include "esp_log.h"
#include "esp_rom_sys.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "disp.h"
#include "encoder.h"
#include "mic_adc.h"
#include "mic_i2s.h"
#include "motion.h"
#include "net_link.h"
#include "rfid.h"

static void bar(float v, float full, int width)
{
    int n = (full > 0.0f) ? (int)((v / full) * width) : 0;
    if (n < 0) n = 0;
    if (n > width) n = width;
    putchar('[');
    for (int i = 0; i < width; i++) putchar(i < n ? '#' : ' ');
    putchar(']');
}

static int key_now(void)
{
    char c = 0;
    return usb_serial_jtag_read_bytes(&c, 1, 0) == 1 ? (int)c : -1;
}

static bool wait_quit(void)          /* true when the operator pressed q */
{
    const int c = key_now();
    return c == 'q' || c == 'Q';
}

/* ------------------------------------------------------------------ tests */

#if CONFIG_PEBBLE_EN_ENCODER
static void test_encoder(void)
{
    /* Register dump first: read each AS5600 register in its OWN transaction.
     * A single burst across 0x0B..0x0D assumes the register pointer
     * auto-increments from STATUS into RAW_ANGLE, which is exactly the
     * assumption to stop trusting when the angle goes constant. */
    {
        printf("\n");
        esp_log_level_set("i2c.master", ESP_LOG_NONE);   /* scan spams on open bus */
        encoder_scan();
        esp_log_level_set("i2c.master", ESP_LOG_WARN);
        printf("\n  --- AS5600 register dump (ok/fail per read) ---\n");
        for (int k = 0; k < 10; k++) {
            uint8_t st = 0, ra[2] = {0}, an[2] = {0}, agc = 0, mg[2] = {0}, cf[2] = {0};
            const bool ok_st = encoder_reg(0x0B, &st, 1);
            const bool ok_ra = encoder_reg(0x0C, ra, 2);
            const bool ok_an = encoder_reg(0x0E, an, 2);
            const bool ok_ag = encoder_reg(0x1A, &agc, 1);
            encoder_reg(0x1B, mg, 2);
            encoder_reg(0x07, cf, 2);
            const uint16_t rawv = (uint16_t)(((ra[0] & 0x0F) << 8) | ra[1]);
            const uint16_t angv = (uint16_t)(((an[0] & 0x0F) << 8) | an[1]);
            const uint16_t magv = (uint16_t)(((mg[0] & 0x0F) << 8) | mg[1]);
            printf("   [%c%c%c%c] STATUS 0x%02x | RAW_ANGLE %4u (%6.1f deg) "
                   "| ANGLE %4u | AGC %3u | MAG %4u | CONF %02x%02x\n",
                   ok_st?'s':'-', ok_ra?'r':'-', ok_an?'a':'-', ok_ag?'g':'-',
                   st, rawv, rawv * 360.0f / 4096.0f, angv, agc, magv, cf[0], cf[1]);
            vTaskDelay(pdMS_TO_TICKS(400));
        }
        printf("   AGC: mid-range is good. 0 = magnet too close, 255 = too far.\n");
        printf("   Turn the shaft NOW and watch RAW_ANGLE above.\n\n");
    }

    /* Burst next. Polling at 10 Hz aliases anything that moves: a steady
     * oscillation shows up as a dwell at two values with a slow beat, which
     * looks like a broken sensor rather than a fast one. 200 Hz for 2 s
     * settles whether the angle is really moving and how fast. */
    {
        static uint16_t burst[400];
        printf("\n  burst: 400 samples @200 Hz (2 s), hold still...\n");
        int n = 0;
        for (; n < 400; n++) {
            uint16_t r;
            if (!encoder_read(&r, NULL, NULL)) break;
            burst[n] = r;
            esp_rom_delay_us(5000);
        }
        uint16_t mn = 4095, mx = 0;
        long sum = 0;
        for (int i = 0; i < n; i++) {
            if (burst[i] < mn) mn = burst[i];
            if (burst[i] > mx) mx = burst[i];
            sum += burst[i];
        }
        printf("  n=%d  min %u  max %u  spread %u counts (%.1f deg)  mean %ld\n",
               n, mn, mx, (unsigned)(mx - mn), (mx - mn) * 360.0f / 4096.0f,
               n ? sum / n : 0);
        printf("  first 64 (every 2nd):\n   ");
        for (int i = 0; i < 64 && i < n; i += 2) {
            printf(" %4u", burst[i]);
            if ((i % 24) == 22) printf("\n   ");
        }
        printf("\n");
    }

    printf("\nENCODER (AS5600 @ 0x%02x on SDA%d/SCL%d)\n"
           "  Turn the base by hand. deg should sweep 0-360 smoothly and wrap.\n"
           "  status bit5 (0x20) = magnet detected. 0x00 means no magnet or no bus.\n"
           "  q to stop.\n\n", ADDR_AS5600, PIN_I2C_SDA, PIN_I2C_SCL);
    uint16_t raw; float deg; uint8_t st;
    while (!wait_quit()) {
        if (encoder_read(&raw, &deg, &st)) {
            uint8_t rb[3]; encoder_last_bytes(rb);
            printf("\r  bytes %02x %02x %02x | raw %4u  %7.2f deg  status 0x%02x  %-18s",
                   rb[0], rb[1], rb[2], raw, deg, st,
                   !encoder_status_sane(st) ? "BUS FAULT (impossible status)" :
                   (st & 0x20) ? "magnet ok" :
                   (st & 0x10) ? "MAGNET TOO WEAK" :
                   (st & 0x08) ? "MAGNET TOO STRONG" : "NO MAGNET");
        } else {
            printf("\r  no I2C response from 0x%02x        ", ADDR_AS5600);
        }
        fflush(stdout);
        vTaskDelay(pdMS_TO_TICKS(100));
    }
    printf("\n");
}
#endif

#if CONFIG_PEBBLE_EN_MOTION
static void test_motion(void)
{
    printf("\nMOTION\n"
           "  Base sweeps +/-30 deg, then each servo sweeps +/-30 deg.\n"
           "  The base angle is MEASURED by the AS5600 on the BGC board, so\n"
           "  commanded and reported should converge. Silence is expected -\n"
           "  a gimbal motor under FOC makes no stepping noise.\n"
           "  NOTE: servos are swept ONE AT A TIME on purpose; moving both\n"
           "  together browns out the board unless they have their own rail.\n"
           "  q to stop and disable the drivers.\n\n");
    motion_enable(true, true);

    const float seq[] = {30, -30, 0};
    for (int i = 0; i < 3 && !wait_quit(); i++) {
        printf("  base -> %+.0f deg\n", seq[i]);
        motion_set(seq[i], 0, 0, 60.0f);
        for (int t = 0; t < 60 && !wait_quit(); t++) {
            float b; bool moving;
            motion_get(&b, NULL, NULL, &moving);
#if CONFIG_PEBBLE_EN_ENCODER
            float e = 0; encoder_read(NULL, &e, NULL);
            printf("\r    commanded %+7.2f  encoder %7.2f  %s   ",
                   b, e, moving ? "moving" : "idle  ");
#else
            printf("\r    commanded %+7.2f  %s   ", b, moving ? "moving" : "idle  ");
#endif
            fflush(stdout);
            if (!moving && t > 5) break;
            vTaskDelay(pdMS_TO_TICKS(50));
        }
        printf("\n");
    }

    for (int s = 0; s < 2 && !wait_quit(); s++) {
        printf("  servo %c sweep\n", 'A' + s);
        for (int a = -90; a <= 90 && !wait_quit(); a += 10) {
            motion_set(0, s == 0 ? a : 0, s == 1 ? a : 0, 0);
            printf("\r    %+3d deg   ", a); fflush(stdout);
            vTaskDelay(pdMS_TO_TICKS(250));
        }
        motion_set(0, 0, 0, 0);
        printf("\n");
    }
    motion_enable(false, true);
    printf("  drivers disabled\n");
}
#endif

#if CONFIG_PEBBLE_EN_RFID
static void test_rfid(void)
{
    printf("\nRFID (MFRC522 on SPI3, CS%d)\n"
           "  Present a card. A good read prints the same UID every time.\n"
           "  q to stop.\n\n", PIN_RFID_CS);
    uint8_t uid[10], len, last[10] = {0}; int misses = 0;
    while (!wait_quit()) {
        if (rfid_poll(uid, &len)) {
            if (memcmp(uid, last, len) != 0) {
                printf("  UID ");
                for (int i = 0; i < len; i++) printf("%02X", uid[i]);
                printf("\n");
                memcpy(last, uid, len);
            }
            misses = 0;
        } else if (++misses == 8) {
            memset(last, 0, sizeof(last));
            printf("  (no card)\n");
        }
        vTaskDelay(pdMS_TO_TICKS(120));
    }
}
#endif

#if CONFIG_PEBBLE_EN_DISP
static void test_disp(void)
{
    printf("\nDISPLAYS (2x GC9A01 on SPI2)\n"
           "  Expect: red, green, blue, white on BOTH, then left-only red and\n"
           "  right-only blue. If both show the same colour in that last step,\n"
           "  the two CS lines are swapped or shorted.\n\n");
    const uint16_t cols[4] = {0xF800, 0x07E0, 0x001F, 0xFFFF};
    const char *names[4] = {"red", "green", "blue", "white"};
    for (int i = 0; i < 4; i++) {
        printf("  both: %s\n", names[i]);
        disp_fill(2, 0, 0, DISP_WIDTH, DISP_HEIGHT, cols[i]);
        vTaskDelay(pdMS_TO_TICKS(600));
    }
    printf("  left red / right blue\n");
    disp_fill(0, 0, 0, DISP_WIDTH, DISP_HEIGHT, 0xF800);
    disp_fill(1, 0, 0, DISP_WIDTH, DISP_HEIGHT, 0x001F);
    vTaskDelay(pdMS_TO_TICKS(1500));
    printf("  centred white square on both (checks window addressing)\n");
    disp_fill(2, 0, 0, DISP_WIDTH, DISP_HEIGHT, 0x0000);
    disp_fill(2, 80, 80, 80, 80, 0xFFFF);
    printf("  done\n");
}
#endif

#if CONFIG_PEBBLE_EN_MIC_ADC
static void test_mic_adc(void)
{
    printf("\nANALOG MIC ARRAY  GPIO%d/%d/%d -> ADC1\n"
           "  dc should sit near VCC/2 on the AUDIO pin (~2048 counts), or low\n"
           "  on the ENVELOPE pin. Make a noise: rms must rise on ALL THREE.\n"
           "  A channel with dc 0 or 4095 is not connected. q to stop.\n\n",
           PIN_MIC_A, PIN_MIC_B, PIN_MIC_C);

    mic_adc_stats_t st;
    float peak_seen[3] = {0, 0, 0};
    while (!wait_quit()) {
        mic_adc_stats(&st);
        if (st.frames == 0) {
            printf("\r  no frames yet - is the ADC task running?   ");
        } else {
            printf("\r ");
            for (int c = 0; c < 3; c++) {
                if (st.rms[c] > peak_seen[c]) peak_seen[c] = st.rms[c];
                printf(" m%d dc%6.0f rms%6.1f", c, st.dc[c], st.rms[c]);
                bar(st.rms[c], 300.0f, 10);
            }
        }
        fflush(stdout);
        vTaskDelay(pdMS_TO_TICKS(120));
    }
    printf("\n  loudest rms seen: %.1f  %.1f  %.1f\n",
           peak_seen[0], peak_seen[1], peak_seen[2]);
    float mx = 0;
    for (int c = 0; c < 3; c++) if (peak_seen[c] > mx) mx = peak_seen[c];
    for (int c = 0; c < 3; c++) {
        if (mx > 20.0f && peak_seen[c] < mx * 0.25f) {
            printf("  mic %d never responded (%.0f%% of the loudest) - check its\n"
                   "  wiring and that it shares ground with the ESP32.\n",
                   c, 100.0f * peak_seen[c] / mx);
        }
    }
}
#endif

#if CONFIG_PEBBLE_EN_MIC_I2S
static void test_mic_i2s(void)
{
    printf("\nDIGITAL MIC  BCLK%d WS%d DIN%d\n"
           "  frames must climb. If it stays 0 the I2S clock is not running or\n"
           "  DIN is not connected. Speak: rms should rise clearly. q to stop.\n\n",
           PIN_I2S_BCLK, PIN_I2S_WS, PIN_I2S_DIN);
    float dc, rms, pk; unsigned fr;
    while (!wait_quit()) {
        mic_i2s_stats(&dc, &rms, &pk, &fr);
        printf("\r  frames %6u  dc %8.1f  rms %8.1f  peak %8.1f ", fr, dc, rms, pk);
        bar(rms, 2000.0f, 18);
        fflush(stdout);
        vTaskDelay(pdMS_TO_TICKS(120));
    }
    printf("\n");
}
#endif

/* ------------------------------------------------------------------- menu */

static void print_menu(void)
{
    printf("\n=============== Pebble self-test ===============\n");
#if CONFIG_PEBBLE_EN_ENCODER
    printf("  e  encoder    live angle, magnet status\n");
#endif
#if CONFIG_PEBBLE_EN_MOTION
    printf("  m  motion     base sweep + servo sweep (one servo at a time)\n");
#endif
#if CONFIG_PEBBLE_EN_RFID
    printf("  r  rfid       read card UIDs\n");
#endif
#if CONFIG_PEBBLE_EN_DISP
    printf("  d  displays   colour and addressing check\n");
#endif
#if CONFIG_PEBBLE_EN_MIC_ADC
    printf("  a  mic array  live levels on all three analog mics\n");
#endif
#if CONFIG_PEBBLE_EN_MIC_I2S
    printf("  i  digital mic  live level on the I2S mic\n");
#endif
    printf("  n  network    link state and drop count\n");
    printf("  g  go         leave the menu, run as a bridge\n");
    printf("  ?  this menu          (q stops a running test)\n");
    printf("================================================\n> ");
    fflush(stdout);
}

bool selftest_key(int c)
{
    switch (c) {
#if CONFIG_PEBBLE_EN_ENCODER
    case 'e': test_encoder(); return true;
#endif
#if CONFIG_PEBBLE_EN_MOTION
    case 'm': test_motion(); return true;
#endif
#if CONFIG_PEBBLE_EN_RFID
    case 'r': test_rfid(); return true;
#endif
#if CONFIG_PEBBLE_EN_DISP
    case 'd': test_disp(); return true;
#endif
#if CONFIG_PEBBLE_EN_MIC_ADC
    case 'a': test_mic_adc(); return true;
#endif
#if CONFIG_PEBBLE_EN_MIC_I2S
    case 'i': test_mic_i2s(); return true;
#endif
    case 'n':
        printf("\n  link %s, %" PRIu32 " frames dropped\n",
               net_link_up() ? "UP" : "DOWN", net_dropped_frames());
        return true;
    case '?': print_menu(); return true;
    default: return false;
    }
}

void selftest_menu(void)
{
    print_menu();
    for (;;) {
        const int c = key_now();
        if (c == 'g' || c == 'G') {
            printf("\nleaving self-test; press '?' at any time to come back\n\n");
            return;
        }
        if (c >= 0) {
            if (selftest_key(c)) print_menu();
        }
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}
