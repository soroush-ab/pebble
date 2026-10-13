# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["openai>=1.40", "pydantic>=2", "httpx>=0.27", "websockets>=13",
#                 "pywhispercpp>=1.3", "piper-tts>=1.3", "numpy", "sounddevice"]
# ///
"""Pebble brain: the real LLM2 pipeline (LLM2) driving the twin or robot.

    python3 twin/run.py                                          # terminal 1
    uv run --env-file ../LLM2/.env brain/llm2_brain.py       # terminal 2
    open http://localhost:8730/   # swap shells, type an utterance

Drop-in replacement for examples/brain_stub.py. It speaks docs/protocol.md, so
it cannot tell the twin from the physical robot. LLM2 is used as-is:

  shell `tag`   -> pipeline.set_active_tag(<tag of that character in LLM1's DB>)
  `utterance`   -> pipeline.handle(source="stt")
  LLM2 affect   -> TwinFace   -> `eyes` (valence, arousal) + `motion`
                   (Russell circumplex; see affect_actions)
  LLM2 tts_text -> TwinSpeaker -> `say` (+ Piper audio with --voice)
  LLM2 HA calls -> real HomeAssistantClient (dry-run without HA_TOKEN),
                   mirrored to the robot as `ha` so the twin logs them

Voice (../speech, all local; needs PortAudio: apt install libportaudio2):
    uv run brain/llm2_brain.py --local --mic --voice
  --mic    the laptop mic stands in for the robot's array: hold "Hold to speak"
           in the twin, talk, release -> whisper STT -> LLM2
  --voice  replies are spoken by Piper in the active character's voice

Home Assistant (HA_BASE_URL + HA_TOKEN in ../LLM2/.env):
    uv run --env-file ../LLM2/.env brain/llm2_brain.py --local --ha-device
  The robot registers as its own HA device (like the phone app): reminders
  sent to notify.mobile_app_pebble are spoken in character, and it reports
  its character/emotion as sensors. Controllable entities come from HA - the
  ones exposed to voice assistants - instead of LLM2/ha_entities.json.

Lines typed into this terminal are sent to LLM2 as Home Assistant
notifications (e.g. "Medication is overdue") - the reminder use case.

Local model instead of OpenAI (llama.cpp server, e.g. Gemma on :9931):
    uv run brain/llm2_brain.py --local
    uv run brain/llm2_brain.py --local http://127.0.0.1:8080/v1

No model at all? Run the canned stand-in first:
    python3 ../speech/fake_openai.py
    OPENAI_BASE_URL=http://127.0.0.1:8790/v1 OPENAI_API_KEY=fake \\
        uv run brain/llm2_brain.py
"""

import argparse
import json
import logging
import os
import queue
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent       # pebble/digital_twin
log = logging.getLogger("pebble.brain")
PEBBLE = ROOT.parent                                  # pebble/ (LLM1, LLM2, speech)
sys.path.insert(0, str(ROOT / "twin"))
sys.path.insert(0, str(PEBBLE / "LLM2"))

import ws  # noqa: E402
from adapters import Face, HomeAssistantClient, Speaker  # noqa: E402
from pipeline import LLM2Pipeline, NoActiveCharacter  # noqa: E402
from schemas import LLM2Input  # noqa: E402
from settings import load_settings  # noqa: E402

TWIN = os.environ.get("PEBBLE_TWIN", "ws://localhost:8730/brain")
LOCAL_LLM = os.environ.get("PEBBLE_LOCAL_LLM", "http://127.0.0.1:9931/v1")

# LLM2 emits a point on Russell's circumplex (valence x arousal, -1..1). The
# eyes get the point itself (the twin draws it continuously; `expression` is the
# nearest named preset, for firmware that only knows names). The motion is picked
# by the octant and scaled by the point: arousal -> faster, intensity -> bigger.
# On top of that the MCU applies the character's own scaling (characters/*.json),
# so the same nod reads slow and large on Baymax, small and quick on R2-D2.
OCTANT_MOTION = {                  # one body language per octant (motions.yaml)
    "neutral":   "ack_nod",
    "happy":     "happy_bounce",
    "excited":   "excited_wiggle",
    "alert":     "perk_up",
    "tense":     "recoil",           # very unpleasant + very aroused: shake_no
    "sad":       "droop",
    "depressed": "sag",
    "sleepy":    "doze",
    "calm":      "sway",
}
# Head gestures the model may ask for (robot.gesture), described in words a
# small model maps requests onto. Only those the robot reports in `hello`.
GESTURE_HINTS = {
    "ack_nod": "one nod: yes / I heard you", "confirm_nod": "double nod: done, agreed",
    "head_tilt": "curious head tilt", "concern_lean": "lean in, concerned",
    "shake_no": "shake: no", "scan_room": "look around the room",
    "settle": "settle back to rest", "wake": "wake up", "sleep": "go to sleep",
    "happy_bounce": "happy little bounce", "excited_wiggle": "excited wiggle",
    "perk_up": "perk up, attentive", "recoil": "startled pull back",
    "droop": "sad droop", "sag": "gloomy sag", "doze": "nod off sleepily",
    "sway": "calm, gentle sway",
}

