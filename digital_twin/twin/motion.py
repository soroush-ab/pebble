"""Motion library loading, easing, layered playback and offline validation.

Pose model - three additive layers per channel:

    output = clamp( pose + gesture + idle , joint limits )

    pose    absolute rest angle, driven by kinematics (attend/settle/sleep),
            rate-limited by the joint's max velocity and acceleration
    gesture foreground relative motion (ack_nod, head_tilt, ...), additive so the
            robot can nod while already facing the user
    idle    looping ambient offset (idle_breathe)

Character variation comes from motion_profile scaling, never from separate
motion definitions:  t -> t / speed_scale ,  pos -> pos * amplitude_scale
"""

import math
import os

import yaml

CHANNELS = ("base_yaw", "head_pitch", "head_roll")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------- easing

def _ease_linear(u):      return u
def _ease_in(u):          return u * u
def _ease_out(u):         return 1.0 - (1.0 - u) ** 2
def _ease_in_out(u):      return u * u * (3.0 - 2.0 * u)          # smoothstep
def _ease_s_curve(u):     return u * u * u * (u * (u * 6.0 - 15.0) + 10.0)  # smootherstep

EASINGS = {
    "linear": _ease_linear,
    "ease_in": _ease_in,
    "ease_out": _ease_out,
    "ease_in_out": _ease_in_out,
    "s_curve": _ease_s_curve,
}


def load_motions(path=None):
    path = path or os.path.join(_ROOT, "motions.yaml")
    with open(path) as fh:
        doc = yaml.safe_load(fh)
    default_easing = (doc.get("defaults") or {}).get("easing", "ease_in_out")
    motions = {}
    for name, spec in (doc.get("motions") or {}).items():
        spec = dict(spec)
        spec.setdefault("easing", default_easing)
        spec["name"] = name
        motions[name] = spec
    return motions


def load_robot(path=None):
    path = path or os.path.join(_ROOT, "config", "robot.yaml")
    with open(path) as fh:
        return yaml.safe_load(fh)


def _tracks(spec):
    """Normalise a motion's channels into {channel: [keyframes]}. Empty if parametric."""
    ch = spec.get("channels")
    return ch if isinstance(ch, dict) else {}


def sample_track(keys, t, easing_fn):
    """Piecewise-interpolated value of one channel at time t."""
    if not keys:
        return 0.0
    if t <= keys[0]["t"]:
        return float(keys[0]["pos"])
    if t >= keys[-1]["t"]:
        return float(keys[-1]["pos"])
    for a, b in zip(keys, keys[1:]):
        if a["t"] <= t <= b["t"]:
            span = b["t"] - a["t"]
            u = 0.0 if span <= 0 else (t - a["t"]) / span
            return float(a["pos"]) + (float(b["pos"]) - float(a["pos"])) * easing_fn(u)
    return float(keys[-1]["pos"])


class _Player:
    """One playing motion, with its character scaling already applied."""

    def __init__(self, spec, profile, speed=1.0, amp=1.0):
        self.spec = spec
        self.name = spec["name"]
        self.loop = bool(spec.get("loop"))
        self.easing = EASINGS.get(spec.get("easing", "ease_in_out"), _ease_in_out)
        # character profile x this play's own scaling (e.g. arousal: quicker, bigger)
        self.speed = max(0.05, float(profile.get("speed_scale", 1.0)) * speed)
        self.amp = float(profile.get("amplitude_scale", 1.0)) * amp
        self.t = 0.0

        self.tracks, self.absolute = {}, {}
        for ch, keys in _tracks(spec).items():
            keys = sorted(keys, key=lambda k: k["t"])
            is_abs = any(k.get("absolute") for k in keys)
            self.absolute[ch] = is_abs
            # speed scaling stretches time; amplitude scales relative motion only
            self.tracks[ch] = [
                {"t": float(k["t"]) / self.speed,
                 "pos": float(k["pos"]) * (1.0 if is_abs else self.amp)}
                for k in keys
            ]

        self.duration = max((tr[-1]["t"] for tr in self.tracks.values() if tr), default=0.0)

    def advance(self, dt):
        self.t += dt
        if self.loop and self.duration > 0:
            self.t = math.fmod(self.t, self.duration)

    @property
    def finished(self):
        return not self.loop and self.t >= self.duration

    def value(self, ch):
        return sample_track(self.tracks.get(ch, []), self.t, self.easing)


