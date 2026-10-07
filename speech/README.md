# speech — STT / TTS

The robot's speech interface: microphone audio -> text for the LLM (STT), and the LLM's reply
text -> audio for the speaker (TTS). Everything runs locally and offline.

```
mic -> [STT: stt.py] -> text -> LLM2 (runtime assistant) -> reply text -> [TTS] -> speaker
```

**Status:** STT (files / mic / network) and TTS (per-character voices) working, usable as Python
classes or as an HTTP service, and wired into LLM2: `voice_chat.py` runs mic -> STT -> LLM2 ->
TTS in the active character's voice. Hardware-independent: the model is picked to suit the
machine, and the mic/speaker can be local or on another device.

## Quick start

```bash
cd speech
uv run stt.py path/to/recording.wav     # first run downloads a model into models/
uv run stt.py --mic                     # talk: Enter to start, Enter to stop, Ctrl+C to quit
uv run stt.py --mic --save-dir recordings   # ...and keep each recording as a WAV
uv run tts.py "Lights on." --character marvin_v1   # speak in a character's voice
uv run server.py                        # STT + TTS over HTTP on 127.0.0.1:8765
```

## Talking to Pebble through LLM2 — `voice_chat.py`

Full loop: mic -> STT -> LLM2 (role-plays the character scanned by NFC) -> reply spoken by
Piper in that character's voice, with speed adjusted for LLM2's `emotion`.

```bash
cd speech
# with a real OpenAI key in ../LLM2/.env (see LLM2/README.md):
uv run --env-file ../LLM2/.env voice_chat.py --mic --tag marvin-demo-01

# no key? run the stand-in model in another terminal first:
uv run fake_openai.py
OPENAI_BASE_URL=http://127.0.0.1:8790/v1 OPENAI_API_KEY=fake uv run voice_chat.py --mic --tag baymax-demo-01
```

`--text "..."` (repeatable) types instead of talking. This runs the real, unmodified LLM2 code
in-process. `fake_openai.py` only mimics OpenAI's API with canned, keyword-based replies — it
tests the plumbing, not the conversation.

If LLM2 is already running as its own server (`LLM2/server.py`), send it the recognised text
instead and speak the reply here:

```bash
uv run voice_chat.py --mic --llm2-url http://127.0.0.1:8000 --character marvin_v1 --token <PEBBLE_API_TOKEN>
```

**For LLM2's server (`LLM2/server.py`)** to speak with Piper itself, its speaker just needs
swapping — `PiperSpeaker` implements LLM2's `Speaker.speak(text, emotion)` and takes the voice
from `pipeline.active`:

```python
sys.path.insert(0, str(ROOT / "speech"))
from llm2_speaker import PiperSpeaker

speaker = PiperSpeaker()
pipeline = LLM2Pipeline(settings, speaker=speaker)
speaker.follow(pipeline)
```

