/*
 * frames - eye frames held in PSRAM, played with local timing.
 *
 * Streaming pixels over Wi-Fi (~340 kB/s measured) made a blink take most of a
 * second and drew the left eye visibly before the right. Instead the PC
 * uploads every frame it may need once per connection, and afterwards sends
 * only frame ids: MSG_FRAME_SHOW for a new expression, MSG_FRAME_ANIM for a
 * blink. Playback runs in its own task, so the timing is the ESP's clock, not
 * the network's, and both eyes are drawn back to back (~15 ms each at 60 MHz).
 *
 * Frames live in PSRAM and are lost on reboot; the PC re-uploads on connect.
 */
#include "frames.h"

#include <stdlib.h>
#include <string.h>

#include "disp.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static const char *TAG = "frames";

typedef struct {
    uint8_t *data;
    uint32_t total, have;
} frame_t;

typedef struct {
    frame_pair_t steps[FRAME_ANIM_MAX];
    uint8_t n;
    uint16_t frame_ms;
} job_t;

static frame_t s_frames[FRAME_MAX_ID];
static SemaphoreHandle_t s_lock;        /* guards s_frames */
static QueueHandle_t s_jobs;            /* length 1: the newest request wins */

static void draw(uint8_t which, uint16_t id)
{
    if (id == FRAME_NONE || id >= FRAME_MAX_ID) return;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    const frame_t *f = &s_frames[id];
    if (f->data && f->have == f->total) {
        disp_rle(which, f->data, f->total);
    } else {
        ESP_LOGW(TAG, "frame %u not stored", id);
    }
    xSemaphoreGive(s_lock);
}

static void player(void *arg)
{
    (void)arg;
    job_t job;
    for (;;) {
        xQueueReceive(s_jobs, &job, portMAX_DELAY);
        for (uint8_t i = 0; i < job.n; i++) {
            const TickType_t t0 = xTaskGetTickCount();
            draw(0, job.steps[i].left);
            draw(1, job.steps[i].right);
            if (i + 1 < job.n) {
                /* a newer request interrupts this one */
                const TickType_t spent = xTaskGetTickCount() - t0;
                const TickType_t want = pdMS_TO_TICKS(job.frame_ms);
                if (xQueuePeek(s_jobs, &job, want > spent ? want - spent : 0) == pdTRUE) {
                    break;
                }
            }
        }
    }
}

esp_err_t frames_init(void)
{
    s_lock = xSemaphoreCreateMutex();
    s_jobs = xQueueCreate(1, sizeof(job_t));
    if (!s_lock || !s_jobs) return ESP_ERR_NO_MEM;
    return xTaskCreate(player, "frames", 4096, NULL, 5, NULL) == pdPASS
           ? ESP_OK : ESP_ERR_NO_MEM;
}

void frames_store(const uint8_t *p, uint32_t len)
{
    if (len < sizeof(frame_store_t)) return;
    frame_store_t h;
    memcpy(&h, p, sizeof(h));
    const uint8_t *chunk = p + sizeof(h);
    const uint32_t n = len - sizeof(h);
    if (h.id >= FRAME_MAX_ID || h.fmt != 1 || h.offset + n > h.total || !h.total) {
        ESP_LOGW(TAG, "bad store: id %u fmt %u %lu+%lu/%lu", h.id, h.fmt,
                 (unsigned long)h.offset, (unsigned long)n, (unsigned long)h.total);
        return;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
    frame_t *f = &s_frames[h.id];
    if (h.offset == 0 || !f->data || f->total != h.total) {
        free(f->data);
        f->data = heap_caps_malloc(h.total, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        f->total = f->data ? h.total : 0;
        f->have = 0;
        if (!f->data) {
            ESP_LOGE(TAG, "PSRAM full: frame %u (%lu B)", h.id, (unsigned long)h.total);
        }
    }
    if (f->data) {
        memcpy(f->data + h.offset, chunk, n);
        if (h.offset + n > f->have) f->have = h.offset + n;
    }
    xSemaphoreGive(s_lock);
}

void frames_show(uint16_t left, uint16_t right)
{
    job_t job = {.n = 1, .frame_ms = 0};
    job.steps[0].left = left;
    job.steps[0].right = right;
    xQueueOverwrite(s_jobs, &job);
}

void frames_anim(const frame_pair_t *steps, uint8_t n, uint16_t frame_ms)
{
    job_t job = {.n = n > FRAME_ANIM_MAX ? FRAME_ANIM_MAX : n, .frame_ms = frame_ms};
    memcpy(job.steps, steps, job.n * sizeof(frame_pair_t));
    xQueueOverwrite(s_jobs, &job);
}

void frames_clear(void)
{
    xSemaphoreTake(s_lock, portMAX_DELAY);
    for (int i = 0; i < FRAME_MAX_ID; i++) {
        free(s_frames[i].data);
        s_frames[i] = (frame_t){0};
    }
    xSemaphoreGive(s_lock);
}

void frames_info(frame_info_t *out)
{
    uint16_t count = 0;
    uint32_t bytes = 0;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    for (int i = 0; i < FRAME_MAX_ID; i++) {
        if (s_frames[i].data && s_frames[i].have == s_frames[i].total) {
            count++;
            bytes += s_frames[i].total;
        }
    }
    xSemaphoreGive(s_lock);
    out->count = count;
    out->max_id = FRAME_MAX_ID;
    out->bytes = bytes;
    out->psram_free = (uint32_t)heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
}
