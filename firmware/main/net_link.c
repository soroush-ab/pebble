#include "net_link.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include <errno.h>
#include <inttypes.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "lwip/sockets.h"
#include "esp_heap_caps.h"
#include "nvs_flash.h"

static const char *TAG = "net";

#define RX_BUF_SZ   (72 * 1024)      /* must exceed the largest display blit */
/* Generous on purpose. A phone hotspot can stall for most of a second during
 * a rate adaptation or a background scan; 300 ms turned every such stall into
 * a dropped frame, and any stall that landed mid-frame cost a reconnect.
 * Waiting is cheap, because a frame skipped before its first byte is now a
 * clean drop rather than a teardown. */
#define SEND_TMO_MS 2000

static int          s_sock = -1;
static SemaphoreHandle_t s_tx_lock;
static EventGroupHandle_t s_wifi_ev;
static net_rx_cb_t  s_rx_cb;
static uint32_t     s_seq, s_dropped;
static uint8_t     *s_rx;

#define WIFI_BIT_GOT_IP BIT0

bool net_link_up(void) { return s_sock >= 0; }
uint32_t net_dropped_frames(void) { return s_dropped; }

/* ------------------------------------------------------------------ wifi */

static void on_wifi(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_wifi_ev, WIFI_BIT_GOT_IP);
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *e = (const ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "ip " IPSTR, IP2STR(&e->ip_info.ip));
        xEventGroupSetBits(s_wifi_ev, WIFI_BIT_GOT_IP);
    }
}

static esp_err_t wifi_start(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, on_wifi, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT, IP_EVENT_STA_GOT_IP, on_wifi, NULL, NULL));

    wifi_config_t wc = {0};
    strncpy((char *)wc.sta.ssid, CONFIG_PEBBLE_WIFI_SSID, sizeof(wc.sta.ssid) - 1);
    strncpy((char *)wc.sta.password, CONFIG_PEBBLE_WIFI_PASS, sizeof(wc.sta.password) - 1);
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    /* Streaming beats power saving; the default modem sleep adds ~100 ms
     * latency spikes that would show up as jitter in the sensor timebase. */
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_start());
    return ESP_OK;
}

/* -------------------------------------------------------------------- tx */

/* Write every byte or fail. send() on a blocking socket may legitimately
 * return short when the kernel buffer is full - treating that as success was
 * silently truncating frames, and treating it as a plain error left the
 * stream desynced. Loop, and let the caller tear down only on a real error. */
static bool send_all(const void *buf, size_t len, size_t *written)
{
    const uint8_t *p = (const uint8_t *)buf;
    while (len) {
        const ssize_t n = send(s_sock, p, len, 0);
        if (n > 0) {
            p += n;
            len -= (size_t)n;
            if (written) *written += (size_t)n;
            continue;
        }
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            return false;            /* send timeout: give up on this frame */
        }
        return false;
    }
    return true;
}

esp_err_t net_send(uint8_t type, const void *sub, size_t sub_len,
                   const void *body, size_t body_len)
{
    if (s_sock < 0) {
        return ESP_ERR_INVALID_STATE;
    }
    if (xSemaphoreTake(s_tx_lock, pdMS_TO_TICKS(SEND_TMO_MS)) != pdTRUE) {
        s_dropped++;
        return ESP_ERR_TIMEOUT;
    }

    proto_hdr_t h = {
        .magic = PROTO_MAGIC,
        .type = type,
        .flags = 0,
        .len = (uint32_t)(sub_len + body_len),
        .seq = s_seq++,
    };

    /* Header and sub-header are small and fixed, so stage them contiguously:
     * one write for the frame prefix, then the body. */
    uint8_t pre[sizeof(h) + 32];
    if (sub_len > sizeof(pre) - sizeof(h)) {
        xSemaphoreGive(s_tx_lock);
        return ESP_ERR_INVALID_SIZE;
    }
    memcpy(pre, &h, sizeof(h));
    if (sub_len) {
        memcpy(pre + sizeof(h), sub, sub_len);
    }

    esp_err_t rc = ESP_OK;
    size_t wrote = 0;
    if (!send_all(pre, sizeof(h) + sub_len, &wrote) ||
        (body_len && !send_all(body, body_len, &wrote))) {
        s_dropped++;
        rc = ESP_FAIL;
        if (wrote != 0) {
            /* Bytes of this frame are already on the wire, so the peer has
             * half a frame and will never resync: the connection has to go.
             * If nothing was written the frame is simply skipped, which is
             * the right response to a momentarily slow consumer and keeps a
             * brief stall from costing a reconnect. */
            close(s_sock);
            s_sock = -1;
        }
    }
    xSemaphoreGive(s_tx_lock);
    return rc;
}

