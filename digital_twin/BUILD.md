# Pebble — Build Plan (Engineering)

> `TODO.md` tracks course deliverables. **This file is the build.**

---

## 0 · The architecture decision that gates everything

The MCU is the brain of the **body**, not of the **cognition**. It cannot run STT, an LLM, or neural TTS.

```
┌─────────────────── ROBOT (MCU) ────────────────────┐      ┌──── BRAIN (companion service) ────┐
│  3-mic array ──→ SSL (GCC-PHAT) ──→ bearing        │      │                                    │
│  precision mic ──→ VAD + opus encode ──────────────┼─ws──→│  STT → LLM2 (personality) → TTS    │
│                                                     │      │         ↓                          │
│  neck 2×servo  ←── motion interpolator ←───────────┼←─ws──┤  action schema + TTS audio         │
│  base stepper  ←──                                  │      │         ↓                          │
│  2× round display ←── eye renderer                  │      │  Home Assistant REST/WS           │
│  RFID ──→ shell tag id ─────────────────────────────┼─ws──→│  character DB ← LLM1 (generator)  │
└─────────────────────────────────────────────────────┘      └────────────────────────────────────┘
```

**Where the brain runs:** a laptop or a Pi on the same LAN. For the demo, a laptop is fine and far easier to debug. Say "companion compute" in the report — it's an honest and defensible product architecture, not a compromise.

- [ ] **Confirm the MCU part.** This plan assumes **ESP32-S3** (wifi, PSRAM, 2× SPI for the displays, I2S TDM for a synchronous 3-mic array). If you're on STM32 or RP2040 you lose wifi and need an ESP32 as a network co-processor — tell me and I'll rework §2–§4.
- [ ] **Decide where the brain runs** (laptop vs Pi) and pin its LAN address.
- [ ] **Update the architecture slide** to show the MCU/brain split explicitly.

---

## 1 · Interface contracts — freeze these first

Four people can't work in parallel until these exist. **Write them in week 1 of the build, before any subsystem is finished.** They are also excellent report figures.

- [ ] **`protocol.md`** — MCU ↔ brain WebSocket.
  - Up: `audio` (binary Opus frames), `bearing {deg, confidence}`, `tag {uid}`, `state {...}`
  - Down: `say {audio}`, `motion {id, params}`, `eyes {expression}`, `profile {json}`
- [ ] **`motions.yaml`** — the motion library, one definition shared by firmware *and* the digital twin.
  ```yaml
  ack_nod:
    channels: [neck_tilt]
    keyframes: [{t: 0.00, pos:   0},
                {t: 0.25, pos: -12},
                {t: 0.50, pos:   0}]
    easing: ease_in_out
  ```
- [ ] **`characters/*.json`** — OCEAN traits + tone + behaviour rules + **motion profile overrides**:
  ```json
  "motion_profile": { "speed_scale": 0.6, "amplitude_scale": 1.3, "settle_ms": 400 }
  ```
  Baymax slow/large/settled · R2-D2 fast/small/chirpy · Marvin minimal/slow-return.
  **This is the mechanism that makes one motion library read as three characters.** It's your clearest design contribution — build it in from the start, don't bolt it on.

---

## 2 · Subsystem bring-up (parallel, bench-first)

Each item has an acceptance test. Don't integrate a subsystem until it passes standalone.

### 2.1 Audio + speaker localisation — *Soroush*
- [ ] **Use TDM digital mics (e.g. ICS-52000), not 3 separate I2S/analog mics.** SSL needs sample-synchronous capture; TDM on one bus gives it for free. Three independent capture paths will drift and silently destroy your bearing estimate.
- [ ] Sample at **48 kHz, not 16 kHz.** Math: mic spacing *d* = 10 cm → max inter-mic delay = d/c = 291 µs. At 16 kHz that's only 4.7 samples across the whole 180° range — far too coarse for ±15°. At 48 kHz it's 14 samples, and with GCC-PHAT plus parabolic peak interpolation (~0.2 sample precision) ±15° is comfortably achievable.
- [ ] **Make the triangle as large as the body allows** — angular precision scales directly with baseline. 10 cm minimum.
- [ ] GCC-PHAT across all 3 pairs → least-squares bearing + confidence. Three pairs matters: each pair is insensitive near its own endfire, and the other two cover it.
- [ ] Reject bearings below a confidence threshold rather than turning to noise.
- [ ] **Acceptance:** ≤ ±15° error at 1 m, 2 m, 3 m × 8 bearings, in a normally furnished room. Log every trial — this table goes straight into the report.
- [ ] Precision head mic on its own path for ASR (VAD → Opus → stream). Keep it separate from the array: array for *where*, head mic for *what*.

### 2.2 Neck (2 DOF) + base stepper — *Soroush*

