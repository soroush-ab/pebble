/*
 * Pebble MCU - a thin I/O bridge between the robot's hardware and a PC.
 *
 * The ESP32 does no signal processing. It streams sensors up and applies
 * actuator setpoints coming down; bearing, TDOA, speech and personality all
 * run on the PC. See proto.h for the wire format and board.h for the pin map.
 *
 * Two things stay on the MCU because they cannot survive network jitter:
 * step-pulse generation (motion.c) and the sample timebase (mic_adc.c).
 *
 * Bring-up: every subsystem can be disabled in menuconfig, and a subsystem
 * that fails to initialise is logged and skipped rather than aborting the
 * boot - one unwired device should never stop the others from being testable.
 * Press '?' on the console at any time for per-peripheral tests.
 */
#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "driver/usb_serial_jtag.h"
#include "driver/usb_serial_jtag_vfs.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "board.h"
#include "proto.h"
#include "selftest.h"

#if CONFIG_PEBBLE_EN_NET
#include "net_link.h"
#endif
#if CONFIG_PEBBLE_EN_MIC_ADC
#include "mic_adc.h"
#endif
#if CONFIG_PEBBLE_EN_MIC_I2S
#include "mic_i2s.h"
#endif
#if CONFIG_PEBBLE_EN_DISP
#include "disp.h"
#include "frames.h"
#endif
#if CONFIG_PEBBLE_EN_RFID
#include "rfid.h"
#endif
#if CONFIG_PEBBLE_EN_ENCODER
#include "encoder.h"
#endif
#if CONFIG_PEBBLE_EN_MOTION
#include "motion.h"
#endif

static const char *TAG = "pebble";

/* Subsystem presence, decided at boot and reported in HELLO so the PC knows
 * what it is actually talking to. */
static struct {
    bool net, mic_adc, mic_i2s, disp, rfid, encoder, motion;
} s_have;

/* Runtime stream gates, set by MSG_CONFIG. Default on so a bare connection
 * still shows signs of life. */
static bool s_stream_encoder = true, s_stream_rfid = true;

#define TRY(flag, label, expr)                                               \
    do {                                                                     \
        esp_err_t _e = (expr);                                               \
        (flag) = (_e == ESP_OK);                                             \
        ESP_LOGI(TAG, "%-10s %s", (label),                                   \
                 (_e == ESP_OK) ? "ok" : esp_err_to_name(_e));               \
    } while (0)

/* ------------------------------------------------------- inbound messages */

#if CONFIG_PEBBLE_EN_NET
static void on_msg(uint8_t type, const uint8_t *p, uint32_t len)
{
    switch (type) {
    case MSG_PING:
        net_send(MSG_PONG, NULL, 0, p, len);
        break;

#if CONFIG_PEBBLE_EN_DISP
    case MSG_DISP_BLIT: {
        if (len < sizeof(disp_blit_t)) break;
        const disp_blit_t *b = (const disp_blit_t *)p;
        const uint32_t need = (uint32_t)b->w * b->h * 2;
        if (len < sizeof(*b) + need) {
            ESP_LOGW(TAG, "short blit: %" PRIu32 " < %u", len,
                     (unsigned)(sizeof(*b) + need));
            break;
        }
        disp_blit(b->which, b->x, b->y, b->w, b->h,
                  (const uint16_t *)(p + sizeof(*b)));
        break;
    }
    case MSG_DISP_FILL: {
        if (len < sizeof(disp_fill_t)) break;
        const disp_fill_t *f = (const disp_fill_t *)p;
        disp_fill(f->which, f->x, f->y, f->w, f->h, f->color);
        break;
    }
    case MSG_FRAME_STORE:
        if (s_have.disp) frames_store(p, len);
        break;
    case MSG_FRAME_SHOW: {
        if (len < sizeof(frame_pair_t) || !s_have.disp) break;
        frame_pair_t pr;
        memcpy(&pr, p, sizeof(pr));
        frames_show(pr.left, pr.right);
        break;
    }
    case MSG_FRAME_ANIM: {
        if (len < sizeof(frame_anim_t) || !s_have.disp) break;
        frame_anim_t a;
        memcpy(&a, p, sizeof(a));
        if (len < sizeof(a) + (uint32_t)a.n * sizeof(frame_pair_t)) break;
        frame_pair_t steps[FRAME_ANIM_MAX];
        const uint8_t n = a.n > FRAME_ANIM_MAX ? FRAME_ANIM_MAX : a.n;
        memcpy(steps, p + sizeof(a), n * sizeof(frame_pair_t));
        frames_anim(steps, n, a.frame_ms);
        break;
    }
    case MSG_FRAME_CLEAR:
        if (s_have.disp) frames_clear();
        break;
    case MSG_FRAME_QUERY: {
        frame_info_t fi = {0};
        if (s_have.disp) frames_info(&fi);
        net_send(MSG_FRAME_INFO, NULL, 0, &fi, sizeof(fi));
        break;
    }
#endif

#if CONFIG_PEBBLE_EN_MOTION
    case MSG_MOTION_SET: {
        if (len < sizeof(motion_set_t)) break;
        const motion_set_t *m = (const motion_set_t *)p;
        motion_set(m->base_deg, m->servo_a_deg, m->servo_b_deg, m->max_vel_dps);
        break;
    }
    case MSG_MOTION_EN: {
        if (len < sizeof(motion_en_t)) break;
        const motion_en_t *m = (const motion_en_t *)p;
        motion_enable(m->stepper_en, m->servo_en);
        break;
    }
#endif

    case MSG_CONFIG: {
        if (len < sizeof(config_t)) break;
        const config_t *c = (const config_t *)p;
#if CONFIG_PEBBLE_EN_MIC_ADC
        if (s_have.mic_adc) mic_adc_enable(c->stream_analog != 0);
#endif
#if CONFIG_PEBBLE_EN_MIC_I2S
        if (s_have.mic_i2s) mic_i2s_enable(c->stream_digital != 0);
#endif
        s_stream_encoder = c->stream_encoder != 0;
        s_stream_rfid = c->stream_rfid != 0;
        ESP_LOGI(TAG, "config: analog=%u digital=%u encoder=%u rfid=%u",
                 c->stream_analog, c->stream_digital,
                 c->stream_encoder, c->stream_rfid);
        break;
    }

    default:
        ESP_LOGW(TAG, "unknown msg 0x%02x (%" PRIu32 " B)", type, len);
        break;
    }
}

