# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["pywhispercpp>=1.3", "numpy", "sounddevice"]
# ///
"""Speech-to-Text (STT) — audio -> text, via a local whisper.cpp model (pywhispercpp).

See README.md for setup and usage. Run: uv run stt.py path/to/audio.wav  (or --mic)
"""
import argparse
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np

MODELS_DIR = Path(__file__).parent / "models"
SAMPLE_RATE = 16_000  # whisper.cpp only accepts 16 kHz mono float32
DEFAULT_MODEL = "auto"  # see pick_model()

# Whisper invents text ("Thank you.", "you") on near-silent input, so very quiet audio is
# treated as "nothing said" before it ever reaches the model.
SILENCE_RMS = 0.005
# Non-speech markers whisper.cpp emits, e.g. "[BLANK_AUDIO]", "(music)".
_NON_SPEECH = re.compile(r"\[[^\]]*\]|\([^)]*\)")
_SENTENCE_END = re.compile(r"(?<=[.!?。！？])\s*")


def _drop_repeats(text: str) -> str:
    """Collapse consecutive identical sentences.

    Whisper sometimes gets stuck in a loop on long or unclear audio (e.g. speech in a language
    the model doesn't support) and emits the same sentence many times in a row.
    """
    kept: list[str] = []
    for sentence in filter(None, _SENTENCE_END.split(text)):
        if not kept or sentence.lower() != kept[-1].lower():
            kept.append(sentence)
    # Re-join with a space only before Latin-script text; Chinese/Japanese use none (Whisper
    # often emits ASCII "?" in Chinese output, so the punctuation alone doesn't tell).
    return "".join((" " if i and s[0].isascii() else "") + s for i, s in enumerate(kept))


def load_audio(source: Path | bytes) -> np.ndarray:
    """Decode an audio file (path or raw file bytes) to 16 kHz mono float32 in [-1, 1].

    16-bit WAV at any sample rate is decoded directly; anything else (mp3, m4a, ogg, ...) is
    converted with ffmpeg, which must then be on PATH.
    """
    from audio_io import read_wav, resample

    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    decoded = read_wav(data)
    if decoded is not None:
        return resample(*decoded, SAMPLE_RATE)

    if shutil.which("ffmpeg") is None:
        raise RuntimeError("Audio is not 16-bit WAV and ffmpeg is not installed to convert it.")
    out = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", "pipe:0",
         "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"],
        input=data,
        capture_output=True,
    )
    if out.returncode != 0:
        raise ValueError(f"Could not decode audio: {out.stderr.decode(errors='replace').strip()}")
    return np.frombuffer(out.stdout, dtype=np.float32)


def _ensure_downloaded(name: str) -> Path:
    """Download a named whisper.cpp model into models/ atomically.

    pywhispercpp writes straight to the final filename, so an interrupted download (or a
    second process starting mid-download) would leave a truncated model that segfaults on
    first use. Download into a temp dir and rename only once it's complete.
    """
    import tempfile

    from pywhispercpp.utils import download_model

    target = MODELS_DIR / f"ggml-{name}.bin"
    if target.exists():
        return target
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=MODELS_DIR, prefix=".download-") as tmp:
        Path(download_model(name, download_dir=tmp)).replace(target)
    return target


def has_gpu_backend() -> bool:
    """True if this pywhispercpp build can use a GPU (Metal on Apple Silicon, CUDA, Vulkan...)."""
    import _pywhispercpp as pw

    return re.search(r"\b(MTL|METAL|CUDA|VULKAN|HIP)\b", pw.whisper_print_system_info()) is not None


def pick_model() -> str:
    """Choose a model this device can run comfortably faster than real time.

    Measured on an M3 Max over 52s of real mic recordings: with the GPU, medium.en takes ~3s;
    CPU-only it takes ~27s, so on a CPU-only device (or a weaker one, e.g. a Raspberry Pi)
    the smaller models are needed. Quantized (q5) variants are used throughout: same accuracy
    and speed in our tests at about a third of the size and memory.
    """
    if has_gpu_backend():
        return "medium.en-q5_0"  # ~540MB, best accuracy we can afford
    if (os.cpu_count() or 1) >= 8:
        return "small.en-q5_1"  # ~190MB, ~0.2x real time on a laptop CPU
    return "base.en-q5_1"  # ~60MB, for small boards


