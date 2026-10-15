# Pebble digital twin — COMPSYS 730 Group 5

Customisable home companion robot. A swappable RFID character shell selects an
LLM personality running on one underlying smart-home assistant.

Soroush Abdollahi · Evren Huang · Ma Luo · Haowei Zheng

| File | What it is |
| --- | --- |
| `BUILD.md` | Engineering plan: architecture, subsystems, bring-up order, risks |
| `docs/protocol.md` | MCU ↔ brain message contract |
| `motions.yaml` | **Shared** motion library — twin and firmware both read this |
| `characters/*.json` | OCEAN profiles + per-character motion scaling |
| `config/robot.yaml` | Joint limits, linkage geometry, mic array |
| `config/urdf_derived.json` | **Generated** from the CAD — do not hand-edit |
| `tools/bake_urdf.py` | Fusion URDF export → twin geometry + meshes |
| `twin/` | Digital twin / MCU emulator |
| `examples/brain_stub.py` | Reference brain service, no LLM required |
| `brain/llm2_brain.py` | **Real brain:** `../LLM2` (+ Piper voices) driving the twin or robot |
| `../LLM1`, `../LLM2`, `../speech` | Character generator · runtime · STT/TTS, the rest of this repo — see their READMEs |

---

## Digital twin

Emulates the MCU so the rest of the team can work before the hardware exists.
**No dependencies beyond PyYAML** — stdlib WebSocket, vendored three.js.

```bash
python3 twin/run.py                 # viewer + MCU emulator on :8730
python3 examples/brain_stub.py      # (optional) reference brain, second terminal
```

Open <http://localhost:8730/>.

### Driving the physical robot

```bash
python3 twin/run.py --robot                  # eyes + RFID; the ESP32 dials in on :9000
python3 twin/run.py --robot --robot-motion   # + neck servos (capped, see below)
uv run --env-file ../LLM2/.env brain/llm2_brain.py --local --mic robot --voice --ha-device
```

The twin's MCU logic drives the real robot (firmware in `../firmware`) through
`twin/robot.py`; the brain and the viewer can't tell the difference.

- **Eyes:** every expression × blink stage × character is rendered here and
  uploaded once per connection into the ESP32's PSRAM (~3 MB, ~10 s;
  the fitted character first, ~4 s). Afterwards only frame ids cross Wi-Fi: a
  blink is one `ANIM` command timed on the robot, so both eyes stay in step.
  Affect snaps to the nearest of 13 faces (neutral + 6 emotions × mild/strong); gaze isn't shown on the robot.
- **Neck:** head pose → linkage → servo setpoints, with per-servo sign, the
  A/B swap, a ±25° cap and a 150°/s limit from `config/robot_hw.yaml`
  (measured on the built robot). A board reboot (brownout) disables motion.
- **Mic:** while *Hold to speak* is held, the robot's INMP441 (Philips I2S,
  left slot) streams to the brain as binary frames; `--mic robot` transcribes it.
- Not yet: the base (not installed), RFID (reader not detected), a speaker on
  the robot (voice plays on the PC).

### Showcase page (for presentations)

Open **http://localhost:8730/showcase** while `twin/run.py` runs (add `--robot
--robot-motion` to drive the real robot). Pick a character, then tap any
emotion (eyes + motion exactly as in conversation; mild/strong; with or without
head movement) or any movement from `motions.yaml`. A large caption names what is
playing, for the audience. **Play tour** runs through every emotion for the
selected character; the 3D twin can be shown on the same page.

### Emotion recognition survey

```bash
python3 twin/run.py --robot --robot-motion      # or plain twin/run.py for the screen condition
# open http://localhost:8730/survey on a laptop/tablet next to the robot
```

Shows each expression (neutral + the 6 emotions: happy, excited, tense, sad,
sleepy, calm; optionally the mild
versions too) in random order, exactly as the brain would show it (eyes +
motion, `twin/expressions.py`). The participant picks what they think it is and
how sure they are. Every finished session is saved to `survey/`:

| File | Contents |
|---|---|
| `responses.csv` | one row per answer: participant, character, condition, shown emotion, valence/arousal, motion, answer, correct, confidence (1-5), response time, replays, comment |
| `report.csv` | per emotion (overall and per character): n, recognised %, mean confidence, mean time, most common confusion, all answers |
| `confusion.csv` | shown × answered counts |

`survey/` is git-ignored: it holds participant data.

### With the real LLM brain (`../LLM2`)

`brain/llm2_brain.py` replaces the stub with the team's LLM2 pipeline, unmodified:
shell swaps select the LLM1 profile from `../LLM1/data/characters.db`, typed
utterances go to LLM2, and its output becomes protocol messages — `tts_text` →
`say`, `emotion` → `eyes` + `motion`, HA calls → Home Assistant (dry-run without
`HA_TOKEN`) and a `ha` event in the twin.

**Emotion is Russell's circumplex** (Russell 1980): LLM2 outputs valence × arousal
(each −1..1) rather than a word. The twin's eyes are drawn continuously from the point:
arousal opens them and dilates the iris, pleasant valence lifts the lower lids, and
unpleasant valence slants the brows (sad or worried when calm, tense when aroused).
The motion is picked by emotion (6 directions on the circle; alert and depressed
were dropped after the survey) and played faster with arousal and bigger with
intensity; the speaking rate follows arousal; HA's `sensor.pebble_emotion` carries
the label plus valence/arousal attributes.

```bash
python3 twin/run.py
uv run --env-file ../LLM2/.env brain/llm2_brain.py           # needs OPENAI_API_KEY