static void send_hello(void)
{
    hello_t h = {0};
    h.version = PROTO_VERSION;
    h.n_analog_mics = s_have.mic_adc ? 3 : 0;
#if CONFIG_PEBBLE_EN_MIC_ADC
    if (s_have.mic_adc) {
        h.analog_fs_per_ch = (uint16_t)mic_adc_fs_per_ch();
        h.analog_fs_total = mic_adc_fs_per_ch() * 3;
        memcpy(h.analog_skew_ns, mic_adc_skew_ns(), sizeof(int32_t) * 3);
    }
#endif
    h.digital_fs = s_have.mic_i2s ? 16000 : 0;
    h.digital_bits = 16;
    h.n_displays = s_have.disp ? 2 : 0;
    h.disp_w = DISP_WIDTH;
    h.disp_h = DISP_HEIGHT;
    h.mic_angle_deg[0] = MIC_ANGLE_A_DEG;
    h.mic_angle_deg[1] = MIC_ANGLE_B_DEG;
    h.mic_angle_deg[2] = MIC_ANGLE_C_DEG;
    h.mic_radius_m = MIC_RADIUS_M;
    h.boot_t_us = (uint64_t)esp_timer_get_time();
    snprintf(h.fw, sizeof(h.fw), "pebble-bridge 2 frames");
    net_send(MSG_HELLO, NULL, 0, &h, sizeof(h));
}
#endif /* CONFIG_PEBBLE_EN_NET */

/* ------------------------------------------------------------- telemetry */