class SpeechToText:
    """Load the model once, then call transcribe() per utterance.

    Model loading takes a second or two; transcribing a short sentence takes well under a
    second, so a conversation loop should keep one instance alive.
    """

    def __init__(self, model: str = DEFAULT_MODEL, language: str = "en", n_threads: int | None = None):
        from pywhispercpp.model import Model

        # `model` is "auto", a whisper.cpp model name (downloaded into models/ on first use),
        # or a path to a ggml .bin file.
        if model == "auto":
            model = pick_model()
        self.model_name = Path(model).name
        # More than 4 threads was slower in our tests (memory-bound), so cap it there.
        n_threads = n_threads or min(4, os.cpu_count() or 1)
        if not Path(model).is_file():
            model = str(_ensure_downloaded(model))
        self._model = Model(
            model,
            models_dir=str(MODELS_DIR),
            redirect_whispercpp_logs_to=None,
            language=language,
            n_threads=n_threads,
            print_progress=False,
            print_realtime=False,
        )

    def transcribe(self, audio: np.ndarray | bytes | str | Path, sample_rate: int = SAMPLE_RATE) -> str:
        """Return the spoken text, or "" if the audio is silent / contains no speech.

        `audio` is one of:
          - a mono float32 numpy array (e.g. from a mic), at `sample_rate` (resampled if not 16 kHz)
          - raw bytes of an audio file (e.g. a WAV received over the network)
          - a path to an audio file
        """
        if isinstance(audio, np.ndarray):
            from audio_io import resample

            audio = resample(audio.astype(np.float32), sample_rate, SAMPLE_RATE)
        else:
            audio = load_audio(audio if isinstance(audio, bytes) else Path(audio))
        if audio.size == 0 or float(np.sqrt(np.mean(audio**2))) < SILENCE_RMS:
            return ""
        segments = self._model.transcribe(audio)
        text = " ".join(seg.text.strip() for seg in segments)
        return _drop_repeats(" ".join(_NON_SPEECH.sub("", text).split()))


def main() -> None:
    parser = argparse.ArgumentParser(description="Transcribe audio files, or the microphone, to text.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("audio", type=Path, nargs="*", default=[], help="Audio file(s): wav, mp3, m4a, ...")
    source.add_argument("--mic", action="store_true", help="Push-to-talk from the microphone (Ctrl+C to quit)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="auto (default: pick by hardware), a whisper.cpp model name, or a .bin path")
    parser.add_argument("--language", default="en")
    parser.add_argument("--save-dir", type=Path, help="With --mic: also save each recording as a WAV here")
    parser.add_argument("--input-device", help="With --mic: device index or name (uv run audio_io.py lists them)")
    args = parser.parse_args()

    t0 = time.perf_counter()
    stt = SpeechToText(args.model, args.language)
    print(f"[model {stt.model_name} loaded in {time.perf_counter() - t0:.1f}s]")
    if args.mic:
        from audio_io import record_push_to_talk, save_wav

        if args.save_dir:
            args.save_dir.mkdir(parents=True, exist_ok=True)
        try:
            while True:
                audio = record_push_to_talk(SAMPLE_RATE, args.input_device)
                if args.save_dir:
                    path = args.save_dir / time.strftime("mic-%Y%m%d-%H%M%S.wav")
                    save_wav(path, audio, SAMPLE_RATE)
                    print(f"[saved {path}]")
                t0 = time.perf_counter()
                text = stt.transcribe(audio)
                print(f"[{audio.size / SAMPLE_RATE:.1f}s audio, {time.perf_counter() - t0:.2f}s] "
                      f"{text or '(no speech)'}")
        except (KeyboardInterrupt, EOFError):
            print()
        return
    for path in args.audio:
        t0 = time.perf_counter()
        text = stt.transcribe(path)
        print(f"[{path.name}: {time.perf_counter() - t0:.2f}s] {text or '(no speech)'}")


if __name__ == "__main__":
    main()
