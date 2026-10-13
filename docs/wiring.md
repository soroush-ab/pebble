# Pebble wiring map

ESP32-S3-N16R8. `main/board.h` is the source of truth; this is the bench copy.
Every device shares **one common ground** with the ESP32.

## Reserved — do not use

| GPIO | why |
| --- | --- |
| 26–32 | SPI flash |
| **33–37** | **octal PSRAM on -R8.** Free on an -R2, which is why pin maps borrowed from other S3 boards fail here |
| 19, 20 | native USB (console + flashing) |
| 43, 44 | UART0 |
| 0, 3, 45, 46 | strapping — avoid driving at reset |

Free pool: 1, 2, 4–18, 21, 38–42, 47, 48. Everything below sits inside it.

---

## 1 · Magnetic encoder — AS5600 (I2C)

| AS5600 | ESP32-S3 | note |
| --- | --- | --- |
| VCC | 3V3 | |
| GND | GND | |
| SDA | **GPIO8** | |
| SCL | **GPIO9** | |
| **DIR** | **GND** | ⚠️ **must be tied** — see below |
| OUT / PGO | leave open | analog output, unused |

> ### ⚠️ DIR must not float
> DIR selects which way the angle counts. Floating, it toggles, and the chip
> alternates between reporting θ and 4095−θ. Measured on this hardware: readings
> flipped between 1600 and 2495 at up to 200 Hz — **1600 + 2495 = 4095** — with
> seven more complement pairs all summing to exactly 4095. It looks exactly like
> a violently unstable sensor and is one wire.
>
> **DIR → GND** = clockwise increases. **DIR → 3V3** = counter-clockwise.

Address is fixed at **0x36**. Magnet: diametrically magnetised, 0.5–3 mm above
the package, centred on the rotation axis. `STATUS` bit5 set = detected,
bit4 = too weak, bit3 = too strong.

**I2C pull-ups:** bus runs at 100 kHz using the ESP32's internal pull-ups
(~45 kΩ). Fit **4.7 kΩ to 3V3 on both SDA and SCL** and you can raise
`I2C_FREQ_HZ` back to 400 kHz.

---

## 2 · Analog microphone array — 3× SparkFun Sound Detector (ADC1)

| mic | detector pin | ESP32-S3 | ADC |
| --- | --- | --- | --- |
| A (90°) | AUDIO (or ENVELOPE) | **GPIO4** | ADC1_CH3 |
| B (210°) | AUDIO (or ENVELOPE) | **GPIO5** | ADC1_CH4 |
| C (330°) | AUDIO (or ENVELOPE) | **GPIO6** | ADC1_CH5 |
| all | VCC | 3V3 or 5V | see divider |
| all | GND | GND | one common ground |

Must be **ADC1**: ADC2 is unusable while Wi-Fi is on.

- **Divider** (needed only if the detectors run at 5 V): 10 kΩ from the output
  to the ADC pin, 20 kΩ from the pin to GND → 1.5:1.
- **100 nF from each ADC pin to GND.** The SAR samples onto an internal
  capacitor and wants a stiff source.
- **Anti-alias RC** if using the AUDIO pin: ~1 kΩ + 15 nF (≈10 kHz corner).
  Each channel is sampled at 27.8 kHz, so anything above 13.9 kHz folds back
  and decorrelates the channels, which destroys TDOA.
- Mic geometry must match `config/robot.yaml`: 90/210/330°, 58 mm radius.

---

## 3 · Digital microphone — I2S (INMP441 / SPH0645 class)

| mic | ESP32-S3 |
| --- | --- |
| VDD | 3V3 |
| GND | GND |
| SCK / BCLK | **GPIO15** |
| WS / LRCL | **GPIO16** |
| SD / DOUT | **GPIO17** |
| L/R · SEL | GND (left channel) |

Firmware assumes **I2S**, not I2C — parts in this class are I2S. If yours is
genuinely I2C, put it on the GPIO8/9 bus and rewrite `main/mic_i2s.c` only.

---

## 4 · Round displays — 2× GC9A01 240×240 (SPI2, shared bus)

| display | ESP32-S3 | note |
| --- | --- | --- |
| VCC | 3V3 | |
| GND | GND | |
| SCL / SCK | **GPIO12** | shared |
| SDA / MOSI | **GPIO11** | shared |
| DC | **GPIO18** | shared |
| RST | **GPIO21** | shared |
| **CS (left)** | **GPIO10** | one each — this is what separates them |
| **CS (right)** | **GPIO14** | |
| BL | 3V3 | or assign a pin for dimming |

No MISO: these are write-only. If the self-test's "left red / right blue" step
shows the same colour on both, the two CS lines are swapped or shorted.

---

## 5 · RFID reader — MFRC522 (SPI3, its own bus)

| MFRC522 | ESP32-S3 |
| --- | --- |
| 3.3V | 3V3 (**not 5 V**) |
| GND | GND |
| SCK | **GPIO39** |
| MOSI | **GPIO40** |
| MISO | **GPIO41** |
| SDA / NSS / CS | **GPIO42** |
| RST | **GPIO47** |
| IRQ | not connected |

On a separate bus from the displays on purpose: a tag poll holds the bus for
milliseconds and would stall a frame blit.

`VersionReg=0x00` at boot means the reader isn't answering — check 3V3, GND and
all four SPI lines. A good reader reports 0x91 or 0x92.

---

## 6 · Stepper driver — TMC2209 (step/dir)

| TMC2209 | ESP32-S3 | note |
| --- | --- | --- |
| STEP | **GPIO1** | |
| DIR | **GPIO2** | |
| EN | **GPIO13** | active **LOW** |
| VIO | 3V3 | logic supply |
| GND | GND | common with ESP |
| VM / GND | motor supply | 12 V + ≥100 µF close to the driver |
| MS1/MS2 | per 1/16 microstepping | matches `STEPPER_MICROSTEPS` |
| PDN/UART | **GPIO48** | optional, for StealthChop config |

EN is deliberately **not** on GPIO3: that's a strapping pin, and a driver that
enables itself during reset can drive the base into a hard stop.

Set the current limit with the Vref pot **before** first enable. Use
StealthChop — the stepper sits centimetres from the mic array.

---

## 7 · Servos — 2× (LEDC PWM)

| servo | ESP32-S3 |
| --- | --- |
| signal A | **GPIO7** |
| signal B | **GPIO38** |
| V+ | **separate 5–6 V supply, not the ESP's 3V3** |
| GND | common with ESP |

50 Hz, 500–2500 µs. MG995-class servos draw amps on stall; powering them from
the dev board will brown out the ESP32. Separate rail, common ground, bulk
capacitance at the servos.

---

## Spare

GPIO **3** (strapping — input only), GPIO **48** if the TMC UART is unused.

## Power

Three rails, one ground: **3V3** logic and sensors, **5–6 V** servos,
**12 V** stepper. Join grounds at a single point near the ESP32. Brown-out
under stepper or servo inrush shows up as random ESP32 reboots and will waste
a day if you aren't expecting it.
