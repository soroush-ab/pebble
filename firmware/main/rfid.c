/* rfid - MFRC522 on its own SPI bus (SPI3).
 *
 * Deliberately not sharing SPI2 with the displays: a tag poll holds the bus
 * for milliseconds, which would stall a frame blit and show up as a visible
 * hitch in the eyes.
 *
 * Only what the shell-swap needs: detect a card and read its UID. No auth, no
 * block reads - the UID is the shell identity.
 */
#include "rfid.h"

#include <string.h>

#include "board.h"
#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "rfid";
static spi_device_handle_t s_dev;

enum { CommandReg=0x01, ComIEnReg=0x02, ComIrqReg=0x04, ErrorReg=0x06,
       FIFODataReg=0x09, FIFOLevelReg=0x0A, ControlReg=0x0C, BitFramingReg=0x0D,
       ModeReg=0x11, TxControlReg=0x14, TxASKReg=0x15,
       TModeReg=0x2A, TPrescalerReg=0x2B, TReloadRegH=0x2C, TReloadRegL=0x2D,
       VersionReg=0x37 };
enum { CMD_IDLE=0x00, CMD_TRANSCEIVE=0x0C, CMD_SOFTRESET=0x0F };

static void reg_w(uint8_t reg, uint8_t val)
{
    uint8_t tx[2] = {(uint8_t)((reg << 1) & 0x7E), val};
    spi_transaction_t t = {.length = 16, .tx_buffer = tx};
    spi_device_polling_transmit(s_dev, &t);
}

static uint8_t reg_r(uint8_t reg)
{
    uint8_t tx[2] = {(uint8_t)(((reg << 1) & 0x7E) | 0x80), 0}, rx[2] = {0};
    spi_transaction_t t = {.length = 16, .tx_buffer = tx, .rx_buffer = rx};
    spi_device_polling_transmit(s_dev, &t);
    return rx[1];
}

static void reg_set(uint8_t reg, uint8_t mask) { reg_w(reg, reg_r(reg) | mask); }
static void reg_clr(uint8_t reg, uint8_t mask) { reg_w(reg, reg_r(reg) & (uint8_t)~mask); }

/* Transceive `send_len` bytes, collect the reply. `bits` is the number of
 * valid bits in the final transmitted byte (7 for a short-frame REQA). */
static bool transceive(const uint8_t *send, uint8_t send_len, uint8_t bits,
                       uint8_t *back, uint8_t *back_len)
{
    reg_w(ComIEnReg, 0x77 | 0x80);
    reg_clr(ComIrqReg, 0x80);
    reg_set(FIFOLevelReg, 0x80);                 /* flush FIFO */
    reg_w(CommandReg, CMD_IDLE);

    for (uint8_t i = 0; i < send_len; i++) reg_w(FIFODataReg, send[i]);
    reg_w(BitFramingReg, bits);
    reg_w(CommandReg, CMD_TRANSCEIVE);
    reg_set(BitFramingReg, 0x80);                /* StartSend */

    /* ~25 ms worth of polling; the timer reload below also bounds it */
    int i = 2000;
    uint8_t irq;
    do {
        irq = reg_r(ComIrqReg);
        i--;
    } while (i && !(irq & 0x01) && !(irq & 0x30));
    reg_clr(BitFramingReg, 0x80);

    if (i == 0 || (reg_r(ErrorReg) & 0x1B)) {
        return false;
    }
    const uint8_t n = reg_r(FIFOLevelReg);
    if (n == 0 || n > *back_len) {
        return false;
    }
    for (uint8_t k = 0; k < n; k++) back[k] = reg_r(FIFODataReg);
    *back_len = n;
    return true;
}

esp_err_t rfid_init(void)
{
    gpio_config_t io = {.pin_bit_mask = 1ULL << PIN_RFID_RST, .mode = GPIO_MODE_OUTPUT};
    ESP_RETURN_ON_ERROR(gpio_config(&io), TAG, "gpio");

    spi_bus_config_t bus = {
        .mosi_io_num = PIN_RFID_MOSI,
        .miso_io_num = PIN_RFID_MISO,
        .sclk_io_num = PIN_RFID_SCLK,
        .quadwp_io_num = -1, .quadhd_io_num = -1,
        .max_transfer_sz = 64,
    };
    ESP_RETURN_ON_ERROR(spi_bus_initialize(SPI3_HOST, &bus, SPI_DMA_CH_AUTO),
                        TAG, "bus");
    spi_device_interface_config_t dc = {
        .clock_speed_hz = RFID_HZ,
        .mode = 0,
        .spics_io_num = PIN_RFID_CS,
        .queue_size = 2,
    };
    ESP_RETURN_ON_ERROR(spi_bus_add_device(SPI3_HOST, &dc, &s_dev), TAG, "dev");

    gpio_set_level(PIN_RFID_RST, 0); vTaskDelay(pdMS_TO_TICKS(5));
    gpio_set_level(PIN_RFID_RST, 1); vTaskDelay(pdMS_TO_TICKS(50));

    reg_w(CommandReg, CMD_SOFTRESET);
    vTaskDelay(pdMS_TO_TICKS(50));

    reg_w(TModeReg, 0x8D);          /* timer auto-start, high bits of prescaler */
    reg_w(TPrescalerReg, 0x3E);     /* ~25 us tick                              */
    reg_w(TReloadRegH, 0x00);
    reg_w(TReloadRegL, 0x1E);       /* 30 ticks -> ~750 us per transaction      */
    reg_w(TxASKReg, 0x40);          /* 100% ASK                                 */
    reg_w(ModeReg, 0x3D);           /* CRC preset 0x6363                        */
    reg_set(TxControlReg, 0x03);    /* antenna on                               */

    /* Report absence as a failure, not a warning. The boot summary is only
     * useful if "rfid:1" actually means the reader is there - otherwise the
     * one line meant to tell you what is wired is the line that lies to you. */
    const uint8_t ver = reg_r(VersionReg);
    if (ver == 0x00 || ver == 0xFF) {
        ESP_LOGW(TAG, "VersionReg=0x%02x - no reader on SPI3 "
                      "(check 3V3, GND, and CS%d/SCLK%d/MOSI%d/MISO%d)",
                 ver, PIN_RFID_CS, PIN_RFID_SCLK, PIN_RFID_MOSI, PIN_RFID_MISO);
        return ESP_ERR_NOT_FOUND;
    }
    ESP_LOGI(TAG, "MFRC522 v0x%02x on SPI3", ver);
    return ESP_OK;
}

bool rfid_poll(uint8_t *uid, uint8_t *uid_len)
{
    uint8_t buf[8], n;

    /* REQA: 7-bit short frame */
    reg_w(BitFramingReg, 0x07);
    uint8_t req = 0x26;
    n = sizeof(buf);
    if (!transceive(&req, 1, 0x07, buf, &n) || n != 2) {
        return false;
    }

    /* Anticollision cascade level 1 -> 5 bytes: UID0..3 + BCC */
    uint8_t anti[2] = {0x93, 0x20};
    n = sizeof(buf);
    if (!transceive(anti, 2, 0x00, buf, &n) || n != 5) {
        return false;
    }
    uint8_t bcc = 0;
    for (int i = 0; i < 4; i++) bcc ^= buf[i];
    if (bcc != buf[4]) {
        return false;                 /* collision or a bad read */
    }
    memcpy(uid, buf, 4);
    *uid_len = 4;
    return true;
}
