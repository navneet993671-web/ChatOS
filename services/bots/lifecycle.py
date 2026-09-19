"""Bot lifecycle state machine.

Bots are long-lived workers, so an illegal transition (resuming a deleted bot,
running a draft that was never activated) must be refused loudly rather than
silently corrupting state. The legal transitions are encoded once here and both
the service layer and the API schemas validate against them — the frontend gets
the same table so its buttons can be disabled before a round-trip.

States:
    draft             configured, never activated
    active            healthy, will fire on triggers
    paused            user-suspended; triggers are ignored
    disabled          decommissioned but kept for history
    error             scheduler parked it after repeated failures
    waiting_approval  a run is parked pending user sign-off
    running           at least one run is executing right now

Any state can move to `disabled` (decommission) and `error` is always
recoverable back to `active` — a bot that can never leave `error` is a bot the
user has to delete, which is the wrong outcome.
"""

from __future__ import annotations

from typing import FrozenSet

STATES = (
    "draft",
    "active",
    "paused",
    "disabled",
    "error",
    "waiting_approval",
    "running",
)

# Autonomy levels — a bot's ceiling for what it may do without asking.
AUTONOMY_LEVELS = {
    0: "observe",       # read/research only, no side effects
    1: "assist",        # drafts and notes; sensitive actions need approval
    2: "act",           # permitted non-sensitive actions execute directly
    3: "autonomous",    # predefined workflows, retries, event reactions
}

# Transitions deliberately follow the spec's table, plus the two recoverability
# edges every real deployment needs: waiting_approval can go back to paused
# (user suspends while a request is pending) and error can go to paused.
TRANSITIONS: dict[str, FrozenSet[str]] = {
    "draft":            frozenset({"active", "disabled"}),
    "active":           frozenset({"paused", "running", "disabled", "error"}),
    "paused":           frozenset({"active", "disabled"}),
    "disabled":         frozenset({"active"}),   # re-enable re-activates history
    "error":            frozenset({"active", "paused", "disabled"}),
    "waiting_approval": frozenset({"running", "active", "paused", "disabled"}),
    "running":          frozenset({"active", "waiting_approval", "error", "disabled"}),
}

TERMINAL_NOTIFICATION = "Bot state changed"


def can_transition(current: str, target: str) -> bool:
    """True when `current → target` is a legal lifecycle move.

    Unknown states are illegal rather than a crash — the API renders them as a
    400 with the transition table, not a 500.
    """
    if current not in TRANSITIONS or target not in STATES:
        return False
    return target in TRANSITIONS[current]


def require_transition(current: str, target: str) -> None:
    """Raise ValueError with the legal options when a move is illegal."""
    if can_transition(current, target):
        return
    legal = ", ".join(sorted(TRANSITIONS.get(current, frozenset()))) or "none"
    raise ValueError(
        f"Illegal bot state transition: {current!r} → {target!r}. "
        f"Allowed from {current!r}: {legal}."
    )


def transitions_for(state: str) -> FrozenSet[str]:
    """Legal targets from `state` — what the UI uses to enable/disable buttons."""
    return TRANSITIONS.get(state, frozenset())
