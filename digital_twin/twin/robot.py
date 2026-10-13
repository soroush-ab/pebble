"""Drive the physical Pebble from the twin's MCU logic (`twin/run.py --robot`).

The twin already does everything the robot's firmware is meant to do above the
I/O layer: motion interpolation, gaze, blink, the eye state, shell swaps, the
brain protocol. RobotBridge copies that state to the real hardware through
RobotLink, so the brain (and the browser viewer) cannot tell which they drive:

    eyes   mcu valence/arousal/blink/gaze -> rendered here -> 2x GC9A01
    neck   mcu head pitch/roll -> linkage -> servo A/B setpoints (capped)
    shell  RFID UID -> mcu.attach_shell
    base   not installed: never enabled, always 0

Calibration and safety caps: config/robot_hw.yaml.
"""

import logging
import os
import threading
import time

import numpy as np
import yaml

log = logging.getLogger("pebble.robot")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = 240                                    # panel size
BG = (0x05, 0x08, 0x0C)
BEZEL = (0x1B, 0x22, 0x2C)
STRIP_PX = 14400                           # <= 28.8 kB per blit (firmware RX buffer 72 kB)


def load_hw(path=None):
    with open(path or os.path.join(_ROOT, "config", "robot_hw.yaml")) as fh:
        return yaml.safe_load(fh)


