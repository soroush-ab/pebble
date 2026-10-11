# 情绪模型：Russell 环形情绪模型（效价 × 唤醒度）。
# Emotion model: Russell's circumplex model of affect (valence x arousal).
#
# Russell, J. A. (1980). A circumplex model of affect. Journal of Personality and
# Social Psychology, 39(6), 1161-1178.
#
# 每种情绪都是二维平面上的一个点 / Every emotion is a point on a plane:
#   valence 效价  -1 不愉快 / unpleasant  ...  +1 愉快 / pleasant
#   arousal 唤醒度 -1 低落/困倦 / deactivated ... +1 激动/警觉 / activated
# 标签由该点的角度得出，强度为到原点的距离。
# The label comes from the point's angle; intensity is its distance from the centre.
from __future__ import annotations

import math
from dataclasses import dataclass

# Russell (1980) 的八个方向（角度，0° = 愉快，逆时针）
# Russell's eight octants (degrees, 0 = pleasant, counter-clockwise)
OCTANTS: list[tuple[float, str]] = [
    (0.0, "happy"),      # 愉快 / pleased, glad
    (45.0, "excited"),   # 兴奋 / excited, delighted
    (90.0, "alert"),     # 警觉 / aroused, astonished
    (135.0, "tense"),    # 紧张 / tense, distressed, angry, afraid
    (180.0, "sad"),      # 难过 / miserable, sad
    (225.0, "depressed"),  # 沮丧 / gloomy, bored
    (270.0, "sleepy"),   # 困倦 / tired, sleepy
    (315.0, "calm"),     # 平静 / relaxed, content, serene
]
LABELS: list[str] = ["neutral"] + [name for _, name in OCTANTS]

# 强度低于此值视为中性 / below this intensity the affect reads as neutral
NEUTRAL_RADIUS = 0.2


def _clamp(x: float) -> float:
    return max(-1.0, min(1.0, float(x)))


@dataclass(frozen=True)
class Affect:
    valence: float = 0.0
    arousal: float = 0.0

    def __post_init__(self):
        object.__setattr__(self, "valence", _clamp(self.valence))
        object.__setattr__(self, "arousal", _clamp(self.arousal))

    @property
    def intensity(self) -> float:
        """到原点的距离，0..1 / Distance from the centre, 0..1."""
        return min(1.0, math.hypot(self.valence, self.arousal))

    @property
    def angle(self) -> float:
        """0..360°，0 = 愉快，90 = 高唤醒 / 0 = pleasant, 90 = activated."""
        return math.degrees(math.atan2(self.arousal, self.valence)) % 360.0

    @property
    def label(self) -> str:
        if self.intensity < NEUTRAL_RADIUS:
            return "neutral"
        a = self.angle
        return min(OCTANTS, key=lambda o: min(abs(a - o[0]), 360 - abs(a - o[0])))[1]

    @classmethod
    def of(cls, label: str) -> "Affect":
        """标签 -> 该方向上的典型点 / A label -> a typical point in that direction."""
        if label == "neutral":
            return cls(0.0, 0.0)
        for deg, name in OCTANTS:
            if name == label:
                r = math.radians(deg)
                return cls(round(0.7 * math.cos(r), 3), round(0.7 * math.sin(r), 3))
        raise KeyError(label)

    def __str__(self) -> str:
        return f"{self.label} (v={self.valence:+.2f}, a={self.arousal:+.2f})"