EYE_PRESETS = {"neutral": (0.0, 0.0), "attentive": (0.2, 0.5), "concerned": (-0.5, 0.3),
               "confirming": (0.7, 0.1), "sleep": (0.0, -1.0)}       # = twin/mcu.py


def affect_actions(affect):
    """Russell point -> (eyes message, motion message)."""
    v, a = affect.valence, affect.arousal
    expr = min(EYE_PRESETS, key=lambda e: (EYE_PRESETS[e][0] - v) ** 2
               + (EYE_PRESETS[e][1] - a) ** 2)
    motion = OCTANT_MOTION[affect.label]
    if affect.label == "tense" and v < -0.5 and a > 0.6:
        motion = "shake_no"
    eyes = {"type": "eyes", "expression": expr, "valence": round(v, 3), "arousal": round(a, 3)}
    move = {"type": "motion", "id": motion, "speed": round(1 + 0.25 * a, 3),
            "amp": round(0.85 + 0.35 * affect.intensity, 3)}
    return eyes, move


class Link:
    """The current robot connection; swapped on reconnect, None while down."""

    def __init__(self):
        self.conn = None

    def send(self, msg):
        c = self.conn
        if c is not None and not c.closed:
            c.send_json(msg)


class TwinFace(Face):
    def __init__(self, link, device=None):
        self.link, self.device = link, device

    skip_motion = False         # a requested gesture replaces the emotion's motion

    def show_emotion(self, affect):
        if self.device:
            threading.Thread(target=self.device.update_sensor, daemon=True, args=(
                "emotion", affect.label,
                {"valence": affect.valence, "arousal": affect.arousal,
                 "intensity": round(affect.intensity, 3), "model": "Russell circumplex"},
            )).start()
        eyes, move = affect_actions(affect)
        self.link.send(eyes)
        if not self.skip_motion:
            self.link.send(move)


class TwinSpeaker(Speaker):
    """Sends `say`; with Piper, also plays the audio and reports its real length."""

    def __init__(self, link, piper=None):
        self.link, self.piper = link, piper

    def speak(self, text, affect):
        if self.piper is None:
            self.link.send({"type": "say", "text": text, "dur_ms": int(55 * len(text))})
            return
        from audio_io import play
        character_id, extraversion = self.piper._active_voice()
        audio, rate = self.piper.tts.synthesize(text, character_id, extraversion,
                                                affect.arousal)
        self.link.send({"type": "say", "text": text, "dur_ms": int(1000 * len(audio) / rate)})
        play(audio, rate, self.piper.device)


