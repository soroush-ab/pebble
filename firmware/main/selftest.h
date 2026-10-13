#ifndef PEBBLE_SELFTEST_H_
#define PEBBLE_SELFTEST_H_
/* Interactive per-subsystem bring-up tests over the USB console.
 * Returns when the operator chooses to leave. */
void selftest_menu(void);
/* Handle one key if it selects a test; returns true if it was consumed. */
bool selftest_key(int c);
#endif
