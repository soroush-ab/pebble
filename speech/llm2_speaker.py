"""Piper speaker for LLM2 — a drop-in replacement for LLM2's ConsoleSpeaker.

LLM2 calls `speaker.speak(text, affect)` (see LLM2/adapters.py `Speaker`). That call doesn't say
which character is talking, so PiperSpeaker reads it from the pipeline's active character
(set by the NFC scan) and speaks in that character's voice from voices.json:

    from llm2_speaker import PiperSpeaker

    speaker = PiperSpeaker()
    pipeline = LLM2Pipeline(settings, speaker=speaker)
    speaker.follow(pipeline)
"""
from tts import TextToSpeech


class PiperSpeaker:
    """Implements LLM2's `Speaker` interface (duck-typed, so `speech/` doesn't import LLM2)."""

    def __init__(self, tts: TextToSpeech | None = None, device=None):
        self.tts = tts or TextToSpeech()
        self.device = device
        self._pipeline = None

    def follow(self, pipeline) -> None:
        """Speak as whichever character `pipeline.active` currently is."""
        self._pipeline = pipeline

    def _active_voice(self) -> tuple[str | None, str | None]:
        active = getattr(self._pipeline, "active", None)
        if active is None:
            return None, None
        return active.character_id, active.profile.personality.traits.extraversion

    def speak(self, text: str, affect) -> None:
        """`affect`: LLM2's Russell-circumplex point; its arousal sets the pace."""
        character_id, extraversion = self._active_voice()
        self.tts.speak(text, character_id, extraversion, getattr(affect, "arousal", None),
                       self.device)
