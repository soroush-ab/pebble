"""Pebble MCU emulator.

Speaks docs/protocol.md on the /brain WebSocket exactly as the real ESP32-S3
firmware will, and streams 50 Hz pose frames to the browser on /viewer.

The point is that the brain service cannot tell the twin from the real robot.
Everything the firmware owns is simulated here: the 3-mic array bearing, the
RFID shell reader, the motion interpolator and the joint rate limits.
"""

import json
import os
import random
import threading
import time

import kinematics
import linkage
import motion

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FW_VERSION = "twin-0.1"

EYE_EXPRESSIONS = ("neutral", "attentive", "concerned", "confirming", "sleep")

# The eyes are drawn from a point on Russell's circumplex (valence x arousal,
# each -1..1). The named expressions are presets on it, so older brains and the
# firmware can keep sending names.
EYE_PRESETS = {
    "neutral":    (0.0, 0.0),
    "attentive":  (0.2, 0.5),
    "concerned":  (-0.5, 0.3),
    "confirming": (0.7, 0.1),
    "sleep":      (0.0, -1.0),
}


def _unit(x, default=0.0):
    try:
        return max(-1.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return default


def nearest_expression(valence, arousal):
    return min(EYE_PRESETS, key=lambda e: (EYE_PRESETS[e][0] - valence) ** 2
               + (EYE_PRESETS[e][1] - arousal) ** 2)


def load_characters():
    """Local profile cache, keyed by RFID uid. Mirrors the firmware's boot cache."""
    chars, by_uid = {}, {}
    cdir = os.path.join(_ROOT, "characters")
    for fn in sorted(os.listdir(cdir)):
        if fn.endswith(".json"):
            with open(os.path.join(cdir, fn)) as fh:
                c = json.load(fh)
            chars[c["character_id"]] = c
            by_uid[c["rfid_uid"]] = c
    return chars, by_uid


class PebbleMCU:
    def __init__(self):
        self.robot = motion.load_robot()
        self.motions = motion.load_motions()
        self.characters, self.by_uid = load_characters()

        self.engine = motion.MotionEngine(self.robot, self.motions)
        self.gaze = kinematics.GazePolicy(self.robot)
        self.linkage = linkage.Linkage(self.robot)

        self.tick_hz = float(self.robot["control"]["tick_hz"])
        self.lock = threading.Lock()

        # simulated world
        self.speaker_deg = 35.0
        self.speaker_dist = 2.0
        self.speaker_active = False

        # robot state
        self.character = None
        self.tag_uid = None
        self.attend_deg = None
        self.eyes = "neutral"
        self.valence, self.arousal = EYE_PRESETS["neutral"]
        self.eye_gaze = 0.0
        self.blink = 0.0
        self._next_blink = time.time() + 3.0
        self.speaking_until = 0.0
        self.last_say = ""
        self.events = []
        self.swap_started = None
        self.last_swap_ms = None

        self.brain = None
        self.viewers = []

        self.engine.play_idle("idle_breathe")
        self.attach_shell("04A1B2C3D4E580")      # boot with Baymax fitted

    # ---------------------------------------------------------- helpers

    def set_eyes(self, expression=None, valence=None, arousal=None):
        """A Russell point wins; a name sets its preset. Returns False if neither."""
        if valence is not None or arousal is not None:
            self.valence, self.arousal = _unit(valence), _unit(arousal)
            self.eyes = nearest_expression(self.valence, self.arousal)
            return True
        if expression in EYE_PRESETS:
            self.eyes = expression
            self.valence, self.arousal = EYE_PRESETS[expression]
            return True
        return False

    def log(self, kind, text):
        self.events.append({"t": time.time(), "kind": kind, "text": text})
        del self.events[:-40]

    def to_brain(self, msg):
        if self.brain and not self.brain.closed:
            self.brain.send_json(msg)

    # ------------------------------------------------------- simulation

    def simulate_bearing(self):
        """3-mic GCC-PHAT bearing with distance-dependent error.

        Error model is tuned to the BUILD.md 2.1 budget: 48 kHz, 10 cm baseline,
        sub-sample interpolation. ~3 deg sigma at 1 m degrading with range, so
        the +/-15 deg spec sits at roughly 2 sigma at 3 m.
        """
        sigma = 3.0 * (max(0.3, self.speaker_dist) ** 0.7)
        deg = kinematics.wrap180(self.speaker_deg + random.gauss(0.0, sigma))
        conf = max(0.05, min(0.99, 1.0 - self.speaker_dist / 5.0 + random.gauss(0, 0.05)))
        return deg, conf

    def attach_shell(self, uid):
        """RFID read -> load the cached profile immediately (the <1.0 s fast path)."""
        char = self.by_uid.get(uid)
        if not char:
            return False
        self.swap_started = time.time()
        self.tag_uid = uid
        self.character = char
        prof = char["motion_profile"]
        self.engine.set_profile(prof)
        self.gaze.set_profile(prof)
        self.engine.play_idle(char.get("idle_motion", "idle_breathe"))
        self.set_eyes(char["eye_style"].get("base_expression", "neutral"))
        self.engine.play("wake")
        self.last_swap_ms = (time.time() - self.swap_started) * 1000.0
        self.log("shell", f"{char['display_name']} fitted ({self.last_swap_ms:.0f} ms)")
        self.to_brain({"type": "tag", "uid": uid, "present": True,
                       "character_id": char["character_id"]})
        return True

    # ------------------------------------------------------------ tick

    def step(self, dt):
        now = time.time()

        if self.speaker_active:
            deg, conf = self.simulate_bearing()
            if conf >= 0.35:
                self.attend_deg = deg
            self.to_brain({"type": "bearing", "deg": round(deg, 2), "conf": round(conf, 3)})

        if self.attend_deg is not None:
            base_now = self.engine.axes["base_yaw"].pos
            base_t, pitch_t, roll_t = self.gaze.targets(self.attend_deg, base_now)
            if base_t is not None:
                self.engine.set_pose_target("base_yaw", base_t)
            self.engine.set_pose_target("head_pitch", pitch_t)
            self.engine.set_pose_target("head_roll", roll_t)
            self.eye_gaze = self.gaze.eye_gaze(self.attend_deg, base_now)

        self.engine.step(dt)

        style = (self.character or {}).get("eye_style", {})
        lo, hi = style.get("blink_interval_s", [4.0, 7.0])
        blink_ms = style.get("blink_ms", 200)
        if now >= self._next_blink:
            self.blink = 1.0
            self._next_blink = now + random.uniform(lo, hi) + blink_ms / 1000.0
        elif self.blink > 0.0:
            self.blink = max(0.0, self.blink - dt / (blink_ms / 1000.0))

        if self.speaking_until and now > self.speaking_until:
            self.speaking_until = 0.0

    def frame(self):
        out = self.engine.output()
        style = (self.character or {}).get("eye_style", {})
        sa, sb = self.linkage.to_servos(out["head_pitch"], out["head_roll"])
        return {
            "type": "frame",
            "t": time.time(),
            "joints": {k: round(v, 3) for k, v in out.items()},
            "servos": {"a": round(sa, 2), "b": round(sb, 2),
                       "over": [v[0] for v in self.linkage.violations(
                           out["head_pitch"], out["head_roll"])]},
            "targets": {k: round(a.target, 3) for k, a in self.engine.axes.items()},
            "eyes": {
                "expression": self.eyes,
                "valence": round(self.valence, 3),
                "arousal": round(self.arousal, 3),
                "gaze": round(self.eye_gaze, 3),
                "blink": round(self.blink, 3),
                "colour": style.get("colour", "#8FD4E8"),
                "iris_scale": style.get("iris_scale", 1.0),
            },
            "character": (self.character or {}).get("character_id"),
            "character_name": (self.character or {}).get("display_name", "-"),
            "speaker": {"deg": self.speaker_deg, "dist": self.speaker_dist,
                        "active": self.speaker_active},
            "speaking": self.speaking_until > time.time(),
            "say": self.last_say,
            "busy": self.engine.busy,
            "brain": bool(self.brain and not self.brain.closed),
            "swap_ms": round(self.last_swap_ms, 1) if self.last_swap_ms else None,
            "events": self.events[-8:],
        }

    def broadcast(self, frame=None):
        """Push one pose frame to every connected viewer.

        Call this from whatever is driving step(): the standalone run() loop, or
        the ROS 2 node's timer. Anything that steps the MCU without broadcasting
        leaves the web viewer connected but frozen.
        """
        payload = json.dumps(frame or self.frame(), separators=(",", ":"))
        for v in list(self.viewers):
            if v.closed:
                self.viewers.remove(v)
            else:
                v.send_text(payload)

    def run(self, stop_evt):
        dt = 1.0 / self.tick_hz
        nxt = time.time()
        while not stop_evt.is_set():
            with self.lock:
                self.step(dt)
                self.broadcast()
            nxt += dt
            time.sleep(max(0.0, nxt - time.time()))

    # -------------------------------------------------- inbound: brain

    def on_brain(self, msg):
        kind = msg.get("type")
        with self.lock:
            if kind == "motion":
                try:
                    speed, amp = float(msg.get("speed", 1.0)), float(msg.get("amp", 1.0))
                except (TypeError, ValueError):
                    speed = amp = 1.0
                ok = self.engine.play(msg.get("id", ""), speed, amp)
                self.log("motion", f"{msg.get('id')}{'' if ok else ' (unknown, ignored)'}")
            elif kind == "eyes":
                if self.set_eyes(msg.get("expression"), msg.get("valence"),
                                 msg.get("arousal")):
                    self.log("eyes", f"{self.eyes} (v {self.valence:+.2f}, "
                                     f"a {self.arousal:+.2f})")
            elif kind == "say":
                text = msg.get("text", "")
                self.last_say = text
                self.speaking_until = time.time() + msg.get("dur_ms", 60 * len(text)) / 1000.0
                self.log("say", text[:70])
            elif kind == "attend":
                self.attend_deg = float(msg.get("deg", 0.0))
                self.log("attend", f"{self.attend_deg:.0f} deg")
            elif kind == "profile":
                char = msg.get("character") or {}
                cid = char.get("character_id")
                if cid:
                    self.characters[cid] = char
                    if char.get("rfid_uid"):
                        self.by_uid[char["rfid_uid"]] = char
                    self.log("profile", f"{cid} updated by brain")
            elif kind == "ha":
                self.log("ha", f"{msg.get('service')} {json.dumps(msg.get('data', {}))}")
            elif kind == "heard":
                # brain-side STT (twin only: the real MCU transcribes locally)
                self.log("user", msg.get("text", "")[:70])

    def on_brain_connect(self, conn):
        self.brain = conn
        self.log("brain", f"connected from {conn.addr[0]}")
        conn.send_json({
            "type": "hello", "fw": FW_VERSION,
            "caps": ["bearing", "tag", "motion", "eyes", "say", "attend", "ha"],
            "motions": sorted(self.motions),
            "expressions": list(EYE_EXPRESSIONS),
            "character_id": (self.character or {}).get("character_id"),
        })

    # ------------------------------------------------- inbound: viewer

    def on_viewer(self, msg):
        kind = msg.get("type")
        with self.lock:
            if kind == "speaker":
                self.speaker_deg = float(msg.get("deg", self.speaker_deg))
                self.speaker_dist = float(msg.get("dist", self.speaker_dist))
            elif kind == "speaking":
                self.speaker_active = bool(msg.get("on"))
                if not self.speaker_active:
                    self.to_brain({"type": "vad", "speaking": False})
                else:
                    self.to_brain({"type": "vad", "speaking": True})
            elif kind == "shell":
                char = self.characters.get(msg.get("id", ""))
                if char:
                    self.attach_shell(char["rfid_uid"])
            elif kind == "play":
                self.engine.play(msg.get("id", ""))
                self.log("motion", f"{msg.get('id')} (manual)")
            elif kind == "eyes":
                self.set_eyes(msg.get("expression"))
            elif kind == "utter":
                text = msg.get("text", "")
                self.log("user", text)
                self.to_brain({"type": "utterance", "text": text,
                               "bearing": self.attend_deg,
                               "character_id": (self.character or {}).get("character_id")})
            elif kind == "home":
                self.attend_deg = None
                self.engine.play("settle")
