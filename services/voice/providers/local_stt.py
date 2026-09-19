"""Local (on-device) speech-to-text provider.

Thin adapter over the STT service that already ships in this repo
(``services/stt/stt_service.py``). It deliberately does **not** reimplement
Whisper loading — there is one speech stack, and this is the voice feature's
view of it.

Audio never leaves the machine when this provider is selected, which is the
default posture for a local-first product.
"""

from __future__ import annotations

from typing import Any, Optional

from services.voice.providers.base import (
    SpeechToTextProvider,
    TranscriptResult,
    VoiceProviderError,
    VoiceProviderUnavailable,
)


class LocalWhisperSTTProvider(SpeechToTextProvider):
    """``faster-whisper`` running on CPU or GPU.

    ``available`` is False until ``faster-whisper`` + ``torch`` are installed
    and the STT settings point at the local provider. The voice service turns
    that into a typed ``voice.error`` instead of a crash, so voice stays
    usable via the browser provider on a stock install.
    """

    name = "local"
    supports_partial = False

    def __init__(self, service: Any = None) -> None:
        self._service = service

    def _svc(self):
        if self._service is None:
            from services.stt.stt_service import get_stt_service

            self._service = get_stt_service()
        return self._service

    @property
    def available(self) -> bool:
        try:
            stats = self._svc().get_stats()
        except Exception:
            return False
        return bool(stats.get("available")) and stats.get("provider") == "local"

    @property
    def model(self) -> str:
        try:
            return str(self._svc().get_stats().get("model") or "")
        except Exception:
            return ""

    def transcribe(
        self,
        audio: bytes,
        *,
        language: Optional[str] = None,
        sample_rate: Optional[int] = None,
    ) -> TranscriptResult:
        if not audio:
            # Malformed / empty audio is a normal condition, not an error.
            return TranscriptResult(text="", provider=self.name)

        svc = self._svc()
        if not self.available:
            raise VoiceProviderUnavailable(
                "Local speech-to-text is not available. Install faster-whisper "
                "and torch, then set stt_provider=local in Settings."
            )

        try:
            # NOTE: the underlying service currently reads the language from
            # saved settings rather than per call, so a `language` argument is
            # recorded here but not forwarded. Per-utterance language override
            # is a follow-up in the shared STT service, not something this
            # adapter can force without duplicating Whisper loading.
            text = svc.transcribe(audio)
        except Exception as exc:  # provider failures must not 500 the session
            raise VoiceProviderError(f"local transcription failed: {exc}") from exc

        if text is None:
            raise VoiceProviderError("local transcription returned no result")

        return TranscriptResult(
            text=text.strip(),
            language=language or str(svc.get_stats().get("language") or ""),
            confidence=None,  # faster-whisper path does not surface this yet
            provider=self.name,
        )
