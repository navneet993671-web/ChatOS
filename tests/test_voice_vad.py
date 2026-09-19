"""Voice state machine + voice activity detection tests.

These cover the two failure modes the spec calls out by name: recording
forever, and getting stuck listening. Both are asserted directly.
"""

import pytest

from services.voice.vad import (
    EnergyVoiceActivityDetector,
    InvalidVoiceTransition,
    VadConfig,
    VoiceState,
    VoiceStateMachine,
    frame_rms,
)

FRAME_MS = 20


def loud(frames: int = 1, amplitude: int = 3000) -> bytes:
    """16-bit PCM with energy well above the default threshold."""
    return (amplitude.to_bytes(2, "little", signed=True)) * 320 * frames


def quiet(frames: int = 1) -> bytes:
    return b"\x00\x00" * 320 * frames


# --------------------------------------------------------------------------
# frame_rms
# --------------------------------------------------------------------------


def test_frame_rms_of_silence_is_zero():
    assert frame_rms(quiet()) == 0.0


def test_frame_rms_matches_amplitude_for_constant_signal():
    assert frame_rms(loud(amplitude=1000)) == pytest.approx(1000.0)


def test_frame_rms_tolerates_empty_and_odd_length_frames():
    assert frame_rms(b"") == 0.0
    assert frame_rms(b"\x01") == 0.0  # a stray half-sample must not raise


# --------------------------------------------------------------------------
# VadConfig validation — bad config must fail loudly at construction
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"frame_ms": 0},
        {"max_recording_seconds": 0},
        {"min_speech_ms": -1},
        {"silence_timeout_ms": -1},
    ],
)
def test_vad_config_rejects_nonsense(kwargs):
    with pytest.raises(ValueError):
        VadConfig(**kwargs)


# --------------------------------------------------------------------------
# State machine
# --------------------------------------------------------------------------


def test_state_machine_starts_idle():
    assert VoiceStateMachine().state is VoiceState.IDLE


@pytest.mark.parametrize(
    "path",
    [
        [VoiceState.LISTENING, VoiceState.SPEECH_DETECTED, VoiceState.PROCESSING,
         VoiceState.SPEAKING, VoiceState.LISTENING],
        [VoiceState.LISTENING, VoiceState.SPEECH_DETECTED, VoiceState.PROCESSING,
         VoiceState.SPEAKING, VoiceState.INTERRUPTED, VoiceState.LISTENING],
        [VoiceState.ERROR, VoiceState.IDLE],
    ],
)
def test_legal_transition_paths(path):
    machine = VoiceStateMachine()
    for state in path:
        assert machine.transition(state) is state


def test_illegal_transition_raises():
    """idle -> speaking would mean talking without ever having listened."""
    machine = VoiceStateMachine()
    with pytest.raises(InvalidVoiceTransition):
        machine.transition(VoiceState.SPEAKING)


def test_cannot_barge_in_from_processing():
    """Barge-in is only meaningful while speaking; from processing you must
    return to listening first, otherwise audio playback state desyncs."""
    machine = VoiceStateMachine(VoiceState.PROCESSING)
    with pytest.raises(InvalidVoiceTransition):
        machine.transition(VoiceState.INTERRUPTED)


def test_repeated_transition_is_idempotent_and_history_is_recorded():
    machine = VoiceStateMachine()
    machine.transition(VoiceState.LISTENING)
    machine.transition(VoiceState.LISTENING)
    assert machine.state is VoiceState.LISTENING
    assert machine.history == (VoiceState.IDLE, VoiceState.LISTENING)


def test_barge_in_sequence_is_supported():
    """speaking -> interrupted -> listening -> new request."""
    machine = VoiceStateMachine(VoiceState.SPEAKING)
    machine.transition(VoiceState.INTERRUPTED)
    machine.transition(VoiceState.LISTENING)
    machine.transition(VoiceState.SPEECH_DETECTED)
    assert machine.state is VoiceState.SPEECH_DETECTED


# --------------------------------------------------------------------------
# Energy VAD
# --------------------------------------------------------------------------


def test_silence_only_never_triggers_speech_or_finalize():
    """A quiet room must not be mistaken for an utterance."""
    vad = EnergyVoiceActivityDetector()
    for _ in range(500):  # 10 seconds of silence
        result = vad.feed(quiet())
        assert result.speaking is False
        assert result.should_finalize is False
    assert vad.is_speaking is False


def test_single_loud_frame_does_not_start_speech():
    """min_speech_ms filters a click or a door slam."""
    cfg = VadConfig(min_speech_ms=200)
    vad = EnergyVoiceActivityDetector(cfg)
    result = vad.feed(loud())
    assert result.speaking is False


def test_sustained_speech_is_detected_after_min_speech_ms():
    cfg = VadConfig(min_speech_ms=200)  # 10 frames at 20 ms
    vad = EnergyVoiceActivityDetector(cfg)
    for _ in range(9):
        vad.feed(loud())
    assert vad.is_speaking is False
    assert vad.feed(loud()).speaking is True


def test_silence_timeout_finalizes_utterance():
    cfg = VadConfig(min_speech_ms=20, silence_timeout_ms=200)
    vad = EnergyVoiceActivityDetector(cfg)
    vad.feed(loud())
    assert vad.is_speaking is True

    finals = []
    for _ in range(20):
        result = vad.feed(quiet())
        if result.should_finalize:
            finals.append(result)
    assert len(finals) == 1
    assert finals[0].reason == "silence"
    assert vad.is_speaking is False


def test_max_recording_duration_always_finalizes_even_while_speaking():
    """The hard ceiling must fire mid-speech, otherwise a stuck microphone
    records forever."""
    cfg = VadConfig(min_speech_ms=20, max_recording_seconds=1, silence_timeout_ms=10_000)
    vad = EnergyVoiceActivityDetector(cfg)

    final = None
    for _ in range(100):  # 2 seconds of continuous speech
        result = vad.feed(loud())
        if result.should_finalize:
            final = result
            break

    assert final is not None
    assert final.reason == "max_duration"


def test_reset_clears_detector_state():
    cfg = VadConfig(min_speech_ms=20, silence_timeout_ms=200)
    vad = EnergyVoiceActivityDetector(cfg)
    vad.feed(loud())
    assert vad.is_speaking is True
    assert vad.elapsed_ms == FRAME_MS

    vad.reset()
    assert vad.is_speaking is False
    assert vad.elapsed_ms == 0


def test_trim_to_speech_drops_leading_silence():
    """Stops the silence timeout being counted from the start of an open mic.

    Trim is buffer-granular: it drops whole leading buffers until the first
    one that contains speech.
    """
    vad = EnergyVoiceActivityDetector()
    lead_silence, speech, trailing = quiet(5), loud(2), quiet(3)
    trimmed = vad.trim_to_speech([lead_silence, speech, trailing])
    assert trimmed == [speech, trailing]


def test_trim_to_speech_returns_nothing_for_all_silence():
    vad = EnergyVoiceActivityDetector()
    assert vad.trim_to_speech([quiet(), quiet()]) == []


def test_energy_threshold_is_respected():
    cfg = VadConfig(energy_threshold=10_000)
    vad = EnergyVoiceActivityDetector(cfg)
    for _ in range(50):
        assert vad.feed(loud(amplitude=1000)).speaking is False
