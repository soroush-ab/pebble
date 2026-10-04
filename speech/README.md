# speech — STT / TTS

The robot's speech interface: microphone audio -> text for the LLM (STT), and the LLM's reply
text -> audio for the speaker (TTS). Everything runs locally and offline.

```
mic -> [STT: stt.py] -> text -> LLM2 (runtime assistant) -> reply text -> [TTS] -> speaker
```

**Status:** STT (file -> text) working. Mic recording, TTS, and the end-to-end demo are next.

## Quick start

```bash
cd speech
uv run stt.py path/to/recording.wav     # first run downloads the model (~140MB) into models/
```

## Contents

| Path | Purpose |
|---|---|
| `stt.py` | `SpeechToText` class + CLI: audio file / numpy array -> text |
| `models/` | whisper.cpp model files, downloaded automatically (not committed) |

## Setup

**Requirements:** Python ≥3.11 + [uv](https://docs.astral.sh/uv/) — dependencies are declared
inline in each script and installed by `uv run`, same as `LLM1/`. `pywhispercpp` ships prebuilt
wheels, so no compiler is needed on common platforms. Scripts pin Python `<3.14` because not all
dependencies have 3.14 wheels yet; uv fetches a suitable Python automatically.

Optional: **ffmpeg** on `PATH` to transcribe anything other than 16 kHz 16-bit WAV (mp3, m4a,
44.1 kHz WAV, ...).

Only verified on macOS (Apple Silicon) so far.

## STT — `stt.py`

Uses [whisper.cpp](https://github.com/ggml-org/whisper.cpp) (OpenAI Whisper, ggml port — same
runtime family as llama.cpp in `LLM1/`) through `pywhispercpp`.

```bash
uv run stt.py a.wav b.mp3                    # transcribe one or more files
uv run stt.py a.wav --model small.en         # more accurate, slower, ~470MB
```

From Python (load once, call per utterance — model load is the slow part):

```python
from stt import SpeechToText

stt = SpeechToText()                  # default model: base.en
text = stt.transcribe("question.wav") # or a 16 kHz mono float32 numpy array from the mic
# -> "" if the audio is silent / no speech
```

**Model choice:** `base.en` (default, ~140MB) is accurate enough for everyday conversation;
`small.en` if accuracy is a problem; `tiny.en` makes too many mistakes. English-only `.en`
models are used because the robot only needs English.

**Silence handling:** Whisper tends to invent text ("Thank you.") on silent input, so audio
below a small RMS threshold returns `""` without calling the model, and non-speech markers like
`[BLANK_AUDIO]` are stripped from the output.

Measured on an M3 Max (CPU, `base.en`): model load ~0.4s, a ~5s sentence transcribes in ~0.1s.
