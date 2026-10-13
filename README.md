# Pebble MCU — thin I/O bridge

ESP32-S3-N16R8. The MCU does **no signal processing**: it streams sensors to a
PC and applies actuator setpoints coming back. Bearing, TDOA, speech and
personality all run on the PC.

```bash
. ~/.espressif/v6.1/esp-idf/export.sh
idf.py set-target esp32s3        # once
idf.py menuconfig                # Wi-Fi SSID/pass, PC IP, subsystem toggles
idf.py build flash monitor
python3 host/pebble_link.py      # on the PC, start this first
```

## Bring-up: test one thing at a time

Every subsystem can be switched off in `menuconfig` → *Pebble subsystems*, and
**a peripheral that fails to initialise is logged and skipped, never fatal** —
one unwired device must not stop the others being testable. The boot summary
tells you what is actually present:

```
I (678) pebble: encoder    ok
W (1175) rfid: VersionReg=0x00 - no reader on SPI3 (check 3V3, GND, and CS42/SCLK39/MOSI40/MISO41)
I (1177) pebble: rfid       ESP_ERR_NOT_FOUND
subsystems: net:1 mic_adc:1 mic_i2s:1 disp:1 rfid:0 enc:1 motion:1
```

Press **`?`** on the console at any time for per-peripheral tests — these need
**no Wi-Fi and no PC**, which is the point:

| key | tests | a good result looks like |
| --- | --- | --- |
| `e` | encoder | angle sweeps 0–360 and wraps; status `0x20` set |
| `m` | motion | stepper sweeps ±30° with the encoder following; no grinding |
| `r` | RFID | the same UID every read |
| `d` | displays | red/green/blue/white on both, then left-red + right-blue |
| `a` | mic array | link state (levels now live on the PC) |
| `n` | network | link up, dropped-frame count |

`q` stops a running test, `g` leaves the menu. Boot straight into it with
*Boot into the self-test menu*.

## What stays on the MCU, and why

Two things cannot cross Wi-Fi:

- **Step pulses.** Jitter would make them uneven and a dropped packet would
  silently lose steps. The PC sends *setpoints*; `motion.c` owns the
  trajectory, with a 20 kHz hardware timer generating the step train and a
  1 kHz task ramping under velocity/acceleration limits. The magnetic encoder
  closes the loop so the PC still sees true position.
- **The audio timebase.** Every audio frame carries a free-running
  `sample_index` (exact — it counts ADC scans) plus `t_us` to anchor it to
  wall time. The PC must time audio from the index, never from arrival.

## Bandwidth

| stream | rate |
| --- | --- |
| 3 analog mics, 27777 Hz/ch × 16 bit | 1.33 Mb/s |
| digital mic, 16 kHz | 0.26 Mb/s |
| displays, two 80×80 dirty rects @ 30 fps | 6.1 Mb/s |
| displays, two **full frames** @ 30 fps | **55 Mb/s — infeasible** |

Send dirty rectangles, not frames. `MSG_DISP_FILL` makes a solid region cost
10 bytes instead of kilobytes.

## The one thing that will bite you

**The three analog mics are not sampled simultaneously.** The S3's SAR is
multiplexed, so they are 12 µs apart — measured on this hardware and confirmed
to ~1 µs. Ignoring it costs 4.7° of bearing. `HELLO` reports the per-channel
offsets; subtract them before any TDOA. `host/tdoa.c` already does.

Related: `mic_adc.c` locks the three channel streams to scan boundaries. A DMA
read can start mid-scan, and letting the channels drift one scan apart adds
±36 µs (~7°) to every delay while looking completely normal in the levels.

## Layout

| path | |
| --- | --- |
| `main/board.h` | **the pin map** — single source of truth, with the N16R8 reserved-pin list |
| `main/proto.h` | wire format, shared with the PC |
| `main/net_link.c` | Wi-Fi + TCP framing; sensor frames drop rather than block |
| `main/mic_adc.c` | 3-ch analog array, scan-aligned, exact timebase |
| `main/mic_i2s.c` | digital mic (assumes I2S — see the note in that file) |
| `main/disp.c` · `rfid.c` · `encoder.c` · `motion.c` | peripherals |
| `main/selftest.c` | the `?` menu |
| `host/pebble_link.py` | PC reference client; `--wav` records the array |
| `host/mic_array.c` · `tdoa.c` | bearing + TDOA, 31 host tests (`host/test/run.sh`) |

## Pin map

`main/board.h` is authoritative. Reserved on this module: GPIO 26–32 (flash),
**33–37 (octal PSRAM — free on an -R2, which is why borrowed schematics
break)**, 19/20 (USB), 43/44 (UART0), 0/3/45/46 (strapping).

Two SPI buses on purpose: a tag poll holds the bus for milliseconds, which
would stall a frame blit, so displays are on SPI2 and the RFID on SPI3.
