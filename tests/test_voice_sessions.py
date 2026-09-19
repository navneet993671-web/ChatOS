"""Voice session lifecycle, ownership and audio-retention tests.

These run against a temporary SQLite database. They must never touch
``data/app.db`` — that is the user's live data.
"""

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.exceptions import SessionNotFoundError
from services.voice.providers import (
    SpeechToTextProvider,
    TextToSpeechProvider,
    VoiceProviderUnavailable,
)
from services.voice.providers.base import TranscriptResult
from services.voice.service import VoiceConfig, VoiceService, safe_user_component
from services.voice.vad import VoiceState


class StubSTT(SpeechToTextProvider):
    name = "stub-stt"

    def __init__(self, text="hello world"):
        self._text = text
        self.seen = []

    @property
    def available(self):
        return True

    def transcribe(self, audio, *, language=None, sample_rate=None):
        self.seen.append((audio, language))
        return TranscriptResult(text=self._text, language=language or "", provider=self.name)


class StubTTS(TextToSpeechProvider):
    name = "stub-tts"

    def __init__(self, audio=b"ID3audio"):
        self._audio = audio

    @property
    def available(self):
        return True

    def synthesize(self, text, *, voice=None, speed=None, language=None):
        return self._audio


@pytest.fixture
def db(tmp_path, monkeypatch, real_core_database):
    """A throwaway database bound to the real models.

    ``real_core_database`` comes from conftest, which captures the genuine
    module before any test stubs ``sys.modules['core.database']``. Patching the
    sys.modules entry (rather than the module object) is deliberate: the voice
    service imports these names inside each method, so it resolves the genuine
    module even after another test module replaced the entry with a mock.
    """
    real = real_core_database
    monkeypatch.setitem(sys.modules, "core.database", real)

    engine = create_engine(
        f"sqlite:///{tmp_path / 'voice-test.db'}",
        connect_args={"check_same_thread": False},
    )
    real.Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    monkeypatch.setattr(real, "SessionLocal", SessionLocal)
    return SessionLocal


def make_conversation(SessionLocal, conversation_id="conv-1", owner="alice"):
    from core.database import Session as DbSession  # the patched, genuine module

    session = SessionLocal()
    try:
        session.add(
            DbSession(
                id=conversation_id,
                name="Test chat",
                endpoint_url="http://localhost:11434/v1",
                model="llama3",
                owner=owner,
            )
        )
        session.commit()
    finally:
        session.close()


def make_service(tmp_path, **cfg):
    config = VoiceConfig(**{"save_audio": False, **cfg})
    return VoiceService(
        config,
        data_dir=str(tmp_path / "data"),
        stt_provider=StubSTT(),
        tts_provider=StubTTS(),
    )


# --------------------------------------------------------------------------
# Path safety
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expect_safe",
    [
        ("alice", "alice"),
        ("..", "unknown"),
        ("", "unknown"),
        ("a/b\\c", "a_b_c"),
        (".hidden", "hidden"),
    ],
)
def test_safe_user_component_sanitises(raw, expect_safe):
    assert safe_user_component(raw) == expect_safe


def test_safe_user_component_neutralises_traversal():
    """A username must never be able to escape its user-scoped audio dir."""
    for raw in ["../../etc/passwd", "..\\..\\windows", "a/../../b", ".../.../x"]:
        cleaned = safe_user_component(raw)
        assert "/" not in cleaned and "\\" not in cleaned
        assert cleaned not in (".", "..")
        assert not cleaned.startswith(".")


def test_audio_dir_is_user_scoped(tmp_path):
    service = make_service(tmp_path)
    assert service.audio_dir("alice") != service.audio_dir("bob")
    assert "alice" in str(service.audio_dir("alice"))


# --------------------------------------------------------------------------
# Session lifecycle + authentication
# --------------------------------------------------------------------------


def test_start_session_requires_a_user(tmp_path, db):
    service = make_service(tmp_path)
    with pytest.raises(PermissionError):
        service.start_session("")


def test_start_session_refused_when_voice_disabled(tmp_path, db):
    service = make_service(tmp_path, enabled=False)
    with pytest.raises(VoiceProviderUnavailable):
        service.start_session("alice")


