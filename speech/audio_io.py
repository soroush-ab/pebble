# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["numpy", "sounddevice"]
# ///
"""Audio helpers: resampling, WAV encode/decode, and local mic/speaker access.

STT/TTS themselves only deal in numpy arrays and WAV bytes, so they work the same whether the
mic/speaker is plugged into this machine (record_push_to_talk / play here) or somewhere else on
the network (see server.py). All audio is mono float32 in [-1, 1].

Run `uv run audio_io.py` to list this machine's audio devices.
"""
import io
import wave
from pathlib import Path

import numpy as np

MAX_RECORD_SECONDS = 30  # safety cap if nobody presses Enter to stop


def _lowpass(audio: np.ndarray, cutoff_ratio: float, taps: int = 101) -> np.ndarray:
    """Windowed-sinc FIR low-pass; cutoff_ratio is the cutoff as a fraction of the sample rate."""
    n = np.arange(taps) - (taps - 1) / 2
    kernel = 2 * cutoff_ratio * np.sinc(2 * cutoff_ratio * n) * np.hamming(taps)
    return np.convolve(audio, kernel / kernel.sum(), mode="same").astype(np.float32)


def resample(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate or audio.size == 0:
        return audio
    if dst_rate < src_rate:
        # Remove everything above the new Nyquist first, otherwise it aliases down into the
        # speech band (e.g. 48 kHz -> 16 kHz) and hurts recognition.
        audio = _lowpass(audio, 0.45 * dst_rate / src_rate)
    n_out = int(round(audio.size * dst_rate / src_rate))
    x_out = np.linspace(0, audio.size - 1, n_out)
    return np.interp(x_out, np.arange(audio.size), audio).astype(np.float32)


def wav_bytes(audio: np.ndarray, sample_rate: int) -> bytes:
    """Encode mono float32 audio as a 16-bit PCM WAV file in memory."""
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


def read_wav(data: bytes) -> tuple[np.ndarray, int] | None:
    """Decode a 16-bit PCM WAV (any rate, mono/stereo) to (mono float32, rate); None if it isn't one."""
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            if wf.getsampwidth() != 2:
                return None
            pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
            pcm = pcm.reshape(-1, wf.getnchannels()).mean(axis=1)
            return (pcm / 32768.0).astype(np.float32), wf.getframerate()
    except (wave.Error, EOFError):
        return None


def save_wav(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    Path(path).write_bytes(wav_bytes(audio, sample_rate))


def _device(device):
    # Device given on the command line as a number means an index, otherwise a name substring.
    return int(device) if isinstance(device, str) and device.isdigit() else device


def record_push_to_talk(sample_rate: int = 16_000, device=None) -> np.ndarray:
    """Record from the mic between two Enter presses; return mono float32 at `sample_rate`.

    Records at the device's native rate and resamples, since not every device/driver
    accepts 16 kHz directly. `device` is an index or name substring (see list_devices()).
    """
    import sounddevice as sd

    device = _device(device)
    native_rate = int(sd.query_devices(device, "input")["default_samplerate"])
    chunks: list[np.ndarray] = []

    def callback(indata, frames, time_info, status):
        if sum(c.shape[0] for c in chunks) < MAX_RECORD_SECONDS * native_rate:
            chunks.append(indata[:, 0].copy())

    input("Press Enter to start talking...")
    with sd.InputStream(samplerate=native_rate, channels=1, dtype="float32",
                        device=device, callback=callback):
        input("Recording — press Enter to stop.")
    audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
    return resample(audio, native_rate, sample_rate)


def play(audio: np.ndarray, sample_rate: int, device=None) -> None:
    """Play mono float32 audio and block until it has finished."""
    import sounddevice as sd

    sd.play(audio, sample_rate, device=_device(device))
    sd.wait()


def list_devices() -> str:
    import sounddevice as sd

    return str(sd.query_devices())


if __name__ == "__main__":
    print(list_devices())
    print("\n> = default input, < = default output. Pass an index or part of a name as "
          "--input-device / --output-device.")
