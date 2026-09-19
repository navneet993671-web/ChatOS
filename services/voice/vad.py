"""Voice activity detection and the voice state machine.

Two responsibilities live here:

* :class:`VoiceState` / :class:`VoiceStateMachine` — the seven states the voice
  feature is allowed to be in, and the legal transitions between them. Keeping
  this explicit is what stops the two classic bugs: recording forever, and
  entering ``speaking`` while still ``listening`` (which breaks barge-in).
* :class:`EnergyVoiceActivityDetector` — a dependency-free detector built on
  frame RMS. It has no ML dependency on purpose: it must work on a fresh
  install before the user has installed anything heavy, and it must be cheap
  enough to run on every frame.
"""

from __future__ import annotations

import math
from array import array
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence

from services.voice.providers.base import VadResult, VoiceActivityDetector


class VoiceState(str, Enum):
    """States of a voice session (matches the spec's required list)."""

    IDLE = "idle"
    LISTENING = "listening"
    SPEECH_DETECTED = "speech_detected"
    PROCESSING = "processing"
    SPEAKING = "speaking"
    INTERRUPTED = "interrupted"
    ERROR = "error"


#: Legal transitions. Anything not listed here is a bug, not a runtime
#: condition, so :meth:`VoiceStateMachine.transition` raises instead of
#: silently accepting it.
_ALLOWED: dict[VoiceState, frozenset[VoiceState]] = {
    VoiceState.IDLE: frozenset(
        {VoiceState.LISTENING, VoiceState.PROCESSING, VoiceState.ERROR}
    ),
    VoiceState.LISTENING: frozenset(
        {VoiceState.SPEECH_DETECTED, VoiceState.IDLE, VoiceState.ERROR}
    ),
    VoiceState.SPEECH_DETECTED: frozenset(
        {VoiceState.PROCESSING, VoiceState.LISTENING, VoiceState.IDLE, VoiceState.ERROR}
    ),
    VoiceState.PROCESSING: frozenset(
        {VoiceState.SPEAKING, VoiceState.LISTENING, VoiceState.IDLE, VoiceState.ERROR}
    ),
    VoiceState.SPEAKING: frozenset(
        {VoiceState.INTERRUPTED, VoiceState.LISTENING, VoiceState.IDLE, VoiceState.ERROR}
    ),
    # Barge-in lands here, then immediately goes back to LISTENING to capture
    # the new request. INTERRUPTED is a real, observable state so the UI can
    # render it rather than inferring it.
    VoiceState.INTERRUPTED: frozenset(
        {VoiceState.LISTENING, VoiceState.IDLE, VoiceState.ERROR}
    ),
    VoiceState.ERROR: frozenset({VoiceState.IDLE, VoiceState.LISTENING}),
}


class InvalidVoiceTransition(RuntimeError):
    """Raised when code attempts an illegal state transition."""


class VoiceStateMachine:
    """Validates and records voice-session state transitions."""

    def __init__(self, state: VoiceState = VoiceState.IDLE) -> None:
        self._state = state
        self._history: list[VoiceState] = [state]

    @property
    def state(self) -> VoiceState:
        return self._state

    @property
    def history(self) -> tuple[VoiceState, ...]:
        return tuple(self._history)

    def can_transition_to(self, target: VoiceState) -> bool:
        if target == self._state:
            return True  # idempotent
        return target in _ALLOWED[self._state]

    def transition(self, target: VoiceState) -> VoiceState:
        """Move to ``target``, raising :class:`InvalidVoiceTransition` if illegal."""
        if not self.can_transition_to(target):
            raise InvalidVoiceTransition(
                f"cannot move {self._state.value} -> {target.value}"
            )
        if target != self._state:
            self._state = target
            self._history.append(target)
        return self._state


# --------------------------------------------------------------------------
# Energy-based VAD
# --------------------------------------------------------------------------