def test_start_session_creates_row_owned_by_caller(tmp_path, db):
    service = make_service(tmp_path)
    row = service.start_session("alice", mode="push_to_talk")
    assert row["user_id"] == "alice"
    assert row["status"] == VoiceState.LISTENING.value
    assert row["started_at"] is not None
    assert row["ended_at"] is None


def test_session_is_retrievable_by_owner(tmp_path, db):
    service = make_service(tmp_path)
    created = service.start_session("alice")
    assert service.get_session("alice", created["id"])["id"] == created["id"]


def test_other_user_cannot_read_a_session(tmp_path, db):
    """Not-yours must be indistinguishable from not-found."""
    service = make_service(tmp_path)
    created = service.start_session("alice")
    with pytest.raises(SessionNotFoundError):
        service.get_session("bob", created["id"])


def test_other_user_cannot_end_a_session(tmp_path, db):
    service = make_service(tmp_path)
    created = service.start_session("alice")
    with pytest.raises(SessionNotFoundError):
        service.end_session("bob", created["id"])


def test_unknown_session_id_raises_not_found(tmp_path, db):
    service = make_service(tmp_path)
    with pytest.raises(SessionNotFoundError):
        service.get_session("alice", "does-not-exist")


def test_list_sessions_is_scoped_to_the_caller(tmp_path, db):
    service = make_service(tmp_path)
    service.start_session("alice")
    service.start_session("alice")
    service.start_session("bob")
    alice = service.list_sessions("alice")
    assert len(alice) == 2
    assert all(s["user_id"] == "alice" for s in alice)


def test_end_session_marks_ended(tmp_path, db):
    service = make_service(tmp_path)
    created = service.start_session("alice")
    ended = service.end_session("alice", created["id"])
    assert ended["status"] == "ended"
    assert ended["ended_at"] is not None


def test_transcribe_after_end_is_rejected(tmp_path, db):
    service = make_service(tmp_path)
    created = service.start_session("alice")
    service.end_session("alice", created["id"])
    with pytest.raises(PermissionError):
        service.transcribe("alice", created["id"], b"audio")


# --------------------------------------------------------------------------
# Conversation binding — voice reuses the chat session table
# --------------------------------------------------------------------------


def test_start_session_binds_an_owned_conversation(tmp_path, db):
    make_conversation(db, "conv-1", owner="alice")
    service = make_service(tmp_path)
    row = service.start_session("alice", conversation_id="conv-1")
    assert row["conversation_id"] == "conv-1"


def test_start_session_rejects_someone_elses_conversation(tmp_path, db):
    make_conversation(db, "conv-bob", owner="bob")
    service = make_service(tmp_path)
    with pytest.raises(SessionNotFoundError):
        service.start_session("alice", conversation_id="conv-bob")


def test_start_session_rejects_missing_conversation(tmp_path, db):
    service = make_service(tmp_path)
    with pytest.raises(SessionNotFoundError):
        service.start_session("alice", conversation_id="ghost")


# --------------------------------------------------------------------------
# Transcription + synthesis
# --------------------------------------------------------------------------


def test_transcribe_returns_provider_result(tmp_path, db):
    service = make_service(tmp_path)
    created = service.start_session("alice")
    result = service.transcribe("alice", created["id"], b"audio-bytes")
    assert result.text == "hello world"
    assert result.provider == "stub-stt"


def test_transcribe_empty_audio_yields_empty_transcript(tmp_path, db):
    service = make_service(tmp_path)
    created = service.start_session("alice")
    result = service.transcribe("alice", created["id"], b"")
    assert result.is_empty is True


def test_transcribe_other_users_session_is_rejected(tmp_path, db):
    service = make_service(tmp_path)
    created = service.start_session("alice")
    with pytest.raises(SessionNotFoundError):
        service.transcribe("bob", created["id"], b"audio")


def test_synthesize_blank_text_returns_no_audio(tmp_path, db):
    service = make_service(tmp_path)
    created = service.start_session("alice")
    assert service.synthesize("alice", created["id"], "   ") == b""


