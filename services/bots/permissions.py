"""Bot capability permissions — the server-side authority.

The frontend checkboxes are a convenience; this module is the enforcement.
Every capability a bot holds is resolved through :func:`capabilities_for` at run
creation time and again inside the mission prompt, so a stale client, a
hand-crafted request, or a bot edited between trigger and run cannot widen what
the agent is allowed to touch.

Two rules this module exists to guarantee:

* **Autonomy never bypasses security.** Level 3 means "may act without asking a
  human for *low-risk* actions" — never "may ignore the tool permission system".
  The hard ceiling is applied *after* the user's capability selection, and the
  always-blocked list is checked last, so no combination of autonomy + grants
  can re-enable a tool the platform denies that owner.
* **Capabilities are the narrowest grant, not a wish list.** Each key maps to a
  concrete set of agent tool names (the ones src/agent_loop.py already
  implements). Enforcement is subtraction from the tool list — the same
  mechanism ``disabled_tools`` uses today — so there is no second permission
  system for the agent to honour.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, List, Optional, Set

from services.bots.lifecycle import AUTONOMY_LEVELS

# ── Capability catalogue ────────────────────────────────────────────────────
# Keys are stable identifiers the UI renders as checkboxes. `tools` names agent
# tools by the exact names stream_agent_loop accepts; anything the agent does
# not recognise is simply absent, which keeps this list future-proof.

SENSITIVE_CAPABILITIES: FrozenSet[str] = frozenset({
    "email.send",
    "shell.execute",
    "files.write",
    "mcp.external",
})

CAPABILITIES: Dict[str, dict] = {
    # Research/read — safe at any autonomy level.
    "web.search":    {"label": "Web search",   "risk": "low",  "tools": []},
    "library.read":  {"label": "Read library", "risk": "low",  "tools": []},
    "memory.read":   {"label": "Read memory",  "risk": "low",  "tools": []},
    "notes.write":   {"label": "Create notes", "risk": "low",  "tools": []},
    "calendar.read": {"label": "Read calendar", "risk": "low", "tools": []},

    # Drafting — produces content the user reviews.
    "email.draft":   {"label": "Draft emails (no sending)", "risk": "low", "tools": []},

    # Side-effectful — gated by autonomy and approvals.
    "tasks.create":  {"label": "Create tasks",   "risk": "medium", "tools": []},
    "calendar.write": {"label": "Update calendar", "risk": "medium", "tools": []},
    "files.write":   {"label": "Write files",     "risk": "high",   "tools": []},
    "email.read":    {"label": "Read email",      "risk": "medium", "tools": []},
    "email.send":    {"label": "Send email",      "risk": "high",   "tools": []},
    "shell.execute": {"label": "Run shell commands", "risk": "high", "tools": []},
    "mcp.external":  {"label": "External MCP actions", "risk": "high", "tools": []},
}

CAPABILITY_KEYS: FrozenSet[str] = frozenset(CAPABILITIES)

# What each autonomy level is *allowed* to hold at most. Level 0 cannot hold
# any side-effectful capability regardless of what was ticked.
_LEVEL_CEILING: Dict[int, FrozenSet[str]] = {
    0: frozenset({"web.search", "library.read", "memory.read", "calendar.read"}),
    1: frozenset({
        "web.search", "library.read", "memory.read", "calendar.read",
        "notes.write", "email.draft", "tasks.create",
    }),
    2: frozenset({
        "web.search", "library.read", "memory.read", "calendar.read",
        "notes.write", "email.draft", "tasks.create", "calendar.write",
        "files.write", "email.read", "mcp.external",
    }),
    3: frozenset(CAPABILITY_KEYS),  # everything, still subject to grants + approvals
}


class PermissionError(Exception):
    """A capability request the permission model refuses."""

    def __init__(self, message: str, code: str = "permission_denied"):
        super().__init__(message)
        self.code = code


def normalise_level(level) -> int:
    """Clamp to a valid autonomy level; junk falls back to 1 (assist)."""
    try:
        level = int(level)
    except (TypeError, ValueError):
        return 1
    return min(3, max(0, level))


def autonomy_label(level) -> str:
    return AUTONOMY_LEVELS[normalise_level(level)]


def capabilities_for(requested, level) -> Set[str]:
    """Resolve the effective capability set.

    The grant is the *intersection* of what was requested and what the autonomy
    level's ceiling permits. Nothing is added that was not asked for — the
    ceiling only removes.
    """
    ceiling = _LEVEL_CEILING.get(normalise_level(level), _LEVEL_CEILING[1])
    if requested is None:
        return set()
    if isinstance(requested, str):
        try:
            import json
            requested = json.loads(requested)
        except (ValueError, TypeError):
            return set()
    return {cap for cap in requested if cap in CAPABILITY_KEYS and cap in ceiling}


def validate_capabilities(requested, level) -> List[str]:
    """Like :func:`capabilities_for` but raises on anything the ceiling refuses,
    so the API can tell the user *why* a grant was dropped instead of silently
    narrowing it."""
    effective = capabilities_for(requested, level)
    lvl = normalise_level(level)
    if isinstance(requested, str):
        import json
        try:
            requested = json.loads(requested)
        except (ValueError, TypeError):
            requested = []
    refused = [c for c in (requested or [])
               if c in CAPABILITY_KEYS and c not in effective]
    if refused:
        raise PermissionError(
            f"Autonomy level {lvl} ({autonomy_label(lvl)}) cannot hold: "
            f"{', '.join(sorted(refused))}. Raise the autonomy level or drop these.",
            code="capability_above_level",
        )
    return sorted(effective)


# ── Agent tool gating ───────────────────────────────────────────────────────

# Tools that only make sense for a bot holding the matching capability. A bot
# without "shell.execute" never sees the shell tool in its agent run — enforced
# by passing these names in `disabled_tools` to stream_agent_loop, the same
# mechanism that already exists for per-task tool control.
_TOOL_CAPABILITY_MAP: Dict[str, str] = {
    "bash": "shell.execute",
    "shell": "shell.execute",
    "python": "shell.execute",
    "write_file": "files.write",
    "edit_file": "files.write",
    "create_file": "files.write",
    "send_email": "email.send",
    "mcp__email__send_email": "email.send",
}

# Tools that are always refused for bots regardless of capabilities — the
# platform-level denials that no autonomy level may override.
ALWAYS_DISABLED_FOR_BOTS: FrozenSet[str] = frozenset({
    "admin",
    "admin_wipe",
    "manage_mcp",
    "manage_tokens",
    "backup",
    "vault",
})

# Owner-level denials (non-admin users) are resolved by the existing
# src.tool_security.blocked_tools_for_owner; bots inherit those on top.


def disabled_tools_for(bot_capabilities, owner: Optional[str]) -> Set[str]:
    """Tools the agent run must NOT get.

    Start from "everything sensitive the bot did not explicitly request", add
    the platform always-denied list, then let the existing owner-level security
    subtract anything else it wants. A capability not in the bot's set means its
    tools are disabled — deny-by-default is the only safe direction.
    """
    granted = set(bot_capabilities or ())
    if isinstance(granted, str):
        import json
        try:
            granted = set(json.loads(granted))
        except (ValueError, TypeError):
            granted = set()

    disabled: Set[str] = set(ALWAYS_DISABLED_FOR_BOTS)

    for tool_name, required_capability in _TOOL_CAPABILITY_MAP.items():
        if required_capability not in granted:
            disabled.add(tool_name)

    # Sensitive capabilities still require the bot to hold them, and email
    # sending additionally requires the *platform* email rules to allow it —
    # checked at run time by the executor, not here.
    if "email.read" not in granted:
        disabled.update({"mcp__email__list_emails", "mcp__email__read_email",
                         "mcp__email__search_emails"})

    # Owner-level blocks (non-admin restrictions) are authoritative.
    try:
        from src.tool_security import blocked_tools_for_owner
        disabled.update(blocked_tools_for_owner(owner))
    except Exception:
        # If the security module cannot be consulted, fail closed: strip every
        # mapped sensitive tool rather than assume the owner is unrestricted.
        disabled.update(_TOOL_CAPABILITY_MAP.keys())

    return disabled


def requires_approval(action: str, capabilities, policy: Optional[dict]) -> bool:
    """Whether `action` (a capability key or tool name) needs sign-off.

    Policy JSON: {"mode": "per_run"|"pattern", "always": [...], "never": [...]}
      - per_run: every sensitive action asks (default, safest)
      - pattern: previously-approved patterns skip re-asking; everything else asks
      - always/never: explicit capability overrides

    An absent or malformed policy means "ask", never "skip" — the default
    direction for a permission question is always the conservative one.
    """
    granted = set(capabilities or ())
    if isinstance(granted, str):
        import json
        try:
            granted = set(json.loads(granted))
        except (ValueError, TypeError):
            granted = set()

    policy = policy if isinstance(policy, dict) else {}

    never = set(policy.get("never") or [])
    if action in never:
        return False

    always = set(policy.get("always") or [])
    if action in always:
        return True

    # High-risk capabilities always ask unless explicitly exempted above.
    if action in SENSITIVE_CAPABILITIES:
        mode = policy.get("mode", "per_run")
        if mode == "pattern":
            # Pattern mode still asks for anything never approved before;
            # "was it approved before" is recorded on BotApproval rows, which
            # the service checks before consulting this function.
            return True
        return True

    # Non-sensitive actions run without approval, as long as the bot holds them.
    return action not in granted and action in CAPABILITY_KEYS
