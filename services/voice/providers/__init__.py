"""Voice provider registry.

Provider selection is by name so the voice service never imports a concrete
engine. To add an engine, implement the relevant ABC and register it here —
no change to ``VoiceService`` is required.

``browser`` is intentionally absent: when the client synthesises or recognises
speech itself, the browser *is* the provider and no server work happens. The
voice service treats that as a distinct mode rather than pretending there is a
backend provider for it.
"""

from __future__ import annotations

from typing import Callable, Dict

from services.voice.providers.base import (
    SpeechToTextProvider,
    TextToSpeechProvider,
    TranscriptResult,
    TranscriptSegment,
    VadResult,
    VoiceActivityDetector,
    VoiceProviderError,
    VoiceProviderUnavailable,
)
from services.voice.providers.local_stt import LocalWhisperSTTProvider
from services.voice.providers.local_tts import LocalKokoroTTSProvider

__all__ = [
    "SpeechToTextProvider",
    "TextToSpeechProvider",
    "TranscriptResult",
    "TranscriptSegment",
    "VadResult",
    "VoiceActivityDetector",
    "VoiceProviderError",
    "VoiceProviderUnavailable",
    "LocalWhisperSTTProvider",
    "LocalKokoroTTSProvider",
    "register_stt_provider",
    "register_tts_provider",
    "get_stt_provider",
    "get_tts_provider",
    "available_stt_providers",
    "available_tts_providers",
    "reset_provider_cache",
]

_STT_FACTORIES: Dict[str, Callable[[], SpeechToTextProvider]] = {
    "local": LocalWhisperSTTProvider,
}
_TTS_FACTORIES: Dict[str, Callable[[], TextToSpeechProvider]] = {
    "local": LocalKokoroTTSProvider,
}

# Providers are cheap wrappers around module-level singletons, but caching
# keeps a session from rebuilding them on every frame.
_STT_CACHE: Dict[str, SpeechToTextProvider] = {}
_TTS_CACHE: Dict[str, TextToSpeechProvider] = {}


def register_stt_provider(name: str, factory: Callable[[], SpeechToTextProvider]) -> None:
    """Register an STT engine under ``name`` (e.g. ``"endpoint:abc123"``)."""
    _STT_FACTORIES[name] = factory
    _STT_CACHE.pop(name, None)


def register_tts_provider(name: str, factory: Callable[[], TextToSpeechProvider]) -> None:
    """Register a TTS engine under ``name``."""
    _TTS_FACTORIES[name] = factory
    _TTS_CACHE.pop(name, None)


def get_stt_provider(name: str) -> SpeechToTextProvider:
    """Build (or reuse) the STT provider registered as ``name``."""
    if name in _STT_CACHE:
        return _STT_CACHE[name]
    factory = _STT_FACTORIES.get(name)
    if factory is None:
        raise VoiceProviderUnavailable(
            f"unknown STT provider {name!r}; registered: "
            f"{sorted(_STT_FACTORIES)} (plus 'browser', handled client-side)"
        )
    provider = factory()
    _STT_CACHE[name] = provider
    return provider


def get_tts_provider(name: str) -> TextToSpeechProvider:
    """Build (or reuse) the TTS provider registered as ``name``."""
    if name in _TTS_CACHE:
        return _TTS_CACHE[name]
    factory = _TTS_FACTORIES.get(name)
    if factory is None:
        raise VoiceProviderUnavailable(
            f"unknown TTS provider {name!r}; registered: "
            f"{sorted(_TTS_FACTORIES)} (plus 'browser', handled client-side)"
        )
    provider = factory()
    _TTS_CACHE[name] = provider
    return provider


def available_stt_providers() -> Dict[str, bool]:
    """Map of name -> currently usable. Used by the settings panel."""
    out: Dict[str, bool] = {}
    for name in sorted(_STT_FACTORIES):
        try:
            out[name] = bool(get_stt_provider(name).available)
        except Exception:
            out[name] = False
    return out


def available_tts_providers() -> Dict[str, bool]:
    out: Dict[str, bool] = {}
    for name in sorted(_TTS_FACTORIES):
        try:
            out[name] = bool(get_tts_provider(name).available)
        except Exception:
            out[name] = False
    return out


def reset_provider_cache() -> None:
    """Drop cached providers. For tests and after a settings change."""
    _STT_CACHE.clear()
    _TTS_CACHE.clear()