class TwinHA(HomeAssistantClient):
    """LLM2's real HA client, with each accepted call mirrored to the robot."""

    def __init__(self, link, settings, entities):
        super().__init__(settings.ha_base_url, settings.ha_token,
                         settings.ha_allowed_domains, frozenset(entities),
                         settings.ha_timeout)
        self.link = link
        self.failed = 0     # calls refused or failed in the current turn

    strict = False      # True: only listed entities, even when the list is empty
    device = None       # HADevice, for ticking reminders off

    def complete_reminder(self, action):
        """The one write Pebble may make to a reminder list: mark an open item
        completed ("I took it"). Not delete, rename, add, or reopen."""
        try:
            data = json.loads(action.data_json or "{}")
        except ValueError:
            data = {}
        item = str(data.get("item", "")).strip()
        # Models copy the whole reminder line ("take medication (due Sun 22:53) -
        # note"), so match an open item on that list whose text it contains.
        hits = [i for i in self.device.items
                if i["done_ok"] and i["list"] == action.entity_id
                and (i["uid"] == item or i["summary"].lower() in item.lower())]
        hit = max(hits, key=lambda i: len(i["summary"])) if hits else None
        if data.get("status", "completed") != "completed" or hit is None:
            log.warning("refused reminder update: %s %s", action.entity_id, data)
            return False
        if self.dry_run:
            print(f"[HA dry-run] todo.update_item {action.entity_id} "
                  f"{hit['summary']!r} -> completed")
        else:
            self.device.complete_item(action.entity_id, hit["summary"])
            print(f"[HA] reminder done: {hit['list_name']}: {hit['summary']}")
        self.link.send({"type": "ha", "service": "todo.update_item",
                        "data": {"entity_id": action.entity_id, "item": hit["summary"],
                                 "status": "completed"}})
        return True

    MAX_DELAY_S = 6 * 3600
    reminder_list = None    # the one list Pebble may add to (e.g. todo.reminders)

    # Health reminders are a carer's job, set by a person in HA, never by voice
    # through a language model: a misheard dose or time is a real-world harm.
    # This is the code-side backstop; the prompt asks the model not to try.
    MEDICAL = re.compile(
        r"\b(medic\w*|medicine\w*|pills?|pils|tablets?|capsules?|dos(e|es|age|ing)|"
        r"insulin|inhaler\w*|prescri\w*|drugs?|antibiotic\w*|paracetamol|panadol|"
        r"ibuprofen|nurofen|aspirin|vitamins?|supplements?|injections?|jabs?|"
        r"\d+\s*(mg|mcg|ml|units?)|eye ?drops?|ointment|patch(es)?|treatment|therapy|"
        r"blood (sugar|pressure)|glucose)\b", re.I)

    def add_reminder(self, action):
        """todo.add_item on Pebble's own list only, never health reminders."""
        from datetime import datetime, timedelta
        data = self._data(action) or {}
        item = str(data.get("item", "")).strip()
        note = str(data.get("description", "")).strip()
        if not self.reminder_list or action.entity_id != self.reminder_list:
            log.warning("refused: Pebble only adds reminders to %s, not %s",
                        self.reminder_list, action.entity_id)
            return False
        if not item:
            log.warning("refused reminder without text: %s", data)
            return False
        if self.MEDICAL.search(f"{item} {note}"):
            log.warning("refused health reminder %r: a carer adds these in Home Assistant",
                        item)
            return False
        now = datetime.now().astimezone()
        due = None
        try:
            if data.get("in_minutes") not in (None, ""):
                due = now + timedelta(minutes=float(data["in_minutes"]))
            elif data.get("due"):
                due = datetime.fromisoformat(str(data["due"]).strip().replace("T", " "))
                due = due if due.tzinfo else due.astimezone()
        except (TypeError, ValueError):
            log.warning("refused reminder with unreadable time: %s", data)
            return False
        if due is not None and not (now - timedelta(minutes=1) <= due
                                    <= now + timedelta(days=366)):
            log.warning("refused reminder at %s: not within the next year", due)
            return False
        when = f" at {due:%a %d %b %H:%M}" if due else ""
        if self.dry_run:
            print(f"[HA dry-run] todo.add_item {action.entity_id} {item!r}{when}")
        else:
            self.device.add_item(action.entity_id, item, due, note)
            print(f"[HA] reminder added: {item!r}{when}")
        self.link.send({"type": "ha", "service": "todo.add_item",
                        "data": {"entity_id": action.entity_id, "item": item,
                                 **({"due": due.isoformat(timespec="minutes")} if due else {})}})
        return True

    @staticmethod
    def _data(action):
        """data_json as a dict. Small models write Python dicts ({'a': 1}) as
        often as JSON; accept both rather than failing the user's request."""
        import ast
        raw = (action.data_json or "").strip() or "{}"
        try:
            data = json.loads(raw)
        except ValueError:
            try:
                data = ast.literal_eval(raw)
            except (ValueError, SyntaxError):
                # last resort, for output like '{} "delay_s": 5': salvage key: value pairs
                pairs = re.findall(r"""["']?(\w+)["']?\s*:\s*("[^"]*"|'[^']*'|-?[\d.]+|true|false)""",
                                   raw)
                if not pairs:
                    return None
                data = {}
                for k, v in pairs:
                    try:
                        data[k] = json.loads(v.replace("'", '"'))
                    except ValueError:
                        return None
        return data if isinstance(data, dict) else None

    def execute(self, action):
        if action.service == "todo.update_item" and self.device is not None:
            ok = self.complete_reminder(action)
            self.failed += not ok
            return ok
        if action.service == "todo.add_item" and self.device is not None:
            ok = self.add_reminder(action)
            self.failed += not ok
            return ok
        # Models like HA's generic homeassistant.turn_off. Map it to the entity's
        # own domain so the domain allowlist still judges the actual device.
        domain, _, service = action.service.partition(".")
        if domain == "homeassistant" and "." in action.entity_id:
            action = action.model_copy(update={
                "service": f"{action.entity_id.split('.', 1)[0]}.{service}"})
        data = self._data(action)
        delay = 0.0
        if data is not None:
            try:
                delay = float(data.pop("delay_s", 0) or 0)
            except (TypeError, ValueError):
                delay = 0.0
            action = action.model_copy(update={"data_json": json.dumps(data)})
        if data is None:
            log.warning("unreadable data_json: %r", action.data_json)
            ok = False
        elif self.strict and action.entity_id not in self.allowed_entities:
            log.warning("entity not exposed to Pebble in HA: %s", action.entity_id)
            ok = False
        elif delay > 0:
            # "turn it off, and back on in 5 seconds": checked now, run later
            if action.service.partition(".")[0] not in self.allowed_domains:
                log.warning("domain not allowed: %s", action.service)
                ok = False
            else:
                delay = min(delay, self.MAX_DELAY_S)
                t = threading.Timer(delay, self._run_later, args=(action,))
                t.daemon = True
                t.start()
                print(f"[HA] {action.service} -> {action.entity_id} in {delay:g} s")
                return True
        else:
            ok = super().execute(action)
        if ok:
            data = {**json.loads(action.data_json or "{}"), "entity_id": action.entity_id}
            self.link.send({"type": "ha", "service": action.service, "data": data})
        else:
            self.failed += 1
        return ok


    def _run_later(self, action):
        if not self.execute(action):
            log.warning("delayed %s on %s failed", action.service, action.entity_id)
            self.link.send({"type": "eyes", "expression": "concerned",
                            "valence": -0.5, "arousal": 0.3})


