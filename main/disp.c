/* disp - two GC9A01 240x240 round displays on a shared SPI2 bus.
 *
 * Pixels come from the PC. A full frame is 115 kB, so streaming both at 30 fps
 * would need 55 Mbit/s - more than the link has. The protocol therefore sends
 * dirty rectangles; an 80x80 iris is 12.5 kB and two eyes at 30 fps is about
 * 6 Mbit/s, which fits alongside the audio streams. disp_fill() exists so a
 * solid region costs 10 bytes instead of kilobytes.
 */
#include "disp.h"

#include <string.h>

#include "board.h"
#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_check.h"
#include "esp_log.h"
#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "disp";
static spi_device_handle_t s_dev[2];

#define LINE_BUF_PX 2048
static uint16_t *s_linebuf;

static void dc(int level) { gpio_set_level(PIN_DISP_DC, level); }

static void wr(spi_device_handle_t d, int is_data, const uint8_t *buf, size_t len)
{
    if (!len) return;
    dc(is_data);
    spi_transaction_t t = {.length = len * 8, .tx_buffer = buf};
    spi_device_polling_transmit(d, &t);
}

static void cmd(spi_device_handle_t d, uint8_t c, const uint8_t *data, size_t n)
{
    wr(d, 0, &c, 1);
    if (n) wr(d, 1, data, n);
}

/* Compact GC9A01 bring-up: the vendor blob minus the panel-tuning registers
 * that only matter for colour accuracy. */
static const uint8_t INIT[] = {
    0xEF, 0,
    0xEB, 1, 0x14,
    0xFE, 0, 0xEF, 0,
    0xEB, 1, 0x14,
    0x84, 1, 0x40,  0x85, 1, 0xFF,  0x86, 1, 0xFF,  0x87, 1, 0xFF,
    0x88, 1, 0x0A,  0x89, 1, 0x21,  0x8A, 1, 0x00,  0x8B, 1, 0x80,
    0x8C, 1, 0x01,  0x8D, 1, 0x01,  0x8E, 1, 0xFF,  0x8F, 1, 0xFF,
    0xB6, 2, 0x00, 0x20,
    0x36, 1, 0x08,                     /* MADCTL: BGR */
    0x3A, 1, 0x05,                     /* COLMOD: 16 bit/px */
    0x90, 4, 0x08, 0x08, 0x08, 0x08,
    0xBD, 1, 0x06,  0xBC, 1, 0x00,
    0xFF, 3, 0x60, 0x01, 0x04,
    0xC3, 1, 0x13,  0xC4, 1, 0x13,  0xC9, 1, 0x22,
    0xBE, 1, 0x11,  0xE1, 2, 0x10, 0x0E,  0xDF, 3, 0x21, 0x0c, 0x02,
    0xF0, 6, 0x45, 0x09, 0x08, 0x08, 0x26, 0x2A,
    0xF1, 6, 0x43, 0x70, 0x72, 0x36, 0x37, 0x6F,
    0xF2, 6, 0x45, 0x09, 0x08, 0x08, 0x26, 0x2A,
    0xF3, 6, 0x43, 0x70, 0x72, 0x36, 0x37, 0x6F,
    0xED, 2, 0x1B, 0x0B,
    0xAE, 1, 0x77,  0xCD, 1, 0x63,
    0x70, 9, 0x07, 0x07, 0x04, 0x0E, 0x0F, 0x09, 0x07, 0x08, 0x03,
    0xE8, 1, 0x34,
    0x62, 12, 0x18,0x0D,0x71,0xED,0x70,0x70,0x18,0x0F,0x71,0xEF,0x70,0x70,
    0x63, 12, 0x18,0x11,0x71,0xF1,0x70,0x70,0x18,0x13,0x71,0xF3,0x70,0x70,
    0x64, 7, 0x28,0x29,0xF1,0x01,0xF1,0x00,0x07,
    0x66, 10, 0x3C,0x00,0xCD,0x67,0x45,0x45,0x10,0x00,0x00,0x00,
    0x67, 10, 0x00,0x3C,0x00,0x00,0x00,0x01,0x54,0x10,0x32,0x98,
    0x74, 7, 0x10,0x85,0x80,0x00,0x00,0x4E,0x00,
    0x98, 2, 0x3e, 0x07,
    0x35, 0, 0x21, 0,
    0x11, 0,                           /* sleep out - needs 120 ms          */
    0xFF,                              /* end marker                        */
};

static void run_init(spi_device_handle_t d)
{
    const uint8_t *p = INIT;
    while (*p != 0xFF || p[1] != 0x00) {
        if (*p == 0xFF && p == INIT) break;
        const uint8_t c = *p++;
        const uint8_t n = *p++;
        if (c == 0xFF && n == 0x00) break;
        cmd(d, c, p, n);
        p += n;
        if (c == 0x11) vTaskDelay(pdMS_TO_TICKS(120));
        if (p - INIT >= (int)sizeof(INIT)) break;
    }
    cmd(d, 0x29, NULL, 0);             /* display on */
}

