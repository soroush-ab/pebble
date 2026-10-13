#ifndef PEBBLE_NET_LINK_H_
#define PEBBLE_NET_LINK_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"
#include "proto.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Called from the receive task for each complete inbound message. */
typedef void (*net_rx_cb_t)(uint8_t type, const uint8_t *payload, uint32_t len);

esp_err_t net_link_init(net_rx_cb_t cb);
bool net_link_up(void);

/* Send one framed message. `sub` is an optional fixed sub-header (audio_hdr_t,
 * disp_blit_t, ...) sent immediately before `body`.
 *
 * Sensor streams DROP rather than block: if the socket cannot take the frame
 * within the send timeout, the frame is discarded and a counter incremented.
 * Blocking here would back up the ADC DMA and corrupt the sample timebase,
 * which is far worse than losing 18 ms of audio. */
esp_err_t net_send(uint8_t type, const void *sub, size_t sub_len,
                   const void *body, size_t body_len);

uint32_t net_dropped_frames(void);
void     net_log(const char *fmt, ...);

#ifdef __cplusplus
}
#endif
#endif
