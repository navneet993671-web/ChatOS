"""Misantropic Voice — an interface layer over the existing assistant.

Voice does not own an AI engine. It captures audio, transcribes it, hands the
transcript to the *existing* chat/agent pipeline, and speaks the reply. Every
conversation, tool call, memory read and permission check goes through the same
code path as typed chat.
"""

from services.voice.service import (
    VoiceConfig,
    VoiceService,
    get_voice_service,
    load_voice_config,
    reset_voice_service,
)

__all__ = [
    "VoiceConfig",
    "VoiceService",
    "get_voice_service",
    "load_voice_config",
    "reset_voice_service",
]