static void set_window(spi_device_handle_t d, uint16_t x, uint16_t y,
                       uint16_t w, uint16_t h)
{
    uint8_t b[4];
    b[0] = x >> 8; b[1] = x & 0xFF;
    b[2] = (x + w - 1) >> 8; b[3] = (x + w - 1) & 0xFF;
    cmd(d, 0x2A, b, 4);
    b[0] = y >> 8; b[1] = y & 0xFF;
    b[2] = (y + h - 1) >> 8; b[3] = (y + h - 1) & 0xFF;
    cmd(d, 0x2B, b, 4);
    cmd(d, 0x2C, NULL, 0);
}

esp_err_t disp_init(void)
{
    gpio_config_t io = {
        .pin_bit_mask = (1ULL << PIN_DISP_DC) | (1ULL << PIN_DISP_RST),
        .mode = GPIO_MODE_OUTPUT,
    };
    ESP_RETURN_ON_ERROR(gpio_config(&io), TAG, "gpio");

    spi_bus_config_t bus = {
        .mosi_io_num = PIN_DISP_MOSI,
        .miso_io_num = -1,             /* displays are write-only */
        .sclk_io_num = PIN_DISP_SCLK,
        .quadwp_io_num = -1, .quadhd_io_num = -1,
        .max_transfer_sz = LINE_BUF_PX * 2,
    };
    ESP_RETURN_ON_ERROR(spi_bus_initialize(SPI2_HOST, &bus, SPI_DMA_CH_AUTO),
                        TAG, "bus");

    const int cs[2] = {PIN_DISP_CS_L, PIN_DISP_CS_R};
    for (int i = 0; i < 2; i++) {
        spi_device_interface_config_t dc_cfg = {
            .clock_speed_hz = DISP_HZ,
            .mode = 0,
            .spics_io_num = cs[i],
            .queue_size = 4,
        };
        ESP_RETURN_ON_ERROR(spi_bus_add_device(SPI2_HOST, &dc_cfg, &s_dev[i]),
                            TAG, "dev%d", i);
    }

    s_linebuf = heap_caps_malloc(LINE_BUF_PX * 2, MALLOC_CAP_DMA);
    ESP_RETURN_ON_FALSE(s_linebuf, ESP_ERR_NO_MEM, TAG, "linebuf");

    gpio_set_level(PIN_DISP_RST, 0); vTaskDelay(pdMS_TO_TICKS(20));
    gpio_set_level(PIN_DISP_RST, 1); vTaskDelay(pdMS_TO_TICKS(120));
    for (int i = 0; i < 2; i++) run_init(s_dev[i]);

    ESP_LOGI(TAG, "2x GC9A01 %dx%d on SPI2 @ %d MHz", DISP_WIDTH, DISP_HEIGHT, DISP_HZ / 1000000);
    return ESP_OK;
}

static esp_err_t clip(uint16_t *x, uint16_t *y, uint16_t *w, uint16_t *h)
{
    if (*x >= DISP_WIDTH || *y >= DISP_HEIGHT) return ESP_ERR_INVALID_ARG;
    if (*x + *w > DISP_WIDTH) *w = DISP_WIDTH - *x;
    if (*y + *h > DISP_HEIGHT) *h = DISP_HEIGHT - *y;
    return (*w && *h) ? ESP_OK : ESP_ERR_INVALID_ARG;
}

esp_err_t disp_blit(uint8_t which, uint16_t x, uint16_t y, uint16_t w, uint16_t h,
                    const uint16_t *px)
{
    ESP_RETURN_ON_ERROR(clip(&x, &y, &w, &h), TAG, "rect");
    for (int i = 0; i < 2; i++) {
        if (which != 2 && which != i) continue;
        set_window(s_dev[i], x, y, w, h);
        dc(1);
        size_t left = (size_t)w * h;
        const uint8_t *p = (const uint8_t *)px;
        while (left) {
            const size_t n = left > LINE_BUF_PX ? LINE_BUF_PX : left;
            memcpy(s_linebuf, p, n * 2);     /* DMA-capable staging buffer */
            spi_transaction_t t = {.length = n * 16, .tx_buffer = s_linebuf};
            spi_device_polling_transmit(s_dev[i], &t);
            p += n * 2;
            left -= n;
        }
    }
    return ESP_OK;
}

esp_err_t disp_fill(uint8_t which, uint16_t x, uint16_t y, uint16_t w, uint16_t h,
                    uint16_t color)
{
    ESP_RETURN_ON_ERROR(clip(&x, &y, &w, &h), TAG, "rect");
    for (int k = 0; k < LINE_BUF_PX; k++) s_linebuf[k] = color;
    for (int i = 0; i < 2; i++) {
        if (which != 2 && which != i) continue;
        set_window(s_dev[i], x, y, w, h);
        dc(1);
        size_t left = (size_t)w * h;
        while (left) {
            const size_t n = left > LINE_BUF_PX ? LINE_BUF_PX : left;
            spi_transaction_t t = {.length = n * 16, .tx_buffer = s_linebuf};
            spi_device_polling_transmit(s_dev[i], &t);
            left -= n;
        }
    }
    return ESP_OK;
}
