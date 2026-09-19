"""Bot REST routes.

Every route resolves the caller with ``get_current_user(request)`` and passes
that username into the service. The request schemas have no owner field at all,
so a crafted body cannot even express an ownership claim — the same invariant
routes/voice.py and routes/video_routes.py hold.

Existence and ownership are indistinguishable: a bot that belongs to someone
else 404s, never 403s, matching routes/task_routes.py.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from src.auth_helpers import get_current_user
from services.bots import get_bot_service
from services.bots.lifecycle import AUTONOMY_LEVELS, STATES
from services.bots.permissions import CAPABILITIES
from services.bots.schemas import (
    BotApprovalDecision,
    BotCreate,
    BotRunRequest,
    BotTransition,
    BotUpdate,
)
from services.bots.service import BotServiceError
from services.bots.templates import BOT_TEMPLATES

logger = logging.getLogger(__name__)


def _require_user(request: Request) -> Optional[str]:
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")
    return user


def _http(exc: BotServiceError) -> HTTPException:
    detail = {"error": str(exc), "code": exc.code}
    return HTTPException(exc.status, detail)


def setup_bots_routes(bot_service=None) -> APIRouter:
    router = APIRouter(tags=["bots"])
    service = bot_service or get_bot_service()

    # ── catalog & templates ─────────────────────────────────────────────

    @router.get("/api/bots/meta/catalog")
    async def bots_catalog(request: Request):
        """Vocabulary for the UI: capabilities, autonomy levels, states,
        templates. Read-only and cheap; the create wizard fetches it once."""
        _require_user(request)
        return {
            "capabilities": CAPABILITIES,
            "autonomy_levels": {str(k): v for k, v in AUTONOMY_LEVELS.items()},
            "states": list(STATES),
            "templates": BOT_TEMPLATES,
        }

    # ── CRUD ────────────────────────────────────────────────────────────

    @router.get("/api/bots")
    async def list_bots(request: Request, status: Optional[str] = None):
        user = _require_user(request)
        try:
            return {"bots": service.list_bots(user, status=status)}
        except BotServiceError as exc:
            raise _http(exc)

    @router.post("/api/bots", status_code=201)
    async def create_bot(request: Request, payload: BotCreate):
        user = _require_user(request)
        try:
            return service.create_bot(user, payload.model_dump())
        except BotServiceError as exc:
            raise _http(exc)

    @router.get("/api/bots/{bot_id}")
    async def get_bot(request: Request, bot_id: str):
        user = _require_user(request)
        try:
            return service.get_bot(bot_id, user)
        except BotServiceError as exc:
            raise _http(exc)

    @router.patch("/api/bots/{bot_id}")
    async def update_bot(request: Request, bot_id: str, payload: BotUpdate):
        user = _require_user(request)
        changes = {k: v for k, v in payload.model_dump().items() if v is not None}
        try:
            return service.update_bot(bot_id, user, changes)
        except BotServiceError as exc:
            raise _http(exc)

    @router.delete("/api/bots/{bot_id}")
    async def delete_bot(request: Request, bot_id: str):
        user = _require_user(request)
        try:
            return service.delete_bot(bot_id, user)
        except BotServiceError as exc:
            raise _http(exc)

    # ── lifecycle ───────────────────────────────────────────────────────

    @router.post("/api/bots/{bot_id}/transition")
    async def transition_bot(request: Request, bot_id: str, payload: BotTransition):
        """One endpoint for every lifecycle move; the state machine validates."""
        user = _require_user(request)
        try:
            return service.transition(bot_id, user, payload.status)
        except BotServiceError as exc:
            raise _http(exc)

    # Convenience aliases the UI's buttons call directly.
    @router.post("/api/bots/{bot_id}/pause")
    async def pause_bot(request: Request, bot_id: str):
        user = _require_user(request)
        try:
            return service.transition(bot_id, user, "paused")
        except BotServiceError as exc:
            raise _http(exc)

    @router.post("/api/bots/{bot_id}/resume")
    async def resume_bot(request: Request, bot_id: str):
        user = _require_user(request)
        try:
            return service.transition(bot_id, user, "active")
        except BotServiceError as exc:
            raise _http(exc)

    @router.post("/api/bots/{bot_id}/enable")
    async def enable_bot(request: Request, bot_id: str):
        user = _require_user(request)
        try:
            return service.transition(bot_id, user, "active")
        except BotServiceError as exc:
            raise _http(exc)

    @router.post("/api/bots/{bot_id}/disable")
    async def disable_bot(request: Request, bot_id: str):
        user = _require_user(request)
        try:
            return service.transition(bot_id, user, "disabled")
        except BotServiceError as exc:
            raise _http(exc)

    # ── runs ────────────────────────────────────────────────────────────

    @router.post("/api/bots/{bot_id}/run")
    async def run_bot(request: Request, bot_id: str, payload: BotRunRequest):
        """Trigger a run. 409 when one is already in flight (duplicate guard)."""
        user = _require_user(request)
        try:
            run = service.start_run(
                bot_id, user,
                trigger_type=payload.trigger_type, force=payload.force,
            )
            return {"ok": True, "run": run}
        except BotServiceError as exc:
            raise _http(exc)

    @router.get("/api/bots/{bot_id}/runs")
    async def list_runs(request: Request, bot_id: str, limit: int = 50):
        user = _require_user(request)
        try:
            return {"runs": service.list_runs(bot_id, user, limit=limit)}
        except BotServiceError as exc:
            raise _http(exc)

    @router.get("/api/bots/{bot_id}/runs/{run_id}")
    async def get_run(request: Request, bot_id: str, run_id: str):
        user = _require_user(request)
        try:
            return service.get_run(bot_id, user, run_id)
        except BotServiceError as exc:
            raise _http(exc)

    @router.post("/api/bots/{bot_id}/runs/{run_id}/retry")
    async def retry_run(request: Request, bot_id: str, run_id: str):
        user = _require_user(request)
        try:
            run = service.retry_run(bot_id, user, run_id)
            return {"ok": True, "run": run}
        except BotServiceError as exc:
            raise _http(exc)

    # ── approvals ───────────────────────────────────────────────────────

    @router.get("/api/bots/{bot_id}/approvals")
    async def list_approvals(request: Request, bot_id: str):
        user = _require_user(request)
        try:
            return {"approvals": service.pending_approvals(bot_id, user)}
        except BotServiceError as exc:
            raise _http(exc)

    @router.post("/api/bots/{bot_id}/approvals/{approval_id}/decide")
    async def decide_approval(
        request: Request, bot_id: str, approval_id: str, payload: BotApprovalDecision
    ):
        """Approve or deny. ``pattern=True`` records a narrowly scoped pattern
        (this action, this bot) — never an unrestricted grant."""
        user = _require_user(request)
        try:
            approval = service.decide_approval(
                bot_id, user, approval_id,
                approve=payload.approve, pattern=payload.pattern,
            )
            return {"ok": True, "approval": approval}
        except BotServiceError as exc:
            raise _http(exc)

    # ── introspection ───────────────────────────────────────────────────

    @router.get("/api/bots/{bot_id}/activity")
    async def bot_activity(request: Request, bot_id: str, limit: int = 100):
        user = _require_user(request)
        try:
            return {"events": service.activity(bot_id, user, limit=limit)}
        except BotServiceError as exc:
            raise _http(exc)

    @router.get("/api/bots/{bot_id}/health")
    async def bot_health(request: Request, bot_id: str):
        user = _require_user(request)
        try:
            return service.health(bot_id, user)
        except BotServiceError as exc:
            raise _http(exc)

    @router.get("/api/bots/{bot_id}/memory")
    async def bot_memory(request: Request, bot_id: str, limit: int = 50):
        user = _require_user(request)
        try:
            return {"memories": service.bot_memories(bot_id, user, limit=limit)}
        except BotServiceError as exc:
            raise _http(exc)

    return router
