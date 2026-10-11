"""Parallel neck linkage: head pose <-> MG995 servo angles.

The head plate sits on a universal joint and is pushed by two rods from two
servos that rotate about the SAME axis:

        servo_a ---rod---\\                     /---rod--- servo_b
                          [ gimbal: pitch + roll ]
                                   |
                              head + displays

    both servos same direction  -> PITCH   (lever arm = pitch_arm_m)
    servos opposite directions  -> ROLL    (lever arm = roll_arm_m)

Small-angle model, linear about neutral:

    servo_a = k_pitch * pitch + k_roll * roll
    servo_b = k_pitch * pitch - k_roll * roll

This is a LINEARISATION. The true mapping is a closed-chain problem with
pushrod-length and ball-joint constraints, and it goes nonlinear toward the
extremes of travel. It is accurate enough to drive the twin and to catch
servo-range violations; calibrate `horn_radius_m` and the arm lengths against
the built mechanism before trusting it near the limits.

Motions are authored in HEAD POSE (pitch/roll). The firmware will do exactly
this conversion before writing servo positions, so the twin and the robot stay
driven by the same `motions.yaml`.
"""


class Linkage:
    def __init__(self, robot):
        cfg = robot["linkage"]
        self.k_pitch = cfg["pitch_arm_m"] / cfg["horn_radius_m"]
        self.k_roll = cfg["roll_arm_m"] / cfg["horn_radius_m"]
        self.a_min, self.a_max = cfg["servo_a"]["min_deg"], cfg["servo_a"]["max_deg"]
        self.b_min, self.b_max = cfg["servo_b"]["min_deg"], cfg["servo_b"]["max_deg"]

    def to_servos(self, pitch_deg, roll_deg):
        """Head pose -> (servo_a, servo_b) in degrees."""
        p = self.k_pitch * pitch_deg
        r = self.k_roll * roll_deg
        return p + r, p - r

    def to_pose(self, servo_a, servo_b):
        """Inverse: servo angles -> (pitch, roll). Useful for reading telemetry."""
        return ((servo_a + servo_b) / 2.0 / self.k_pitch,
                (servo_a - servo_b) / 2.0 / self.k_roll)

    def violations(self, pitch_deg, roll_deg):
        """Which servos this pose would drive past their mechanical stops."""
        a, b = self.to_servos(pitch_deg, roll_deg)
        out = []
        if not (self.a_min <= a <= self.a_max):
            out.append(("servo_a", a, self.a_min, self.a_max))
        if not (self.b_min <= b <= self.b_max):
            out.append(("servo_b", b, self.b_min, self.b_max))
        return out

    def headroom(self):
        """Largest symmetric pitch/roll envelope that never exceeds a servo stop."""
        lim = min(abs(self.a_min), self.a_max, abs(self.b_min), self.b_max)
        return {"pitch_only_deg": lim / self.k_pitch,
                "roll_only_deg": lim / self.k_roll,
                "servo_limit_deg": lim}