@dataclass
class VadConfig:
    """Tunables for :class:`EnergyVoiceActivityDetector`.

    Defaults come from the ``VOICE_*`` environment variables via
    :func:`services.voice.service.load_voice_config`; these are the fallbacks.
    """

    #: Frame duration in milliseconds. 20 ms is the usual WebRTC choice.
    frame_ms: int = 20
    #: RMS amplitude (0-32767) above which a frame counts as voiced.
    #: 500 ≈ quiet-room speech floor; low enough to catch soft speech, high
    #: enough to ignore fan/room hum.
    energy_threshold: float = 500.0
    #: Consecutive voiced audio required before we call it speech. Filters
    #: out a single key-click or door slam.
    min_speech_ms: int = 200
    #: Silence after speech before the utterance is finalised.
    silence_timeout_ms: int = 1200
    #: Hard ceiling, so a stuck microphone can never record forever.
    max_recording_seconds: int = 120

    def __post_init__(self) -> None:
        if self.frame_ms <= 0:
            raise ValueError("frame_ms must be positive")
        if self.min_speech_ms < 0:
            raise ValueError("min_speech_ms must be non-negative")
        if self.silence_timeout_ms < 0:
            raise ValueError("silence_timeout_ms must be non-negative")
        if self.max_recording_seconds <= 0:
            raise ValueError("max_recording_seconds must be positive")


def frame_rms(frame: bytes) -> float:
    """Root-mean-square amplitude of 16-bit signed little-endian PCM."""
    usable = len(frame) - (len(frame) % 2)
    if usable <= 0:
        return 0.0
    samples = array("h")
    samples.frombytes(frame[:usable])
    if not samples:
        return 0.0
    total = 0
    for s in samples:
        total += s * s
    return math.sqrt(total / len(samples))


class EnergyVoiceActivityDetector(VoiceActivityDetector):
    """Detects speech from frame energy.

    Deliberately conservative: it would rather miss the first 200 ms of a
    quiet utterance than fire on room tone. Because it also owns the two hard
    limits (silence timeout, maximum duration) a session can never hang in
    ``listening`` — the failure mode the spec explicitly calls out.
    """

    name = "energy"

    def __init__(self, config: Optional[VadConfig] = None) -> None:
        self.config = config or VadConfig()
        self.frame_ms = self.config.frame_ms
        self._speaking = False
        self._voiced_ms = 0
        self._silence_ms = 0
        self._utterance_ms = 0
        self._has_spoken = False

    # -- introspection ---------------------------------------------------

    @property
    def is_speaking(self) -> bool:
        return self._speaking

    @property
    def elapsed_ms(self) -> int:
        """Total audio fed since the last :meth:`reset`."""
        return self._utterance_ms

    def reset(self) -> None:
        self._speaking = False
        self._voiced_ms = 0
        self._silence_ms = 0
        self._utterance_ms = 0
        self._has_spoken = False

    # -- hot path --------------------------------------------------------

    def feed(self, frame: bytes, *, now: Optional[float] = None) -> VadResult:
        cfg = self.config
        energy = frame_rms(frame)
        voiced = energy >= cfg.energy_threshold

        if voiced:
            self._voiced_ms += cfg.frame_ms
            self._silence_ms = 0
            self._has_spoken = True
            if not self._speaking and self._voiced_ms >= cfg.min_speech_ms:
                self._speaking = True
        else:
            if self._has_spoken:
                self._silence_ms += cfg.frame_ms
            self._voiced_ms = 0

        self._utterance_ms += cfg.frame_ms

        # Hard ceiling first: it must win even while the user is still talking.
        if self._utterance_ms >= cfg.max_recording_seconds * 1000:
            return VadResult(
                speaking=self._speaking,
                energy=energy,
                voiced_ms=self._voiced_ms,
                silence_ms=self._silence_ms,
                should_finalize=True,
                reason="max_duration",
            )

        if self._speaking and self._silence_ms >= cfg.silence_timeout_ms:
            self._speaking = False
            return VadResult(
                speaking=False,
                energy=energy,
                voiced_ms=self._voiced_ms,
                silence_ms=self._silence_ms,
                should_finalize=True,
                reason="silence",
            )

        return VadResult(
            speaking=self._speaking,
            energy=energy,
            voiced_ms=self._voiced_ms,
            silence_ms=self._silence_ms,
        )

    def trim_to_speech(self, frames: Sequence[bytes]) -> list[bytes]:
        """Drop leading silent frames so the timeout is not counted from the
        start of an open microphone."""
        threshold = self.config.energy_threshold
        for i, frame in enumerate(frames):
            if frame_rms(frame) >= threshold:
                return list(frames[i:])
        return []
