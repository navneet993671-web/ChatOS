"""Provider interfaces for Misantropic Voice.

Voice is an **interface layer** over the existing Misantropic intelligence
stack, not a second assistant. It reuses the same conversations, context
engine, agent runtime, tools, memory, RAG and permission system as text chat.

These ABCs exist so :class:`services.voice.service.VoiceService` depends on
*capabilities* rather than a concrete implementation. Adding a new STT/TTS/VAD
engine must never require touching voice assistant logic.

The shipped adapters (:mod:`services.voice.providers.local_stt`,
:mod:`services.voice.providers.local_tts`) wrap the services that already exist
in this repo — ``services/stt/stt_service.py`` and
``services/tts/tts_service.py`` — so there is exactly one speech stack.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator, Iterator, Optional, Sequence


class VoiceProviderError(RuntimeError):
    """A provider is unavailable or failed to serve a request.

    Callers surface this to the client as a typed voice error rather than a
    500, so a broken provider never takes down the Chat UI.
    """


class VoiceProviderUnavailable(VoiceProviderError):
    """The provider exists and is configured, but cannot currently serve.

    Typical causes: ``faster-whisper``/``torch`` not installed, no model
    downloaded, or an ``endpoint:<id>`` provider whose host is unreachable.
    """


# --------------------------------------------------------------------------
# Transcript value objects
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TranscriptSegment:
    """One timed span of recognised speech."""

    text: str
    start: float = 0.0
    end: float = 0.0


@dataclass(frozen=True)
class TranscriptResult:
    """Result of transcribing one utterance.

    ``confidence`` and ``timestamps`` are optional by design: not every
    provider can supply them, and the voice service must not require what a
    provider cannot give.
    """

    text: str
    language: str = ""
    confidence: Optional[float] = None
    segments: Sequence[TranscriptSegment] = field(default_factory=tuple)
    provider: str = ""

    @property
    def is_empty(self) -> bool:
        """True when nothing intelligible was recognised."""
        return not self.text.strip()

    def as_event(self) -> dict:
        """Serialise for a ``voice.transcript_final`` event."""
        payload = {
            "type": "voice.transcript_final",
            "text": self.text,
            "language": self.language,
            "provider": self.provider,
        }
        if self.confidence is not None:
            payload["confidence"] = self.confidence
        if self.segments:
            payload["segments"] = [
                {"text": s.text, "start": s.start, "end": s.end} for s in self.segments
            ]
        return payload


# --------------------------------------------------------------------------
# Speech to text
# --------------------------------------------------------------------------


class SpeechToTextProvider(ABC):
    """Turns recorded audio into text.

    Implementations must be safe to call from a worker thread and must never
    raise on malformed audio — return an empty transcript instead, so a garbled
    frame cannot break a session.
    """

    name: str = "base"
    #: True when the provider can emit partial transcripts as audio arrives.
    supports_partial: bool = False

    @property
    @abstractmethod
    def available(self) -> bool:
        """True when this provider can currently serve a transcription."""

    @abstractmethod
    def transcribe(
        self,
        audio: bytes,
        *,
        language: Optional[str] = None,
        sample_rate: Optional[int] = None,
    ) -> TranscriptResult:
        """Transcribe a complete utterance.

        Args:
            audio: Encoded audio bytes (webm/opus from MediaRecorder, or raw
                PCM for streaming providers).
            language: BCP-47/ISO code to force, or None for auto-detection.
            sample_rate: Required for raw-PCM providers; ignored otherwise.
        """

    async def transcribe_stream(
        self, chunks: AsyncIterator[bytes], **kwargs
    ) -> AsyncIterator[TranscriptResult]:
        """Yield partial then final transcripts. Optional capability."""
        raise NotImplementedError(
            f"{self.name} does not support streaming transcription"
        )
        yield  # pragma: no cover - makes this an async generator


# --------------------------------------------------------------------------
# Text to speech
# --------------------------------------------------------------------------


class TextToSpeechProvider(ABC):
    """Turns assistant text into playable audio."""

    name: str = "base"
    #: True when :meth:`stream` yields usable audio before synthesis completes.
    supports_streaming: bool = False

    @property
    @abstractmethod
    def available(self) -> bool:
        """True when this provider can currently serve a synthesis."""

    @abstractmethod
    def synthesize(
        self,
        text: str,
        *,
        voice: Optional[str] = None,
        speed: Optional[float] = None,
        language: Optional[str] = None,
    ) -> bytes:
        """Return encoded audio for ``text`` (MP3 or WAV)."""

    def stream(
        self,
        text: str,
        *,
        voice: Optional[str] = None,
        speed: Optional[float] = None,
        language: Optional[str] = None,
    ) -> Iterator[bytes]:
        """Yield audio incrementally so playback can start before the whole
        utterance is synthesised.

        The default is honest rather than clever: providers that cannot stream
        yield their full buffer in one chunk. Override for lower latency.
        """
        yield self.synthesize(text, voice=voice, speed=speed, language=language)

    def voices(self) -> Sequence[str]:
        """Selectable voice identifiers. Empty means 'provider default'."""
        return ()


# --------------------------------------------------------------------------
# Voice activity detection
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class VadResult:
    """Outcome of feeding one audio frame to a detector."""

    speaking: bool
    energy: float = 0.0
    voiced_ms: int = 0
    silence_ms: int = 0
    #: True when this frame ended an utterance (silence timeout, max
    #: duration, or an explicit stop) and the utterance should be transcribed.
    should_finalize: bool = False
    #: Why finalisation happened: "silence" | "max_duration" | "stopped" | ""
    reason: str = ""


class VoiceActivityDetector(ABC):
    """Decides when the user started and stopped speaking.

    Implementations must be cheap: ``feed`` runs once per audio frame, on the
    hot path between the microphone and speech-to-text.
    """

    name: str = "base"
    #: Expected frame duration in milliseconds.
    frame_ms: int = 20

    @abstractmethod
    def reset(self) -> None:
        """Clear state for a fresh utterance."""

    @abstractmethod
    def feed(self, frame: bytes, *, now: Optional[float] = None) -> VadResult:
        """Consume one PCM frame and report the detector's decision.

        Args:
            frame: 16-bit signed little-endian mono PCM.
            now: Monotonic timestamp in seconds. Injectable for tests.
        """

    @property
    @abstractmethod
    def is_speaking(self) -> bool:
        """True when speech is currently in progress."""

    def trim_to_speech(self, frames: Sequence[bytes]) -> list[bytes]:
        """Return only the frames from first detected speech onward.

        Prevents the silence timeout from being counted from the start of
        recording, which would cut off a user who pauses before speaking.
        """
        return list(frames)
