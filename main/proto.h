/*
 * proto.h - Pebble MCU <-> PC wire protocol.
 *
 * The ESP32 is a thin I/O bridge: it moves bytes between hardware and the
 * network and does no signal processing. All DSP (bearing, TDOA, ASR,
 * personality) runs on the PC.
 *
 * Transport: one TCP connection, ESP32 dials OUT to the PC. Client-side so
 * the ESP recovers by itself after a reboot or a DHCP address change, and so
 * the PC never has to discover it.
 *
 * Byte order is little-endian throughout (both ends are LE; no swapping).
 *
 * ---------------------------------------------------------------------------
 * THREE THINGS THIS PROTOCOL HAS TO GET RIGHT
 *
 * 1. TIME. Wi-Fi adds tens of milliseconds of jitter, so arrival time says
 *    nothing about when a sample was taken. Every audio frame therefore
 *    carries the ESP's own monotonic microsecond clock AND a free-running
 *    sample index. The PC reconstructs the true timebase from those, never
 *    from recv() time. Without this, TDOA on the PC is impossible.
 *
 * 2. ADC CHANNEL SKEW. The S3's SAR is multiplexed, so the three analog mics
 *    are sampled 12 us apart, not simultaneously. Measured on this hardware
 *    and confirmed to ~1 us. Ignoring it costs 4.7 deg of bearing. HELLO
 *    reports the per-channel offsets so the PC can subtract them.
 *
 * 3. MOTION STAYS LOCAL. Step pulses cannot come over Wi-Fi - jitter would be
 *    audible and lost packets would lose steps. The PC sends SETPOINTS at a
 *    modest rate; the ESP interpolates and generates the step train from a
 *    hardware timer. That is still "control over Wi-Fi" in the way that
 *    matters, and it is the only version that works.
 */

#ifndef PEBBLE_PROTO_H_
#define PEBBLE_PROTO_H_

#include <stdint.h>

#define PROTO_MAGIC    0x5042u      /* 'PB' */
#define PROTO_VERSION  1

/* 12-byte header, then `len` bytes of payload. Kept small because the analog
 * mic stream sends ~160 frames/s. */
typedef struct __attribute__((packed)) {
    uint16_t magic;
    uint8_t  type;
    uint8_t  flags;
    uint32_t len;        /* payload bytes following this header */
    uint32_t seq;        /* per-direction, wraps; gaps mean dropped frames */
} proto_hdr_t;

/* ------------------------------------------------- ESP32 -> PC (uplink) */
enum {
    MSG_HELLO       = 0x01,
    MSG_LOG         = 0x02,
    MSG_PONG        = 0x03,

    MSG_MIC_ANALOG  = 0x10,   /* 3-channel ADC array                      */
    MSG_MIC_DIGITAL = 0x11,   /* I2S microphone                           */
    MSG_ENCODER     = 0x20,
    MSG_RFID        = 0x21,
    MSG_MOTION      = 0x22,
};

/* ------------------------------------------------- PC -> ESP32 (downlink) */
enum {
    MSG_PING        = 0x80,
    MSG_CONFIG      = 0x81,

    MSG_DISP_BLIT   = 0x90,   /* pixels for a rectangle                   */
    MSG_DISP_FILL   = 0x91,   /* solid rectangle, 10 bytes instead of kB  */

    MSG_MOTION_SET  = 0xA0,   /* setpoints; ESP interpolates              */
    MSG_MOTION_EN   = 0xA1,
};

/* --------------------------------------------------------------- payloads */

typedef struct __attribute__((packed)) {
    uint8_t  version;
    uint8_t  n_analog_mics;
    uint16_t analog_fs_per_ch;     /* Hz, per channel                      */
    uint32_t analog_fs_total;      /* Hz, SAR total across the scan        */
    int32_t  analog_skew_ns[4];    /* per-channel sampling offset, ns      */
    uint32_t digital_fs;           /* Hz, 0 if absent                      */
    uint8_t  digital_bits;
    uint8_t  n_displays;
    uint16_t disp_w, disp_h;
    float    mic_angle_deg[4];
    float    mic_radius_m;
    uint64_t boot_t_us;
    char     fw[24];
} hello_t;

/* Audio frames: header then int16 samples.
 * Analog is CHANNEL-INTERLEAVED in scan order: [a0,b0,c0, a1,b1,c1, ...].
 * The driver guarantees the stream starts on a scan boundary, so sample k of
 * every channel belongs to the same scan - a detail that silently added
 * +/-36 us to every delay when it was missing. */
typedef struct __attribute__((packed)) {
    uint64_t t_us;            /* ESP monotonic clock at the FIRST sample   */
    uint64_t sample_index;    /* free-running, per channel, never resets   */
    uint16_t n_frames;        /* samples per channel in this message       */
    uint8_t  n_ch;
    uint8_t  bits;            /* 16                                        */
} audio_hdr_t;

typedef struct __attribute__((packed)) {
    uint64_t t_us;
    uint16_t raw;             /* 0..4095 from the AS5600                   */
    float    deg;             /* unwrapped, calibrated                     */
    uint8_t  status;          /* AS5600 magnet status bits                 */
} encoder_t;

typedef struct __attribute__((packed)) {
    uint64_t t_us;
    uint8_t  present;
    uint8_t  uid_len;
    uint8_t  uid[10];
} rfid_t;

typedef struct __attribute__((packed)) {
    uint64_t t_us;
    float    base_deg;        /* stepper, from its own step count          */
    float    servo_a_deg;
    float    servo_b_deg;
    uint8_t  moving;
    uint8_t  enabled;
} motion_state_t;

/* Display. fmt 0 = raw RGB565 LE, fmt 1 = RLE16 (u16 count, u16 colour).
 * Budget: a full 240x240 frame is 115 kB, so two displays at 30 fps would be
 * 55 Mbit/s - more than the link. Send dirty rectangles: an 80x80 iris is
 * 12.8 kB, two eyes at 30 fps is 6 Mbit/s, which fits alongside audio. */
typedef struct __attribute__((packed)) {
    uint8_t  which;           /* 0 = left, 1 = right, 2 = both             */
    uint8_t  fmt;
    uint16_t x, y, w, h;
} disp_blit_t;

typedef struct __attribute__((packed)) {
    uint8_t  which;
    uint8_t  _pad;
    uint16_t x, y, w, h;
    uint16_t color;           /* RGB565                                    */
} disp_fill_t;

/* Setpoints, not trajectories. Send these at 20-50 Hz; the ESP interpolates
 * under its own velocity and acceleration limits. */
typedef struct __attribute__((packed)) {
    float base_deg;
    float servo_a_deg;
    float servo_b_deg;
    float max_vel_dps;        /* 0 = keep current limit                    */
} motion_set_t;

typedef struct __attribute__((packed)) {
    uint8_t stepper_en;
    uint8_t servo_en;
} motion_en_t;

typedef struct __attribute__((packed)) {
    uint8_t stream_analog;
    uint8_t stream_digital;
    uint8_t stream_encoder;
    uint8_t stream_rfid;
    uint16_t encoder_hz;
    uint16_t rfid_hz;
} config_t;

#endif /* PEBBLE_PROTO_H_ */
