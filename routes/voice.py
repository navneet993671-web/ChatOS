"""Voice assistant REST routes.

Slice 1 surface: settings, session lifecycle, transcribe and synthesize over
plain HTTP. That is enough for push-to-talk on its own, and it is what the
WebSocket conversation mode (Slice 2) will call into rather than duplicate.

Every route resolves the caller with the repo's existing
``get_current_user(request)`` and passes *that* username to the service. A
``user_id`` in a request body is never trusted — the schemas do not even accept
one.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import Response

from src.auth_helpers import get_current_user
from services.voice.providers import VoiceProviderError, VoiceProviderUnavailable
from services.voice.schemas import (
    VoiceSessionCreate,
    VoiceSettingsOut,
    VoiceSynthesizeIn,
    VoiceTranscribeOut,
)

logger = logging.getLogger(__name__)


def _require_user(request: Request) -> str:
    """Resolve the authenticated user or fail closed.

    Voice is a privileged capability (microphone + agent tools), so an
    unauthenticated caller is rejected outright rather than falling back to a
    shared/legacy owner.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Authentication required")
    return user


def _handle(exc: Exception) -> HTTPException:
    """Map service failures onto typed HTTP errors.

    A provider problem must surface as a specific, renderable error — never a
    bare 500 that could take the Chat page down with it.
    """
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail={"code": "forbidden", "message": str(exc)})
    if isinstance(exc, VoiceProviderUnavailable):
        return HTTPException(
            status_code=503,
            detail={"code": "voice_provider_unavailable", "message": str(exc)},
        )
    if isinstance(exc, VoiceProviderError):
        return HTTPException(
            status_code=502,
            detail={"code": "voice_provider_error", "message": str(exc)},
        )
    logger.error("Voice route error: %s", exc, exc_info=True)
    return HTTPException(
        status_code=500, detail={"code": "voice_error", "message": "Voice request failed"}
    )


def setup_voice_routes(voice_service) -> APIRouter:
    """Build the voice router around an injected service."""
    router = APIRouter(prefix="/api/voice", tags=["voice"])

    # -- settings --------------------------------------------------------

    @router.get("/settings", response_model=VoiceSettingsOut)
    async def get_voice_settings(request: Request):
        """Effective voice config + provider capability. Never returns keys."""
        _require_user(request)
        try:
            return voice_service.settings_payload()
        except Exception as exc:
            raise _handle(exc)

    # -- sessions --------------------------------------------------------

    @router.post("/session")
    async def create_voice_session(request: Request, payload: VoiceSessionCreate):
        """Start a voice session owned by the authenticated user."""
        user = _require_user(request)
        try:
            return voice_service.start_session(
                user,
                conversation_id=payload.conversation_id,
                language=payload.language,
                mode=payload.mode,
                voice=payload.voice,
            )
        except Exception as exc:
            raise _handle(exc)

    @router.get("/sessions")
    async def list_voice_sessions(request: Request, limit: int = 50):
        """List only the caller's own voice sessions."""
        user = _require_user(request)
        try:
            sessions = voice_service.list_sessions(user, limit=limit)
            return {"sessions": sessions, "count": len(sessions)}
        except Exception as exc:
            raise _handle(exc)

    @router.get("/session/{session_id}")
    async def get_voice_session(request: Request, session_id: str):
        user = _require_user(request)
        try:
            return voice_service.get_session(user, session_id)
        except Exception as exc:
            raise _from_session_lookup(exc)

    @router.post("/session/{session_id}/end")
    async def end_voice_session(request: Request, session_id: str):
        """End a session and purge any temporary audio for it."""
        user = _require_user(request)
        try:
            return voice_service.end_session(user, session_id)
        except Exception as exc:
            raise _from_session_lookup(exc)

    # -- speech ----------------------------------------------------------

    @router.post("/session/{session_id}/transcribe", response_model=VoiceTranscribeOut)
    async def transcribe(request: Request, session_id: str, file: UploadFile = File(...)):
        """Transcribe one utterance recorded by the browser."""
        user = _require_user(request)
        try:
            audio = await file.read()
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_audio", "message": f"Could not read audio: {exc}"},
            )
        try:
            result = voice_service.transcribe(user, session_id, audio)
            return {
                "session_id": session_id,
                "text": result.text,
                "language": result.language,
                "confidence": result.confidence,
                "provider": result.provider,
                "empty": result.is_empty,
                "segments": [
                    {"text": s.text, "start": s.start, "end": s.end} for s in result.segments
                ],
            }
        except Exception as exc:
            raise _from_session_lookup(exc)

    @router.post("/session/{session_id}/synthesize")
    async def synthesize(request: Request, session_id: str, payload: VoiceSynthesizeIn):
        """Synthesize the assistant's reply as audio."""
        user = _require_user(request)
        try:
            audio = voice_service.synthesize(
                user,
                session_id,
                payload.text,
                voice=payload.voice,
                speed=payload.speed,
                language=payload.language,
            )
        except Exception as exc:
            raise _from_session_lookup(exc)

        if not audio:
            raise HTTPException(
                status_code=400,
                detail={"code": "empty_text", "message": "No text to synthesize"},
            )

        is_mp3 = audio[:3] == b"ID3" or (
            len(audio) >= 2 and audio[0] == 0xFF and (audio[1] & 0xE0) == 0xE0
        )
        mime = "audio/mpeg" if is_mp3 else "audio/wav"
        return Response(
            content=audio,
            media_type=mime,
            headers={"Cache-Control": "no-store"},
        )

    @router.delete("/session/{session_id}/audio")
    async def purge_audio(request: Request, session_id: str):
        """Delete any retained audio for this session (user-scoped)."""
        user = _require_user(request)
        try:
            # Ownership first: purge only ever touches the caller's directory.
            voice_service.get_session(user, session_id)
            removed = voice_service.purge_audio(user, session_id)
            return {"removed": removed}
        except Exception as exc:
            raise _from_session_lookup(exc)

    return router


def _from_session_lookup(exc: Exception) -> HTTPException:
    """Not-found and not-yours both become 404, so session ids cannot be probed."""
    from core.exceptions import SessionNotFoundError

    if isinstance(exc, SessionNotFoundError):
        return HTTPException(
            status_code=404,
            detail={"code": "voice_session_not_found", "message": str(exc)},
        )
    return _handle(exc)
