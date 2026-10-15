"""Emotion -> what the robot does: ONE mapping for the brain, the twin and the survey.

An emotion is a point on Russell's circumplex (valence x arousal, each -1..1;
see LLM2/affect.py). The eyes are drawn from the point itself; the head plays
the motion of the point's octant, faster with arousal and bigger with
intensity. Keeping this in one place means the recognition survey
(twin/survey.py) tests exactly what the robot shows in conversation.
"""

import math

# Same directions and neutral radius as LLM2/affect.py. "alert" (90) and
# "depressed" (225) were dropped after the 2026-10-14 survey: each sits between
# two neighbours and people mostly read it as one of them.
OCTANTS = [(0, "happy"), (45, "excited"), (135, "tense"), (180, "sad"),
           (270, "sleepy"), (315, "calm")]
NEUTRAL_RADIUS = 0.3

# One body language per octant (motions.yaml). Tuned on the robot 2026-10-13:
# tense uses recoil at every intensity (shake_no read as refusal, not tension).
OCTANT_MOTION = {
    "neutral": "ack_nod", "happy": "happy_bounce", "excited": "excited_wiggle",
    "tense": "recoil", "sad": "droop",
    "sleepy": "doze", "calm": "sway",
}

# Named eye presets, for displays/firmware that only know names (= twin/mcu.py).
EYE_PRESETS = {"neutral": (0.0, 0.0), "attentive": (0.2, 0.5), "concerned": (-0.5, 0.3),
               "confirming": (0.7, 0.1), "sleep": (0.0, -1.0)}


def label(valence, arousal):
    if math.hypot(valence, arousal) < NEUTRAL_RADIUS:
        return "neutral"
    ang = math.degrees(math.atan2(arousal, valence)) % 360
    return min(OCTANTS, key=lambda o: min(abs(ang - o[0]), 360 - abs(ang - o[0])))[1]


def actions(valence, arousal):
    """(eyes message, motion message) in the robot protocol (docs/protocol.md)."""
    v, a = max(-1.0, min(1.0, valence)), max(-1.0, min(1.0, arousal))
    intensity = min(1.0, math.hypot(v, a))
    expr = min(EYE_PRESETS, key=lambda e: (EYE_PRESETS[e][0] - v) ** 2
               + (EYE_PRESETS[e][1] - a) ** 2)
    eyes = {"type": "eyes", "expression": expr, "valence": round(v, 3), "arousal": round(a, 3)}
    move = {"type": "motion", "id": OCTANT_MOTION[label(v, a)],
            "speed": round(1 + 0.25 * a, 3), "amp": round(0.85 + 0.35 * intensity, 3)}
    return eyes, move


# ---- recognition survey ------------------------------------------------------

# What participants choose from: plain words, one per octant + neutral.
CHOICES = [
    ("happy", "Happy"), ("excited", "Excited"),
    ("tense", "Tense / angry"), ("sad", "Sad / gloomy"),
    ("sleepy", "Sleepy / tired"), ("calm", "Calm / relaxed"), ("neutral", "Neutral"),
]
INTENSITY = {"strong": 0.9, "mild": 0.45}


def stimuli(include_mild=False):
    """Every expression to show: neutral + each direction (strong, optionally mild)."""
    out = [{"id": "neutral", "emotion": "neutral", "intensity": "none",
            "valence": 0.0, "arousal": 0.0}]
    for deg, name in OCTANTS:
        for level in (("strong", "mild") if include_mild else ("strong",)):
            r = INTENSITY[level]
            out.append({"id": f"{level} {name}", "emotion": name, "intensity": level,
                        "valence": round(r * math.cos(math.radians(deg)), 3),
                        "arousal": round(r * math.sin(math.radians(deg)), 3)})
    return out