void net_log(const char *fmt, ...)
{
    char buf[160];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    if (n > 0) {
        net_send(MSG_LOG, NULL, 0, buf, (size_t)n);
    }
    ESP_LOGI(TAG, "%s", buf);
}

/* -------------------------------------------------------------------- rx */

static void pump_rx(void)
{
    size_t have = 0;
    for (;;) {
        ssize_t got = recv(s_sock, s_rx + have, RX_BUF_SZ - have, 0);
        if (got <= 0) {
            return;                  /* closed or error; caller reconnects */
        }
        have += (size_t)got;

        size_t off = 0;
        for (;;) {
            if (have - off < sizeof(proto_hdr_t)) break;
            const proto_hdr_t *h = (const proto_hdr_t *)(s_rx + off);
            if (h->magic != PROTO_MAGIC) {
                ESP_LOGE(TAG, "framing lost, resyncing");
                have = off = 0;
                break;
            }
            if (h->len > RX_BUF_SZ - sizeof(proto_hdr_t)) {
                ESP_LOGE(TAG, "oversize frame %" PRIu32, h->len);
                have = off = 0;
                break;
            }
            if (have - off < sizeof(proto_hdr_t) + h->len) break;   /* partial */

            if (s_rx_cb) {
                s_rx_cb(h->type, s_rx + off + sizeof(proto_hdr_t), h->len);
            }
            off += sizeof(proto_hdr_t) + h->len;
        }
        if (off) {
            memmove(s_rx, s_rx + off, have - off);
            have -= off;
        }
        if (have == RX_BUF_SZ) {     /* no progress possible */
            ESP_LOGE(TAG, "rx buffer full, dropping");
            have = 0;
        }
    }
}

static void link_task(void *arg)
{
    (void)arg;
    for (;;) {
        xEventGroupWaitBits(s_wifi_ev, WIFI_BIT_GOT_IP, pdFALSE, pdTRUE, portMAX_DELAY);

        int s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        if (s < 0) {
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        struct sockaddr_in a = {
            .sin_family = AF_INET,
            .sin_port = htons(CONFIG_PEBBLE_HOST_PORT),
        };
        inet_pton(AF_INET, CONFIG_PEBBLE_HOST_IP, &a.sin_addr);

        if (connect(s, (struct sockaddr *)&a, sizeof(a)) != 0) {
            close(s);
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }

        int one = 1;
        setsockopt(s, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
        struct timeval tv = {.tv_sec = 0, .tv_usec = SEND_TMO_MS * 1000};
        setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
        int sndbuf = 32 * 1024;      /* absorb bursts; audio is ~3 kB/frame */
        setsockopt(s, SOL_SOCKET, SO_SNDBUF, &sndbuf, sizeof(sndbuf));

        ESP_LOGI(TAG, "connected to %s:%d", CONFIG_PEBBLE_HOST_IP,
                 CONFIG_PEBBLE_HOST_PORT);
        s_sock = s;

        pump_rx();                   /* blocks until the peer goes away */

        ESP_LOGW(TAG, "link down (%" PRIu32 " frames dropped)", s_dropped);
        xSemaphoreTake(s_tx_lock, pdMS_TO_TICKS(500));
        if (s_sock >= 0) { close(s_sock); s_sock = -1; }
        xSemaphoreGive(s_tx_lock);
        vTaskDelay(pdMS_TO_TICKS(500));
    }
}

esp_err_t net_link_init(net_rx_cb_t cb)
{
    s_rx_cb = cb;
    s_tx_lock = xSemaphoreCreateMutex();
    s_wifi_ev = xEventGroupCreate();
    s_rx = heap_caps_malloc(RX_BUF_SZ, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!s_rx) {
        s_rx = malloc(RX_BUF_SZ);    /* fall back to internal RAM */
    }
    if (!s_tx_lock || !s_wifi_ev || !s_rx) {
        return ESP_ERR_NO_MEM;
    }
    ESP_ERROR_CHECK(wifi_start());
    return xTaskCreate(link_task, "link", 5120, NULL, 7, NULL) == pdPASS
           ? ESP_OK : ESP_ERR_NO_MEM;
}
