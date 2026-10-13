/*
 * board.h - the single source of truth for Pebble's ESP32-S3 pin map.
 *
 * Target: ESP32-S3-N16R8 (16 MB flash, 8 MB OCTAL PSRAM).
 *
 * ---------------------------------------------------------------------------
 * PINS YOU CANNOT USE ON THIS MODULE
 *
 *   GPIO 26-32   SPI flash                     always reserved
 *   GPIO 33-37   OCTAL PSRAM                   reserved on -R8 parts. On a
 *                                              quad-PSRAM -R2 these are free,
 *                                              which is why schematics copied
 *                                              from other boards break here.
 *   GPIO 19, 20  native USB D-/D+              console + flashing
 *   GPIO 43, 44  UART0                         secondary console
 *   GPIO 0       BOOT strapping
 *   GPIO 3,45,46 strapping                     avoid driving at reset
 *
 * That leaves 1,2,4-18,21,38-42,47,48. Every assignment below is inside it.
 *
 * ---------------------------------------------------------------------------
 * BUS LAYOUT
 *
 * Two SPI buses on purpose. The displays want to run fast and in bursts; the
 * RFID reader is a slow device that holds the bus for milliseconds at a time.
 * Sharing one bus would let a tag poll stall a frame blit, so they are split:
 *
 *   SPI2  both GC9A01 displays   (shared bus, one CS each, no MISO needed)
 *   SPI3  RFID reader            (needs MISO)
 *   I2C0  magnetic encoder (+ an I2C mic, if that is what you actually have)
 *   I2S0  digital microphone
 *   ADC1  three analog microphones  (ADC2 is unusable while Wi-Fi is on)
 */

#ifndef PEBBLE_BOARD_H_
#define PEBBLE_BOARD_H_

/* ---- analog microphone array (ADC1) ----------------------------------- */
/* GPIO4/5/6 = ADC1_CH3/4/5. Derived at runtime, not hard-coded, in mic_adc.c */
#define PIN_MIC_A            4
#define PIN_MIC_B            5
#define PIN_MIC_C            6

/* Array geometry, must match config/robot.yaml mic_array.mount_angles_deg */
#define MIC_ANGLE_A_DEG      90.0f
#define MIC_ANGLE_B_DEG      210.0f
#define MIC_ANGLE_C_DEG      330.0f
#define MIC_RADIUS_M         0.058f

/* ---- digital microphone (I2S0) ---------------------------------------- */
/* NOTE: "I2C microphone" is almost always an I2S part (INMP441, SPH0645,
 * ICS-43434). If yours is genuinely I2C, drop these and add it to the I2C bus
 * below - mic_i2s.c is the only file that changes. */
#define PIN_I2S_BCLK         15
#define PIN_I2S_WS           16
#define PIN_I2S_DIN          17

/* ---- I2C0: magnetic encoder, and anything else slow ------------------- */
#define PIN_I2C_SDA          8
#define PIN_I2C_SCL          9
/* 100 kHz, not 400 kHz. The ESP32's internal pull-ups are ~45 kohm; I2C wants
 * 2.2-4.7 kohm. At 400 kHz the rise time is too slow and reads come back
 * corrupted in a way that looks like plausible-but-wrong angles. If you fit
 * proper external pull-ups you can raise this again. */
#define I2C_FREQ_HZ          100000
#define ADDR_AS5600          0x36      /* fixed address, not configurable */

/* ---- SPI2: two round displays ----------------------------------------- */
#define PIN_DISP_SCLK        12
#define PIN_DISP_MOSI        11
#define PIN_DISP_DC          18        /* shared */
#define PIN_DISP_RST         21        /* shared */
#define PIN_DISP_CS_L        10
#define PIN_DISP_CS_R        14
#define PIN_DISP_BL          (-1)      /* backlight: tie high, or set a pin */
#define DISP_WIDTH               240
#define DISP_HEIGHT               240
#define DISP_HZ              60000000  /* 60 MHz; drop to 40 if wiring is long */

/* ---- SPI3: RFID reader ------------------------------------------------ */
#define PIN_RFID_SCLK        39
#define PIN_RFID_MOSI        40
#define PIN_RFID_MISO        41
#define PIN_RFID_CS          42
#define PIN_RFID_RST         47
#define RFID_HZ              5000000

/* ---- base: BLDC gimbal motor via the BGC controller ------------------- */
/*
 * The base is a direct-drive AX-2206 gimbal motor run by the BGC v3 board
 * (ATmega328P + SimpleFOC, see gimbal/pebble_gimbal.ino). We talk to it over
 * a UART instead of generating step pulses.
 *
 * Chosen over a stepper mainly for NOISE: this robot's whole purpose is
 * listening to someone speak in a quiet room, and a stepper clicks at the
 * step rate and turns the chassis into a speaker. Sinusoidal FOC is silent.
 * It also needs no homing (the AS5600 is absolute) and cannot lose steps.
 *
 * LEVEL SHIFTING: the ATmega runs at 5 V and ESP32 pins are NOT 5 V tolerant.
 * Its TX must come through a divider (e.g. 10k/20k -> 3.3 V) before reaching
 * PIN_GIMBAL_RX. The other direction is fine: 3.3 V clears the ATmega's 3.0 V
 * input threshold. Check the ATmega's VCC first - some boards run at 3.3 V,
 * in which case connect directly.
 */
#define PIN_GIMBAL_TX        1         /* ESP32 TX -> ATmega RX (pin 0)      */
#define PIN_GIMBAL_RX        2         /* ATmega TX (pin 1) -> ESP32, DIVIDED */
#define GIMBAL_BAUD          57600
#define BASE_MIN_DEG         (-170.0f) /* software limit: wire twist         */
#define BASE_MAX_DEG         (170.0f)
#define BASE_MAX_VEL_DPS     90.0f

/* ---- servos (LEDC) ----------------------------------------------------- */
#define PIN_SERVO_A          7
#define PIN_SERVO_B          38
#define SERVO_MIN_US         500
#define SERVO_MAX_US         2500
#define SERVO_HZ             50

/* spare: GPIO 3 (strapping, input only), 13, 48 */

#endif /* PEBBLE_BOARD_H_ */
