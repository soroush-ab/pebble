# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["pywhispercpp>=1.3", "piper-tts>=1.3", "numpy", "sounddevice"]
# ///
"""Speech service over HTTP — STT and TTS for any process or device on the network.

Run it on whichever machine has the compute; the robot's board, Home Assistant, or the LLM
pipeline (any language) then call it, and the mic/speaker can live anywhere.

    uv run server.py                     # http://127.0.0.1:8765, this machine only
    uv run server.py --host 0.0.0.0      # reachable from the LAN (no auth — trusted networks only)

    POST /stt   body: an audio file (WAV at any rate; mp3/m4a/... needs ffmpeg)
                -> {"text": "..."}             ("" = no speech)
    POST /tts   body: {"text": "...", "character_id": "marvin_v1", "arousal": 0.5}
                (character_id / extraversion / arousal optional) -> audio/wav
    GET  /health -> {"stt_model": "...", "characters": [...]}

See README.md for examples.
"""
import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from stt import DEFAULT_MODEL, SpeechToText
from tts import TextToSpeech

MAX_BODY_BYTES = 50 * 1024 * 1024  # ~25 min of 16 kHz WAV; reject anything bigger


class SpeechHandler(BaseHTTPRequestHandler):
    stt: SpeechToText
    tts: TextToSpeech
    # whisper.cpp / Piper models aren't safe to call from several threads at once.
    lock = threading.Lock()

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj: dict) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json")

    def _body(self) -> bytes | None:
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY_BYTES:
            self._json(400 if length == 0 else 413, {"error": "missing or too large request body"})
            return None
        return self.rfile.read(length)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"stt_model": self.stt.model_name,
                             "characters": sorted(self.tts.voice_map["characters"])})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path not in ("/stt", "/tts"):
            self._json(404, {"error": "not found"})
            return
        body = self._body()
        if body is None:
            return
        t0 = time.perf_counter()
        try:
            if self.path == "/stt":
                with self.lock:
                    text = self.stt.transcribe(body)
                self._json(200, {"text": text})
                self.log_message("stt %.2fs: %r", time.perf_counter() - t0, text)
            else:
                req = json.loads(body)
                if not isinstance(req, dict) or not str(req.get("text", "")).strip():
                    self._json(400, {"error": 'expected JSON {"text": ..., "character_id": ...}'})
                    return
                with self.lock:
                    wav = self.tts.synthesize_wav(str(req["text"]), req.get("character_id"),
                                                  req.get("extraversion"), req.get("arousal"))
                self._send(200, wav, "audio/wav")
                self.log_message("tts %.2fs: %s %r", time.perf_counter() - t0,
                                 req.get("character_id"), req["text"])
        except ValueError as e:  # undecodable audio, malformed JSON
            self._json(400, {"error": str(e)})
        except Exception as e:  # report to the caller instead of dropping the connection
            self._json(500, {"error": f"{type(e).__name__}: {e}"})


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve STT and TTS over HTTP.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="0.0.0.0 to accept connections from other devices (no auth)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--model", default=DEFAULT_MODEL, help="STT model (see stt.py)")
    parser.add_argument("--language", default="en")
    args = parser.parse_args()

    SpeechHandler.stt = SpeechToText(args.model, args.language)
    SpeechHandler.tts = TextToSpeech()
    server = ThreadingHTTPServer((args.host, args.port), SpeechHandler)
    print(f"[STT model {SpeechHandler.stt.model_name}] serving on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
