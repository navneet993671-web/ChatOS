"""Bot service — CRUD, run orchestration, and the mission bridge.

Boundaries this module holds:

* **No second execution engine.** A bot run always ends in the existing
  ScheduledTask → task_scheduler → stream_agent_loop pipeline. This service
  creates/owns that ScheduledTask, asks the real scheduler to execute it, and
  records a BotRun around the TaskRun that comes back. It never talks to an LLM
  directly and never spawns its own agent loop.
* **Ownership is absolute.** Every lookup is filtered by the authenticated
  owner *at the database level*, not checked after fetching. A bot that exists
  but belongs to someone else reads as "not found", matching how
  routes/task_routes.py treats tasks.
* **Duplicate execution is prevented at the DB.** A UNIQUE partial index on
  (bot_id) WHERE status IN ('queued','running','waiting_approval') — enforced in
  Python here because SQLite's partial indexes are created via DDL — plus an
  in-flight check, so scheduler storms, webhook retries and double-clicks all
  collapse into one run.
* **Failures never crash the app.** Every run path is wrapped; failures are
  recorded on the BotRun and the bot's own status/error fields.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.database import (
    Bot,
    BotApproval,
    BotRun,
    ScheduledTask,
    SessionLocal,
)
from services.bots import lifecycle, permissions
from services.bots.lifecycle import can_transition, require_transition
from services.bots.permissions import PermissionError as BotPermissionError

logger = logging.getLogger(__name__)


class BotServiceError(Exception):
    """A bot operation that cannot proceed, with a machine-readable code."""

    def __init__(self, message: str, *, code: str = "bot_error", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def _now() -> datetime:
    return datetime.utcnow()


def _parse_json_list(raw, default=None) -> list:
    if raw is None:
        return list(default or [])
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else list(default or [])
    except (ValueError, TypeError):
        return list(default or [])


def _parse_json_dict(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


# ── Serialisation ───────────────────────────────────────────────────────────

def bot_to_dict(bot: Bot, *, include_stats: bool = True) -> Dict[str, Any]:
    d = {
        "id": bot.id,
        "owner": bot.owner,
        "name": bot.name,
        "description": bot.description or "",
        "avatar": bot.avatar or "🤖",
        "status": bot.status or "draft",
        "model": bot.model or "",
        "endpoint_url": bot.endpoint_url or "",
        "system_prompt": bot.system_prompt or "",
        "instructions": bot.instructions or "",
        "autonomy_level": permissions.normalise_level(bot.autonomy_level),
        "autonomy_label": permissions.autonomy_label(bot.autonomy_level),
        "capabilities": _parse_json_list(bot.capabilities),
        "triggers": _parse_json_list(bot.triggers),
        "approval_policy": _parse_json_dict(bot.approval_policy),
        "memory_enabled": bool(bot.memory_enabled),
        "memory_scope": bot.memory_scope or "bot",
        "notify_on": _parse_json_list(bot.notify_on, ["completed", "failed", "approval"]),
        "max_concurrent_runs": bot.max_concurrent_runs or 1,
        "max_retries": bot.max_retries or 0,
        "timeout_seconds": bot.timeout_seconds,
        "workspace_dir": bot.workspace_dir or "",
        "allowed_transitions": sorted(lifecycle.transitions_for(bot.status or "draft")),
        "created_at": bot.created_at.isoformat() if bot.created_at else None,
        "updated_at": bot.updated_at.isoformat() if bot.updated_at else None,
        "last_run_at": bot.last_run_at.isoformat() if bot.last_run_at else None,
        "last_success_at": bot.last_success_at.isoformat() if bot.last_success_at else None,
        "last_error": bot.last_error,
    }
    if include_stats:
        d.update({
            "total_runs": bot.total_runs or 0,
            "successful_runs": bot.successful_runs or 0,
            "failed_runs": bot.failed_runs or 0,
            "consecutive_failures": bot.consecutive_failures or 0,
        })
    return d


def run_to_dict(run: BotRun) -> Dict[str, Any]:
    return {
        "id": run.id,
        "bot_id": run.bot_id,
        "owner": run.owner,
        "mission_id": run.mission_id,
        "task_run_id": run.task_run_id,
        "trigger_type": run.trigger_type or "manual",
        "status": run.status or "queued",
        "attempt": run.attempt or 1,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "duration_ms": run.duration_ms,
        "summary": run.summary or "",
        "result": run.result or "",
        "error": run.error,
        "artifacts": _parse_json_list(run.artifacts),
        "model_used": run.model_used or "",
        "tokens_used": run.tokens_used,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


def approval_to_dict(a: BotApproval) -> Dict[str, Any]:
    return {
        "id": a.id,
        "bot_id": a.bot_id,
        "owner": a.owner,
        "run_id": a.run_id,
        "action": a.action,
        "description": a.description or "",
        "risk_level": a.risk_level or "medium",
        "context": _parse_json_dict(a.context),
        "status": a.status or "pending",
        "decided_at": a.decided_at.isoformat() if a.decided_at else None,
        "decided_by": a.decided_by,
        "pattern": bool(a.pattern),
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


# ── Service ─────────────────────────────────────────────────────────────────

class BotService:
    """All bot business logic. Routes stay thin: parse, delegate, serialise."""

    def __init__(self, scheduler=None):
        # The real task scheduler is injected by app.py; tests may pass a stub.
        self._scheduler = scheduler

    # ── lookup ──────────────────────────────────────────────────────────

    @staticmethod
    def _owned_bot(db, bot_id: str, owner: Optional[str]) -> Bot:
        """Fetch a bot scoped to its owner. Missing == someone else's."""
        q = db.query(Bot).filter(Bot.id == bot_id)
        if owner:
            q = q.filter(Bot.owner == owner)
        bot = q.first()
        if not bot:
            # Deliberately indistinguishable: 404 for both missing and foreign.
            raise BotServiceError("Bot not found", code="bot_not_found", status=404)
        return bot

    def get_bot(self, bot_id: str, owner: Optional[str]) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            return bot_to_dict(self._owned_bot(db, bot_id, owner))
        finally:
            db.close()

    def list_bots(self, owner: Optional[str], status: Optional[str] = None) -> List[Dict[str, Any]]:
        db = SessionLocal()
        try:
            q = db.query(Bot)
            if owner:
                q = q.filter(Bot.owner == owner)
            if status:
                q = q.filter(Bot.status == status)
            bots = q.order_by(Bot.updated_at.desc()).all()
            return [bot_to_dict(b) for b in bots]
        finally:
            db.close()

    # ── CRUD ────────────────────────────────────────────────────────────

    def create_bot(self, owner: Optional[str], payload: Dict[str, Any]) -> Dict[str, Any]:
        name = (payload.get("name") or "").strip()
        if not name:
            raise BotServiceError("A bot needs a name", code="invalid_bot", status=400)

        level = permissions.normalise_level(payload.get("autonomy_level", 1))
        try:
            caps = permissions.validate_capabilities(payload.get("capabilities"), level)
        except BotPermissionError as exc:
            raise BotServiceError(str(exc), code=exc.code, status=400)

        bot = Bot(
            id=str(uuid.uuid4()),
            owner=owner,
            name=name[:120],
            description=(payload.get("description") or "").strip() or None,
            avatar=(payload.get("avatar") or "🤖")[:8],
            status="draft",  # always start as draft; the user activates explicitly
            model=(payload.get("model") or "").strip() or None,
            endpoint_url=(payload.get("endpoint_url") or "").strip() or None,
            system_prompt=(payload.get("system_prompt") or "").strip() or None,
            instructions=(payload.get("instructions") or "").strip() or None,
            autonomy_level=level,
            capabilities=json.dumps(caps),
            triggers=json.dumps(self._validate_triggers(payload.get("triggers"))),
            approval_policy=json.dumps(self._validate_policy(payload.get("approval_policy"))),
            memory_enabled=bool(payload.get("memory_enabled", True)),
            memory_scope="bot" if payload.get("memory_scope", "bot") == "bot" else "user",
            notify_on=json.dumps(payload.get("notify_on") or ["completed", "failed", "approval"]),
            max_concurrent_runs=max(1, min(4, int(payload.get("max_concurrent_runs") or 1))),
            max_retries=max(0, min(5, int(payload.get("max_retries") or 0))),
            timeout_seconds=payload.get("timeout_seconds"),
            workspace_dir=None,  # set on activation, lazily
        )
        db = SessionLocal()
        try:
            db.add(bot)
            db.commit()
            return bot_to_dict(bot)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def update_bot(self, bot_id: str, owner: Optional[str], payload: Dict[str, Any]) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            bot = self._owned_bot(db, bot_id, owner)

            # Identity fields.
            for field in ("name", "description", "avatar", "model", "endpoint_url",
                          "system_prompt", "instructions"):
                if field in payload:
                    value = payload[field]
                    if field == "name":
                        value = (value or "").strip() or bot.name
                        value = value[:120]
                    elif isinstance(value, str) and not value.strip():
                        value = None
                    setattr(bot, field, value)

            # Permission-sensitive fields re-validate.
            if "autonomy_level" in payload:
                bot.autonomy_level = permissions.normalise_level(payload["autonomy_level"])
            if "capabilities" in payload:
                try:
                    caps = permissions.validate_capabilities(
                        payload["capabilities"], bot.autonomy_level
                    )
                except BotPermissionError as exc:
                    raise BotServiceError(str(exc), code=exc.code, status=400)
                bot.capabilities = json.dumps(caps)
            if "approval_policy" in payload:
                bot.approval_policy = json.dumps(self._validate_policy(payload["approval_policy"]))
            if "triggers" in payload:
                bot.triggers = json.dumps(self._validate_triggers(payload["triggers"]))

            for field in ("memory_enabled",):
                if field in payload:
                    setattr(bot, field, bool(payload[field]))
            if "memory_scope" in payload:
                bot.memory_scope = "user" if payload["memory_scope"] == "user" else "bot"
            if "notify_on" in payload:
                bot.notify_on = json.dumps(payload["notify_on"] or [])
            if "max_concurrent_runs" in payload:
                bot.max_concurrent_runs = max(1, min(4, int(payload["max_concurrent_runs"] or 1)))
            if "max_retries" in payload:
                bot.max_retries = max(0, min(5, int(payload["max_retries"] or 0)))
            if "timeout_seconds" in payload:
                bot.timeout_seconds = payload["timeout_seconds"]

            bot.updated_at = _now()
            db.commit()
            return bot_to_dict(bot)
        except BotServiceError:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            raise BotServiceError(f"Could not update bot: {exc}", code="update_failed", status=500)
        finally:
            db.close()

    def delete_bot(self, bot_id: str, owner: Optional[str]) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            bot = self._owned_bot(db, bot_id, owner)
            # Owned ScheduledTasks are the bot's missions; deleting the bot
            # pauses them rather than deleting work the user may want to keep.
            paused = (
                db.query(ScheduledTask)
                .filter(ScheduledTask.id.in_(self._mission_ids(db, bot.id)))
                .filter(ScheduledTask.status == "active")
                .all()
            )
            for task in paused:
                task.status = "paused"
            info = {"id": bot.id, "name": bot.name, "missions_paused": len(paused)}
            db.delete(bot)  # runs/approvals cascade via FK
            db.commit()
            return info
        finally:
            db.close()

    # ── lifecycle ───────────────────────────────────────────────────────

    def transition(self, bot_id: str, owner: Optional[str], target: str) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            bot = self._owned_bot(db, bot_id, owner)
            try:
                require_transition(bot.status or "draft", target)
            except ValueError as exc:
                raise BotServiceError(str(exc), code="invalid_transition", status=400)

            previous = bot.status
            bot.status = target
            bot.updated_at = _now()

            if target == "active":
                self._ensure_workspace(bot)
                self._sync_missions(db, bot)   # activate owned scheduled tasks
                bot.consecutive_failures = 0
            elif target == "paused" and previous == "active":
                self._sync_missions(db, bot, activate=False)

            db.commit()
            return bot_to_dict(bot)
        except BotServiceError:
            db.rollback()
            raise
        finally:
            db.close()

    # ── triggers ────────────────────────────────────────────────────────

    _TRIGGER_TYPES = {"manual", "schedule", "interval", "event", "webhook"}

    def _validate_triggers(self, raw) -> List[Dict[str, Any]]:
        if not raw:
            return []
        if isinstance(raw, str):
            raw = _parse_json_list(raw)
        out = []
        for trigger in raw:
            if not isinstance(trigger, dict):
                continue
            ttype = trigger.get("type")
            if ttype not in self._TRIGGER_TYPES:
                raise BotServiceError(
                    f"Unknown trigger type: {ttype!r}. "
                    f"Valid: {', '.join(sorted(self._TRIGGER_TYPES))}",
                    code="invalid_trigger", status=400,
                )
            spec: Dict[str, Any] = {"type": ttype}
            if ttype == "schedule":
                # Reuse ScheduledTask's vocabulary exactly.
                spec["schedule"] = trigger.get("schedule") or "daily"
                spec["scheduled_time"] = trigger.get("scheduled_time") or "08:00"
                if trigger.get("scheduled_day") is not None:
                    spec["scheduled_day"] = int(trigger["scheduled_day"])
                if trigger.get("cron_expression"):
                    spec["cron_expression"] = str(trigger["cron_expression"])[:100]
            elif ttype == "interval":
                hours = max(0.05, min(24 * 30, float(trigger.get("hours") or 6)))
                spec["hours"] = hours
            elif ttype == "event":
                if not trigger.get("event"):
                    raise BotServiceError("Event trigger needs an event name",
                                          code="invalid_trigger", status=400)
                spec["event"] = str(trigger["event"])[:100]
                spec["every_n"] = max(1, int(trigger.get("every_n") or 1))
            elif ttype == "webhook":
                spec["note"] = "webhook token is generated per mission"
            out.append(spec)
        return out

    def _validate_policy(self, raw) -> Dict[str, Any]:
        if not raw:
            return {"mode": "per_run"}
        if isinstance(raw, str):
            raw = _parse_json_dict(raw)
        mode = raw.get("mode", "per_run")
        if mode not in ("per_run", "pattern"):
            mode = "per_run"
        return {
            "mode": mode,
            "always": [str(a) for a in (raw.get("always") or [])][:20],
            "never": [str(a) for a in (raw.get("never") or [])][:20],
        }

    # ── workspace ───────────────────────────────────────────────────────

    def _workspace_root(self, bot: Bot) -> Path:
        """data/bots/<owner>/<bot-id>/ — user-scoped by construction.

        The DB column stores the path *relative to the repo root* so rows stay
        portable and the browser is never handed an absolute filesystem path.
        """
        from src.constants import DATA_DIR
        owner = bot.owner or "_shared"
        return Path(DATA_DIR) / "bots" / owner / bot.id

    def _relative_workspace(self, bot: Bot) -> str:
        """Workspace path relative to the repo root, for the DB column."""
        try:
            return str(self._workspace_root(bot).relative_to(Path.cwd()))
        except ValueError:
            # Different drive or otherwise un-relatable: store the path as-is
            # rather than fail activation over a cosmetic column.
            return str(self._workspace_root(bot))

    def _ensure_workspace(self, bot: Bot) -> Path:
        """Create the workspace tree and persist the relative path. Idempotent."""
        root = self._workspace_root(bot)
        for sub in ("workspace", "artifacts", "runs", "temp", "logs"):
            (root / sub).mkdir(parents=True, exist_ok=True)

        relative = self._relative_workspace(bot)
        if bot.workspace_dir != relative:
            db = SessionLocal()
            try:
                db_bot = db.query(Bot).filter(Bot.id == bot.id).first()
                if db_bot:
                    db_bot.workspace_dir = relative
                    db.commit()
            finally:
                db.close()
        return root

    # ── missions (ScheduledTasks owned by the bot) ──────────────────────

    @staticmethod
    def _mission_ids(db, bot_id: str) -> List[str]:
        return [row[0] for row in db.query(BotRun.mission_id)
                .filter(BotRun.bot_id == bot_id, BotRun.mission_id.isnot(None)).all()]

    def _sync_missions(self, db, bot: Bot, *, activate: bool = True) -> None:
        """Point the bot's missions at the bot's lifecycle state."""
        mission_ids = self._mission_ids(db, bot.id)
        if not mission_ids:
            return
        target_status = "active" if (activate and bot.status == "active") else "paused"
        db.query(ScheduledTask).filter(
            ScheduledTask.id.in_(mission_ids),
            ScheduledTask.status.in_(("active", "paused")),
        ).update({ScheduledTask.status: target_status}, synchronize_session=False)

    def _build_mission_prompt(self, bot: Bot) -> str:
        """The standing instruction set for this bot's missions."""
        parts = [f"You are {bot.name}, a persistent automated worker."]
        if bot.description:
            parts.append(f"Purpose: {bot.description}")
        if bot.instructions:
            parts.append(f"Standing instructions:\n{bot.instructions}")
        level = permissions.autonomy_label(bot.autonomy_level)
        parts.append(
            f"Autonomy: {level}. Work within your granted capabilities; "
            "if an action exceeds them, report what you would have done "
            "rather than attempting it."
        )
        return "\n\n".join(parts)

    def _ensure_mission(self, db, bot: Bot, trigger_type: str) -> ScheduledTask:
        """Create (or reuse) the ScheduledTask that will execute this run.

        One ScheduledTask per bot: the bot's prompt/system-prompt live there, so
        the execution path is *literally* the normal task path — the scheduler
        cannot tell bot work from human work, which is the point.
        """
        existing_id = (
            db.query(BotRun.mission_id)
            .filter(BotRun.bot_id == bot.id, BotRun.mission_id.isnot(None))
            .order_by(BotRun.created_at.desc())
            .first()
        )
        if existing_id:
            task = db.query(ScheduledTask).filter(ScheduledTask.id == existing_id[0]).first()
            if task:
                return task

        task = ScheduledTask(
            id=str(uuid.uuid4()),
            owner=bot.owner,
            name=f"[Bot] {bot.name}",
            prompt=self._build_mission_prompt(bot),
            task_type="llm",
            schedule="manual",          # firing is driven by the bot dispatcher
            status="active",
            output_target="session",
            model=bot.model,
            endpoint_url=bot.endpoint_url,
            notifications_enabled=False,  # bot notifies through its own channel
            max_steps=20,
        )
        db.add(task)
        db.flush()
        return task

    # ── runs ────────────────────────────────────────────────────────────

    def _has_active_run(self, db, bot_id: str) -> bool:
        return db.query(BotRun.id).filter(
            BotRun.bot_id == bot_id,
            BotRun.status.in_(("queued", "running", "waiting_approval")),
        ).first() is not None

    def start_run(
        self,
        bot_id: str,
        owner: Optional[str],
        *,
        trigger_type: str = "manual",
        force: bool = False,
    ) -> Dict[str, Any]:
        """Begin a bot run: create the run row, park it as queued, hand the
        mission to the real scheduler. Returns the run view immediately."""
        if trigger_type not in {"manual", "schedule", "interval", "event", "webhook", "retry"}:
            raise BotServiceError(f"Invalid trigger type: {trigger_type}",
                                  code="invalid_trigger", status=400)

        db = SessionLocal()
        run = None
        try:
            bot = self._owned_bot(db, bot_id, owner)

            if bot.status in ("draft",):
                raise BotServiceError(
                    "This bot is still a draft — activate it first",
                    code="bot_is_draft", status=400,
                )
            if bot.status in ("paused", "disabled") and trigger_type != "manual":
                raise BotServiceError(
                    f"Bot is {bot.status}; only manual runs are possible",
                    code="bot_not_active", status=409,
                )
            if bot.status == "disabled" and trigger_type == "manual":
                raise BotServiceError(
                    "Bot is disabled — re-enable it to run",
                    code="bot_disabled", status=409,
                )
            if bot.status == "error" and trigger_type != "manual":
                raise BotServiceError(
                    "Bot is in error state — resume it or run manually after review",
                    code="bot_in_error", status=409,
                )

            # Duplicate protection: one in-flight run per bot (unless explicitly
            # forced, which is what Run Now (parallel) uses).
            if not force and self._has_active_run(db, bot.id):
                raise BotServiceError(
                    "A run is already queued or executing for this bot",
                    code="run_in_progress", status=409,
                )

            mission = self._ensure_mission(db, bot, trigger_type)
            run = BotRun(
                id=str(uuid.uuid4()),
                bot_id=bot.id,
                owner=bot.owner,
                mission_id=mission.id,
                trigger_type=trigger_type,
                status="queued",
                attempt=1,
            )
            db.add(run)
            bot.last_run_at = _now()
            bot.total_runs = (bot.total_runs or 0) + 1
            bot.updated_at = _now()
            if bot.status not in ("error", "disabled"):
                bot.status = "running"
            db.commit()
            # Serialise *inside* the session: after close() the ORM object is
            # expired and attribute access re-fetches against a dead session.
            run_view = run_to_dict(run)
            mission_id = mission.id
            run_id = run.id
        finally:
            db.close()

        # Hand off to the existing scheduler (outside the DB session).
        self._dispatch(mission_id, run_id)
        return run_view

    def _dispatch(self, mission_id: str, run_id: str) -> None:
        """Ask the real scheduler to execute the mission now.

        Fire-and-forget: the scheduler owns execution, retries within the loop,
        and TaskRun recording. BotRun completion is reconciled by
        :meth:`_watch_run` polling the TaskRun — which keeps us honest even if
        the scheduler's internals change.
        """
        if self._scheduler is None:
            try:
                from src.event_bus import get_task_scheduler
                self._scheduler = get_task_scheduler()
            except Exception:
                self._scheduler = None
        if self._scheduler is None:
            self._fail_run(run_id, "Scheduler unavailable — run was queued but not started",
                           code="scheduler_unavailable")
            return
        try:
            started = self._scheduler.run_task_now(mission_id, force=True)
            if not started:
                self._fail_run(run_id, "Scheduler refused to start the mission",
                               code="scheduler_refused")
                return
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._watch_run(run_id, mission_id))
            except RuntimeError:
                # No running loop (e.g. sync test context) — reconcile inline.
                self._reconcile_run(run_id, mission_id)
        except Exception as exc:
            logger.exception("bot run dispatch failed")
            self._fail_run(run_id, f"Could not start the mission: {exc}",
                           code="dispatch_failed")

    async def _watch_run(self, run_id: str, mission_id: str) -> None:
        """Reconcile the BotRun with its TaskRun while the mission executes."""
        import asyncio
        waited = 0.0
        while waited < 6 * 3600:  # 6h ceiling; the scheduler has its own timeouts
            await asyncio.sleep(2)
            waited += 2
            if self._reconcile_run(run_id, mission_id):
                return

    def _reconcile_run(self, run_id: str, mission_id: str) -> bool:
        """Copy the newest TaskRun state into the BotRun. True when finished."""
        db = SessionLocal()
        try:
            run = db.query(BotRun).filter(BotRun.id == run_id).first()
            if not run or run.status in ("completed", "failed", "cancelled", "skipped"):
                return run is not None and run.status in ("completed", "failed", "cancelled", "skipped")

            from core.database import TaskRun
            task_run = (
                db.query(TaskRun)
                .filter(TaskRun.task_id == mission_id)
                .order_by(TaskRun.started_at.desc())
                .first()
            )
            if not task_run:
                return False

            run.task_run_id = task_run.id
            run.model_used = task_run.model or run.model_used
            run.tokens_used = task_run.tokens_used or run.tokens_used

            if task_run.status == "running":
                run.status = "running"
                run.started_at = run.started_at or task_run.started_at
                db.commit()
                return False

            # Terminal TaskRun states.
            run.started_at = run.started_at or task_run.started_at
            run.completed_at = task_run.finished_at or _now()
            if run.started_at:
                delta = (run.completed_at - run.started_at).total_seconds()
                run.duration_ms = int(max(0, delta) * 1000)
            run.result = (task_run.result or "")[:100000] or None
            run.error = task_run.error

            bot = db.query(Bot).filter(Bot.id == run.bot_id).first()
            if task_run.status == "success" or (task_run.status == "aborted" and not task_run.error):
                run.status = "completed"
                run.summary = self._summarise(task_run.result or "")
                if bot:
                    bot.successful_runs = (bot.successful_runs or 0) + 1
                    bot.last_success_at = _now()
                    bot.consecutive_failures = 0
                    bot.last_error = None
            else:
                run.status = "failed"
                run.error = task_run.error or "Mission failed"
                if bot:
                    bot.failed_runs = (bot.failed_runs or 0) + 1
                    bot.consecutive_failures = (bot.consecutive_failures or 0) + 1
                    bot.last_error = run.error
            run.updated_at = _now()

            if bot:
                # Back to a resting state: running → active, unless failures
                # parked it in error.
                if bot.status == "running":
                    if run.status == "failed" and (bot.consecutive_failures or 0) >= 3:
                        bot.status = "error"
                        self._notify(bot, "failed", run)
                    else:
                        bot.status = "active"
                elif bot.status == "error" and run.status == "completed":
                    bot.status = "active"
                if run.status == "completed":
                    self._notify(bot, "completed", run)
                bot.updated_at = _now()

            db.commit()
            return True
        except Exception:
            db.rollback()
            logger.exception("bot run reconciliation failed for %s", run_id)
            return False
        finally:
            db.close()

    @staticmethod
    def _summarise(text: str, limit: int = 280) -> str:
        text = (text or "").strip()
        if len(text) <= limit:
            return text
        return text[:limit].rsplit(" ", 1)[0] + "…"

    # ── failure / retries ───────────────────────────────────────────────

    def _fail_run(self, run_id: str, message: str, *, code: str = "run_failed") -> None:
        db = SessionLocal()
        try:
            run = db.query(BotRun).filter(BotRun.id == run_id).first()
            if not run:
                return
            run.status = "failed"
            run.error = message
            run.completed_at = _now()
            bot = db.query(Bot).filter(Bot.id == run.bot_id).first()
            if bot:
                bot.failed_runs = (bot.failed_runs or 0) + 1
                bot.consecutive_failures = (bot.consecutive_failures or 0) + 1
                bot.last_error = message
                if bot.status == "running":
                    bot.status = "error" if (bot.consecutive_failures or 0) >= 3 else "active"
                bot.updated_at = _now()
                self._notify(bot, "failed", run)
            db.commit()
        finally:
            db.close()

    def retry_run(self, bot_id: str, owner: Optional[str], run_id: str) -> Dict[str, Any]:
        """Re-run a failed run, respecting the bot's max_retries cap."""
        db = SessionLocal()
        try:
            bot = self._owned_bot(db, bot_id, owner)
            run = db.query(BotRun).filter(
                BotRun.id == run_id, BotRun.bot_id == bot.id
            ).first()
            if not run:
                raise BotServiceError("Run not found", code="run_not_found", status=404)
            if run.status not in ("failed", "cancelled"):
                raise BotServiceError("Only failed runs can be retried",
                                      code="not_retryable", status=400)
            attempts = db.query(BotRun).filter(
                BotRun.bot_id == bot.id,
                BotRun.mission_id == run.mission_id,
            ).count()
            if attempts > (bot.max_retries or 0) + 1:
                raise BotServiceError(
                    f"Retry limit reached ({bot.max_retries or 0} retries)",
                    code="retry_limit", status=429,
                )
        finally:
            db.close()
        return self.start_run(bot_id, owner, trigger_type="retry", force=False)

    # ── approvals ───────────────────────────────────────────────────────

    def raise_approval(
        self, bot: Bot, run_id: str, action: str, description: str,
        *, risk_level: str = "medium", context: Optional[dict] = None,
    ) -> Dict[str, Any]:
        """Park a run awaiting sign-off. Returns the approval view."""
        db = SessionLocal()
        try:
            approval = BotApproval(
                id=str(uuid.uuid4()),
                bot_id=bot.id,
                owner=bot.owner,
                run_id=run_id,
                action=action,
                description=description[:2000],
                risk_level=risk_level if risk_level in ("low", "medium", "high") else "medium",
                context=json.dumps(context or {}),
                status="pending",
            )
            db.add(approval)
            run = db.query(BotRun).filter(BotRun.id == run_id).first()
            if run:
                run.status = "waiting_approval"
            bot.status = "waiting_approval"
            bot.updated_at = _now()
            db.commit()
            view = approval_to_dict(approval)
        finally:
            db.close()
        self._notify(bot, "approval", None)
        return view

    def decide_approval(
        self, bot_id: str, owner: Optional[str], approval_id: str,
        *, approve: bool, pattern: bool = False,
    ) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            bot = self._owned_bot(db, bot_id, owner)
            approval = db.query(BotApproval).filter(
                BotApproval.id == approval_id, BotApproval.bot_id == bot.id
            ).first()
            if not approval:
                raise BotServiceError("Approval not found", code="approval_not_found", status=404)
            if approval.status != "pending":
                raise BotServiceError(
                    f"Approval already {approval.status}", code="already_decided", status=409
                )

            approval.status = "approved" if approve else "denied"
            approval.decided_at = _now()
            approval.decided_by = owner
            approval.pattern = bool(pattern and approve)

            # "Approve and allow pattern" narrows the policy, never widens it:
            # it records that THIS action, on THIS bot, may skip the next ask —
            # within the mode switch. Sensitive actions in per_run mode still
            # ask again; the pattern is stored so pattern-mode bots skip it.
            if approve and pattern:
                policy = _parse_json_dict(bot.approval_policy)
                policy["mode"] = "pattern"
                always = set(policy.get("always") or [])
                always.add(approval.action)
                policy["always"] = sorted(always)[:20]
                bot.approval_policy = json.dumps(policy)

            run = db.query(BotRun).filter(BotRun.id == approval.run_id).first()
            if run:
                if approve:
                    run.status = "running"   # resume
                else:
                    run.status = "cancelled"
                    run.error = run.error or "Denied by user"
                    run.completed_at = _now()

            bot.status = "running" if approve else "active"
            bot.updated_at = _now()
            db.commit()
            view = approval_to_dict(approval)
        finally:
            db.close()
        return view

    def pending_approvals(self, bot_id: str, owner: Optional[str]) -> List[Dict[str, Any]]:
        db = SessionLocal()
        try:
            bot = self._owned_bot(db, bot_id, owner)
            rows = db.query(BotApproval).filter(
                BotApproval.bot_id == bot.id, BotApproval.status == "pending"
            ).order_by(BotApproval.created_at.desc()).all()
            return [approval_to_dict(a) for a in rows]
        finally:
            db.close()

    # ── queries for the detail page ─────────────────────────────────────

    def list_runs(self, bot_id: str, owner: Optional[str], *, limit: int = 50) -> List[Dict[str, Any]]:
        db = SessionLocal()
        try:
            bot = self._owned_bot(db, bot_id, owner)
            runs = db.query(BotRun).filter(
                BotRun.bot_id == bot.id
            ).order_by(BotRun.created_at.desc()).limit(min(200, max(1, limit))).all()
            return [run_to_dict(r) for r in runs]
        finally:
            db.close()

    def get_run(self, bot_id: str, owner: Optional[str], run_id: str) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            bot = self._owned_bot(db, bot_id, owner)
            run = db.query(BotRun).filter(
                BotRun.id == run_id, BotRun.bot_id == bot.id
            ).first()
            if not run:
                raise BotServiceError("Run not found", code="run_not_found", status=404)
            return run_to_dict(run)
        finally:
            db.close()

    def activity(self, bot_id: str, owner: Optional[str], *, limit: int = 100) -> List[Dict[str, Any]]:
        """Timeline of what the bot did, from real run rows."""
        runs = self.list_runs(bot_id, owner, limit=limit)
        events = []
        for run in runs:
            if run["started_at"]:
                events.append({
                    "at": run["started_at"], "kind": "run_started",
                    "text": f"Run {run['id'][:8]} started ({run['trigger_type']})",
                    "run_id": run["id"],
                })
            if run["status"] == "waiting_approval":
                events.append({
                    "at": run["started_at"], "kind": "approval",
                    "text": "Waiting for approval", "run_id": run["id"],
                })
            if run["completed_at"]:
                kind = "run_completed" if run["status"] == "completed" else "run_failed"
                text = ("Completed" if run["status"] == "completed"
                        else f"Failed: {run['error'] or 'unknown error'}")
                events.append({
                    "at": run["completed_at"], "kind": kind, "text": text,
                    "run_id": run["id"],
                })
        events.sort(key=lambda e: e["at"] or "", reverse=True)
        return events[:limit]

    def health(self, bot_id: str, owner: Optional[str]) -> Dict[str, Any]:
        """Operational health from real counters — no invented scores."""
        bot = self.get_bot(bot_id, owner)
        total = bot["total_runs"]
        success = bot["successful_runs"]
        failed = bot["failed_runs"]
        return {
            "status": bot["status"],
            "total_runs": total,
            "successful_runs": success,
            "failed_runs": failed,
            "success_rate": round(success / total, 3) if total else None,
            "consecutive_failures": bot["consecutive_failures"],
            "last_run_at": bot["last_run_at"],
            "last_success_at": bot["last_success_at"],
            "last_error": bot["last_error"],
            "blocked": bot["status"] in ("error", "waiting_approval", "paused"),
        }

    # ── memory (existing memory infrastructure) ─────────────────────────

    def remember(self, bot: Bot, text: str, category: str = "fact") -> bool:
        """Store a durable preference through the existing memory manager.

        Only stores when the bot has memory enabled and the text is
        non-empty; categories reuse the existing memory vocabulary.
        """
        if not bot.memory_enabled or not (text or "").strip():
            return False
        try:
            from src.app_helpers import get_components
            components = get_components()
            memory_manager = components["memory_manager"]
            memory_manager.add_entry(
                text=text.strip(),
                source=f"bot:{bot.name}",
                category=category if category in ("fact", "preference", "skill") else "fact",
                owner=bot.owner,
            )
            return True
        except Exception:
            logger.debug("bot memory write failed", exc_info=True)
            return False

    def bot_memories(self, bot_id: str, owner: Optional[str], *, limit: int = 50) -> List[dict]:
        """Memories this bot wrote (source-tagged), scoped to the owner."""
        db = SessionLocal()
        try:
            bot = self._owned_bot(db, bot_id, owner)
        finally:
            db.close()
        try:
            from src.app_helpers import get_components
            memory_manager = get_components()["memory_manager"]
            entries = memory_manager.list_entries(owner=bot.owner) or []
            prefix = f"bot:{bot.name}"
            mine = [e for e in entries if (e.get("source") or "").startswith(prefix)]
            return mine[:limit]
        except Exception:
            return []

    # ── notifications (existing channel) ────────────────────────────────

    def _notify(self, bot: Bot, kind: str, run: Optional[BotRun]) -> None:
        wants = _parse_json_list(bot.notify_on, ["completed", "failed", "approval"])
        if kind not in wants:
            return
        try:
            from src.event_bus import get_task_scheduler
            scheduler = get_task_scheduler()
            if scheduler is None:
                return
            body = None
            if run is not None:
                body = run.error if kind == "failed" else self._summarise(run.result or "")
            scheduler.add_notification(
                task_name=f"[Bot] {bot.name}",
                status={"completed": "success", "failed": "error", "approval": "approval"}.get(kind, kind),
                task_id=bot.id,
                owner=bot.owner,
                body=body,
            )
        except Exception:
            logger.debug("bot notification failed", exc_info=True)

    # ── the dispatcher: which bots are due? ─────────────────────────────

    def due_bots(self, *, now: Optional[datetime] = None) -> List[Tuple[Bot, str]]:
        """Bots whose triggers are due, with the trigger that fired.

        Mirrors the scheduler's own due-query semantics but for bots: only
        active bots, only their trigger specs, one firing per tick. Manual and
        webhook triggers never appear here — they fire through their own paths.
        """
        now = now or _now()
        out: List[Tuple[Bot, str]] = []
        db = SessionLocal()
        try:
            bots = db.query(Bot).filter(Bot.status == "active").all()
            for bot in bots:
                for trigger in _parse_json_list(bot.triggers):
                    ttype = trigger.get("type")
                    if ttype == "interval":
                        last = bot.last_run_at
                        hours = float(trigger.get("hours") or 6)
                        if not last or last <= now - timedelta(hours=hours):
                            out.append((bot, "interval"))
                            break
                    elif ttype == "schedule":
                        # ScheduledTask's own scheduler handles cron/daily; the
                        # bot only maps the spec onto its mission at creation.
                        out.append((bot, "schedule"))
                        break
        finally:
            db.close()
        return out

    def tick(self) -> int:
        """One scheduler pass: start runs for every due bot.

        Called from the app's existing background loop. Returns how many runs
        were started; never raises.
        """
        started = 0
        try:
            for bot, trigger in self.due_bots():
                try:
                    self.start_run(bot.id, bot.owner, trigger_type=trigger)
                    started += 1
                except BotServiceError as exc:
                    if exc.code != "run_in_progress":
                        logger.debug("bot %s tick skipped: %s", bot.id, exc.code)
        except Exception:
            logger.exception("bot dispatcher tick failed")
        return started
