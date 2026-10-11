# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["pywhispercpp>=1.3", "piper-tts>=1.3", "numpy", "sounddevice",
#                 "openai>=1.40", "pydantic>=2", "httpx>=0.27"]
# ///
"""Talk to Pebble: mic -> STT -> LLM2 (character) -> TTS in that character's voice.

Two ways to reach LLM2:

  In-process (default) — runs the real LLM2 pipeline from ../LLM2 here, with PiperSpeaker as its
  speaker, so replies are spoken in the active character's voice:
      uv run --env-file ../LLM2/.env voice_chat.py --mic --tag marvin-demo-01

  HTTP — LLM2 already running as `LLM2/server.py`; recognised text is POSTed to its /stt and the
  reply is spoken here (pass --character to pick the voice):
      uv run voice_chat.py --mic --llm2-url http://127.0.0.1:8000 --character marvin_v1

Use --text "..." instead of --mic to type instead of talk. Without an OpenAI key, start
fake_openai.py and set OPENAI_BASE_URL=http://127.0.0.1:8790/v1 OPENAI_API_KEY=fake.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

LLM2_DIR = Path(__file__).resolve().parent.parent / "LLM2"


def utterances(args, stt):
    """Yield user text: typed (--text) or spoken (--mic, push-to-talk until Ctrl+C)."""
    if args.text:
        yield from args.text
        return
    from audio_io import record_push_to_talk
    from stt import SAMPLE_RATE

    while True:
        audio = record_push_to_talk(SAMPLE_RATE, args.input_device)
        t0 = time.perf_counter()
        text = stt.transcribe(audio)
        print(f"[you, {time.perf_counter() - t0:.2f}s STT] {text or '(no speech)'}")
        if text:
            yield text


def in_process(args, stt) -> None:
    from llm2_speaker import PiperSpeaker

    sys.path.append(str(LLM2_DIR))
    from pipeline import LLM2Pipeline
    from schemas import LLM2Input
    from settings import load_settings

    speaker = PiperSpeaker(device=args.output_device)
    pipeline = LLM2Pipeline(load_settings(), speaker=speaker)
    speaker.follow(pipeline)
    if args.tag:
        pipeline.set_active_tag(args.tag)
    if pipeline.active is None:
        sys.exit("No character selected: pass --tag (e.g. baymax-demo-01) or set DEFAULT_NFC_TAG_ID.")
    print(f"[character: {pipeline.active.profile.display_name}]")
    for text in utterances(args, stt):
        t0 = time.perf_counter()
        out = pipeline.handle(LLM2Input(source="stt", text=text))  # speaks via PiperSpeaker
        print(f"[{out.emotion}, {time.perf_counter() - t0:.2f}s LLM+TTS] {out.tts_text}"
              + (f"  HA: {[a.model_dump() for a in out.ha_actions]}" if out.ha_actions else ""))


def over_http(args, stt) -> None:
    from tts import TextToSpeech

    tts = TextToSpeech()
    token = args.token or os.getenv("PEBBLE_API_TOKEN", "")
    for text in utterances(args, stt):
        req = urllib.request.Request(
            f"{args.llm2_url.rstrip('/')}/stt", data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json", "X-Pebble-Token": token})
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                out = json.load(resp)
        except urllib.error.HTTPError as e:  # e.g. 401 bad token, 409 no character scanned yet
            print(f"[LLM2 error {e.code}] {e.read().decode(errors='replace')}")
            continue
        except urllib.error.URLError as e:
            sys.exit(f"Can't reach LLM2 at {args.llm2_url}: {e.reason}")
        print(f"[{out['emotion']}, {time.perf_counter() - t0:.2f}s LLM] {out['tts_text']}")
        tts.speak(out["tts_text"], args.character, arousal=out.get("arousal"),
                  device=args.output_device)


def main() -> None:
    parser = argparse.ArgumentParser(description="Voice conversation with Pebble via LLM2.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--mic", action="store_true", help="Push-to-talk (Ctrl+C to quit)")
    source.add_argument("--text", action="append", help="Typed input instead of the mic (repeatable)")
    parser.add_argument("--tag", help="In-process: NFC tag of the character, e.g. marvin-demo-01")
    parser.add_argument("--llm2-url", help="Use a running LLM2 server instead, e.g. http://127.0.0.1:8000")
    parser.add_argument("--character", help="HTTP mode: character_id for the voice, e.g. marvin_v1")
    parser.add_argument("--token", help="HTTP mode: X-Pebble-Token (default: $PEBBLE_API_TOKEN)")
    parser.add_argument("--model", default="auto", help="STT model (see stt.py)")
    parser.add_argument("--input-device")
    parser.add_argument("--output-device")
    args = parser.parse_args()

    stt = None
    if args.mic:
        from stt import SpeechToText

        stt = SpeechToText(args.model)
        print(f"[STT model {stt.model_name}]")
    try:
        (over_http if args.llm2_url else in_process)(args, stt)
    except (KeyboardInterrupt, EOFError):
        print()


if __name__ == "__main__":
    main()
