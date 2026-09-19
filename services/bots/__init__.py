"""Misantropic Bots — persistent AI workers.

A Bot is a durable identity that owns instructions, permissions and history
across many runs. It is an orchestration layer *above* the existing stack, not
a second agent framework: every execution goes through ScheduledTask →
task_scheduler → stream_agent_loop, the same path human-created tasks use.
"""

from typing import Optional

from services.bots.lifecycle import (
    AUTONOMY_LEVELS,
    STATES,
    TRANSITIONS,
    can_transition,
    require_transition,
    transitions_for,
)
from services.bots.permissions import (
    ALWAYS_DISABLED_FOR_BOTS,
    CAPABILITIES,
    CAPABILITY_KEYS,
    SENSITIVE_CAPABILITIES,
    capabilities_for,
    disabled_tools_for,
    requires_approval,
    validate_capabilities,
)
from services.bots.service import (
    BotService,
    BotServiceError,
    approval_to_dict,
    bot_to_dict,
    run_to_dict,
)

_service: Optional[BotService] = None


def get_bot_service() -> BotService:
    """Process-wide bot service, wired to the real task scheduler."""
    global _service
    if _service is None:
        scheduler = None
        try:
            from src.event_bus import get_task_scheduler
            scheduler = get_task_scheduler()
        except Exception:
            scheduler = None
        _service = BotService(scheduler)
    return _service


__all__ = [
    "ALWAYS_DISABLED_FOR_BOTS",
    "AUTONOMY_LEVELS",
    "BotService",
    "BotServiceError",
    "CAPABILITIES",
    "CAPABILITY_KEYS",
    "SENSITIVE_CAPABILITIES",
    "STATES",
    "TRANSITIONS",
    "approval_to_dict",
    "bot_to_dict",
    "can_transition",
    "capabilities_for",
    "disabled_tools_for",
    "get_bot_service",
    "require_transition",
    "requires_approval",
    "run_to_dict",
    "transitions_for",
    "validate_capabilities",
]