# local model instead of OpenAI (llama.cpp server; Gemma 4 E4B on :9931 here)
uv run brain/llm2_brain.py --local                                # or --local http://host:port/v1

# voice: hold "Hold to speak" in the twin and talk; replies in the character's voice
uv run brain/llm2_brain.py --local --mic --voice                  # needs: apt install libportaudio2

# no API key: canned stand-in model
python3 ../speech/fake_openai.py
OPENAI_BASE_URL=http://127.0.0.1:8790/v1 OPENAI_API_KEY=fake uv run brain/llm2_brain.py
```

`--local` picks up whatever model the server has loaded and switches off
Gemma's hidden "thinking" (~25 s → ~6 s per turn on the laptop 3060). Replies are
forced to English (`--reply-language`), since LLM2's bilingual prompt otherwise
makes small models answer in Chinese.

With `--mic` the laptop mic stands in for the robot's array: the twin's
**Hold to speak** button starts/stops recording, pebble's whisper STT transcribes
it (shown in the twin's event log), and LLM2 answers. `--voice` speaks the reply
with Piper in the character's voice from `../speech/voices.json`. STT
defaults to `base.en` (~0.8 s per sentence on CPU); `--stt-model small.en-q5_1`
is more robust in noise but ~4 s. `--audio-device` picks the mic/speaker.

### Wake word (no button)

```bash
uv run --env-file ../LLM2/.env brain/llm2_brain.py --local --mic robot --voice --ha-device --wake
```

Say the fitted character's name ("Baymax", "Marvin", "R2-D2"/"Artoo") or
"Pebble". On its own, it wakes Pebble up (attentive eyes, perk-up, "Yes?" in
character) and the next thing you say is the request, ended by a pause. In one
breath ("Hey Baymax, turn on the light") it goes straight through. After a reply
you can carry on for 8 s without the name. Speech is cut into utterances by an
adaptive-noise VAD and spotted with the same whisper used for STT
(`brain/wake.py`); anything heard while Pebble itself is talking is ignored.
The robot streams its mic continuously in this mode (`listen` message).
*Hold to speak* still works alongside.

### Pebble as a Home Assistant device

```bash
# HA_BASE_URL and HA_TOKEN (a long-lived token from your HA profile) in ../LLM2/.env
uv run --env-file ../LLM2/.env brain/llm2_brain.py --local --ha-device
```

`--ha-device` registers the robot through HA's mobile_app integration, the same
way the phone app does, so it appears under **Settings → Devices** with:

- **`notify.mobile_app_pebble`**: reminders and messages sent to it reach the
  robot over HA's WebSocket (local push, no cloud) and are spoken in character.
  Use it from any automation, e.g. the medication reminder:
  ```yaml
  action: notify.mobile_app_pebble
  data: {title: Reminder, message: "Evening medication is due"}
  ```
- **`sensor.pebble_character`** / **`sensor.pebble_emotion`**: which shell is
  fitted and how it feels, for automations and dashboards.

The registration is kept in `config/ha_device.json` (git-ignored; the webhook id
in it is a credential), so restarts reuse the same device. Delete the device in
HA and Pebble registers itself again.

**Reminders.** Pebble reads open items from every HA to-do list and the next
24 h of calendar events (refreshed every minute), so "any reminders?" is answered
from HA, never guessed. When an item falls due, Pebble **announces it unprompted**
in character, and repeats every 10 min (3 times max) until it is done. Saying
"I took it" ticks the item off in HA. That is the only change Pebble may make
to a list: complete an open item, never add, delete or reopen. Items overdue by
more than 12 h are not announced, so a restart doesn't replay yesterday.
`--no-reminders` turns the announcements off. A to-do list called `medication`
with due times is all the medication use case needs.

**Adding reminders by voice.** "Remind me to call Mum at 6" / "…in 20 minutes to
check the oven" adds an item to **one** HA to-do list, `todo.reminders` by default
(`--reminder-list`; create it as a Local To-do list named *Reminders*). Pebble is
given the current time with every request so it can work out "tomorrow at 6".
**Medication and other health reminders can't be added by voice.** A misheard
dose or time is real harm, so they're added by a carer in HA. Enforced in code,
not just asked of the model: Pebble may only add to its own list (never
`medication`), anything mentioning medication, pills, doses, insulin etc. is
refused, and so are times in the past or more than a year ahead. Gemma
declines these itself ("a carer or family member needs to add that in Home
Assistant"); the code check is the backstop.

**Entities come from HA, not `ha_entities.json`.** Pebble controls what you've
exposed to voice assistants (**Settings → Voice assistants → Expose**), named
with their area ("Bedroom Lamp (Bedroom)"); if nothing is exposed, everything in
`HA_ALLOWED_DOMAINS`. The list is re-read on every shell swap. `--ha-entities file`
goes back to the JSON file.

Lines typed into the brain's terminal are sent as Home Assistant notifications
(e.g. `Medication is overdue`). Emotion → eyes/motion mapping is
`affect_actions` in `brain/llm2_brain.py`.

- **Shell buttons** simulate an RFID read → personality swap, timed against the ≤ 1.0 s target
- **Bearing / distance** place a speaker; hold **speak** to feed the simulated 3-mic array
- **Motion buttons** play anything in `motions.yaml` with the active character's scaling
- Error readout turns amber past ±15°, the spec from the deck

### What it emulates

| Real hardware | In the twin |
| --- | --- |
| 3-mic array + GCC-PHAT | Bearing with distance-dependent noise (σ ≈ 3° at 1 m) |
| RFID reader | Shell buttons → cached profile load |
| NEMA17 base + parallel neck | Real CAD axes, rate-limited, **pose → servo** via `linkage.py` |
| 2× GC9A01 round displays | Canvas eyes at the CAD display positions |
| Motion interpolator | Same `motions.yaml`, same 50 Hz tick as the firmware target |

### Geometry comes from the CAD

```bash
python3 tools/bake_urdf.py ~/Downloads/Assembly/assembly_1
```

Flattens the 136-link Fusion export into the four bodies that move, bakes their
fixed-transform chains into merged STLs, and writes the real joint axes, origins
and servo limits to `config/urdf_derived.json`. Re-run after any CAD change.

The baked output (`twin/web/meshes/*.stl`, `config/urdf_derived.json`) **is
committed**, so a fresh clone runs the twin with no CAD and no extra step. Only
whoever changes the CAD re-bakes, from their Fusion URDF export, and commits the
new meshes and JSON together.

**The mechanism is a parallel one.** The neck is a universal joint driven by two
MG995 pushrods, so its DOF are **roll** and **pitch** — there is no head yaw, and
all turning comes from the base stepper. Motions are authored in head pose;
`twin/linkage.py` converts to servo angles exactly as the firmware will.

### Screenshot / health-check the viewer headlessly

```bash
python3 tools/shot.py                       # shots/viewer.png + a page probe
python3 tools/shot.py --out shots/x.png \
  --script 'window.__twin.send({type:"play",id:"head_tilt"}); return "ok"' --after 0.6
```

Drives Firefox over the W3C WebDriver REST API using the `geckodriver` that
ships with the Firefox snap — **no pip, no node, stdlib only**. Prints a probe
(page errors, WebGL renderer, joints wired, mesh count) and saves a PNG.

`--script` runs JS in the page and can drive the UI through `window.__twin`
(`send`, `orbit`, `pivots`, `meta`), so a motion can be triggered and captured
mid-flight. Useful for the report's figures as well as for debugging.

### Validate motions against the hardware

```bash
python3 twin/run.py --validate
```

Checks every motion × character against joint limits and servo speed. This is
what backs the deck's claim that movements are validated in simulation before
running on the physical robot — run it in CI.

---

## ROS 2 (optional)

Same engine, same `motions.yaml`, exposed as ROS topics with RViz for
visualisation. Requires ROS 2 (built and tested on *lyrical*).

```bash
python3 tools/gen_ros2.py                      # URDF + meshes from the CAD bake
cd ros2_ws && colcon build --symlink-install
source install/setup.bash
ros2 launch pebble_twin twin.launch.py         # RViz + MCU node
ros2 topic pub --once /pebble/shell std_msgs/String '{data: r2d2}'
ros2 topic pub --once /pebble/motion std_msgs/String '{data: head_tilt}'
```

`mcu_node` also serves the web viewer and `/brain` on :8730, so
`examples/brain_stub.py` drives the ROS robot unmodified. The two stacks share
port 8730 — run one at a time, or pass `serve_web:=false`.

See the ROS 2 appendix in `BUILD.md` for the trade-off against the plain twin.

---

## Why a twin at all

Three people are blocked on hardware that does not exist yet:

- **Ma Luo** develops the brain service against `/brain` — same protocol as the robot
- **Evren** authors motions and sees each character's scaling immediately
- **Haowei** gets keyframe renders for the report without waiting for servos

And it is a hedge: if a servo dies the week of the final presentation, the twin
still demonstrates the full interaction loop.