def _hex(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def render_eye(state, inner_on_left, bezel=True):
    """One eye as seen by someone facing the robot: (240, 240, 3) float RGB 0..255.

    A port of drawEye() in twin/web/app.js, so the robot and the viewer show the
    same face. `inner_on_left`: the eye's nose side is on the viewer's left
    (true for the robot's LEFT eye, which the viewer sees on their right).
    `bezel=False` (the robot): no drawn ring and background outside the circle,
    so lid bands are one solid colour the firmware can fill without pixel data;
    the round panel's own edge is the bezel there."""
    v = float(state.get("valence") or 0.0)
    a = float(state.get("arousal") or 0.0)
    openness = 1.0 - float(state.get("blink") or 0.0)
    neg, pos = max(0.0, -v), max(0.0, v)
    iris_r = 52 * float(state.get("iris_scale") or 1.0) * (1 + 0.08 * a) * (1 - 0.15 * neg)
    lid_top = (-0.06 * a if a >= 0 else 0.76 * (-a) ** 1.3) + 0.3 * neg
    lid_bot = 0.12 * max(0.0, -a)
    squint = 0.6 * pos
    tilt = 0.75 * neg * (-1 if a > 0.55 else 1)          # > 0: inner corners up
    c = S / 2
    # robot faces the viewer: gaze toward the robot's left = the viewer's right
    gx = c + float(state.get("gaze") or 0.0) * 26
    # look down when unpleasant or low-energy, a little up when alert
    gy = c + 10 * neg + 8 * max(0.0, -a) - 6 * max(0.0, a)
    pupil_r = iris_r * (0.38 + 0.12 * a)                    # dilates with arousal
    bulge = 40 * pos                                        # smiling lower lid
    sparkle = min(1.0, 3 * pos * max(0.0, a))               # extra glint when excited
    colour = np.array(_hex(state.get("colour") or "#8FD4E8"), np.float32)

    y, x = np.mgrid[0:S, 0:S].astype(np.float32) + 0.5
    img = np.empty((S, S, 3), np.float32)
    img[:] = BG

    d = np.hypot(x - gx, y - gy)
    # glow: radial gradient, alpha 0.8 at 0.2 R fading to 0 at 2.3 R
    t = np.clip((d - 0.2 * iris_r) / (2.1 * iris_r), 0, 1)
    alpha = (0.8 * (1 - t))[..., None] * (d < 2.3 * iris_r)[..., None]
    img = img * (1 - alpha) + colour * alpha
    img[d < iris_r] = colour
    img[d < pupil_r] = (0x06, 0x09, 0x0D)
    hl = np.hypot(x - (gx - 0.3 * iris_r), y - (gy - 0.34 * iris_r)) < 0.21 * iris_r
    img[hl] = img[hl] * 0.15 + 255 * 0.85
    if sparkle > 0.05:
        sp = np.hypot(x - (gx + 0.32 * iris_r), y - (gy + 0.3 * iris_r)) < 0.1 * iris_r
        k = 0.85 * sparkle
        img[sp] = img[sp] * (1 - k) + 255 * k

    # upper lid: everything above a line through (c, top_y), tilted so the
    # inner corner sits higher when tilt > 0
    top_y = S * (lid_top + (1 - openness) * 0.52)
    th = tilt if inner_on_left else -tilt
    img[y * np.cos(th) - (x - c) * np.sin(th) < top_y] = BG       # = canvas rotate(th)
    bot_y = S * (1 - lid_bot - (1 - openness) * 0.48 - squint * 0.3)
    # lower lid: a parabola peaking `bulge` px above bot_y at the centre (= the
    # viewer's quadraticCurveTo with the control point at 2x bulge)
    img[y >= bot_y - bulge * (1 - ((x - c) / c) ** 2)] = BG

    if bezel:
        r = np.hypot(x - c, y - c)
        img[np.abs(r - (c - 4)) <= 4] = BEZEL
        img[r > c] = 0
    return img


# Stored-frame mode (firmware "pebble-bridge 2 frames"): every expression the
# robot can show is rendered here and uploaded once per connection; afterwards
# only frame ids cross Wi-Fi. Continuous affect snaps to the nearest of these
# Russell-circumplex anchors (the same octants as LLM2/affect.py).
def _anchors():
    """Neutral, then every octant at a mild and a strong intensity: 17 faces."""
    import math
    out = [("neutral", 0.0, 0.0)]
    for deg, name in ((0, "happy"), (45, "excited"), (90, "alert"), (135, "tense"),
                      (180, "sad"), (225, "depressed"), (270, "sleepy"), (315, "calm")):
        for level, r in (("mild", 0.45), ("strong", 0.9)):
            out.append((f"{level} {name}", round(r * math.cos(math.radians(deg)), 3),
                        round(r * math.sin(math.radians(deg)), 3)))
    return out


ANCHORS = _anchors()
BLINK_STAGES = (0.0, 0.5)                  # open, half closed (+ the shared closed frame)
CLOSED = (1000, 1001)                      # fully closed is the same for every face


def frame_id(char_i, emo_i, stage, eye):
    return ((char_i * len(ANCHORS) + emo_i) * len(BLINK_STAGES) + stage) * 2 + eye


def nearest_anchor(valence, arousal):
    return min(range(len(ANCHORS)), key=lambda i: (ANCHORS[i][1] - valence) ** 2
               + (ANCHORS[i][2] - arousal) ** 2)


def rle16(panel):
    """Panel pixels (any uint16 dtype) -> RLE16 runs {u16 count LE, colour BE}."""
    flat = panel.astype(">u2").ravel()
    starts = np.concatenate([[0], np.flatnonzero(flat[1:] != flat[:-1]) + 1])
    out = np.empty(starts.size, dtype=[("n", "<u2"), ("c", ">u2")])
    out["n"] = np.diff(np.concatenate([starts, [flat.size]]))
    out["c"] = flat[starts]
    return out.tobytes()


def to_rgb565(img, byteorder="big"):
    q = np.clip(img + 0.5, 0, 255).astype(np.uint16)
    px = ((q[..., 0] >> 3) << 11) | ((q[..., 1] >> 2) << 5) | (q[..., 2] >> 3)
    return px.astype(">u2" if byteorder == "big" else "<u2")


class RobotBridge:
    def __init__(self, mcu, link, hw=None, motion=False):
        self.mcu, self.link = mcu, link
        self.hw = hw or load_hw()
        self.motion_on = motion
        self.stop = threading.Event()
        d = self.hw["displays"]
        self.order = d.get("byteorder", "big")
        self.eyes = {"left": d.get("left_eye", 0), "right": d.get("right_eye", 1)}
        self._last_panel = {}
        self._last_state = None
        self.chars = sorted(mcu.characters)        # stable frame-id order
        self.frames_ready = set()                  # character indices uploaded
        self._shown = None
        self._last_blink = 0.0
        self._stats = [0, 0, time.time()]           # updates, bytes, since
        sv = self.hw["servos"]
        self.cal = {k: (float(sv[k].get("sign", 1)), float(sv[k].get("offset_deg", 0.0)))
                    for k in ("a", "b")}
        self.limit = float(sv.get("limit_deg", 10.0))
        self.swap = bool(sv.get("swap", False))
        self.rate = float(sv.get("max_rate_dps", 60.0))
        self.cmd = {"a": self.cal["a"][1], "b": self.cal["b"][1]}   # start at neutral
        self.unknown_tags = set()
        link.on_rfid = self._on_rfid
        link.on_audio = self._on_audio
        self._mic_on = False                       # streaming the robot mic to the brain
        mic = self.hw.get("mic", {})
        self.mic_slot = 1 if mic.get("channel", "left") == "right" else 0
        self.mic_gain = 10 ** (float(mic.get("gain_db", 0)) / 20)
        self._mic_off_at = None
        link.on_connect = self._on_connect
        self._gen_seen = None

    # ----------------------------------------------------------- events

    def _on_connect(self, hello):
        self._last_panel.clear()                 # a rebooted robot shows garbage: redraw
        gen = self.link.generation
        if self._gen_seen is not None and gen != self._gen_seen and self.motion_on:
            # The ESP32 rebooted. With servos on its own supply that is a
            # brownout; stop moving until a person has looked at it.
            self.motion_on = False
            log.error("robot reconnected (reboot / brownout?) - servo motion DISABLED; "
                      "check the servo power rail, then restart with --robot-motion")
        self._gen_seen = gen
        self.frames_ready = set()                # PSRAM is empty after a reboot
        self._shown = None
        if self.link.has_frames:
            threading.Thread(target=self._upload_frames, args=(gen,), daemon=True,
                             name="frame-upload").start()
        self.link.config(analog=0, digital=0, encoder=1, rfid=1)
        self.link.motion_enable(base=False, servos=True)   # the base is never enabled

    def _on_rfid(self, uid):
        if uid is None:
            return
        with self.mcu.lock:
            ok = self.mcu.attach_shell(uid)
        if ok:
            log.info("RFID %s -> %s", uid, self.mcu.character["display_name"])
        elif uid not in self.unknown_tags:
            self.unknown_tags.add(uid)
            log.warning("RFID %s is not a known shell: set it as rfid_uid in "
                        "characters/<name>.json", uid)

    # -------------------------------------------------------------- mic

    def _on_audio(self, kind, hdr, pcm):
        """Robot's digital mic -> brain, as binary WebSocket frames (PCM16 LE,
        16 kHz mono) while "Hold to speak" is held: the protocol's audio path."""
        if kind != "digital" or not self._mic_on:
            return
        x = np.frombuffer(pcm, dtype="<i2")
        if hdr["n_ch"] > 1:                      # stereo: keep the mic's own slot
            x = x[self.mic_slot::hdr["n_ch"]]
        if self.mic_gain != 1.0:
            x = np.clip(x.astype(np.float32) * self.mic_gain, -32768, 32767).astype("<i2")
        brain = self.mcu.brain
        if brain is not None and not brain.closed:
            brain.send_bytes(x.astype("<i2").tobytes())

    def _mic_tick(self):
        with self.mcu.lock:
            speaking = self.mcu.speaker_active
        now = time.time()
        if speaking and not self._mic_on:
            self._mic_on, self._mic_off_at = True, None
            self.link.config(analog=0, digital=1, encoder=1, rfid=1)
        elif not speaking and self._mic_on:
            # keep going briefly: the end of the sentence is still in flight
            if self._mic_off_at is None:
                self._mic_off_at = now + 0.3
            elif now >= self._mic_off_at:
                self._mic_on, self._mic_off_at = False, None
                self.link.config(analog=0, digital=0, encoder=1, rfid=1)

    # ------------------------------------------------------------- eyes

    def _panel(self, view, which):
        if which == self.eyes["left"]:
            return np.ascontiguousarray(view[:, ::-1].T)
        return np.ascontiguousarray(view[::-1, :].T)

    def _send_dirty(self, which, panel):
        """Send only what changed. Panels are mounted turned 90 degrees, so a
        panel COLUMN is a row of the face: changes are grouped into runs of
        columns (bands of the face, e.g. the strip a lid just uncovered) and
        sent band by band. One big bounding box painted in panel-row strips
        swept sideways across the face ("squares from the side")."""
        last = self._last_panel.get(which)
        diff = np.ones(panel.shape, bool) if last is None else panel != last
        cols = np.nonzero(diff.any(axis=0))[0]
        if cols.size == 0:
            return 0
        runs, start = [], cols[0]
        for prev, cur in zip(cols[:-1], cols[1:]):
            if cur - prev > 6:                       # a real gap: new band
                runs.append((start, prev + 1))
                start = cur
        runs.append((start, cols[-1] + 1))
        sent = 0
        for c0, c1 in runs:
            rows = np.nonzero(diff[:, c0:c1].any(axis=1))[0]
            r0, r1 = int(rows[0]), int(rows[-1]) + 1
            block = panel[r0:r1, c0:c1]
            v = int(block.flat[0])
            if (block == block.flat[0]).all():
                # solid: 10-byte fill. The firmware sends the colour from a
                # little-endian uint16, so swap it to reach the panel high byte first
                ok = self.link.fill(which, int(c0), r0, int(c1 - c0), r1 - r0,
                                    ((v & 0xFF) << 8 | v >> 8) if self.order == "big" else v)
                sent += 10
            else:
                ok = True
                cw = max(1, STRIP_PX // (r1 - r0))
                for c in range(int(c0), int(c1), cw):
                    sub = block[:, c - c0:min(c + cw, c1) - c0]
                    ok = ok and self.link.blit(which, c, r0, sub.shape[1], sub.shape[0],
                                               np.ascontiguousarray(sub).tobytes())
                    sent += sub.nbytes
            if not ok:
                self._last_panel.pop(which, None)
                return sent
        self._last_panel[which] = panel
        self._stats[0] += 1
        self._stats[1] += sent
        return sent

    def _render_pair(self, char, valence, arousal, blink):
        st = self.mcu.characters[char].get("eye_style", {})
        state = {"valence": valence, "arousal": arousal, "blink": blink, "gaze": 0.0,
                 "colour": st.get("colour", "#8FD4E8"), "iris_scale": st.get("iris_scale", 1.0)}
        out = []
        for side, inner_left in (("left", True), ("right", False)):
            view = to_rgb565(render_eye(state, inner_on_left=inner_left, bezel=False), self.order)
            out.append(rle16(self._panel(view, self.eyes[side])))
        return out

    def _upload_frames(self, gen):
        """Render and upload every frame, the fitted character first."""
        t0 = time.time()
        with self.mcu.lock:
            current = (self.mcu.character or {}).get("character_id")
        order = sorted(range(len(self.chars)), key=lambda i: self.chars[i] != current)
        self.link.frame_clear()
        closed = self._render_pair(self.chars[0], 0.0, 0.0, 1.0)
        for eye in (0, 1):
            self.link.frame_store(CLOSED[eye], closed[eye])
        total = sum(map(len, closed))
        for ci in order:
            for ei, (_name, v, a) in enumerate(ANCHORS):
                for si, b in enumerate(BLINK_STAGES):
                    if self.link.generation != gen:
                        return                       # reconnected: a new upload started
                    pair = self._render_pair(self.chars[ci], v, a, b)
                    for eye in (0, 1):
                        self.link.frame_store(frame_id(ci, ei, si, eye), pair[eye])
                        total += len(pair[eye])
            info = self.link.frame_query(timeout=30)
            if info is None or self.link.generation != gen:
                log.warning("frame upload for %s not confirmed", self.chars[ci])
                return
            self.frames_ready.add(ci)
            log.info("eye frames for %s on the robot (%d stored, %.1f MB, %.1f MB PSRAM free, "
                     "%.1f s)", self.chars[ci], info["count"], info["bytes"] / 2 ** 20,
                     info["psram_free"] / 2 ** 20, time.time() - t0)

    def _frames_tick(self, e):
        """Stored-frame mode: name frames, never send pixels."""
        with self.mcu.lock:
            char = (self.mcu.character or {}).get("character_id")
            blink_ms = (self.mcu.character or {}).get("eye_style", {}).get("blink_ms", 200)
        ci = self.chars.index(char) if char in self.chars else -1
        if ci not in self.frames_ready:
            return False
        ei = nearest_anchor(e["valence"], e["arousal"])
        blink = float(e["blink"])
        if blink > self._last_blink + 0.2:       # rose: a blink began (it decays fast)
            # a new blink: closed -> half -> open, timed on the robot
            steps = [CLOSED] + [(frame_id(ci, ei, si, 0), frame_id(ci, ei, si, 1))
                                for si in reversed(range(len(BLINK_STAGES)))]
            self.link.frame_anim(steps, max(20, blink_ms // len(BLINK_STAGES)))
            self._shown = (ci, ei)
        elif (ci, ei) != self._shown:
            self.link.frame_show(frame_id(ci, ei, 0, 0), frame_id(ci, ei, 0, 1))
            self._shown = (ci, ei)
        self._last_blink = blink
        self._last_panel.clear()                 # streamed pixels are stale now
        return True

    def _eyes_tick(self):
        with self.mcu.lock:
            e = self.mcu.frame()["eyes"]
        if self.link.has_frames and self._frames_tick(e):
            return
        state = (round(e["valence"], 2), round(e["arousal"], 2), round(e["blink"], 2),
                 round(e["gaze"], 2), e["colour"], e["iris_scale"])
        if state == self._last_state and self._last_panel:
            return
        self._last_state = state
        for side, inner_left in (("left", True), ("right", False)):
            view = to_rgb565(render_eye(e, inner_on_left=inner_left, bezel=False), self.order)
            self._send_dirty(self.eyes[side], self._panel(view, self.eyes[side]))

    # ----------------------------------------------------------- motion

    def _motion_tick(self, dt):
        with self.mcu.lock:
            out = self.mcu.engine.output()
            sa, sb = self.mcu.linkage.to_servos(out["head_pitch"], out["head_roll"])
        if self.swap:                       # firmware channel A drives the twin's servo_b
            sa, sb = sb, sa
        for k, raw in (("a", sa), ("b", sb)):
            sign, off = self.cal[k]
            target = off + max(-self.limit, min(self.limit, sign * raw))
            step = self.rate * dt
            self.cmd[k] += max(-step, min(step, target - self.cmd[k]))
        self.link.motion_set(0.0, self.cmd["a"], self.cmd["b"])

    def park(self):
        """Ease both servos back to neutral (their offsets)."""
        if not self.link.connected:
            return
        dt = 1.0 / float(self.hw["servos"].get("rate_hz", 25))
        for _ in range(int(2.0 / dt)):
            for k in ("a", "b"):
                off = self.cal[k][1]
                step = self.rate * dt
                self.cmd[k] += max(-step, min(step, off - self.cmd[k]))
            self.link.motion_set(0.0, self.cmd["a"], self.cmd["b"])
            time.sleep(dt)

    # ------------------------------------------------------------- loop

    def run(self):
        eye_dt = 1.0 / float(self.hw["displays"].get("fps", 15))
        mot_dt = 1.0 / float(self.hw["servos"].get("rate_hz", 25))
        next_eye = next_mot = time.time()
        while not self.stop.is_set():
            now = time.time()
            if not self.link.connected:
                time.sleep(0.2)
                next_eye = next_mot = time.time()
                continue
            try:
                self._mic_tick()
                if now >= next_mot:
                    if self.motion_on:
                        self._motion_tick(mot_dt)
                    next_mot += mot_dt
                if now >= next_eye:
                    t0 = time.time()
                    self._eyes_tick()
                    if time.time() - t0 > 2 * eye_dt:
                        log.debug("slow eye update: %.0f ms", (time.time() - t0) * 1000)
                    next_eye = max(next_eye + eye_dt, time.time())
                    if now - self._stats[2] > 30 and self._stats[0]:
                        n, b, since = self._stats
                        log.info("eyes: %d panel updates, %.0f kB/s over %.0f s",
                                 n, b / 1024 / (now - since), now - since)
                        self._stats = [0, 0, now]
            except Exception as e:  # noqa: BLE001 - keep the robot alive
                log.warning("bridge tick failed: %s", e)
            time.sleep(min(0.02, max(0.0, min(next_eye, next_mot) - time.time())))

    def start(self):
        threading.Thread(target=self.run, daemon=True, name="robot-bridge").start()
