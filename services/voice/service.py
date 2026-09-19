"""Voice session service.

Boundaries this module holds:

* **No second AI engine.** The service never calls an LLM. It turns audio into
  a transcript and a reply into audio. The transcript is handed to the existing
  chat/agent pipeline (``POST /api/chat_stream``), which is what produces the
  reply — so conversations, memory, tools, RAG and permissions behave exactly
  as they do for typed input.
* **No second conversation store.** Sessions are rows in the existing
  ``sessions`` table; voice only adds a ``voice_sessions`` bookkeeping row that
  points at one.
* **No trusted client identity.** ``user_id`` always comes from the
  authenticated request, never from the body.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from services.voice.providers import (
    SpeechToTextProvider,
    TextToSpeechProvider,
    VoiceProviderError,
    VoiceProviderUnavailable,
    available_stt_providers,
    available_tts_providers,
    get_stt_provider,
    get_tts_provider,
)
from services.voice.vad import VoiceState, VoiceStateMachine, VadConfig

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid %s=%r; using %s", name, raw, default)
        return default


@dataclass(frozen=True)
class VoiceConfig:
    """Voice configuration, from ``VOICE_*`` environment variables."""

    enabled: bool = True
    save_audio: bool = False
    default_language: str = ""
    default_voice: str = ""
    silence_timeout_ms: int = 1200
    max_recording_seconds: int = 120
    stt_provider: str = "local"
    tts_provider: str = "local"

    def vad_config(self) -> VadConfig:
        return VadConfig(
            silence_timeout_ms=self.silence_timeout_ms,
            max_recording_seconds=self.max_recording_seconds,
        )


def load_voice_config() -> VoiceConfig:
    """Read voice configuration from the environment (falls back to defaults).

    Audio retention defaults to OFF: microphone audio is not written to disk
    unless the operator explicitly opts in with ``VOICE_SAVE_AUDIO=true``.
    """
    return VoiceConfig(
        enabled=_env_bool("VOICE_ENABLED", True),
        save_audio=_env_bool("VOICE_SAVE_AUDIO", False),
        default_language=os.environ.get("VOICE_DEFAULT_LANGUAGE", "").strip(),
        default_voice=os.environ.get("VOICE_DEFAULT_VOICE", "").strip(),
        silence_timeout_ms=_env_int("VOICE_SILENCE_TIMEOUT_MS", 1200),
        max_recording_seconds=_env_int("VOICE_MAX_RECORDING_SECONDS", 120),
        stt_provider=os.environ.get("VOICE_STT_PROVIDER", "local").strip() or "local",
        tts_provider=os.environ.get("VOICE_TTS_PROVIDER", "local").strip() or "local",
    )


_SAFE = re.compile(r"[^A-Za-z0-9._@-]")


def safe_user_component(user_id: str) -> str:
    """Make a username safe to use as a single path component.

    Defence in depth: a username is attacker-influenced only if signup allows
    odd characters, but voice audio must never be able to escape its
    user-scoped directory regardless.
    """
    cleaned = _SAFE.sub("_", (user_id or "").strip())
    cleaned = cleaned.lstrip(".")  # no "." / ".." components
    return cleaned or "unknown"


class VoiceService:
    """Owns voice session lifecycle, transcription and synthesis."""

    def __init__(
        self,
        config: Optional[VoiceConfig] = None,
        *,
        data_dir: str = "data",
        stt_provider: Optional[SpeechToTextProvider] = None,
        tts_provider: Optional[TextToSpeechProvider] = None,
    ) -> None:
        self.config = config or load_voice_config()
        self.data_dir = Path(data_dir)
        self._stt_override = stt_provider
        self._tts_override = tts_provider
        # Live session state; DB is the durable record.
        self._states: Dict[str, VoiceStateMachine] = {}

    # -- configuration ---------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def stt(self) -> SpeechToTextProvider:
        if self._stt_override is not None:
            return self._stt_override
        return get_stt_provider(self.config.stt_provider)

    def tts(self) -> TextToSpeechProvider:
        if self._tts_override is not None:
            return self._tts_override
        return get_tts_provider(self.config.tts_provider)

    def settings_payload(self) -> Dict[str, Any]:
        """Capability report for the settings panel (no secrets)."""
        from services.voice.schemas import VOICE_MODES, VOICE_STATUSES

        stt_ok = False
        tts_ok = False
        try:
            stt_ok = bool(self.stt().available)
        except VoiceProviderError:
            stt_ok = False
        try:
            tts_ok = bool(self.tts().available)
        except VoiceProviderError:
            tts_ok = False

        return {
            "enabled": self.config.enabled,
            "save_audio": self.config.save_audio,
            "default_language": self.config.default_language,
            "default_voice": self.config.default_voice,
            "silence_timeout_ms": self.config.silence_timeout_ms,
            "max_recording_seconds": self.config.max_recording_seconds,
            "stt_provider": self.config.stt_provider,
            "tts_provider": self.config.tts_provider,
            "stt_available": stt_ok,
            "tts_available": tts_ok,
            "stt_providers": available_stt_providers(),
            "tts_providers": available_tts_providers(),
            "modes": list(VOICE_MODES),
            "statuses": list(VOICE_STATUSES),
        }

    # -- session lifecycle -----------------------------------------------

    def state_machine(self, session_id: str) -> VoiceStateMachine:
        """Per-session state machine. In-memory: state is ephemeral by nature,
        the durable record is the ``voice_sessions`` row."""
        machine = self._states.get(session_id)
        if machine is None:
            machine = VoiceStateMachine(VoiceState.IDLE)
            self._states[session_id] = machine
        return machine

    def start_session(
        self,
        user_id: str,
        *,
        conversation_id: Optional[str] = None,
        language: Optional[str] = None,
        mode: str = "push_to_talk",
        voice: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a voice session owned by ``user_id``.

        The conversation must already exist and belong to the caller — checked
        here rather than trusted from the request body.
        """
        if not user_id:
            raise PermissionError("a voice session requires an authenticated user")
        if not self.enabled:
            raise VoiceProviderUnavailable("voice is disabled (VOICE_ENABLED=false)")

        if conversation_id:
            self._assert_conversation_owned(user_id, conversation_id)

        from core.database import SessionLocal, VoiceSession

        row = VoiceSession(
            id=str(uuid.uuid4()),
            user_id=user_id,
            conversation_id=conversation_id,
            status=VoiceState.LISTENING.value,
            language=language or self.config.default_language or None,
            voice=voice or self.config.default_voice or None,
            provider=self.config.stt_provider,
            mode=mode if mode in ("push_to_talk", "conversation") else "push_to_talk",
            started_at=datetime.utcnow(),
        )
        db = SessionLocal()
        try:
            db.add(row)
            db.commit()
            db.refresh(row)
            payload = self._serialize(row)
        finally:
            db.close()

        machine = self.state_machine(row.id)
        machine.transition(VoiceState.LISTENING)
        logger.info("Voice session %s started for %s", row.id, user_id)
        return payload

    def end_session(self, user_id: str, session_id: str) -> Dict[str, Any]:
        """Mark a session ended and drop its temp audio."""
        from core.database import SessionLocal

        db = SessionLocal()
        try:
            row = self._get_owned_row(db, user_id, session_id)
            row.status = "ended"
            row.ended_at = datetime.utcnow()
            db.commit()
            db.refresh(row)
            payload = self._serialize(row)
        finally:
            db.close()

        self._states.pop(session_id, None)
        self.purge_audio(user_id, session_id)
        logger.info("Voice session %s ended", session_id)
        return payload

    def get_session(self, user_id: str, session_id: str) -> Dict[str, Any]:
        from core.database import SessionLocal

        db = SessionLocal()
        try:
            return self._serialize(self._get_owned_row(db, user_id, session_id))
        finally:
            db.close()

    def list_sessions(self, user_id: str, limit: int = 50) -> list[Dict[str, Any]]:
        from core.database import SessionLocal, VoiceSession

        db = SessionLocal()
        try:
            rows = (
                db.query(VoiceSession)
                .filter(VoiceSession.user_id == user_id)
                .order_by(VoiceSession.created_at.desc())
                .limit(max(1, min(limit, 200)))
                .all()
            )
            return [self._serialize(r) for r in rows]
        finally:
            db.close()

    # -- speech -----------------------------------------------------------

    def transcribe(
        self,
        user_id: str,
        session_id: str,
        audio: bytes,
        *,
        language: Optional[str] = None,
    ):
        """Transcribe one utterance for an owned, live session.

        Returns a :class:`~services.voice.providers.base.TranscriptResult`.
        Empty audio yields an empty transcript rather than an error.
        """
        from core.database import SessionLocal

        db = SessionLocal()
        try:
            row = self._get_owned_row(db, user_id, session_id)
            if row.status == "ended":
                raise PermissionError("voice session has ended")
        finally:
            db.close()

        if not audio:
            from services.voice.providers import TranscriptResult

            return TranscriptResult(text="", provider=self.stt().name)

        machine = self.state_machine(session_id)
        if machine.can_transition_to(VoiceState.PROCESSING):
            machine.transition(VoiceState.PROCESSING)

        self._maybe_persist_audio(user_id, session_id, audio)
        try:
            result = self.stt().transcribe(
                audio, language=language or self.config.default_language or None
            )
        finally:
            # Default posture: do not keep microphone audio around.
            if not self.config.save_audio:
                self.purge_audio(user_id, session_id)
        return result

    def synthesize(
        self,
        user_id: str,
        session_id: str,
        text: str,
        *,
        voice: Optional[str] = None,
        speed: Optional[float] = None,
        language: Optional[str] = None,
    ) -> bytes:
        """Synthesise the assistant reply for an owned session."""
        from core.database import SessionLocal

        db = SessionLocal()
        try:
            self._get_owned_row(db, user_id, session_id)
        finally:
            db.close()

        if not text or not text.strip():
            return b""

        machine = self.state_machine(session_id)
        if machine.can_transition_to(VoiceState.SPEAKING):
            machine.transition(VoiceState.SPEAKING)
        try:
            return self.tts().synthesize(
                text,
                voice=voice or self.config.default_voice or None,
                speed=speed,
                language=language or self.config.default_language or None,
            )
        finally:
            if machine.can_transition_to(VoiceState.LISTENING):
                machine.transition(VoiceState.LISTENING)

    # -- audio retention --------------------------------------------------

    def audio_dir(self, user_id: str) -> Path:
        """User-scoped directory for saved audio (never shared across users)."""
        return self.data_dir / "voice" / safe_user_component(user_id)

    def _maybe_persist_audio(self, user_id: str, session_id: str, audio: bytes) -> None:
        if not self.config.save_audio:
            return
        try:
            target_dir = self.audio_dir(user_id)
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / f"{safe_user_component(session_id)}.webm").write_bytes(audio)
        except Exception as exc:  # never fail a session over an optional write
            logger.warning("Could not persist voice audio: %s", exc)

    def purge_audio(self, user_id: str, session_id: str) -> int:
        """Delete temporary audio for a session. Returns files removed."""
        target_dir = self.audio_dir(user_id)
        if not target_dir.is_dir():
            return 0
        removed = 0
        try:
            for path in target_dir.glob(f"{safe_user_component(session_id)}.*"):
                # Belt and braces: never delete outside the user's directory.
                if target_dir in path.resolve().parents:
                    path.unlink(missing_ok=True)
                    removed += 1
        except Exception as exc:
            logger.warning("Could not purge voice audio for %s: %s", session_id, exc)
        return removed

    # -- internals --------------------------------------------------------

    @staticmethod
    def _serialize(row) -> Dict[str, Any]:
        def iso(value):
            return value.isoformat() if value else None

        return {
            "id": row.id,
            "user_id": row.user_id,
            "conversation_id": row.conversation_id,
            "status": row.status,
            "language": row.language,
            "voice": row.voice,
            "provider": row.provider,
            "mode": getattr(row, "mode", None),
            "started_at": iso(row.started_at),
            "ended_at": iso(row.ended_at),
            "created_at": iso(row.created_at),
        }

    @staticmethod
    def _get_owned_row(db, user_id: str, session_id: str):
        """Fetch a session by id **scoped to the caller**.

        Not-found and not-yours are both 404 at the route layer, so a caller
        cannot probe which session ids exist. Returns the ORM row.
        """
        from core.database import VoiceSession
        from core.exceptions import SessionNotFoundError

        row = (
            db.query(VoiceSession)
            .filter(VoiceSession.id == session_id, VoiceSession.user_id == user_id)
            .first()
        )
        if row is None:
            raise SessionNotFoundError(f"Voice session {session_id} not found")
        return row

    @staticmethod
    def _assert_conversation_owned(user_id: str, conversation_id: str) -> None:
        """Reuse the chat session table as the conversation store.

        Voice attaches to the same conversations as text — it must never be
        able to write into someone else's.
        """
        from core.database import Session as DbSession, SessionLocal
        from core.exceptions import SessionNotFoundError

        db = SessionLocal()
        try:
            row = db.query(DbSession).filter(DbSession.id == conversation_id).first()
            if row is None:
                raise SessionNotFoundError(
                    f"Conversation {conversation_id} not found"
                )
            owner = getattr(row, "owner", None)
            if owner != user_id:
                # Same 404 as missing: do not disclose existence.
                raise SessionNotFoundError(
                    f"Conversation {conversation_id} not found"
                )
        finally:
            db.close()


_service: Optional[VoiceService] = None


def get_voice_service() -> VoiceService:
    """Module-level singleton, matching how STT/TTS services are wired."""
    global _service
    if _service is None:
        _service = VoiceService()
    return _service


def reset_voice_service() -> None:
    """Drop the singleton (tests, and re-reading config after a change)."""
    global _service
    _service = None
