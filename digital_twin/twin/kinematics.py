"""Gaze policy for a robot whose only yaw is the base stepper.

The CAD settles a question the earlier model got wrong: there is NO head pan.
The neck gimbal gives pitch and roll only, so every degree of turning toward a
speaker comes from the NEMA17 on the lazy susan - the slowest, loudest actuator
on the robot. A quick head-flick toward a sound is not physically available.

So the per-character difference in *orienting* has to move somewhere else:

    lead_roll_deg   how far the head rolls toward the sound immediately, as an
                    anticipatory "I heard you", while the base is still turning
    lead_pitch_deg  small lift on acquiring a speaker

    R2-D2   large lead roll, fast        - reacts visibly before it turns
    Baymax  small lead roll, slow        - addresses you with the whole body
    Marvin  almost none                  - turns, eventually

The roll lead is what buys back expressiveness the missing yaw took away, and
it costs nothing: the servos are fast even though the stepper is not.
"""

import math


def wrap180(deg):
    """Fold an angle into (-180, 180]."""
    return -((-deg + 180.0) % 360.0 - 180.0)


class GazePolicy:
    def __init__(self, robot):
        j = robot["joints"]
        self.base_min = float(j["base_yaw"]["min_deg"])
        self.base_max = float(j["base_yaw"]["max_deg"])
        self.roll_min = float(j["head_roll"]["min_deg"])
        self.roll_max = float(j["head_roll"]["max_deg"])
        self.pitch_min = float(j["head_pitch"]["min_deg"])
        self.pitch_max = float(j["head_pitch"]["max_deg"])
        self.lead_roll_deg = 8.0
        self.lead_pitch_deg = 2.0
        self.lead_falloff_deg = 50.0

    def set_profile(self, profile):
        p = profile or {}
        self.lead_roll_deg = float(p.get("lead_roll_deg", 8.0))
        self.lead_pitch_deg = float(p.get("lead_pitch_deg", 2.0))

    def targets(self, bearing_deg, base_yaw_now):
        """Bearing in the body-neutral frame -> (base_target, pitch, roll).

        base_target is None when the base should hold position.
        """
        bearing = wrap180(bearing_deg)
        err = wrap180(bearing - base_yaw_now)

        base_target = bearing
        if base_target > self.base_max or base_target < self.base_min:
            # software-limited base (wire twist, BUILD.md 2.2): unwind the long way
            alt = base_target - math.copysign(360.0, base_target)
            if self.base_min <= alt <= self.base_max:
                base_target = alt
            else:
                base_target = max(self.base_min, min(self.base_max, base_target))

        # Anticipatory lead: strongest while the base still has far to travel,
        # decaying to zero as it arrives, so the head settles level.
        frac = max(-1.0, min(1.0, err / self.lead_falloff_deg))
        roll = max(self.roll_min, min(self.roll_max, self.lead_roll_deg * frac))
        pitch = max(self.pitch_min,
                    min(self.pitch_max, self.lead_pitch_deg * abs(frac)))

        if abs(err) < 1.0:
            base_target = None                      # already there; stop stepping
        return base_target, pitch, roll

    def eye_gaze(self, bearing_deg, base_yaw_now):
        """Residual yaw the EYES must cover, normalised to -1..1.

        With no head yaw this carries more weight than it would on a pan/tilt
        neck: between hearing you and finishing the turn, the eyes are the only
        thing that can point at you. Eyes lead by ~100 ms (BUILD.md 2.3).
        """
        residual = wrap180(bearing_deg - base_yaw_now)
        return max(-1.0, min(1.0, residual / 45.0))
