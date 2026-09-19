"""Local (on-device) text-to-speech provider.

Adapter over the TTS service that already ships in this repo
(``services/tts/tts_service.py``), which uses Kokoro-82M locally. As with STT,
this exists to give the voice service a capability interface — not to add a
second synthesis stack.
"""

from __future__ import annotations

from typing import Any, Iterator, Optional, Sequence

from services.voice.providers.base import (
    TextToSpeechProvider,
    VoiceProviderError,
    VoiceProviderUnavailable,
)


class LocalKokoroTTSProvider(TextToSpeechProvider):
    """Kokoro-82M synthesis, on GPU when available.

    Synthesis is synchronous and buffered: ``supports_streaming`` is False so
    the UI does not promise incremental audio the backend cannot deliver.
    Playback still starts as soon as the first chunk arrives because
    :meth:`stream` yields it immediately.
    """

    name = "local"
    supports_streaming = False

    def __init__(self, service: Any = None) -> None:
        self._service = service

    def _svc(self):
        if self._service is None:
            from services.tts.tts_service import get_tts_service

            self._service = get_tts_service()
        return self._service

    @property
    def available(self) -> bool:
        try:
            stats = self._svc().get_stats()
        except Exception:
            return False
        return bool(stats.get("available")) and stats.get("provider") == "local"

    def voices(self) -> Sequence[str]:
        try:
            return tuple(self._svc().list_voices() or ())
        except Exception:
            return ()

    def synthesize(
        self,
        text: str,
        *,
        voice: Optional[str] = None,
        speed: Optional[float] = None,
        language: Optional[str] = None,
    ) -> bytes:
        if not text or not text.strip():
            return b""

        svc = self._svc()
        if not self.available:
            raise VoiceProviderUnavailable(
                "Local text-to-speech is not available. Install the Kokoro TTS "
                "stack, then set tts_provider=local in Settings."
            )

        try:
            # As with STT, voice/speed come from saved settings today; the
            # arguments are part of the interface for providers that can honour
            # them per call.
            audio = svc.synthesize(text)
        except Exception as exc:
            raise VoiceProviderError(f"local synthesis failed: {exc}") from exc

        if not audio:
            raise VoiceProviderError("local synthesis returned no audio")
        return audio

    def stream(
        self,
        text: str,
        *,
        voice: Optional[str] = None,
        speed: Optional[float] = None,
        language: Optional[str] = None,
    ) -> Iterator[bytes]:
        audio = self.synthesize(text, voice=voice, speed=speed, language=language)
        if audio:
            yield audio
