# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["pywhispercpp>=1.3", "numpy"]
# ///
"""Speech-to-Text (STT) — audio -> text, via a local whisper.cpp model (pywhispercpp).

See README.md for setup and usage. Run: uv run stt.py path/to/audio.wav
"""
import argparse
import re
import shutil
import subprocess
import time
import wave
from pathlib import Path

import numpy as np

MODELS_DIR = Path(__file__).parent / "models"
SAMPLE_RATE = 16_000  # whisper.cpp only accepts 16 kHz mono float32
DEFAULT_MODEL = "base.en"

# Whisper invents text ("Thank you.", "you") on near-silent input, so very quiet audio is
# treated as "nothing said" before it ever reaches the model.
SILENCE_RMS = 0.005
# Non-speech markers whisper.cpp emits, e.g. "[BLANK_AUDIO]", "(music)".
_NON_SPEECH = re.compile(r"\[[^\]]*\]|\([^)]*\)")


def load_audio(path: Path) -> np.ndarray:
    """Read an audio file as 16 kHz mono float32 in [-1, 1].

    16 kHz 16-bit WAV is read directly; anything else (other rates, mp3, m4a, ...) is
    converted with ffmpeg, which must then be on PATH.
    """
    try:
        with wave.open(str(path), "rb") as wf:
            if wf.getframerate() == SAMPLE_RATE and wf.getsampwidth() == 2:
                pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
                pcm = pcm.reshape(-1, wf.getnchannels()).mean(axis=1)
                return (pcm / 32768.0).astype(np.float32)
    except (wave.Error, EOFError):
        pass  # not a plain PCM WAV — fall through to ffmpeg

    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            f"{path} is not 16 kHz 16-bit WAV and ffmpeg is not installed to convert it."
        )
    out = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path),
         "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"],
        capture_output=True,
        check=True,
    )
    return np.frombuffer(out.stdout, dtype=np.float32)


class SpeechToText:
    """Load the model once, then call transcribe() per utterance.

    Model loading takes a second or two; transcription of a short sentence is well under a
    second on a laptop CPU, so a conversation loop should keep one instance alive.
    """

    def __init__(self, model: str = DEFAULT_MODEL, language: str = "en", n_threads: int = 4):
        from pywhispercpp.model import Model

        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        # `model` is a whisper.cpp model name (downloaded into models/ on first use) or a
        # path to a ggml .bin file.
        self._model = Model(
            model,
            models_dir=str(MODELS_DIR),
            redirect_whispercpp_logs_to=None,
            language=language,
            n_threads=n_threads,
            print_progress=False,
            print_realtime=False,
        )

    def transcribe(self, audio: np.ndarray | str | Path) -> str:
        """Return the spoken text, or "" if the audio is silent / contains no speech.

        `audio` is a file path or a 16 kHz mono float32 array (e.g. straight from a mic).
        """
        if not isinstance(audio, np.ndarray):
            audio = load_audio(Path(audio))
        if audio.size == 0 or float(np.sqrt(np.mean(audio**2))) < SILENCE_RMS:
            return ""
        segments = self._model.transcribe(audio)
        text = " ".join(seg.text.strip() for seg in segments)
        return " ".join(_NON_SPEECH.sub("", text).split())


def main() -> None:
    parser = argparse.ArgumentParser(description="Transcribe an audio file to text.")
    parser.add_argument("audio", type=Path, nargs="+", help="Audio file(s): wav, mp3, m4a, ...")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="whisper.cpp model name or .bin path")
    parser.add_argument("--language", default="en")
    args = parser.parse_args()

    t0 = time.perf_counter()
    stt = SpeechToText(args.model, args.language)
    print(f"[model loaded in {time.perf_counter() - t0:.1f}s]")
    for path in args.audio:
        t0 = time.perf_counter()
        text = stt.transcribe(path)
        print(f"[{path.name}: {time.perf_counter() - t0:.2f}s] {text or '(no speech)'}")


if __name__ == "__main__":
    main()
