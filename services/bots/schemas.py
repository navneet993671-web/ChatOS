"""Pydantic schemas for the bot API.

Request models never accept an ``owner``/``user_id`` — identity is resolved
from the authenticated session in the route layer, never from the body. This is
the same invariant the voice and video modules hold, and it means a crafted
request cannot even *express* an ownership claim.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from services.bots.lifecycle import STATES, can_transition
from services.bots.permissions import CAPABILITIES


class BotCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=2000)
    avatar: Optional[str] = Field(default=None, max_length=8)
    model: Optional[str] = Field(default=None, max_length=200)
    endpoint_url: Optional[str] = Field(default=None, max_length=500)
    system_prompt: Optional[str] = None
    instructions: Optional[str] = None
    autonomy_level: int = Field(default=1, ge=0, le=3)
    capabilities: List[str] = Field(default_factory=list)
    triggers: List[Dict[str, Any]] = Field(default_factory=list)
    approval_policy: Optional[Dict[str, Any]] = None
    memory_enabled: bool = True
    memory_scope: str = Field(default="bot", pattern="^(bot|user)$")
    notify_on: List[str] = Field(
        default_factory=lambda: ["completed", "failed", "approval"]
    )
    max_concurrent_runs: int = Field(default=1, ge=1, le=4)
    max_retries: int = Field(default=0, ge=0, le=5)
    timeout_seconds: Optional[int] = Field(default=None, ge=30, le=86400)

    @field_validator("capabilities")
    @classmethod
    def _known_capabilities(cls, value: List[str]) -> List[str]:
        unknown = [c for c in value if c not in CAPABILITIES]
        if unknown:
            raise ValueError(f"Unknown capabilities: {', '.join(sorted(unknown))}")
        return value


class BotUpdate(BaseModel):
    """PATCH body — every field optional; absent fields are left untouched."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=2000)
    avatar: Optional[str] = Field(default=None, max_length=8)
    model: Optional[str] = Field(default=None, max_length=200)
    endpoint_url: Optional[str] = Field(default=None, max_length=500)
    system_prompt: Optional[str] = None
    instructions: Optional[str] = None
    autonomy_level: Optional[int] = Field(default=None, ge=0, le=3)
    capabilities: Optional[List[str]] = None
    triggers: Optional[List[Dict[str, Any]]] = None
    approval_policy: Optional[Dict[str, Any]] = None
    memory_enabled: Optional[bool] = None
    memory_scope: Optional[str] = Field(default=None, pattern="^(bot|user)$")
    notify_on: Optional[List[str]] = None
    max_concurrent_runs: Optional[int] = Field(default=None, ge=1, le=4)
    max_retries: Optional[int] = Field(default=None, ge=0, le=5)
    timeout_seconds: Optional[int] = Field(default=None, ge=30, le=86400)

    @field_validator("capabilities")
    @classmethod
    def _known_capabilities(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        if value is None:
            return None
        unknown = [c for c in value if c not in CAPABILITIES]
        if unknown:
            raise ValueError(f"Unknown capabilities: {', '.join(sorted(unknown))}")
        return value


class BotTransition(BaseModel):
    """A lifecycle move. Validated against the shared transition table so the
    API refuses illegal moves before the service layer ever sees them."""

    status: str

    @field_validator("status")
    @classmethod
    def _legal_state(cls, value: str) -> str:
        if value not in STATES:
            raise ValueError(
                f"Unknown bot state {value!r}. Valid states: {', '.join(STATES)}"
            )
        return value


class BotRunRequest(BaseModel):
    trigger_type: str = Field(default="manual")
    force: bool = False

    @field_validator("trigger_type")
    @classmethod
    def _known_trigger(cls, value: str) -> str:
        allowed = {"manual", "schedule", "interval", "event", "webhook", "retry"}
        if value not in allowed:
            raise ValueError(f"trigger_type must be one of: {', '.join(sorted(allowed))}")
        return value


class BotApprovalDecision(BaseModel):
    approve: bool
    pattern: bool = False


class BotErrorOut(BaseModel):
    error: str
    code: str


class BotCatalog(BaseModel):
    """The capability catalogue and lifecycle vocabulary, for the UI."""

    capabilities: Dict[str, Any]
    autonomy_levels: Dict[int, str]
    states: List[str]