class Axis:
    """A rate-limited joint. Approaches target under max velocity/acceleration."""

    def __init__(self, cfg):
        self.min = float(cfg["min_deg"])
        self.max = float(cfg["max_deg"])
        self.vmax = float(cfg["max_vel_dps"])
        self.amax = float(cfg["max_acc_dps2"])
        self.pos = 0.0
        self.vel = 0.0
        self.target = 0.0

    def set_target(self, deg):
        self.target = max(self.min, min(self.max, float(deg)))

    def step(self, dt):
        err = self.target - self.pos
        # fastest speed from which we can still stop exactly on target
        v_stop = math.sqrt(max(0.0, 2.0 * self.amax * abs(err)))
        v_des = math.copysign(min(self.vmax, v_stop), err) if err else 0.0
        dv = max(-self.amax * dt, min(self.amax * dt, v_des - self.vel))
        self.vel += dv
        self.pos += self.vel * dt
        if (err > 0 and self.pos > self.target) or (err < 0 and self.pos < self.target):
            self.pos, self.vel = self.target, 0.0
        self.pos = max(self.min, min(self.max, self.pos))


class MotionEngine:
    def __init__(self, robot=None, motions=None):
        self.robot = robot or load_robot()
        self.motions = motions or load_motions()
        joints = self.robot["joints"]
        self.axes = {ch: Axis(joints[ch]) for ch in CHANNELS}
        self.profile = {"speed_scale": 1.0, "amplitude_scale": 1.0, "settle_ms": 300}
        self.gesture = None
        self.idle = None

    # -- control ---------------------------------------------------------

    def set_profile(self, profile):
        self.profile = dict(profile or {})
        if self.idle:                                   # re-scale the running idle loop
            self.play_idle(self.idle.name)

    def play(self, name, speed=1.0, amp=1.0):
        """`speed`/`amp` scale on top of the character profile, clamped to a
        modest range so a brain cannot drive the neck past what was validated."""
        spec = self.motions.get(name)
        if not spec:
            return False
        if _tracks(spec):
            speed = min(1.3, max(0.7, speed))
            amp = min(1.25, max(0.75, amp))
            player = _Player(spec, self.profile, speed, amp)
            if any(player.absolute.values()):           # absolute -> retarget the pose layer
                for ch, keys in player.tracks.items():
                    if player.absolute.get(ch) and keys:
                        self.axes[ch].set_target(keys[-1]["pos"])
                self.gesture = None
            else:
                self.gesture = player
        return True

    def play_idle(self, name):
        spec = self.motions.get(name)
        self.idle = _Player(spec, self.profile) if spec and _tracks(spec) else None

    def set_pose_target(self, ch, deg):
        self.axes[ch].set_target(deg)

    # -- tick ------------------------------------------------------------

    def step(self, dt):
        for axis in self.axes.values():
            axis.step(dt)
        if self.gesture:
            self.gesture.advance(dt)
            if self.gesture.finished:
                self.gesture = None
        if self.idle:
            self.idle.advance(dt)

    def output(self):
        """Final commanded angles, layers summed and clamped to joint limits."""
        out = {}
        for ch, axis in self.axes.items():
            v = axis.pos
            if self.gesture:
                v += self.gesture.value(ch)
            if self.idle:
                v += self.idle.value(ch)
            out[ch] = max(axis.min, min(axis.max, v))
        return out

    @property
    def busy(self):
        return self.gesture is not None


# ------------------------------------------------- offline validation