> ⚠️ **The CAD changed this section.** `tools/bake_urdf.py` on the Fusion export
> shows the neck is a **parallel** mechanism, not a serial pan/tilt: a universal
> joint driven by two MG995 pushrods. Its two DOF are **roll** (about the
> forward axis) and **pitch** (about the lateral axis). **There is no head yaw.**
> Every degree of turning toward a speaker comes from the NEMA17 — the slowest,
> loudest actuator on the robot. A quick head-flick toward a sound is not
> physically available, so the per-character orienting difference moved to
> `lead_roll_deg` (see `twin/kinematics.py`).

- [ ] **Stepper driver: TMC2209 in StealthChop.** Non-negotiable — a standard A4988 is loud, and the stepper sits centimetres from the mic array in a bedroom at night. Noise here wrecks both SSL and the product story.
- [ ] **Solve wire twist before building the base.** Continuous rotation twists the loom and eventually tears it out. Either fit a slip ring, or software-limit to ±170° with an unwind move. Decide now; it's structural.
- [ ] Home the stepper against a hall or optical endstop at boot — it's open-loop and has no idea where it is.
- [ ] Generate steps from a hardware timer / MCPWM / RMT, never from a FreeRTOS task loop.
- [ ] **Motion interpolator at 50–100 Hz** with S-curve easing and per-motion `speed_scale` / `amplitude_scale`. The easing *is* the character design — if firmware only does "go to angle", every character moves identically and §1's motion profiles do nothing.
- [ ] **Implement the linkage conversion in firmware.** Motions are authored in head pose (pitch/roll); the servos need angles. `twin/linkage.py` has the small-angle differential model — port it, don't reinvent it, or the twin stops predicting the robot.
- [ ] **Calibrate the linkage.** `horn_radius_m`, `pitch_arm_m` and `roll_arm_m` in `config/robot.yaml` are CAD estimates. Measure commanded servo angle vs actual head pitch/roll on the built mechanism and fit them, then re-run `--validate`. Until this is done the servo-range output is indicative only.
- [ ] **Orienting is stepper-only.** Budget for it: at 90°/s a 90° turn takes a full second, during which the only things that can react are the eyes and the roll lead. Tune `lead_roll_deg` per character against how it actually reads.
- [ ] **Acceptance:** commanded vs measured angle within tolerance; full orient-to-speaker under 1.5 s; no audible stepper whine at 1 m.

### 2.3 Eyes — 2× round display — *Evren + Haowei*
- [ ] GC9A01 240×240 round SPI. Put them on **separate SPI hosts** (ESP32-S3 has SPI2 and SPI3) so both can update concurrently.
- [ ] Budget: 240×240×16bpp = 115 KB/frame/eye. With PSRAM framebuffers and DMA at 40–80 MHz you'll get ~25–30 fps. Plenty — but **update only the dirty rect** (the iris moves, the sclera doesn't) or you'll burn the whole SPI budget on static pixels.
- [ ] Expression set: neutral, attentive, concerned, confirming, sleep, plus blink and saccade as procedural overlays.
- [ ] **Gaze must track the SSL bearing** — eyes lead the neck by ~100 ms. This single detail does more for perceived aliveness than anything else on the robot.
- [ ] **Acceptance:** ≥ 20 fps sustained on both eyes while the neck is moving.

### 2.4 Shell identification (RFID) — *Soroush*
- [ ] MFRC522 or PN532, reader in the head, tag in each shell. Check read range through your actual shell material before committing to the mounting.
- [ ] Poll at ~5 Hz. RFID has no presence event — detect **removal** as N consecutive failed reads, and debounce so a marginal read doesn't thrash the personality.
- [ ] **Cache all character profiles on the MCU/brain at boot.** Your ≤ 1.0 s swap target is easy if the profile is local and impossible to guarantee if it needs a network fetch at swap time.
- [ ] Give the swap a visible transition — eyes blink/fade, a settling motion in the new character's profile. A hard cut looks like a bug; a transition looks like a personality arriving.
- [ ] **Acceptance:** 20/20 swaps detected, each under 1.0 s tag-contact → new character's first behaviour.

### 2.5 Brain service — *Ma Luo*
- [ ] WebSocket server implementing `protocol.md`; stub it on day one so the MCU team has something to talk to.
- [ ] STT (streaming) → **LLM2** with the active character profile → structured output: `{speech, motion, eyes, ha_call}` → TTS.
- [ ] **Constrain the LLM to emit the action schema** (tool/function calling or JSON mode). Never parse motions out of prose — it will fail live, in front of the class.
- [ ] **LLM1** (character generator): concept text → OCEAN JSON → character DB keyed by tag UID. This is the "add a personality by describing it" objective; it's a separate offline path, not in the latency loop.
- [ ] Home Assistant REST/WS for lights, heating, doors, appliances + reminder notifications.
- [ ] **Log every turn**: timestamp, character, transcript, action, and per-stage latency. This is your objective results data, free.
- [ ] **Acceptance:** end-to-end ≤ 2.5 s p50 measured from the logs; HA execution ≥ 98% over 50 calls.