def test_synthesize_other_users_session_is_rejected(tmp_path, db):
    service = make_service(tmp_path)
    created = service.start_session("alice")
    with pytest.raises(SessionNotFoundError):
        service.synthesize("bob", created["id"], "hello")


# --------------------------------------------------------------------------
# Audio retention — OFF by default, user-isolated when ON
# --------------------------------------------------------------------------


def test_audio_is_not_retained_by_default(tmp_path, db):
    service = make_service(tmp_path, save_audio=False)
    created = service.start_session("alice")
    service.transcribe("alice", created["id"], b"raw-mic-audio")
    leftover = list((service.audio_dir("alice")).glob("*")) if service.audio_dir("alice").exists() else []
    assert leftover == [], "microphone audio must not persist unless opted in"


def test_audio_is_retained_when_explicitly_enabled(tmp_path, db):
    service = make_service(tmp_path, save_audio=True)
    created = service.start_session("alice")
    service.transcribe("alice", created["id"], b"raw-mic-audio")

    saved = list(service.audio_dir("alice").glob("*.webm"))
    assert len(saved) == 1
    assert saved[0].read_bytes() == b"raw-mic-audio"
    # ...and it lives under the owner's own directory.
    assert "alice" in str(saved[0])


def test_retained_audio_is_user_isolated(tmp_path, db):
    service = make_service(tmp_path, save_audio=True)
    a = service.start_session("alice")
    b = service.start_session("bob")
    service.transcribe("alice", a["id"], b"alice-audio")
    service.transcribe("bob", b["id"], b"bob-audio")

    alice_files = [p.read_bytes() for p in service.audio_dir("alice").glob("*.webm")]
    bob_files = [p.read_bytes() for p in service.audio_dir("bob").glob("*.webm")]
    assert alice_files == [b"alice-audio"]
    assert bob_files == [b"bob-audio"]


def test_purge_removes_only_that_sessions_audio(tmp_path, db):
    service = make_service(tmp_path, save_audio=True)
    a = service.start_session("alice")
    b = service.start_session("alice")
    service.transcribe("alice", a["id"], b"audio-a")
    service.transcribe("alice", b["id"], b"audio-b")

    assert service.purge_audio("alice", a["id"]) == 1
    remaining = [p.name for p in service.audio_dir("alice").glob("*.webm")]
    assert remaining == [f"{b['id']}.webm"]


def test_purge_for_unknown_session_is_a_noop(tmp_path, db):
    service = make_service(tmp_path)
    assert service.purge_audio("alice", "never-existed") == 0


def test_end_session_purges_retained_audio(tmp_path, db):
    service = make_service(tmp_path, save_audio=True)
    created = service.start_session("alice")
    service.transcribe("alice", created["id"], b"audio")
    assert list(service.audio_dir("alice").glob("*.webm"))

    service.end_session("alice", created["id"])
    assert list(service.audio_dir("alice").glob("*.webm")) == []


# --------------------------------------------------------------------------
# Settings payload
# --------------------------------------------------------------------------


def test_settings_payload_reports_capability_without_secrets(tmp_path):
    service = make_service(tmp_path)
    payload = service.settings_payload()
    assert payload["enabled"] is True
    assert payload["save_audio"] is False
    assert payload["silence_timeout_ms"] == 1200
    assert payload["max_recording_seconds"] == 120
    assert payload["stt_available"] is True
    assert "push_to_talk" in payload["modes"]
    # No provider credentials may ever appear here.
    assert not any("key" in k.lower() or "secret" in k.lower() for k in payload)


def test_settings_payload_survives_missing_providers(tmp_path):
    """A missing engine must report unavailable, not raise — otherwise the
    settings panel would break the whole Chat UI."""
    service = VoiceService(VoiceConfig(), data_dir=str(tmp_path / "d"))
    payload = service.settings_payload()
    # The 'local' engine requires faster-whisper/Kokoro and is not installed
    # in CI, so both report unavailable rather than exploding.
    assert payload["stt_available"] is False
    assert payload["tts_available"] is False
    assert payload["stt_providers"].get("local") is False
    assert payload["tts_providers"].get("local") is False
