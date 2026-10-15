# Pebble MCU ↔ Brain protocol

WebSocket, JSON text frames, one message per frame. The MCU is the client in the
real build (it dials out to the brain); in the twin the roles are inverted for
convenience, but **the message set is identical** — the brain service cannot
tell the twin from the physical robot, which is the entire point.

```
ws://<twin-or-robot>:8730/brain
```

Binary frames carry audio. From the robot: its own mic while the user is
speaking (between `vad` true and false), as raw **PCM16 LE, 16 kHz mono**:
the twin's robot mode (`twin/run.py --robot`) forwards the physical robot's
I2S mic this way, and `brain/llm2_brain.py --mic robot` transcribes it. (Opus
would cut the bandwidth ~10x if the link ever needs it.)

---

## MCU → Brain

| `type` | Fields | When |
| --- | --- | --- |
| `hello` | `fw`, `caps[]`, `motions[]`, `expressions[]`, `character_id` | On connect |
| `bearing` | `deg` (−180..180), `conf` (0..1) | ~50 Hz while voice is active |
| `vad` | `speaking` (bool) | Speech start / end |
| `tag` | `uid`, `present`, `character_id` | RFID shell attached or removed |
| `utterance` | `text`, `bearing`, `character_id` | After local STT, or typed in the twin |
| `state` | `joints{}`, `busy` | On request |

`joints` carries `base_yaw`, `head_pitch`, `head_roll` — **head pose**, not servo
angles. There is no head yaw; the neck is a parallel pushrod mechanism and the
MCU converts pose to the two MG995 angles itself (`twin/linkage.py`). The brain
never sees servo angles, so re-calibrating the linkage cannot break it.

`bearing` is in the robot's **body-neutral frame**: 0° is the robot's front,
positive is counter-clockwise seen from above (the robot's left).

The MCU acts on `bearing` **locally** — it does not wait for the brain to tell
it where to look. Gaze is a reflex, not a round trip, so orienting stays
responsive even when the network or the LLM is slow. The brain receives bearings
for logging and context only.

## Brain → MCU

| `type` | Fields | Effect |
| --- | --- | --- |
| `say` | `text`, `dur_ms` | Speak (TTS audio follows as binary in the real build) |
| `motion` | `id`, `speed`?, `amp`? | Play a motion from `motions.yaml`; optional `speed`/`amp` scale it on top of the character profile (clamped 0.7–1.3 / 0.75–1.25) |
| `eyes` | `expression`?, `valence`?, `arousal`? | Emotion as a point on Russell's circumplex: `valence` (−1 unpleasant … +1 pleasant) × `arousal` (−1 sleepy … +1 activated). The eyes are drawn from the point. `expression` is one of `neutral·attentive·concerned·confirming·sleep`, a named preset on the circumplex, for displays that only know names; send both |
| `attend` | `deg` | Override gaze target |
| `profile` | `character{}` | Push an updated/new character (LLM 1 output) |
| `ha` | `service`, `data{}` | Home Assistant call; logged by the twin |
| `listen` | `on` | `true`: stream the mic to the brain continuously (wake-word mode), not only while the user holds *speak*; `false`: back to push-to-talk |
| `heard` | `text` | Transcript of brain-side STT, for display. Twin only: there the laptop mic stands in for the array |

**Unknown `type` values and unknown motion ids are ignored, never fatal.** The
firmware must behave the same way — an LLM that invents a motion name should
produce a no-op, not a crash in front of the class.

---

## Rules that carry over to the firmware

1. **Actions are structured, never prose.** The brain emits `motion`/`eyes`/`ha`
   as discrete messages. When an LLM replaces the stub, constrain it to this
   schema with tool calling or JSON mode. Do not parse motions out of a sentence.
2. **Gaze is local.** See above.
3. **Profiles are cached on the MCU.** A shell swap applies the cached profile
   immediately and tells the brain afterwards. This is what makes the ≤ 1.0 s
   swap target achievable; a network fetch on the critical path would not be.
4. **The brain is optional.** With no brain connected the robot still orients,
   blinks, idles and swaps character. Degrade, don't die.
5. **Pose, never servo angles.** Motions and protocol messages are in head pose.
   Only the layer below the interpolator knows about horns and pushrods.

---

## Minimal brain

```python
import sys; sys.path.insert(0, "twin")
import ws

conn = ws.connect("ws://localhost:8730/brain")
print(conn.recv_json())                     # hello

while True:
    msg = conn.recv_json()
    if msg and msg.get("type") == "utterance":
        conn.send_json({"type": "eyes", "expression": "attentive"})
        conn.send_json({"type": "motion", "id": "ack_nod"})
        conn.send_json({"type": "say", "text": "On it.", "dur_ms": 700})
```

See `examples/brain_stub.py` for the full reference implementation.