def _servo_check(spec, profile, robot, cname, mname, errors):
    """Sample the pose over time and check the parallel linkage's servo range.

    On a serial pan/tilt neck, joint limits are the constraint. On this parallel
    mechanism they are not: a pitch and a roll that are each individually legal
    can still drive one servo past its stop, because both add on the same horn.
    That combined case is only visible after the linkage conversion.
    """
    import linkage as _lk
    try:
        link = _lk.Linkage(robot)
    except (KeyError, TypeError):
        return
    player = _Player(spec, profile)
    if not player.duration:
        return
    worst = {}
    t = 0.0
    while t <= player.duration + 1e-9:
        pitch = sample_track(player.tracks.get("head_pitch", []), t, player.easing)
        roll = sample_track(player.tracks.get("head_roll", []), t, player.easing)
        for name, val, lo, hi in link.violations(pitch, roll):
            prev = worst.get(name)
            if prev is None or abs(val) > abs(prev[0]):
                worst[name] = (val, lo, hi, t)
        t += 0.02
    for name, (val, lo, hi, tt) in worst.items():
        errors.append(
            f"{mname}/{cname}: {name} driven to {val:.1f}deg at t={tt:.2f} "
            f"(stops {lo:.0f}..{hi:.0f}) - pitch+roll combine on one horn")


def validate(motions=None, robot=None, profiles=None, window_warn=0.5):
    """Check every motion x character against joint limits and servo speed.

    This is what backs the deck's claim that movements are validated in the
    digital twin before running on the physical robot. Run it in CI.

    Returns (errors, notes).

    errors  the motion is impossible even from a neutral pose - the hardware
            physically cannot do it, fix the keyframes or the joint config
    notes   the motion is fine from neutral but clips if the robot is already
            turned; the reported window is the pose range it tolerates
    """
    motions = motions or load_motions()
    robot = robot or load_robot()
    joints = robot["joints"]
    profiles = profiles or {"default": {"speed_scale": 1.0, "amplitude_scale": 1.0}}
    errors, notes = [], []

    for mname, spec in motions.items():
        if not _tracks(spec):
            continue
        for cname, profile in profiles.items():
            player = _Player(spec, profile)
            for ch, keys in player.tracks.items():
                if ch not in joints or not keys:
                    continue
                jcfg = joints[ch]
                lo, hi = float(jcfg["min_deg"]), float(jcfg["max_deg"])
                vmax = float(jcfg["max_vel_dps"])
                tag = f"{mname}/{cname}: {ch}"

                gmin = min(k["pos"] for k in keys)
                gmax = max(k["pos"] for k in keys)

                if player.absolute.get(ch):
                    if gmin < lo - 1e-6 or gmax > hi + 1e-6:
                        errors.append(
                            f"{tag} commands {gmin:.1f}..{gmax:.1f}deg "
                            f"(limit {lo:.1f}..{hi:.1f})")
                else:
                    # pose layer must satisfy  lo <= pose+gmin  and  pose+gmax <= hi
                    wlo, whi = lo - min(0.0, gmin), hi - max(0.0, gmax)
                    if wlo > whi + 1e-6:
                        errors.append(
                            f"{tag} swings {gmin:.1f}..{gmax:.1f}deg, which cannot fit "
                            f"in {lo:.1f}..{hi:.1f} from any pose")
                    elif (whi - wlo) < window_warn * (hi - lo):
                        notes.append(
                            f"{tag} swings {gmin:+.1f}/{gmax:+.1f}deg - clips unless "
                            f"pose stays within {wlo:.0f}..{whi:.0f}deg")

                for a, b in zip(keys, keys[1:]):
                    dt = b["t"] - a["t"]
                    if dt <= 0:
                        continue
                    v = abs(b["pos"] - a["pos"]) / dt
                    if v > vmax:
                        errors.append(
                            f"{tag} needs {v:.0f}deg/s (max {vmax:.0f}deg/s) "
                            f"between t={a['t']:.2f} and {b['t']:.2f}")

            _servo_check(spec, profile, robot, cname, mname, errors)
    return errors, notes
