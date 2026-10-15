"""Wake word: say the character's name instead of holding "Hold to speak".

    listener = Segmenter(rate)          # continuous audio -> utterances
    for seg in listener.feed(samples):  # float32 mono at `rate`
        ...                             # transcribe, then WakeController.on_text

Two parts:

Segmenter   an energy voice-activity detector with an adaptive noise floor.
            Splits continuous audio into utterances (speech, then ~0.7 s of
            silence), with a little pre-roll so first syllables aren't clipped.

WakeController  what to do with each transcribed utterance:
              idle       ignore it unless it contains the active character's name
                         ("Baymax", "Marvin", "R2-D2"/"Artoo", or "Pebble").
                         "Baymax, turn on the light" -> a request straight away;
                         "Baymax?" alone -> wake up and wait for the request.
              awake      the next utterance is the request (name optional),
                         for up to AWAKE_S seconds.
              follow-up  after a reply, carry on talking for FOLLOW_UP_S
                         seconds without the name.
            Speech captured while the robot itself is talking is dropped, so it
            can't hear its own name in its own voice.

Spotting the name with whisper (already loaded for STT) needs no extra model or
account and works for any character name; it costs one transcription per
utterance heard near the robot.
"""

import difflib
import re
import time
from collections import deque

import numpy as np

# Spoken forms whisper produces for each character (lower case, spaced words).
ALIASES = {
    "baymax": ["baymax", "bay max", "beymax", "bae max", "baymaxx", "maymax", "baymacks",
               "bay macks", "bay mags"],
    "marvin": ["marvin", "marv", "marvyn"],
    "r2d2": ["r2d2", "r2 d2", "r2", "artoo", "artoo detoo", "r two", "r two d two",
             "are two", "arturo"],
}
UNIVERSAL = ["pebble"]                # the robot's own name works with any shell
FILLERS = {"hey", "hi", "hello", "ok", "okay", "oh", "um", "uh", "yo", "excuse", "me"}


def normalise(text):
    t = text.lower().replace("-", " ").replace("2-d2", "2d2")
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return " ".join(t.split())


def match(text, aliases):
    """Return the request after the name if `text` addresses one of `aliases`,
    else None. "" means the name alone (wake up and wait)."""
    words = normalise(text).split()
    best = None
    for alias in aliases:
        a = alias.split()
        for i in range(len(words) - len(a) + 1):
            chunk = words[i:i + len(a)]
            exact = chunk == a
            fuzzy = (len(a) == 1 and len(chunk[0]) >= 4
                     and difflib.SequenceMatcher(None, chunk[0], a[0]).ratio() >= 0.8)
            if exact or fuzzy:
                # only count it as addressing the robot near the start, after fillers
                if all(w in FILLERS for w in words[:i]) and (best is None or i < best[0]):
                    best = (i, i + len(a))
    if best is None:
        return None
    return " ".join(words[best[1]:])


class Segmenter:
    def __init__(self, rate, frame_ms=30, start_db=9.0, stop_db=5.0, min_speech_s=0.3,
                 end_silence_s=0.5, max_s=15.0, pre_roll_s=0.3, partial_s=1.0):
        self.rate = rate
        self.n = int(rate * frame_ms / 1000)
        self.start_k = 10 ** (start_db / 10)          # energy ratios over the floor
        self.stop_k = 10 ** (stop_db / 10)
        self.min_frames = int(min_speech_s * 1000 / frame_ms)
        self.end_frames = int(end_silence_s * 1000 / frame_ms)
        self.max_frames = int(max_s * 1000 / frame_ms)
        # an early look at long utterances, so "Baymax, ..." wakes the robot
        # while the person is still talking
        self.partial_frames = int(partial_s * 1000 / frame_ms)
        self.sent_partial = False
        self.pre = deque(maxlen=int(pre_roll_s * 1000 / frame_ms))
        self.floor = None
        self.idle_e = deque(maxlen=int(3000 / frame_ms))     # last 3 s of non-speech
        self.buf = np.zeros(0, np.float32)
        self.seg, self.loud, self.quiet, self.hot = [], 0, 0, 0
        self.t_start = None

    def feed(self, x):
        """float32 mono samples in; yields ("partial", audio, t_start, now) once
        after `partial_s` of speech, then ("final", audio, t_start, t_end)."""
        self.buf = np.concatenate([self.buf, np.asarray(x, np.float32)])
        while self.buf.size >= self.n:
            f, self.buf = self.buf[:self.n], self.buf[self.n:]
            f = f - f.mean()
            e = float(np.mean(f * f)) + 1e-12
            now = time.time()
            if not self.seg:
                # noise floor = median energy of recent non-speech. (A running minimum
                # sat at the quietest frames, so ordinary noise looked like speech and
                # an utterance never ended.)
                if self.floor is None or e < self.floor * self.start_k:
                    self.idle_e.append(e)
                    self.floor = float(np.median(self.idle_e))
                self.pre.append(f)
                self.hot = self.hot + 1 if e > self.floor * self.start_k else 0
                if self.hot >= 3:                         # 90 ms of clear speech
                    self.seg = list(self.pre)
                    self.t_start = now - len(self.seg) * self.n / self.rate
                    self.loud, self.quiet = self.hot, 0
                    self.sent_partial = False
                continue
            self.seg.append(f)
            if not self.sent_partial and self.loud >= self.partial_frames:
                self.sent_partial = True
                yield "partial", np.concatenate(self.seg), self.t_start, now
            if e > self.floor * self.stop_k:
                self.loud += 1
                self.quiet = 0
            else:
                self.quiet += 1
            if self.quiet >= self.end_frames or len(self.seg) >= self.max_frames:
                audio = np.concatenate(self.seg)
                loud = self.loud
                self.seg, self.hot, self.pre = [], 0, deque(maxlen=self.pre.maxlen)
                if loud >= self.min_frames:
                    yield "final", audio, self.t_start, now


class WakeController:
    AWAKE_S = 8.0         # wait this long for the request after "Baymax?"
    FOLLOW_UP_S = 8.0     # talk on without the name for this long after a reply
    ECHO_GUARD_S = 0.4    # ignore speech this soon after the robot stops talking

    def __init__(self):
        self.state, self.until = "idle", 0.0
        self.speaking = (0.0, 0.0)                    # robot's own speech interval

    def robot_speaking(self, start, end):
        self.speaking = (start, end)

    def heard_robot(self, t_start, t_end):
        s, e = self.speaking
        return t_start < e + self.ECHO_GUARD_S and t_end > s

    def replied(self):
        """After the robot answers: allow follow-ups without the name."""
        self.state = "follow-up"
        self.until = max(time.time(), self.speaking[1]) + self.FOLLOW_UP_S

    def on_text(self, text, aliases):
        """-> ("wake", None) | ("request", text) | ("ignore", reason)."""
        now = time.time()
        if self.state != "idle" and now > self.until:
            self.state = "idle"
        req = match(text, aliases)
        if self.state == "idle":
            if req is None:
                return "ignore", "no wake word"
            if len(req.split()) >= 2:                  # "Baymax, turn on the light"
                return "request", req
            self.state, self.until = "awake", now + self.AWAKE_S
            return "wake", None
        # awake or follow-up: everything is for us
        if req == "":                                  # just the name again: keep waiting
            self.state, self.until = "awake", now + self.AWAKE_S
            return "wake", None
        return "request", req if req is not None else text