class RobotMic:
    """The robot's own mic: PCM16 LE 16 kHz mono arriving as binary frames
    between `vad` start and stop. Same start/stop shape as MicRecorder."""

    rate = 16000
    TAIL_S = 0.4          # audio still in flight when the stop message arrives

    def __init__(self):
        self.chunks, self.active = [], False

    def start(self):
        self.chunks, self.active = [], True

    def feed(self, data):
        if self.active:
            self.chunks.append(data)

    def stop(self):
        if not self.active:
            return None
        time.sleep(self.TAIL_S)
        self.active = False
        import numpy as np
        pcm = np.frombuffer(b"".join(self.chunks), dtype="<i2").astype(np.float32) / 32768
        dump = os.environ.get("PEBBLE_MIC_DUMP")          # debugging: keep each capture
        if dump and pcm.size:
            import wave
            os.makedirs(dump, exist_ok=True)
            with wave.open(os.path.join(dump, f"robot_mic_{int(time.time())}.wav"), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(self.rate)
                w.writeframes(b"".join(self.chunks))
        if pcm.size:
            pcm -= pcm.mean()                  # MEMS mics carry a DC offset
            log.info("robot mic: %.1f s, rms %.4f, peak %.3f", pcm.size / self.rate,
                     float(np.sqrt(np.mean(pcm ** 2))), float(np.abs(pcm).max()))
        return pcm, self.rate


class MicRecorder:
    """Records from this machine's mic between `vad` start and stop.

    In the real robot the MCU streams audio from its own array; in the twin the
    "Hold to speak" button sends the same vad messages and the laptop mic
    supplies the audio. Same start/stop shape as pebble's record_push_to_talk,
    but driven by the robot instead of the Enter key."""

    MAX_SECONDS = 30

    def __init__(self, device=None):
        import sounddevice as sd
        from audio_io import _device
        self.sd, self.device = sd, _device(device)
        self.rate = int(sd.query_devices(self.device, "input")["default_samplerate"])
        self.stream, self.chunks = None, []

    def _callback(self, indata, frames, time_info, status):
        if sum(len(c) for c in self.chunks) < self.MAX_SECONDS * self.rate:
            self.chunks.append(indata[:, 0].copy())

    def start(self):
        if self.stream is not None:
            return
        self.chunks = []
        self.stream = self.sd.InputStream(samplerate=self.rate, channels=1, dtype="float32",
                                          device=self.device, callback=self._callback)
        self.stream.start()

    def stop(self):
        """Returns (audio, sample_rate), or None if not recording."""
        if self.stream is None:
            return None
        self.stream.stop()
        self.stream.close()
        self.stream = None
        import numpy as np
        audio = np.concatenate(self.chunks) if self.chunks else np.zeros(0, np.float32)
        return audio, self.rate


def use_local_model(url):
    """Point LLM2 at a local OpenAI-compatible server (llama.cpp `llama serve`).

    LLM2 reads its endpoint from the environment, so this only has to set the
    variables before load_settings(). The model name is whatever the server has
    loaded, unless PEBBLE_LOCAL_MODEL says otherwise (LLM2_MODEL is ignored: in
    LLM2/.env it names an OpenAI model)."""
    import urllib.request
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/models", timeout=5) as r:
            models = [m["id"] for m in json.load(r).get("data", [])]
    except OSError as exc:
        sys.exit(f"no local model server at {url} ({getattr(exc, 'reason', exc)}).\n"
                 f"Start it first, e.g.: llama serve -hf ggml-org/gemma-4-E4B-it-GGUF:Q8_0 "
                 f"--port 9931")
    if not models:
        sys.exit(f"local server {url} has no model loaded")
    os.environ["OPENAI_BASE_URL"] = url
    os.environ.setdefault("OPENAI_API_KEY", "local")     # llama.cpp ignores it
    os.environ["LLM2_MODEL"] = os.environ.get("PEBBLE_LOCAL_MODEL") or models[0]


def tune_for_local(client):
    """Small local models (Gemma 4 E4B) open a hidden "thought" channel on their
    own even with thinking off in the chat template: ~300 extra tokens, ~20 s
    per turn on a laptop GPU. A zero thinking budget stops that; the token cap
    stops a runaway reply from holding the robot hostage."""
    create = client.chat.completions.create

    def fast_create(**kw):
        kw.setdefault("max_tokens", 400)
        # repeat_penalty: Gemma E4B sometimes loops ("I am here. I am here. ...")
        kw["extra_body"] = {"thinking_budget_tokens": 0, "repeat_penalty": 1.15,
                            **(kw.get("extra_body") or {})}
        return create(**kw)

    client.chat.completions.create = fast_create


class Pipeline(LLM2Pipeline):
    """LLM2 with a fixed reply language. Its prompt says "reply in the user's
    language", but the user text arrives wrapped in JSON and the prompt is half
    Chinese, so smaller models answer in Chinese."""

    reply_language = ""
    gestures = {}               # motion id -> description, from the robot's hello
    link = None                 # to send gestures to the robot
    entity_source = None        # callable -> {entity_id: description}, e.g. from HA

    def _load_entities(self):
        if self.entity_source is None:
            return super()._load_entities()
        try:
            return self.entity_source()
        except Exception as e:  # noqa: BLE001 - keep the last good list
            log.warning("could not fetch entities from HA (%s); keeping %d known",
                        e, len(getattr(self, "ha_entities", {})))
            return getattr(self, "ha_entities", {})

    state_source = None         # callable(entity_ids) -> {entity_id: state}
    reminder_source = None      # callable() -> list of lines, or None if unknown

    def handle(self, event):
        """Tell the model how the home is right now. Appended to the user turn,
        not the system prompt, so llama.cpp keeps the long prompt cached."""
        if self.state_source and event.source == "stt" and self.ha_entities:
            try:
                now = self.state_source(set(self.ha_entities))
            except Exception as e:  # noqa: BLE001 - answer without states
                log.warning("could not read device states (%s)", e)
                now = {}
            if now:
                listed = "; ".join(f"{self.ha_entities[eid]} [{eid}]: {st}"
                                   for eid, st in now.items())
                event = event.model_copy(update={
                    "text": f"{event.text}\n\n(Current device states: {listed})"})
        if event.source == "stt":                 # the model has no clock otherwise
            now = time.strftime("%A %Y-%m-%d %H:%M")
            event = event.model_copy(update={"text": f"{event.text}\n(Now: {now})"})
        if self.reminder_source and event.source == "stt":
            items = self.reminder_source()
            if items is not None:
                listed = "; ".join(items) if items else "none"
                event = event.model_copy(update={
                    "text": f"{event.text}\n(Reminders, to-do items and calendar events "
                            f"in Home Assistant: {listed})"})
        return super().handle(event)

    def refresh_entities(self):
        """Re-read what Pebble may control; the prompt picks it up on the next
        set_active_tag, the HA allowlist immediately."""
        self.ha_entities = self._load_entities()
        self.ha.allowed_entities = frozenset(self.ha_entities)

    GROUNDING = (
        "\nFacts about the home arrive in parentheses after the user's words: current "
        "device states and readings, and the reminders, to-do items and calendar events "
        "in Home Assistant. They are the ONLY facts you know about the home. Never invent "
        "a device state, reading, reminder, medication, dose or time. If the user asks "
        "about something not listed, say Home Assistant has nothing about it. Repeat "
        "what a reminder says (including its note) when asked, but give no medical "
        "advice beyond it: for health worries, suggest contacting someone who can help."
        "\nWhen the user says they have done a to-do reminder (e.g. took their "
        "medication), tick it off with one ha_action: service \"todo.update_item\", "
        "entity_id = the list shown in [brackets], data_json = "
        "{\"item\": \"<the item text exactly>\", \"status\": \"completed\"}. Only "
        "when they say it is done, never on a guess.")

    HA_RULES = (
        "\ndata_json must be valid JSON with double quotes, e.g. {\"brightness_pct\": 50}. "
        "To do something later (\"in 5 seconds\", \"after 10 minutes\"), add "
        "\"delay_s\": <seconds> to that action's data_json, and give each step its own "
        "action: e.g. light.turn_off now, then light.turn_on with {\"delay_s\": 5}. "
        "Only say something will happen later if you emitted the delayed action."
        "\nIf you asked the user to confirm something and they agree (yes, sure, ok, "
        "please, go ahead), do it now: emit that action in this reply.")

    reminder_list = None        # where Pebble may add reminders (None: it can't)

    def reminder_rules(self):
        if not self.reminder_list:
            return ("\nYou cannot create reminders; if asked, say someone can add them "
                    "in Home Assistant.")
        return (
            f"\nTo set a reminder the user asks for, emit one ha_action: service "
            f"\"todo.add_item\", entity_id \"{self.reminder_list}\", data_json "
            f"{{\"item\": \"<what to do>\", \"due\": \"YYYY-MM-DD HH:MM\"}} in local time "
            f"(work it out from Now), or {{\"item\": ..., \"in_minutes\": N}}. If they "
            f"gave no time, leave both out (never invent one). NEVER create reminders about medication, medicine, "
            f"pills, doses or any health treatment: say that for safety a carer or family "
            f"member has to add those in Home Assistant.")

    def body_rules(self):
        """Small models answer "I have no head" unless told what the body is."""
        rules = ("\nYour body: a small desk robot with two round eye displays that show "
                 "your feelings, and a head that can nod, tilt, lean and shake. You have no "
                 "arms, legs or camera and cannot search the internet; your voice and your "
                 "head are how you act, plus the home devices listed.")
        if self.gestures:
            listed = "; ".join(f"{k} ({v})" for k, v in self.gestures.items())
            rules += (f"\nTo move your head, add one ha_action: service \"robot.gesture\", "
                      f"entity_id \"robot.head\", data_json {{\"name\": \"<gesture>\"}}. "
                      f"Gestures: {listed}. Do one when asked to move, or to underline a reply.")
        return rules

    def _dispatch(self, out):
        """Gestures play WITH the reply (LLM2 runs actions after speaking), and
        replace the motion the emotion would have picked."""
        moves = [a for a in out.ha_actions if a.service == "robot.gesture"]
        if moves:
            data = TwinHA._data(moves[0]) or {}
            name = str(data.get("name", "")).strip()
            if name in self.gestures and self.link:
                self.face.skip_motion = True
                self.link.send({"type": "motion", "id": name})
                print(f"  gesture: {name}")
            else:
                log.warning("unknown gesture %r", name)
                self.ha.failed += 1
            out = out.model_copy(update={"ha_actions": [a for a in out.ha_actions
                                                        if a.service != "robot.gesture"]})
        try:
            super()._dispatch(out)
        finally:
            self.face.skip_motion = False

    def set_active_tag(self, nfc_tag_id):
        char = super().set_active_tag(nfc_tag_id)
        with self._lock:
            self._system_prompt += self.body_rules()
            if self.reply_language:
                self._system_prompt += (f"\nAlways write tts_text in {self.reply_language}, "
                                        f"in one or two short spoken sentences.")
            if self.state_source or self.reminder_source:
                self._system_prompt += self.GROUNDING
            self._system_prompt += self.HA_RULES
            if self.reminder_source:
                self._system_prompt += self.reminder_rules()
        return char


def tag_for(db_path, twin_character):
    """Twin shells are "baymax"/"r2d2"/...; LLM1's DB keys them by NFC tag with
    character_id "baymax_v1"... Pick the highest version of that character."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT nfc_tag_id, character_id, version FROM characters").fetchall()
    finally:
        conn.close()
    want = twin_character.lower().replace("-", "").replace("_", "")
    hits = [r for r in rows
            if r[1].lower().rsplit("_v", 1)[0].replace("-", "").replace("_", "") == want]
    return max(hits, key=lambda r: r[2])[0] if hits else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--twin", default=TWIN, help="robot/twin brain WebSocket URL")
    ap.add_argument("--voice", action="store_true",
                    help="speak replies with Piper in the character's voice")
    ap.add_argument("--mic", nargs="?", const="laptop", choices=("laptop", "robot"),
                    help='speech input while "Hold to speak" is held: this machine\'s '
                         'mic (default), or "robot" for the robot\'s own mic, streamed '
                         "by the robot as binary frames")
    ap.add_argument("--audio-device", help="mic/speaker index or name (see "
                                           "`python -m sounddevice`)")
    # base.en: 0.8 s per sentence on this laptop's CPU vs 4.1 s for small.en,
    # same transcript on clean speech. Use small.en-q5_1 for noisy rooms.
    ap.add_argument("--stt-model", default="base.en-q5_1",
                    help='whisper.cpp model name or .bin path ("auto" = pebble\'s pick)')
    ap.add_argument("--local", nargs="?", const=LOCAL_LLM, metavar="URL",
                    help=f"use a local llama.cpp model instead of OpenAI (default {LOCAL_LLM})")
    ap.add_argument("--reply-language", default="English",
                    help='language for spoken replies; "" keeps LLM2\'s own rule')
    ap.add_argument("--ha-device", nargs="?", const="Pebble", metavar="NAME",
                    help="register as a Home Assistant device (default name Pebble) "
                         "and receive its notifications")
    ap.add_argument("--reminder-list", default="todo.reminders", metavar="ENTITY",
                    help="HA to-do list Pebble may add reminders to (never medication; "
                         "default todo.reminders)")
    ap.add_argument("--no-reminders", action="store_true",
                    help="don't announce HA to-do/calendar items when they fall due")
    ap.add_argument("--ha-entities", choices=("auto", "ha", "file"), default="auto",
                    help="where controllable entities come from: ha, file "
                         "(LLM2/ha_entities.json), auto = ha when HA is configured")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="  %(name)s: %(message)s")
    for noisy in ("httpx", "httpx2", "pywhispercpp.model"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.local:
        use_local_model(args.local)
    Pipeline.reply_language = args.reply_language
    settings = load_settings()
    link = Link()

    piper = stt = mic = None
    if args.voice or args.mic:
        sys.path.insert(0, str(PEBBLE / "speech"))
    if args.voice:
        from llm2_speaker import PiperSpeaker
        piper = PiperSpeaker(device=args.audio_device)
    if args.mic:
        from stt import SpeechToText
        mic = RobotMic() if args.mic == "robot" else MicRecorder(args.audio_device)
        print("loading whisper (first run downloads it into ../speech/models) ...")
        stt = SpeechToText(args.stt_model, n_threads=min(8, os.cpu_count() or 1))
        print(f"  STT {stt.model_name}, mic at {mic.rate} Hz")

    device = None
    if (args.ha_device or args.ha_entities == "ha") and settings.ha_dry_run:
        sys.exit("--ha-device / --ha-entities ha need HA_BASE_URL and HA_TOKEN "
                 "(see ../LLM2/.env.example)")
    if not settings.ha_dry_run:
        from ha_device import HADevice
        device = HADevice(settings.ha_base_url, settings.ha_token, args.ha_device or "Pebble")
        if args.ha_entities != "file":
            Pipeline.entity_source = staticmethod(
                lambda: device.entities(settings.ha_allowed_domains))
            Pipeline.state_source = staticmethod(device.states)
        Pipeline.reminder_source = staticmethod(lambda: device.reminders)
    if args.ha_device:
        device.register()

    pipe = Pipeline(settings, speaker=TwinSpeaker(link, piper),
                    face=TwinFace(link, device if args.ha_device else None),
                    ha=TwinHA(link, settings, {}))
    pipe.ha.allowed_entities = frozenset(pipe.ha_entities)   # loaded by __init__
    # LLM2's client reads an empty list as "anything in the allowed domains";
    # when HA decides what is exposed, empty must mean nothing.
    pipe.ha.strict = Pipeline.entity_source is not None
    pipe.ha.device = device
    Pipeline.link = link
    if device is not None:
        lists = device.todo_lists()
        if args.reminder_list in lists:
            Pipeline.reminder_list = pipe.ha.reminder_list = args.reminder_list
            print(f"  Pebble adds reminders to {args.reminder_list} "
                  f"({lists[args.reminder_list]}); health reminders stay manual")
        else:
            print(f"  no to-do list {args.reminder_list} in HA, so Pebble can't add "
                  f"reminders. Create one: Settings > Devices & services > Add integration "
                  f"> Local To-do, named \"Reminders\"")
    if args.local:
        tune_for_local(pipe.client)
    if piper:
        piper.follow(pipe)

    # LLM calls take seconds; run them off the receive loop, one at a time.
    jobs = queue.Queue()

    def worker():
        while True:
            source, text = jobs.get()
            t0 = time.time()
            try:
                if source == "warm":                 # load the voice before it is needed
                    piper.tts.synthesize(".", text)
                    continue
                if source == "mic":                  # text is (audio, rate) here
                    text = stt.transcribe(*text)
                    if not text:
                        print("  (nothing heard)")
                        link.send({"type": "eyes", "expression": "neutral"})
                        continue
                    print(f'  heard: "{text}"  ({(time.time() - t0) * 1000:.0f} ms STT)')
                    link.send({"type": "heard", "text": text})
                    source, t0 = "stt", time.time()
                pipe.ha.failed = 0
                out = pipe.handle(LLM2Input(source=source, text=text))
                print(f"  -> [{out.affect}] {(time.time() - t0) * 1000:.0f} ms  "
                      f"\"{out.tts_text[:60]}\"  ha={len(out.ha_actions)}"
                      + (f" ({pipe.ha.failed} FAILED)" if pipe.ha.failed else ""))
                if pipe.ha.failed:              # it may have said "done"; show it wasn't
                    link.send({"type": "eyes", "expression": "concerned"})
                    link.send({"type": "motion", "id": "shake_no"})
            except NoActiveCharacter:
                print("  no character active yet - fit a shell")
            except Exception as exc:  # noqa: BLE001 - never kill the brain
                print(f"  LLM2 failed: {exc}")
                link.send({"type": "eyes", "expression": "concerned"})
                link.send({"type": "motion", "id": "shake_no"})

    threading.Thread(target=worker, daemon=True).start()

    def notifications():
        for line in sys.stdin:
            if line.strip():
                jobs.put(("ha_notification", line.strip()))

    if sys.stdin.isatty():
        threading.Thread(target=notifications, daemon=True).start()

    if device is not None:
        def on_due(item, times):
            """A reminder fell due: Pebble speaks first, nobody has to ask."""
            note = f" ({item['note']})" if item["note"] else ""
            lead = ("Reminder due now" if times == 0 else
                    f"Reminder still not done (reminded {times} time"
                    f"{'s' if times > 1 else ''} already)")
            text = f"{lead}: {item['summary']}{note}."
            print(f'  reminder due: "{text}"')
            jobs.put(("ha_notification", text))
        device.watch_reminders(on_due=on_due if not args.no_reminders else None)

    if args.ha_device:
        def on_push(message, title, data):
            text = f"{title}: {message}" if title else message
            print(f'  HA notification: "{text}"')
            jobs.put(("ha_notification", text))
        device.listen(on_push)

    def prime_stt():
        """Whisper mishears names it has never seen ("disc light", "sushi man").
        Prime it with the home's own device and room names."""
        if stt is None or not pipe.ha_entities:
            return
        # "soshi desk - light, in Bedroom" -> device "soshi desk", room "Bedroom".
        # Hyphens out: primed with "soshi-room", whisper wrote "isshi-room".
        names, rooms = set(), set()
        for desc in pipe.ha_entities.values():
            m = re.match(r"(.*?) - [^,]*(?:, in (.*))?$", desc)
            name, room = (m.group(1), m.group(2)) if m else (desc, None)
            names.add(" ".join(re.sub(r"[^\w&']", " ", name).split()))
            if room:
                rooms.add(room)
        prompt = "Talking to Pebble, a home robot. Devices: " + ", ".join(sorted(names)) + "."
        if rooms:
            prompt += " Rooms: " + ", ".join(sorted(rooms)) + "."
        stt._model._set_params({"initial_prompt": prompt})
        log.info("STT primed: %s", prompt)

    prime_stt()

    def select(character):
        if not character:
            return
        tag = tag_for(settings.db_path, character)
        if tag is None:
            print(f"  no LLM1 profile for shell {character!r} - "
                  f"run ../LLM1/seed_characters.py or character_generator.py")
            return
        if Pipeline.entity_source:
            pipe.refresh_entities()             # pick up devices added in HA
            prime_stt()
        c = pipe.set_active_tag(tag)
        print(f"  shell {character} -> {c.profile.display_name} ({tag}, v{c.version})")
        if args.ha_device:
            device.update_sensor("character", c.profile.display_name)
        if piper:
            jobs.put(("warm", c.character_id))

    print(f"LLM2 model {settings.openai_model} · db {settings.db_path} · "
          f"HA {'dry-run' if settings.ha_dry_run else settings.ha_base_url} · "
          f"{len(pipe.ha_entities)} entities"
          + (f" · notify via {device.notify_service}" if args.ha_device else ""))
    print(f"connecting to {args.twin} ... (type a line here to send an HA notification)")
    while True:
        try:
            conn = ws.connect(args.twin)
        except OSError as exc:
            print(f"  robot not up ({exc}); retrying in 2 s")
            time.sleep(2)
            continue
        link.conn = conn
        try:
            while True:
                raw = conn.recv()
                if isinstance(raw, bytes):           # audio from the robot's mic
                    if isinstance(mic, RobotMic):
                        mic.feed(raw)
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if not msg:
                    continue
                kind = msg.get("type")
                if kind == "hello":
                    print(f"connected: fw={msg['fw']}  shell={msg.get('character_id')}")
                    Pipeline.gestures = {m: GESTURE_HINTS[m] for m in msg.get("motions", [])
                                         if m in GESTURE_HINTS}
                    select(msg.get("character_id"))
                elif kind == "tag" and msg.get("present"):
                    select(msg.get("character_id"))
                elif kind == "utterance":
                    print(f'  heard: "{msg.get("text", "")}"')
                    # look like we're listening while the LLM thinks
                    link.send({"type": "eyes", "expression": "attentive"})
                    jobs.put(("stt", msg.get("text", "")))
                elif kind == "vad" and msg.get("speaking"):
                    link.send({"type": "eyes", "expression": "attentive"})
                    if mic:
                        mic.start()
                elif kind == "vad" and mic:
                    def finish():                    # RobotMic waits for its tail
                        rec = mic.stop()
                        if rec is not None:
                            jobs.put(("mic", rec))
                    threading.Thread(target=finish, daemon=True).start()
        except (ws.WSClosed, ConnectionError, OSError):
            link.conn = None
            print("robot disconnected; reconnecting")
            time.sleep(1.5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
