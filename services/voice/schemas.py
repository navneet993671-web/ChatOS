"""Pydantic schemas for the voice API.

Response models never carry raw audio, provider credentials, or another user's
data. ``VoiceSessionOut`` intentionally omits nothing about *its own* owner,
and is only ever built for the authenticated user.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

# Keep the allowed voice states and settings bounds in one place so the API
# and the service cannot drift apart.
VOICE_STATUSES = (
    "idle",
    "listening",
    "speech_detected",
    "processing",
    "speaking",
    "interrupted",
    "error",
    "ended",
)

VOICE_MODES = ("push_to_talk", "conversation")


class VoiceSessionCreate(BaseModel):
    """Start a voice session.

    ``conversation_id`` binds the session to an existing chat session so the
    transcript lands in the same conversation as text chat. It is optional:
    the client may start a fresh one.
    """

    conversation_id: Optional[str] = None
    language: Optional[str] = None
    mode: str = Field(default="push_to_talk")
    voice: Optional[str] = None


class VoiceSessionOut(BaseModel):
    id: str
    user_id: str
    conversation_id: Optional[str] = None
    status: str
    language: Optional[str] = None
    voice: Optional[str] = None
    provider: Optional[str] = None
    mode: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    created_at: Optional[str] = None


class VoiceSessionList(BaseModel):
    sessions: List[VoiceSessionOut]
    count: int


class VoiceSettingsOut(BaseModel):
    """Effective, env-driven voice configuration for the settings panel.

    This reports capability, never secrets. Provider API keys stay server-side
    and are never included here.
    """

    enabled: bool
    save_audio: bool
    default_language: str
    default_voice: str
    silence_timeout_ms: int
    max_recording_seconds: int
    stt_provider: str
    tts_provider: str
    stt_available: bool
    tts_available: bool
    stt_providers: Dict[str, bool]
    tts_providers: Dict[str, bool]
    modes: List[str]
    statuses: List[str]


class VoiceSettingsUpdate(BaseModel):
    """Subset of voice settings a user may change at runtime."""

    language: Optional[str] = None
    voice: Optional[str] = None
    mode: Optional[str] = None


class VoiceTranscribeOut(BaseModel):
    session_id: str
    text: str
    language: str = ""
    confidence: Optional[float] = None
    provider: str = ""
    empty: bool = False
    segments: List[Dict[str, Any]] = Field(default_factory=list)


class VoiceSynthesizeIn(BaseModel):
    text: str
    voice: Optional[str] = None
    speed: Optional[float] = None
    language: Optional[str] = None


class VoiceErrorOut(BaseModel):
    """Typed failure so the UI can render a specific message instead of a
    generic 500 and never crash the Chat page."""

    error: str
    code: str
    detail: Optional[str] = None