(plus `piper-tts`, `numpy`, `sounddevice` in that script's dependencies).

## How to integrate (LLM pipeline, robot, Home Assistant)

Pick whichever fits; both use the same models and voices.

**1. Same Python process** — import the classes (load once at startup, they keep models in
memory):

```python
import sys; sys.path.insert(0, "path/to/pebble/speech")
from stt import SpeechToText
from tts import TextToSpeech

stt, tts = SpeechToText(), TextToSpeech()
text = stt.transcribe(audio)   # numpy float32 (+ sample_rate=...), WAV/mp3 bytes, or a file path
tts.speak(reply, character_id="marvin_v1")             # play on this machine's speaker
wav = tts.synthesize_wav(reply, character_id="marvin_v1")  # or get WAV bytes to send elsewhere
```

**2. HTTP service** — for another process, another language, or another device (e.g. the
robot's board records audio and plays the reply, while a laptop does the compute):

```bash
uv run server.py --host 0.0.0.0        # on the compute machine; LAN-reachable, no auth

curl --data-binary @question.wav http://<host>:8765/stt
# {"text": "Can you turn on the lights?"}
curl -d '{"text": "Lights on.", "character_id": "marvin_v1"}' http://<host>:8765/tts -o reply.wav
curl http://<host>:8765/health
# {"stt_model": "medium.en-q5_0", "characters": ["baymax_v1", "marvin_v1", "r2d2_v1"]}
```

`/stt` takes any audio file as the request body (16-bit WAV at any sample rate; other formats
need ffmpeg on the server). `/tts` takes JSON `text` plus optional `character_id` /
`extraversion` and returns `audio/wav`. Bad input gets HTTP 400 with `{"error": ...}`. Requests
are handled one at a time (the models aren't thread-safe), ~0.3-1s each on an M3 Max. Only use
`--host 0.0.0.0` on a trusted network — there's no authentication.

## Contents

| Path | Purpose |
|---|---|
| `stt.py` | `SpeechToText` class + CLI: audio file / numpy array / mic -> text |
| `tts.py` | `TextToSpeech` class + CLI: text (+ character) -> audio / speaker / WAV |
| `voices.json` | Which voice and speed each character uses |
| `voice_chat.py` | Voice conversation: mic -> STT -> LLM2 -> TTS (in-process or via LLM2's HTTP API) |
| `llm2_speaker.py` | `PiperSpeaker` — LLM2 `Speaker` that talks in the active character's voice |
| `fake_openai.py` | Stand-in OpenAI endpoint for testing LLM2 without an API key |
| `server.py` | HTTP service exposing STT (`POST /stt`) and TTS (`POST /tts`) |
| `audio_io.py` | Resampling, WAV encode/decode, local mic/speaker (`uv run audio_io.py` lists devices) |
| `models/` | whisper.cpp models and Piper voices, downloaded automatically (not committed) |

## Setup

**Requirements:** Python ≥3.11 + [uv](https://docs.astral.sh/uv/) — dependencies are declared
inline in each script and installed by `uv run`, same as `LLM1/`. `pywhispercpp` ships prebuilt
wheels, so no compiler is needed on common platforms. Scripts pin Python `<3.14` because not all
dependencies have 3.14 wheels yet; uv fetches a suitable Python automatically.

Optional: **ffmpeg** on `PATH` to transcribe formats other than 16-bit WAV (mp3, m4a, ogg, ...).

Mic/speaker access uses `sounddevice` (PortAudio). macOS/Windows wheels bundle PortAudio; on
Linux install it first (`libportaudio2` on Ubuntu, `nix-shell -p portaudio` on NixOS). On macOS
the first `--mic` run triggers a microphone permission prompt for your terminal app — if
recording hangs or is silent, allow it under System Settings → Privacy & Security → Microphone.

Mic/speaker choice: `uv run audio_io.py` lists devices; pass an index or part of a name with
`stt.py --mic --input-device "USB"` / `tts.py ... --output-device 3` (e.g. a USB mic on a Pi).

Only verified on macOS (Apple Silicon) so far.

## STT — `stt.py`

Uses [whisper.cpp](https://github.com/ggml-org/whisper.cpp) (OpenAI Whisper, ggml port — same
runtime family as llama.cpp in `LLM1/`) through `pywhispercpp`.

```bash
uv run stt.py a.wav b.mp3                    # transcribe one or more files
uv run stt.py a.wav --model small.en         # force a specific model
```

From Python (load once, call per utterance — model load is the slow part):

```python
from stt import SpeechToText

stt = SpeechToText()                  # model picked by hardware (see below)
text = stt.transcribe("question.wav") # or WAV/mp3 bytes, or a numpy array (+ sample_rate=...)
# -> "" if the audio is silent / no speech
```

Microphone: `audio_io.record_push_to_talk()` returns a 16 kHz float32 array ready for
`transcribe()`. It records at the mic's native rate (often 48 kHz) and resamples, and keeps at
most 30s per utterance. Push-to-talk (rather than always listening) also keeps the robot from
transcribing its own TTS output.

**Model choice — `--model auto` (default)** picks by hardware at startup:

| Device | Model | Size |
|---|---|---|
| GPU backend available (Apple Silicon/Metal, or a CUDA/Vulkan build) | `medium.en-q5_0` | ~540MB |
| CPU only, 8+ cores (typical laptop/desktop) | `small.en-q5_1` | ~190MB |
| CPU only, fewer cores (e.g. Raspberry Pi) | `base.en-q5_1` | ~60MB |

The chosen model is printed at startup; override with `--model <name>` (e.g. `small.en`,
`medium.en`) or `SpeechToText("small.en")`. Measured on an M3 Max over 52s of real mic
recordings:

| Model | GPU (Metal) | CPU only, 4 threads | Accuracy on our recordings |
|---|---|---|---|
| `medium.en` / `medium.en-q5_0` | 2.9s | 26-27s | best — the only one that got the longer, accented sentences right |
| `small.en` / `small.en-q5_1` | 1.3-1.4s | 11-12s | good on short sentences, misheard longer/accented ones |
| `base.en` | — | — | poor on real mic speech ("Hello, platform." for "Hello, where are you from?") |

So: a GPU is worth ~9x; q5 quantization keeps accuracy and speed at about a third of the
size/memory; more than 4 threads didn't help (memory-bound), so threads are capped at 4.
The prebuilt Linux wheels are, as far as we know, CPU-only — to use an NVIDIA GPU there,
pywhispercpp has to be built with CUDA (see the [pywhispercpp README](https://github.com/absadiki/pywhispercpp)).

All `.en` models are English-only — they **cannot transcribe other languages** (Chinese speech
comes out as made-up English). For other languages use a multilingual model, e.g.
`uv run stt.py --mic --model medium-q5_0 --language zh` (`--language auto` also works but can
mis-detect very short utterances).

**Silence handling:** Whisper tends to invent text ("Thank you.") on silent input, so audio
below a small RMS threshold returns `""` without calling the model, and non-speech markers like
`[BLANK_AUDIO]` are stripped from the output. Whisper can also get stuck repeating one sentence
on long/unclear audio, so consecutive duplicate sentences are collapsed.


## TTS — `tts.py`

Uses [Piper](https://github.com/OHF-Voice/piper1-gpl) — fast local neural TTS (VITS models
exported to ONNX), runs on CPU, ~60MB per voice. Voices are downloaded into `models/` on first
use. Note: the `piper-tts` package is GPL-3.0, and each voice has its own license (see its
model card) — fine for coursework, check before any commercial use.

```bash
uv run tts.py "Of course, I'll dim the lights." --character baymax_v1   # plays it
uv run tts.py "Lights on." --character marvin_v1 --out marvin.wav        # writes a WAV
uv run tts.py "Hello." --character some_new_id --extraversion High       # unlisted character
```

From Python (LLM2 side):

```python
from tts import TextToSpeech

tts = TextToSpeech()                          # load once
tts.speak(reply_text, character_id="marvin_v1")   # blocks until playback finishes
audio, sample_rate = tts.synthesize(reply_text, "baymax_v1")  # or get the audio instead
```

**Character voices** live in `voices.json`, keyed by LLM1's `character_id`. Current picks are
placeholders to be tuned by ear:

| Character | Voice | Speed (`length_scale`) | Why |
|---|---|---|---|
| Baymax | `en_US-ryan-medium` | 1.2 (slower) | "soft, slow, reassuring" |
| Marvin | `en_GB-alan-medium` | 0.9 (faster) | "dry, deadpan, direct" |
| R2-D2 | `en_US-lessac-medium` | 1.0 | placeholder — R2-D2 is meant to chirp, not talk |
| anyone else | `en_US-lessac-medium` | from extraversion: Low 1.15 / Medium 1.0 / High 0.9 | generated characters |

Speaking rate follows extraversion because rate is one of the main vocal cues people use to
perceive extraversion in synthetic speech; Piper has no pitch control, so voice choice + rate
are the knobs. To change a voice, edit `voices.json` (browse voices at
https://rhasspy.github.io/piper-samples/).

**Emotion:** LLM2 also outputs an `emotion`; `emotion_length_scale` in `voices.json` multiplies
the character's speed by it (excited 0.9 … sad 1.15, sleepy 1.25), e.g.
`uv run tts.py "..." --character baymax_v1 --emotion sad`.

Asterisks are stripped before speaking so markdown-style `*beep*` isn't read out.

Measured on an M3 Max (CPU): first use of a voice ~1-2s to load; after that a ~3s sentence
synthesizes in ~0.2s.
