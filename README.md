# Pebble: a customisable home companion robot

COMPSYS 730, Group 5: Soroush Abdollahi · Evren Huang · Ma Luo · Haowei Zheng

Pebble is a small desk robot with two round eye displays and a nodding,
tilting head. Snap on a character shell (Baymax, Marvin, R2-D2…) and the same
smart-home assistant takes on that character's personality: how it talks,
how it feels (Russell's valence × arousal), how it moves. It controls your
Home Assistant devices, reads and sets your reminders (never medication ones,
which a carer adds), and speaks reminders on its own when they fall due.

```
 mic ──► speech (whisper STT) ──► LLM2 runtime ──► speech (Piper TTS) ──► speaker
                                     │  ▲   character profile from LLM1
                 emotion + motion    │  │   devices, states, reminders
                                     ▼  │
 robot (firmware) ◄──► digital twin ◄──┘ brain ◄──► Home Assistant
   eyes, neck, mic       (MCU logic,          (digital_twin/brain:
                          3D viewer)           LLM2 + speech + HA device)
```

## What's in each folder

| Folder | What it is | Details |
|---|---|---|
| [`LLM1/`](LLM1/README.md) | **Character generator.** Turns a character concept into a personality profile (Big Five, tone, behaviour rules, example dialogue), stored by NFC tag in `LLM1/data/characters.db`. The three demo characters are already in it. | local GGUF model |
| [`LLM2/`](LLM2/README.md) | **Runtime character engine.** Role-plays the active character: user speech or HA notifications in, spoken reply + emotion (Russell circumplex, `affect.py`) + Home Assistant calls out. | OpenAI API or any compatible server |
| [`speech/`](speech/README.md) | **Speech.** Local, offline STT (whisper.cpp) and TTS (Piper, one voice per character), as Python classes or an HTTP service. | |
| [`digital_twin/`](digital_twin/README.md) | **Digital twin + brain.** A 3D model of the robot built from the CAD, an emulator of the robot's MCU logic (motions, gaze, eyes, shell swaps), and `brain/llm2_brain.py`, which joins LLM2, speech and Home Assistant and drives the twin **or the real robot**. | [`docs/protocol.md`](digital_twin/docs/protocol.md) is the robot ↔ brain contract |
| [`firmware/`](firmware/README.md) | **ESP32-S3 firmware.** A thin I/O bridge: streams the mics, encoder and RFID to the PC over Wi-Fi, and applies display frames and servo setpoints. Eye frames are cached in PSRAM so blinks run locally. | ESP-IDF v6.1 |

## Prerequisites

| For | You need |
|---|---|
| everything Python | Python 3.11–3.13 and [uv](https://docs.astral.sh/uv/). Scripts declare their own dependencies; `uv run` installs them. |
| twin only | `python3` + PyYAML (`pip install pyyaml`) |
| voice (mic / speaker) | PortAudio: `sudo apt install libportaudio2` |
| a local LLM | [llama.cpp](https://github.com/ggml-org/llama.cpp) `llama serve`, e.g. Gemma 4 E4B |
| Home Assistant | a long-lived access token from your HA profile |
| the real robot | ESP-IDF v6.1, the robot on the same Wi-Fi as the PC |

Configuration lives in **`LLM2/.env`** (copy `LLM2/.env.example`: OpenAI key,
`HA_BASE_URL`, `HA_TOKEN`). It's git-ignored, so never put a token in `.env.example`.

## How to run

Each step adds to the one before. Commands run from `digital_twin/` unless noted.

### 1. The digital twin on its own

```bash
cd digital_twin
python3 twin/run.py              # open http://localhost:8730/
```

Swap shells, play motions, change the eyes, drag the speaker marker. No LLM or
keys needed. The 3D model is committed, so this works straight after cloning.

### 2. Add the real brain (LLM2 + characters)

```bash
# terminal 1
python3 twin/run.py
# terminal 2: pick ONE model
uv run --env-file ../LLM2/.env brain/llm2_brain.py                   # OpenAI (key in .env)
uv run brain/llm2_brain.py --local                                   # local llama.cpp on :9931
#   start one with:  llama serve -hf DuoNeural/Gemma-4-E4B-Q4_K_M:Q4_K_M --port 9931
python3 ../speech/fake_openai.py &                                   # no model: canned replies
OPENAI_BASE_URL=http://127.0.0.1:8790/v1 OPENAI_API_KEY=fake uv run brain/llm2_brain.py
```

Type in the twin's *Utterance* box; the robot answers in character, with eyes
and motion to match.

### 3. Talk to it

```bash
uv run brain/llm2_brain.py --local --mic --voice
```

Hold **Hold to speak** in the twin and talk; release, and the reply is spoken
in the character's voice. Add `--wake` to just say the character's name instead
("Baymax, turn on the light").

### 4. Connect Home Assistant

```bash
uv run --env-file ../LLM2/.env brain/llm2_brain.py --local --mic --voice --ha-device
```

Pebble registers as its own HA device (like the phone app):
- **Devices:** it controls the devices you've exposed to Assist.
- **Live state:** it answers from live states and sensor readings.
- **Reminders:** it reads your to-do lists and calendar, announces due items
  itself, and adds reminders to `todo.reminders`. It never adds medication ones.
- **Messages:** anything sent to `notify.mobile_app_pebble` is spoken in character.

### 5. The real robot

Flash the firmware once (from `firmware/`; the board on USB):

```bash
. ~/.espressif/v6.1/esp-idf/export.sh
idf.py menuconfig                    # "Pebble link": Wi-Fi SSID/password, PC IP
idf.py build && idf.py -p /dev/ttyACM0 flash
```

Then run the twin in robot mode (from `digital_twin/`):

```bash
python3 twin/run.py --robot --robot-motion        # the robot dials in on :9000
uv run --env-file ../LLM2/.env brain/llm2_brain.py --local --mic robot --voice --ha-device
```

The robot's eyes, head and own microphone are now driven exactly as the twin
was. Calibration (display mapping, servo signs and limits, mic slot) is in
`digital_twin/config/robot_hw.yaml`. Leave out `--robot-motion` to keep the
servos still while testing.

## Status

| | |
|---|---|
| Working on the robot | eyes (stored frames), neck servos, robot mic → STT, full brain loop, Home Assistant |
| Not yet | base rotation (not installed), RFID reader (not detected; use the twin's shell buttons), a speaker on the robot (voice plays on the PC) |
| Latency | STT ~1 s; local Gemma reply 4–6 s on a laptop RTX 3060 |
