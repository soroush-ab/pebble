#!/usr/bin/env python3
"""Reference brain service for Pebble - rule-based, no LLM, no API key.

    python3 twin/run.py              # terminal 1
    python3 examples/brain_stub.py   # terminal 2
    open http://localhost:8730/      # hold "Hold to speak", or type an utterance

Purpose: a working implementation of docs/protocol.md that the real brain can
replace piece by piece. The twin cannot tell this apart from the final service,
and the final service will not be able to tell the twin from the real robot.

Two things here are load-bearing for the real version:

  1. The reply table is keyed by (intent, character). ONE capability, three
     expressions - exactly the structure of the three use cases in the deck.
  2. Responses are a STRUCTURED action set (say + motion + eyes + ha), never
     prose that someone parses afterwards. When this is swapped for an LLM,
     constrain it to emit this same schema. See BUILD.md 2.5.
"""

import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "twin"))

import ws  # noqa: E402

TWIN = os.environ.get("PEBBLE_TWIN", "ws://localhost:8730/brain")

INTENTS = [
    ("lights_on",   r"\b(light|lamp)s?\b.*\b(on|up)\b|\bturn on\b.*\blight"),
    ("lights_off",  r"\b(light|lamp)s?\b.*\b(off|down)\b"),
    ("lonely",      r"\blonel|\bby myself\b|\bno ?one\b|\bsad\b"),
    ("medication",  r"\bmed(ication|icine)?\b|\bpills?\b|\bdose\b"),
    ("sleep",       r"\bcan.?t sleep\b|\binsomnia\b|\bawake\b"),
    ("thanks",      r"\bthank|\bcheers\b|\bappreciate\b"),
]

# (intent, character) -> {say, motion, eyes, ha}
REPLIES = {
    ("lights_on", "baymax"): dict(
        say="Of course. I will use a soft light. Would you like to talk?",
        motion="concern_lean", eyes="concerned",
        ha=("light.turn_on", {"brightness_pct": 25, "kelvin": 2200})),
    ("lights_on", "r2d2"): dict(
        say="Beep-boop.", motion="head_tilt", eyes="attentive",
        ha=("light.turn_on", {"brightness_pct": 30, "kelvin": 2400})),
    ("lights_on", "marvin"): dict(
        say="Lights on. Insomnia remains undefeated.",
        motion="ack_nod", eyes="neutral",
        ha=("light.turn_on", {"brightness_pct": 60})),

    ("lonely", "baymax"): dict(
        say="I am here with you. We can talk, listen to music, or call someone you trust.",
        motion="concern_lean", eyes="concerned"),
    ("lonely", "r2d2"): dict(
        say="Bweep...", motion="head_tilt", eyes="attentive"),
    ("lonely", "marvin"): dict(
        say="Loneliness detected. I can stay, play something, or contact someone.",
        motion="ack_nod", eyes="neutral"),

    ("medication", "baymax"): dict(
        say="Your medication is overdue. Are you feeling okay? I can remind you again.",
        motion="concern_lean", eyes="concerned",
        ha=("notify.persistent", {"message": "medication check-in"})),
    ("medication", "r2d2"): dict(
        say="Medication time. I will check again in fifteen.",
        motion="confirm_nod", eyes="attentive",
        ha=("notify.persistent", {"message": "medication reminder, +15 min"})),
    ("medication", "marvin"): dict(
        say="Medication overdue. Take it now or confirm you are skipping it.",
        motion="ack_nod", eyes="neutral",
        ha=("notify.persistent", {"message": "medication overdue, response logged"})),

    ("sleep", "baymax"): dict(
        say="I am sorry you cannot sleep. Shall I dim the lights and stay nearby?",
        motion="concern_lean", eyes="concerned"),
    ("sleep", "r2d2"): dict(say="Bloop-beep.", motion="head_tilt", eyes="attentive"),
    ("sleep", "marvin"): dict(
        say="Noted. Sleep is overrated anyway.", motion="ack_nod", eyes="neutral"),
}

FALLBACK = {
    "baymax": dict(say="I am not sure I understood. Could you say that again?",
                   motion="head_tilt", eyes="concerned"),
    "r2d2":   dict(say="Bwoop?", motion="shake_no", eyes="attentive"),
    "marvin": dict(say="That is not a thing I do.", motion="shake_no", eyes="neutral"),
}


def classify(text):
    low = text.lower()
    for name, pattern in INTENTS:
        if re.search(pattern, low):
            return name
    return None


def respond(conn, character, text):
    intent = classify(text)
    plan = REPLIES.get((intent, character)) or FALLBACK.get(character, FALLBACK["marvin"])

    t0 = time.time()
    if plan.get("eyes"):
        conn.send_json({"type": "eyes", "expression": plan["eyes"]})
    if plan.get("motion"):
        conn.send_json({"type": "motion", "id": plan["motion"]})
    if plan.get("ha"):
        service, data = plan["ha"]
        conn.send_json({"type": "ha", "service": service, "data": data})
    say = plan["say"]
    conn.send_json({"type": "say", "text": say, "dur_ms": int(55 * len(say))})

    print(f"  [{character}] intent={intent or 'unknown':11} "
          f"-> {plan.get('motion','-'):12} {(time.time()-t0)*1000:5.1f} ms  \"{say[:52]}\"")


def main():
    print(f"connecting to {TWIN} ...")
    while True:
        try:
            conn = ws.connect(TWIN)
        except OSError as exc:
            print(f"  twin not up ({exc}); retrying in 2 s")
            time.sleep(2)
            continue

        character = "baymax"
        try:
            while True:
                msg = conn.recv_json()
                if not msg:
                    continue
                kind = msg.get("type")

                if kind == "hello":
                    character = msg.get("character_id") or character
                    print(f"connected: fw={msg['fw']}  motions={len(msg['motions'])}  "
                          f"shell={character}")
                elif kind == "tag":
                    character = msg.get("character_id", character)
                    print(f"  shell swapped -> {character}")
                    conn.send_json({"type": "eyes", "expression": "attentive"})
                elif kind == "utterance":
                    print(f'  heard: "{msg.get("text","")}"')
                    respond(conn, msg.get("character_id") or character, msg.get("text", ""))
                elif kind == "vad" and msg.get("speaking"):
                    conn.send_json({"type": "eyes", "expression": "attentive"})
                # "bearing" arrives at 50 Hz while speaking; the MCU already acts
                # on it locally, so the brain only needs it for logging.
        except (ws.WSClosed, ConnectionError, OSError):
            print("twin disconnected; reconnecting")
            time.sleep(1.5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
