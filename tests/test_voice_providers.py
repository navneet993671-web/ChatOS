"""Voice provider registry + adapter tests.

The adapters wrap the speech services that already exist in this repo, so
these tests drive them with stubs rather than loading Whisper/Kokoro.
"""

import pytest

from services.voice.providers import (
    LocalKokoroTTSProvider,
    LocalWhisperSTTProvider,
    SpeechToTextProvider,
    TextToSpeechProvider,
    VoiceProviderError,
    VoiceProviderUnavailable,
    get_stt_provider,
    get_tts_provider,
    register_stt_provider,
    register_tts_provider,
    reset_provider_cache,
)
from services.voice.providers.base import TranscriptResult


class StubSTTService:
    """Stands in for services/stt/stt_service.py."""

    def __init__(self, provider="local", available=True, text="hello world", raises=None):
        self._provider = provider
        self._available = available
        self._text = text
        self._raises = raises
        self.calls = []

    def get_stats(self):
        return {"available": self._available, "provider": self._provider,
                "model": "base", "language": "en"}

    def transcribe(self, audio):
        self.calls.append(audio)
        if self._raises:
            raise self._raises
        return self._text


class StubTTSService:
    def __init__(self, provider="local", available=True, audio=b"ID3-audio", raises=None):
        self._provider = provider
        self._available = available
        self._audio = audio
        self._raises = raises

    def get_stats(self):
        return {"available": self._available, "provider": self._provider}

    def synthesize(self, text):
        if self._raises:
            raise self._raises
        return self._audio


@pytest.fixture(autouse=True)
def _clean_registry():
    """Snapshot and restore the registry so a provider registered by one test
    cannot leak into another test module's expectations."""
    import services.voice.providers as registry

    stt_snapshot = dict(registry._STT_FACTORIES)
    tts_snapshot = dict(registry._TTS_FACTORIES)
    reset_provider_cache()
    yield
    registry._STT_FACTORIES.clear()
    registry._STT_FACTORIES.update(stt_snapshot)
    registry._TTS_FACTORIES.clear()
    registry._TTS_FACTORIES.update(tts_snapshot)
    reset_provider_cache()


# --------------------------------------------------------------------------
# Registry / selection
# --------------------------------------------------------------------------


def test_local_providers_are_registered_by_name():
    assert isinstance(get_stt_provider("local"), LocalWhisperSTTProvider)
    assert isinstance(get_tts_provider("local"), LocalKokoroTTSProvider)


def test_providers_are_cached_per_name():
    assert get_stt_provider("local") is get_stt_provider("local")


def test_unknown_provider_raises_and_lists_alternatives():
    with pytest.raises(VoiceProviderUnavailable) as exc:
        get_stt_provider("nope")
    assert "nope" in str(exc.value)
    assert "local" in str(exc.value)


def test_new_provider_can_be_registered_without_touching_voice_logic():
    """The extension point the spec requires: add an engine, change no
    assistant code."""

    class FakeProvider(SpeechToTextProvider):
        name = "fake"

        @property
        def available(self):
            return True

        def transcribe(self, audio, *, language=None, sample_rate=None):
            return TranscriptResult(text="from fake", provider="fake")

    register_stt_provider("fake", FakeProvider)
    provider = get_stt_provider("fake")
    assert provider.available is True
    assert provider.transcribe(b"x").text == "from fake"


def test_reset_provider_cache_forces_rebuild():
    first = get_tts_provider("local")
    reset_provider_cache()
    assert get_tts_provider("local") is not first


# --------------------------------------------------------------------------
# STT adapter
# --------------------------------------------------------------------------


def test_stt_adapter_delegates_to_existing_service():
    service = StubSTTService(text="  find my meetings  ")
    provider = LocalWhisperSTTProvider(service)
    result = provider.transcribe(b"audio-bytes")
    assert result.text == "find my meetings"  # trimmed
    assert result.provider == "local"
    assert service.calls == [b"audio-bytes"]


def test_stt_adapter_empty_audio_is_not_an_error():
    provider = LocalWhisperSTTProvider(StubSTTService())
    result = provider.transcribe(b"")
    assert result.is_empty is True


def test_stt_adapter_reports_unavailable_when_whisper_missing():
    provider = LocalWhisperSTTProvider(StubSTTService(available=False))
    assert provider.available is False
    with pytest.raises(VoiceProviderUnavailable):
        provider.transcribe(b"audio")


def test_stt_adapter_ignores_other_providers():
    """It is the local adapter; it must not claim to serve endpoint/browser."""
    provider = LocalWhisperSTTProvider(StubSTTService(provider="endpoint:abc"))
    assert provider.available is False


def test_stt_adapter_maps_engine_failure_to_provider_error():
    provider = LocalWhisperSTTProvider(StubSTTService(raises=RuntimeError("boom")))
    with pytest.raises(VoiceProviderError):
        provider.transcribe(b"audio")


def test_stt_adapter_provider_failure_is_not_a_crash():
    """None from the engine becomes a typed error, never an AttributeError."""
    provider = LocalWhisperSTTProvider(StubSTTService(text=None))
    with pytest.raises(VoiceProviderError):
        provider.transcribe(b"audio")


# --------------------------------------------------------------------------
# TTS adapter
# --------------------------------------------------------------------------


def test_tts_adapter_delegates_and_returns_audio():
    provider = LocalKokoroTTSProvider(StubTTSService(audio=b"ID3xx"))
    assert provider.synthesize("hi") == b"ID3xx"


def test_tts_adapter_blank_text_returns_no_audio():
    provider = LocalKokoroTTSProvider(StubTTSService())
    assert provider.synthesize("   ") == b""


def test_tts_adapter_reports_unavailable():
    provider = LocalKokoroTTSProvider(StubTTSService(available=False))
    assert provider.available is False
    with pytest.raises(VoiceProviderUnavailable):
        provider.synthesize("hi")


def test_tts_stream_yields_the_buffer():
    provider = LocalKokoroTTSProvider(StubTTSService(audio=b"WAVEdata"))
    assert list(provider.stream("hi")) == [b"WAVEdata"]


def test_default_stream_is_single_chunk_and_never_silently_empty():
    """Base-class default must not pretend to stream incrementally."""
    provider = LocalKokoroTTSProvider(StubTTSService(audio=b"abc"))
    assert provider.supports_streaming is False
    assert list(provider.stream("x")) == [b"abc"]


def test_transcript_result_serialises_for_sse():
    result = TranscriptResult(text="hello", language="en", provider="local")
    payload = result.as_event()
    assert payload["type"] == "voice.transcript_final"
    assert payload["text"] == "hello"
    assert "confidence" not in payload  # omitted when unknown