static void telem_task(void *arg)
{
    (void)arg;
    bool was_up = false;
    int tick = 0;
    uint32_t hello_fs = 0;      /* rate quoted in the HELLO we last sent */

    for (;;) {
#if CONFIG_PEBBLE_EN_NET
        const bool up = net_link_up();
        if (up && !was_up) {
            send_hello();
            hello_fs = 0;
#if CONFIG_PEBBLE_EN_MIC_ADC
            if (s_have.mic_adc) hello_fs = mic_adc_fs_per_ch();
#endif
            ESP_LOGI(TAG, "link up, hello sent");
        }
        was_up = up;

#if CONFIG_PEBBLE_EN_MIC_ADC
        /* The first HELLO goes out before the ADC rate has been measured, so
         * it quotes the nominal rate - and the nominal rate is wrong by 2x.
         * Re-send once the measurement lands so the PC never keeps a stale
         * sample rate or skew. */
        if (up && s_have.mic_adc) {
            const uint32_t fs = mic_adc_fs_per_ch();
            /* Only on a MATERIAL change. The measured rate wobbles a few
             * percent with load, and re-announcing on every wobble would
             * spam the PC with corrections that carry no new information. */
            if (hello_fs && fs > 100) {
                const uint32_t d = (fs > hello_fs) ? fs - hello_fs : hello_fs - fs;
                if ((float)d / (float)hello_fs > 0.05f) {
                    send_hello();
                    hello_fs = fs;
                    ESP_LOGI(TAG, "hello re-sent: measured %" PRIu32 " Hz/ch", fs);
                }
            }
        }
#endif

        if (up) {
            const uint64_t now = (uint64_t)esp_timer_get_time();
#if CONFIG_PEBBLE_EN_ENCODER
            if (s_have.encoder && s_stream_encoder && (tick % 2) == 0) {
                /* locals first: &packed_member is unaligned-unsafe */
                uint16_t raw = 0; float deg = 0; uint8_t st = 0;
                if (encoder_read(&raw, &deg, &st)) {
                    encoder_t e = {.t_us = now, .raw = raw, .deg = deg, .status = st};
                    net_send(MSG_ENCODER, NULL, 0, &e, sizeof(e));
                }
            }
#endif
#if CONFIG_PEBBLE_EN_RFID
            if (s_have.rfid && s_stream_rfid && (tick % 5) == 0) {
                uint8_t uid[10] = {0}, ulen = 0;
                const bool got = rfid_poll(uid, &ulen);
                rfid_t r = {.t_us = now, .present = got ? 1u : 0u, .uid_len = ulen};
                memcpy(r.uid, uid, sizeof(r.uid));
                net_send(MSG_RFID, NULL, 0, &r, sizeof(r));
            }
#endif
#if CONFIG_PEBBLE_EN_MOTION
            if (s_have.motion && (tick % 2) == 0) {
                float base = 0, sa = 0, sb = 0;
                bool moving = false;
                motion_get(&base, &sa, &sb, &moving);
                motion_state_t m = {.t_us = now, .base_deg = base,
                                    .servo_a_deg = sa, .servo_b_deg = sb,
                                    .moving = moving, .enabled = 1};
                net_send(MSG_MOTION, NULL, 0, &m, sizeof(m));
            }
#endif
        }
#else
        (void)was_up;
#endif
        /* '?' drops into the per-peripheral tests from anywhere */
        char c = 0;
        if (usb_serial_jtag_read_bytes(&c, 1, 0) == 1) {
            if (c == '?') {
                selftest_menu();
            } else {
                selftest_key(c);
            }
        }
        tick++;
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

/* ------------------------------------------------------------------ main */

void app_main(void)
{
    usb_serial_jtag_driver_config_t uc = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    if (usb_serial_jtag_driver_install(&uc) == ESP_OK) {
        usb_serial_jtag_vfs_use_driver();
        setvbuf(stdin, NULL, _IONBF, 0);
    }

    printf("\n=== Pebble MCU bridge ===\n"
           "ESP32-S3 thin I/O bridge; all DSP runs on the PC.\n");

    /* Nothing below aborts on failure: a peripheral that is not wired yet
     * should still leave every other peripheral testable. */
#if CONFIG_PEBBLE_EN_MOTION
    TRY(s_have.motion, "motion", motion_init());
#endif
#if CONFIG_PEBBLE_EN_ENCODER
    TRY(s_have.encoder, "encoder", encoder_init());
#endif
#if CONFIG_PEBBLE_EN_DISP
    TRY(s_have.disp, "displays", disp_init());
    if (s_have.disp) TRY(s_have.disp, "eye frames", frames_init());
#endif
#if CONFIG_PEBBLE_EN_RFID
    TRY(s_have.rfid, "rfid", rfid_init());
#endif
#if CONFIG_PEBBLE_EN_MIC_ADC
    TRY(s_have.mic_adc, "mic array", mic_adc_init());
#endif
#if CONFIG_PEBBLE_EN_MIC_I2S
    TRY(s_have.mic_i2s, "mic i2s", mic_i2s_init());
#endif
#if CONFIG_PEBBLE_EN_NET
    TRY(s_have.net, "wifi link", net_link_init(on_msg));
#endif

#if CONFIG_PEBBLE_EN_MIC_ADC
    if (s_have.mic_adc) mic_adc_start();
#endif
#if CONFIG_PEBBLE_EN_MIC_I2S
    if (s_have.mic_i2s) mic_i2s_start();
#endif

    printf("\nsubsystems: net:%d mic_adc:%d mic_i2s:%d disp:%d rfid:%d enc:%d motion:%d\n",
           s_have.net, s_have.mic_adc, s_have.mic_i2s, s_have.disp,
           s_have.rfid, s_have.encoder, s_have.motion);
    printf("press '?' for per-peripheral self-tests\n\n");

#if CONFIG_PEBBLE_START_SELFTEST
    selftest_menu();
#endif

    xTaskCreate(telem_task, "telem", 4096, NULL, 4, NULL);
}
