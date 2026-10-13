#ifndef PEBBLE_RFID_H_
#define PEBBLE_RFID_H_
#include "esp_err.h"
#include <stdbool.h>
#include <stdint.h>
esp_err_t rfid_init(void);
/* True when a card is present; uid/uid_len filled on success. */
bool rfid_poll(uint8_t *uid, uint8_t *uid_len);
#endif