### 2.6 Digital twin — *Haowei*  ✅ built — see `twin/`, `README.md`
- [x] Consumes **the same `motions.yaml`** as the firmware.
- [x] Real joint limits + base/neck yaw coupling, 50 Hz tick.
- [x] Emulates the MCU on `/brain`, so the brain service can be developed against it.
- [x] `python3 twin/run.py --validate` checks every motion × character against limits.
- [ ] Batch-render keyframe strips per character — the §2 visualisations the report needs.
- [ ] Re-tune `config/robot.yaml` once the real joint limits and mic baseline are measured.
- [ ] **Acceptance:** a motion that passes in sim runs on hardware without hitting a limit.

---

## 3 · Integration order

1. **Power first.** Separate rails for servos (5–6 V), stepper (12 V), logic (3.3 V); common ground; bulk caps at the motors. Brown-out under stepper inrush is the single most common way projects like this die, and it presents as random MCU reboots you'll waste a week chasing.
2. Firmware task skeleton + the protocol stub — real tasks, fake data.
3. Eyes + motion (the robot looks alive but is deaf).
4. Mic array + SSL → orient-to-speaker (the robot looks at you). **This is your first demoable milestone — get here early.**
5. RFID + profile swap (three characters, different idle and orient behaviour).
6. Brain service in the loop (it now listens, answers, and controls the house).
7. Benchmarks against the four thresholds, then freeze for the user study.

---

## 4 · Risks with specific mitigations

| Risk | Mitigation |
| --- | --- |
| 3 mics sampled non-synchronously → SSL never works | TDM on one bus (ICS-52000); verify with a known-bearing source before building anything on top |
| 16 kHz sampling too coarse for ±15° | 48 kHz + GCC-PHAT sub-sample interpolation |
| Stepper/servo noise into the mic array | TMC2209 StealthChop; suppress SSL during motion; head mic for ASR |
| Base rotation shears the wiring loom | Slip ring, or ±170° software limit with unwind |
| No head yaw: slow, audible orienting | Eyes + roll lead cover the gap while the base turns; keep the stepper quiet (TMC2209) |
| Linkage model wrong near the limits | Calibrate (§2.2); the model is linear and the mechanism is not |
| `shake_no` cannot be a yaw shake | Re-choreographed as a roll wobble — verify it reads as refusal in the user study |
| Servo brown-out reboots the MCU | Separate rails + bulk caps, checked at step 1 |
| LLM emits prose instead of actions | Schema-constrained output; firmware ignores unknown action ids |
| Wifi round-trip blows the 2.5 s budget | Stream audio during speech, don't wait for end-of-utterance; measure early |
| All three characters move identically | `motion_profile` overrides built into the interpolator from day one, not retrofitted |

---

## 5 · What to build this week

- [ ] Confirm the MCU part and where the brain runs (§0)
- [ ] Write `protocol.md`, `motions.yaml`, one `characters/*.json` (§1)
- [ ] Order long-lead parts: TDM mics, TMC2209, GC9A01 ×2, slip ring
- [ ] Power rails on the bench, measured under stepper load
- [ ] Brain service stub answering the WebSocket so firmware work can start


---

## Appendix · ROS 2 — built, and when to use it

Both stacks now exist and share one engine. This records the trade-off, because
it will come up in Q&A.

**What was built** (`ros2_ws/`, ROS 2 lyrical):

| | |
| --- | --- |
| `pebble_description` | URDF + meshes, **generated** by `tools/gen_ros2.py` from the CAD bake, plus an RViz config |
| `pebble_twin` | `mcu_node` — the same `PebbleMCU`, exposed as ROS topics *and* still serving the web viewer and `/brain` on :8730 |

```bash
cd ros2_ws && colcon build --symlink-install
source install/setup.bash
ros2 launch pebble_twin twin.launch.py            # RViz + node + robot_state_publisher
```

Topics: `/joint_states` (drives TF → RViz), `/pebble/servo_states`, `/pebble/bearing`,
`/pebble/character`, `/pebble/eye_markers`; commands on `/pebble/{motion,eyes,shell,attend,speaker,utterance}`.

**Nothing forked.** `pebble_twin.repo` locates the repo and imports the existing
`motion.py`, `linkage.py`, `kinematics.py` and `mcu.py`, so `motions.yaml` stays
the single source of motion truth and `examples/brain_stub.py` drives the ROS
robot unmodified.

**A URDF works here after all** — not despite the closed chain, but because the
baked model is already a serial tree. The pushrod linkage is abstracted *below*
the pitch/roll joints, exactly as the firmware treats it, so there is nothing
for URDF to fail to express. The pushrods are frozen into the yaw body and do
not animate; that is stated in the generated URDF.

**What ROS 2 gives you:** RViz, TF, `ros2 bag` recording for the user study,
`ros2 topic` introspection, and a path to Gazebo physics later.

**What it does not change:** the firmware target is still an ESP32, and ROS 2
does not run there. The MCU/brain split in §0 stands, and the ROS node is a
development and visualisation surface, not the robot's architecture. The web
twin remains the zero-install path for teammates who have not installed ROS 2 —
it needs only Python and PyYAML.

**Both bind :8730**, so run one at a time (or pass `serve_web:=false` to the
launch file, or `--port` to the standalone twin).
