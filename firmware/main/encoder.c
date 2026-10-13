/* encoder - AS5600 magnetic angle sensor on I2C0.
 *
 * NOTE: with the base now a BLDC run by the BGC board, the AS5600 that closes
 * that loop lives on the BGC board's I2C bus, not this one. This driver stays
 * for a second encoder on the ESP32's own bus if one is fitted - and it is
 * also why moving the base encoder to the BGC board sidesteps the I2C fault
 * diagnosed on GPIO8/9.
 */
#include "encoder.h"

#include <stdio.h>
#include <stddef.h>

#include "board.h"
#include "driver/i2c_master.h"
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

static const char *TAG = "encoder";
static i2c_master_dev_handle_t s_dev;
static i2c_master_bus_handle_t s_bus;
static SemaphoreHandle_t s_lock;
static uint8_t s_last[3];

/* STATUS(0x0B), RAW_ANGLE_H(0x0C), RAW_ANGLE_L(0x0D) are consecutive, so all
 * three come back in ONE transaction. Reading angle and status separately was
 * not just slower - two tasks (telemetry and the self-test) call this
 * concurrently, and the interleaved transactions returned a repeating pair of
 * bogus angles that looked like a flaky magnet. */
#define REG_STATUS    0x0B

i2c_master_bus_handle_t encoder_bus(void) { return s_bus; }

esp_err_t encoder_init(void)
{
    s_lock = xSemaphoreCreateMutex();
    ESP_RETURN_ON_FALSE(s_lock, ESP_ERR_NO_MEM, TAG, "mutex");

    i2c_master_bus_config_t bc = {
        .i2c_port = I2C_NUM_0,
        .sda_io_num = PIN_I2C_SDA,
        .scl_io_num = PIN_I2C_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    ESP_RETURN_ON_ERROR(i2c_new_master_bus(&bc, &s_bus), TAG, "bus");

    i2c_device_config_t dc = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = ADDR_AS5600,
        .scl_speed_hz = I2C_FREQ_HZ,
    };
    ESP_RETURN_ON_ERROR(i2c_master_bus_add_device(s_bus, &dc, &s_dev), TAG, "dev");

    uint8_t st = 0;
    if (encoder_read(NULL, NULL, &st)) {
        /* MD=bit5 magnet detected, ML=bit4 too weak, MH=bit3 too strong */
        ESP_LOGI(TAG, "AS5600 status 0x%02x%s", st,
                 (st & 0x20) ? " (magnet ok)" : " (NO MAGNET DETECTED)");
    } else {
        ESP_LOGW(TAG, "AS5600 not responding at 0x%02x (SDA%d/SCL%d)",
                 ADDR_AS5600, PIN_I2C_SDA, PIN_I2C_SCL);
        return ESP_ERR_NOT_FOUND;
    }
    return ESP_OK;
}

bool encoder_read(uint16_t *raw, float *deg, uint8_t *status)
{
    uint8_t reg = REG_STATUS, b[3] = {0};

    if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) != pdTRUE) {
        return false;
    }
    const esp_err_t err = i2c_master_transmit_receive(s_dev, &reg, 1, b, 3, 50);
    xSemaphoreGive(s_lock);
    if (err != ESP_OK) {
        return false;
    }

    s_last[0] = b[0]; s_last[1] = b[1]; s_last[2] = b[2];

    /* b[0]=STATUS, b[1]=RAW_ANGLE high nibble, b[2]=RAW_ANGLE low byte */
    const uint16_t v = (uint16_t)(((b[1] & 0x0F) << 8) | b[2]);   /* 12-bit */
    if (raw) *raw = v;
    if (deg) *deg = v * (360.0f / 4096.0f);
    if (status) *status = b[0];
    return true;
}

void encoder_last_bytes(uint8_t out[3])
{
    out[0] = s_last[0]; out[1] = s_last[1]; out[2] = s_last[2];
}

bool encoder_reg(uint8_t reg, uint8_t *buf, size_t n)
{
    if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) != pdTRUE) {
        return false;
    }
    const esp_err_t err = i2c_master_transmit_receive(s_dev, &reg, 1, buf, n, 50);
    xSemaphoreGive(s_lock);
    return err == ESP_OK;
}

void encoder_scan(void)
{
    printf("  I2C scan on SDA%d/SCL%d @ %d Hz:\n   ", PIN_I2C_SDA, PIN_I2C_SCL,
           I2C_FREQ_HZ);
    int found = 0;
    for (uint8_t a = 0x08; a < 0x78; a++) {
        if (i2c_master_probe(s_bus, a, 50) == ESP_OK) {
            printf(" 0x%02x", a);
            found++;
        }
    }
    if (found > 4) {
        printf("\n   -> %d addresses ACKed. A healthy bus has one or two; this\n"
               "      many means SDA/SCL are floating or shorted, not that you\n"
               "      have %d devices. Fit 4.7k pull-ups to 3V3 and check for a\n"
               "      short to GND on GPIO%d/GPIO%d.\n", found, found,
               PIN_I2C_SDA, PIN_I2C_SCL);
    } else if (!found) {
        printf(" (nothing responded)\n"
               "   -> no device is ACKing. Check 3V3, GND, and that SDA/SCL are\n"
               "      really on GPIO%d/GPIO%d and not swapped with DIR or OUT.\n",
               PIN_I2C_SDA, PIN_I2C_SCL);
    } else {
        printf("   (%d device%s)\n", found, found == 1 ? "" : "s");
    }
}

bool encoder_status_sane(uint8_t status)
{
    /* Reserved bits 0-2 and 6-7 read as 0 on a real part; ML and MH are
     * mutually exclusive. Anything else came off a floating bus. */
    if (status & 0xC7) return false;
    if ((status & 0x10) && (status & 0x08)) return false;
    return true;
}
