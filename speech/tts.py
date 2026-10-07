# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["piper-tts>=1.3", "numpy", "sounddevice"]
# ///
"""Text-to-Speech (TTS) — reply text -> audio, via local Piper voices, one voice per character.

See README.md for setup and usage. Run: uv run tts.py "Hello there" --character marvin_v1
"""
import argparse
import json
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

MODELS_DIR = Path(__file__).parent / "models"
VOICES_PATH = Path(__file__).parent / "voices.json"


@dataclass(frozen=True)
class VoiceSettings:
    voice: str  # Piper voice name, e.g. "en_US-lessac-medium"
    length_scale: float = 1.0  # >1 slower, <1 faster


def load_voice_map(path: Path = VOICES_PATH) -> dict:
    return json.loads(path.read_text())


def voice_for(character_id: str | None, extraversion: str | None = None,
              voice_map: dict | None = None) -> VoiceSettings:
    """Pick the voice for a character.

    Characters listed in voices.json get their configured voice. Anyone else (e.g. a character
    LLM1 generated on the fly) gets the default voice, spoken slower or faster according to
    their OCEAN extraversion level ("Low" | "Medium" | "High") if one is given.
    """
    voice_map = voice_map or load_voice_map()
    if character_id in voice_map["characters"]:
        return VoiceSettings(**voice_map["characters"][character_id])
    default = VoiceSettings(**voice_map["default"])
    scale = voice_map["extraversion_length_scale"].get(extraversion)
    return VoiceSettings(default.voice, scale) if scale else default


class TextToSpeech:
    """Load once, then call synthesize()/speak() per reply.

    Each Piper voice is loaded on first use and kept in memory, so switching between
    characters only costs a load the first time.
    """

    def __init__(self, voices_path: Path = VOICES_PATH):
        self.voice_map = load_voice_map(voices_path)
        self._voices: dict = {}

    def _load(self, name: str):
        if name not in self._voices:
            from piper import PiperVoice
            from piper.download_voices import download_voice

            model_path = MODELS_DIR / f"{name}.onnx"
            if not model_path.exists():
                # Download into a temp dir and move into place only when complete, so an
                # interrupted download can't leave a truncated voice behind.
                import tempfile

                MODELS_DIR.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(dir=MODELS_DIR, prefix=".download-") as tmp:
                    download_voice(name, Path(tmp))
                    (Path(tmp) / f"{name}.onnx.json").replace(MODELS_DIR / f"{name}.onnx.json")
                    (Path(tmp) / f"{name}.onnx").replace(model_path)
            self._voices[name] = PiperVoice.load(model_path)
        return self._voices[name]

    def synthesize(self, text: str, character_id: str | None = None,
                   extraversion: str | None = None) -> tuple[np.ndarray, int]:
        """Return (mono float32 audio, sample_rate) for `text` in the character's voice."""
        from piper import SynthesisConfig

        settings = voice_for(character_id, extraversion, self.voice_map)
        voice = self._load(settings.voice)
        # LLM output may contain markdown emphasis like *beep*; don't read the asterisks out.
        text = text.replace("*", "").strip()
        chunks = [c.audio_float_array for c in voice.synthesize(
            text, SynthesisConfig(length_scale=settings.length_scale))]
        audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
        return audio.astype(np.float32), voice.config.sample_rate

    def synthesize_wav(self, text: str, character_id: str | None = None,
                       extraversion: str | None = None) -> bytes:
        """Like synthesize(), but as WAV file bytes — for sending over the network or saving."""
        from audio_io import wav_bytes

        return wav_bytes(*self.synthesize(text, character_id, extraversion))

    def speak(self, text: str, character_id: str | None = None,
              extraversion: str | None = None, device=None) -> None:
        """Synthesize and play on this machine's speaker, blocking until playback finishes."""
        from audio_io import play

        audio, rate = self.synthesize(text, character_id, extraversion)
        play(audio, rate, device)


def main() -> None:
    parser = argparse.ArgumentParser(description="Speak text aloud in a character's voice.")
    parser.add_argument("text")
    parser.add_argument("--character", default=None, help="LLM1 character_id, e.g. baymax_v1")
    parser.add_argument("--extraversion", choices=["Low", "Medium", "High"],
                        help="Speed hint for characters not in voices.json")
    parser.add_argument("--out", type=Path, help="Write a WAV file instead of playing")
    parser.add_argument("--output-device", help="Device index or name (uv run audio_io.py lists them)")
    args = parser.parse_args()

    tts = TextToSpeech()
    t0 = time.perf_counter()
    audio, rate = tts.synthesize(args.text, args.character, args.extraversion)
    settings = voice_for(args.character, args.extraversion, tts.voice_map)
    print(f"[{settings.voice} x{settings.length_scale}: {audio.size / rate:.1f}s audio "
          f"in {time.perf_counter() - t0:.2f}s]")
    if args.out:
        from audio_io import save_wav

        save_wav(args.out, audio, rate)
        print(f"wrote {args.out}")
    else:
        from audio_io import play

        play(audio, rate, args.output_device)


if __name__ == "__main__":
    main()
