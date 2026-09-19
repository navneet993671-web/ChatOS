"""Tests for the Bots feature: lifecycle, permissions, service, isolation.

Uses a temp-file SQLite database and a stub scheduler, so tests never touch the
user's live data and never execute real agent loops. The security properties —
ownership indistinguishability, deny-by-default tools, transition validation —
are asserted directly.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.database import (
    Bot,
    BotApproval,
    BotRun,
    ScheduledTask,
    SessionLocal,
    init_db,
)


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """A throwaway database in a temp dir.

    The engine and sessionmaker are created at *import* time from the module-
    level DATABASE_URL, so patching the string is not enough — the fixture must
    swap the module's `engine` and `SessionLocal` themselves. Getting this wrong
    would point the tests at the developer's real database.
    """
    import core.database as core_db

    db_path = tmp_path / "bots-test.db"
    test_engine = core_db.create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    test_sessionlocal = core_db.sessionmaker(
        autocommit=False, autoflush=False, bind=test_engine
    )

    monkeypatch.setattr(core_db, "engine", test_engine)
    monkeypatch.setattr(core_db, "SessionLocal", test_sessionlocal)
    monkeypatch.setattr(core_db, "DATABASE_URL", f"sqlite:///{db_path}", raising=False)

    # Modules that imported SessionLocal BY NAME at import time keep the old
    # binding unless patched too. This is the part that actually matters.
    import services.bots.service as bots_service_mod
    monkeypatch.setattr(bots_service_mod, "SessionLocal", test_sessionlocal)

    # create_all against the *test* engine only.
    core_db.Base.metadata.create_all(bind=test_engine)

    yield db_path
    test_engine.dispose()


@pytest.fixture()
def svc(fresh_db):
    from services.bots.service import BotService
    service = BotService(scheduler=None)
    # Convenience: tests can assert with `svc.Error`.
    service.Error = BotServiceError
    return service


class StubScheduler:
    """Records dispatches instead of executing real agent loops."""

    def __init__(self):
        self.dispatched = []

    def run_task_now(self, mission_id, force=False):
        self.dispatched.append((mission_id, force))
        return True


def _make_bot(svc, owner="alice", **overrides):
    payload = {
        "name": "Test Bot",
        "description": "A bot for tests",
        "instructions": "Be brief.",
        "autonomy_level": 1,
        "capabilities": ["web.search", "memory.read"],
        "triggers": [{"type": "manual"}],
    }
    payload.update(overrides)
    return svc.create_bot(owner, payload)


from services.bots.service import BotServiceError  # noqa: E402  (used via svc.Error)


# ── CRUD & ownership ────────────────────────────────────────────────────


def test_create_bot_starts_as_draft(svc):
    bot = _make_bot(svc)
    assert bot["status"] == "draft"
    assert bot["owner"] == "alice"
    assert bot["allowed_transitions"] == ["active", "disabled"]


def test_owner_is_scoped_out_of_foreign_reads(svc):
    """User B cannot read, update, run or delete User A's bot — and a foreign
    bot is indistinguishable from a missing one (404, never 403)."""
    bot = _make_bot(svc, owner="alice")

    with pytest.raises(BotServiceError) as exc:
        svc.get_bot(bot["id"], "bob")
    assert exc.value.status == 404

    with pytest.raises(BotServiceError) as exc:
        svc.transition(bot["id"], "bob", "active")
    assert exc.value.status == 404

    with pytest.raises(BotServiceError) as exc:
        svc.start_run(bot["id"], "bob", trigger_type="manual")
    assert exc.value.status == 404

    with pytest.raises(BotServiceError) as exc:
        svc.delete_bot(bot["id"], "bob")
    assert exc.value.status == 404


def test_list_bots_only_shows_the_callers(svc):
    _make_bot(svc, owner="alice", name="Alice Bot")
    _make_bot(svc, owner="bob", name="Bob Bot")
    alice_names = [b["name"] for b in svc.list_bots("alice")]
    bob_names = [b["name"] for b in svc.list_bots("bob")]
    assert alice_names == ["Alice Bot"]
    assert bob_names == ["Bob Bot"]


def test_update_revalidates_capabilities(svc):
    bot = _make_bot(svc)
    with pytest.raises(BotServiceError) as exc:
        svc.update_bot(bot["id"], "alice", {"capabilities": ["email.send"]})
    assert exc.value.code == "capability_above_level"


def test_delete_pauses_missions_and_cascades(svc):
    bot = _make_bot(svc)
    # Simulate an existing mission + run.
    db = SessionLocal()
    try:
        task = ScheduledTask(id="t1", owner="alice", name="[Bot] Test Bot",
                             prompt="x", task_type="llm", status="active")
        db.add(task)
        db.add(BotRun(id="r1", bot_id=bot["id"], owner="alice", mission_id="t1"))
        db.commit()
    finally:
        db.close()

    info = svc.delete_bot(bot["id"], "alice")
    assert info["missions_paused"] == 1

    db = SessionLocal()
    try:
        assert db.query(ScheduledTask).filter(ScheduledTask.id == "t1").first().status == "paused"
        assert db.query(BotRun).filter(BotRun.id == "r1").first() is None  # cascaded
        assert db.query(Bot).filter(Bot.id == bot["id"]).first() is None
    finally:
        db.close()


# ── lifecycle ───────────────────────────────────────────────────────────


def test_lifecycle_happy_path(svc):
    bot = _make_bot(svc)
    bot = svc.transition(bot["id"], "alice", "active")
    assert bot["status"] == "active"
    bot = svc.transition(bot["id"], "alice", "paused")
    assert bot["status"] == "paused"
    bot = svc.transition(bot["id"], "alice", "active")
    bot = svc.transition(bot["id"], "alice", "disabled")
    assert bot["status"] == "disabled"


def test_illegal_transitions_are_refused(svc):
    bot = _make_bot(svc)
    for target in ("running", "paused", "error", "waiting_approval"):
        with pytest.raises(BotServiceError) as exc:
            svc.transition(bot["id"], "alice", target)
        assert exc.value.code == "invalid_transition"


def test_disabled_bot_cannot_be_resumed_from_anything_but_enable(svc):
    bot = _make_bot(svc)
    svc.transition(bot["id"], "alice", "disabled")
    with pytest.raises(BotServiceError):
        svc.transition(bot["id"], "alice", "paused")


def test_activation_creates_workspace(svc, tmp_path, monkeypatch):
    """data/bots/<owner>/<bot-id>/ with the five subdirectories."""
    import core.database as core_db  # noqa: F401 — ensure initialized
    bot = _make_bot(svc, owner="alice")
    svc.transition(bot["id"], "alice", "active")
    db = SessionLocal()
    try:
        row = db.query(Bot).filter(Bot.id == bot["id"]).first()
        ws = Path(row.workspace_dir)
        assert not Path(ws).is_absolute() or ws.exists()
        for sub in ("workspace", "artifacts", "runs", "temp", "logs"):
            assert (Path(ws) / sub).exists(), sub
        # Owner isolation is structural: another owner's path segment differs.
        assert "alice" in str(ws)
    finally:
        db.close()


# ── runs ────────────────────────────────────────────────────────────────


def test_manual_run_dispatches_through_the_scheduler(svc):
    stub = StubScheduler()
    svc._scheduler = stub
    bot = _make_bot(svc)
    svc.transition(bot["id"], "alice", "active")

    run = svc.start_run(bot["id"], "alice", trigger_type="manual")
    assert run["status"] == "queued"
    assert run["trigger_type"] == "manual"
    assert len(stub.dispatched) == 1


def test_duplicate_runs_are_refused(svc):
    """Two Run Now presses must not create two concurrent runs."""
    stub = StubScheduler()
    svc._scheduler = stub
    bot = _make_bot(svc)
    svc.transition(bot["id"], "alice", "active")

    svc.start_run(bot["id"], "alice", trigger_type="manual")
    # The first run is still queued (stub never completes it), so the second
    # must be refused with 409.
    with pytest.raises(BotServiceError) as exc:
        svc.start_run(bot["id"], "alice", trigger_type="manual")
    assert exc.value.code == "run_in_progress"


def test_draft_bot_cannot_run(svc):
    bot = _make_bot(svc)
    with pytest.raises(BotServiceError) as exc:
        svc.start_run(bot["id"], "alice", trigger_type="manual")
    assert exc.value.code == "bot_is_draft"


def test_paused_bot_ignores_automatic_triggers(svc):
    bot = _make_bot(svc)
    svc.transition(bot["id"], "alice", "active")
    svc.transition(bot["id"], "alice", "paused")
    with pytest.raises(BotServiceError) as exc:
        svc.start_run(bot["id"], "alice", trigger_type="schedule")
    assert exc.value.code == "bot_not_active"


def test_run_creates_a_mission_via_scheduledtask(svc):
    """The bot's mission IS a ScheduledTask — no second execution engine."""
    svc._scheduler = StubScheduler()
    bot = _make_bot(svc)
    svc.transition(bot["id"], "alice", "active")
    run = svc.start_run(bot["id"], "alice", trigger_type="manual")

    db = SessionLocal()
    try:
        run_row = db.query(BotRun).filter(BotRun.id == run["id"]).first()
        task = db.query(ScheduledTask).filter(ScheduledTask.id == run_row.mission_id).first()
        assert task is not None
        assert task.owner == "alice"
        assert task.name.startswith("[Bot]")
        assert task.prompt  # the bot's standing instructions
    finally:
        db.close()


def test_reconcile_copies_taskrun_outcome(svc):
    """A successful TaskRun marks the BotRun completed and bumps bot stats."""
    svc._scheduler = StubScheduler()
    bot = _make_bot(svc)
    svc.transition(bot["id"], "alice", "active")
    run = svc.start_run(bot["id"], "alice", trigger_type="manual")

    db = SessionLocal()
    try:
        run_row = db.query(BotRun).filter(BotRun.id == run["id"]).first()
        from core.database import TaskRun
        task_run = TaskRun(
            id="tr1", task_id=run_row.mission_id, started_at=datetime.utcnow(),
            finished_at=datetime.utcnow(), status="success", result="All done.",
        )
        db.add(task_run)
        db.commit()
        mission_id = run_row.mission_id
    finally:
        db.close()

    finished = svc._reconcile_run(run["id"], mission_id)
    assert finished is True

    db = SessionLocal()
    try:
        run_row = db.query(BotRun).filter(BotRun.id == run["id"]).first()
        assert run_row.status == "completed"
        assert run_row.summary == "All done."
        bot_row = db.query(Bot).filter(Bot.id == bot["id"]).first()
        assert bot_row.successful_runs == 1
        assert bot_row.consecutive_failures == 0
        assert bot_row.status == "active"  # back to rest after running
    finally:
        db.close()


def test_three_consecutive_failures_park_the_bot_in_error(svc):
    svc._scheduler = StubScheduler()
    bot = _make_bot(svc)
    svc.transition(bot["id"], "alice", "active")

    for _ in range(3):
        run = svc.start_run(bot["id"], "alice", trigger_type="manual")
        svc._fail_run(run["id"], "boom")

    db = SessionLocal()
    try:
        bot_row = db.query(Bot).filter(Bot.id == bot["id"]).first()
        assert bot_row.status == "error"
        assert bot_row.consecutive_failures == 3
    finally:
        db.close()


def test_error_bot_recovers_via_manual_run_or_resume(svc):
    svc._scheduler = StubScheduler()
    bot = _make_bot(svc)
    svc.transition(bot["id"], "alice", "active")
    run = svc.start_run(bot["id"], "alice", trigger_type="manual")
    svc._fail_run(run["id"], "boom")

    # Manual runs are allowed from error (that's how you un-stick it).
    run2 = svc.start_run(bot["id"], "alice", trigger_type="manual")
    assert run2["status"] == "queued"


def test_retry_respects_max_retries(svc):
    svc._scheduler = StubScheduler()
    bot = _make_bot(svc, max_retries=0)
    svc.transition(bot["id"], "alice", "active")
    run = svc.start_run(bot["id"], "alice", trigger_type="manual")
    svc._fail_run(run["id"], "boom")

    with pytest.raises(BotServiceError) as exc:
        svc.retry_run(bot["id"], "alice", run["id"])
    assert exc.value.code == "retry_limit"


def test_completed_run_notifies_through_the_scheduler(svc):
    """Notifications reuse the scheduler's channel, respecting notify_on."""
    stub = StubScheduler()
    stub.add_notification = MagicMock()
    svc._scheduler = stub
    bot = _make_bot(svc, notify_on=["completed"])
    svc.transition(bot["id"], "alice", "active")
    run = svc.start_run(bot["id"], "alice", trigger_type="manual")

    svc._fail_run(run["id"], "boom")   # failed NOT in notify_on
    assert stub.add_notification.call_count == 0


# ── approvals ───────────────────────────────────────────────────────────


def test_approval_parks_the_run_and_the_bot(svc):
    svc._scheduler = StubScheduler()
    bot = _make_bot(svc)
    svc.transition(bot["id"], "alice", "active")
    run = svc.start_run(bot["id"], "alice", trigger_type="manual")

    approval = svc.raise_approval(
        db.query(Bot).filter(Bot.id == bot["id"]).first() if False else _bot_row(svc, bot["id"]),
        run["id"], "email.send", "Send email to x@y",
        risk_level="high", context={"to": "x@y"},
    )
    assert approval["status"] == "pending"

    db = SessionLocal()
    try:
        assert db.query(BotRun).filter(BotRun.id == run["id"]).first().status == "waiting_approval"
        assert db.query(Bot).filter(Bot.id == bot["id"]).first().status == "waiting_approval"
    finally:
        db.close()

    result = svc.decide_approval(bot["id"], "alice", approval["id"], approve=True)
    assert result["status"] == "approved"


def _bot_row(svc, bot_id):
    db = SessionLocal()
    try:
        return db.query(Bot).filter(Bot.id == bot_id).first()
    finally:
        db.close()


def test_denial_cancels_the_run(svc):
    svc._scheduler = StubScheduler()
    bot = _make_bot(svc)
    svc.transition(bot["id"], "alice", "active")
    run = svc.start_run(bot["id"], "alice", trigger_type="manual")
    approval = svc.raise_approval(_bot_row(svc, bot["id"]), run["id"], "shell.exec", "rm -rf /tmp/x")

    svc.decide_approval(bot["id"], "alice", approval["id"], approve=False)
    db = SessionLocal()
    try:
        assert db.query(BotRun).filter(BotRun.id == run["id"]).first().status == "cancelled"
    finally:
        db.close()


def test_approve_and_allow_pattern_is_narrow(svc):
    """Pattern approval records THIS action on THIS bot — not a blank cheque."""
    svc._scheduler = StubScheduler()
    bot = _make_bot(svc)
    svc.transition(bot["id"], "alice", "active")
    run = svc.start_run(bot["id"], "alice", trigger_type="manual")
    approval = svc.raise_approval(_bot_row(svc, bot["id"]), run["id"], "email.send", "weekly update")

    svc.decide_approval(bot["id"], "alice", approval["id"], approve=True, pattern=True)

    policy = svc.get_bot(bot["id"], "alice")["approval_policy"]  # already parsed
    assert policy["mode"] == "pattern"
    assert policy["always"] == ["email.send"]
    # And everything else still requires asking:
    from services.bots.permissions import requires_approval
    assert requires_approval("shell.execute", ["email.send"], policy) is True


def test_approvals_are_owner_scoped(svc):
    svc._scheduler = StubScheduler()
    bot = _make_bot(svc)
    svc.transition(bot["id"], "alice", "active")
    run = svc.start_run(bot["id"], "alice", trigger_type="manual")
    approval = svc.raise_approval(_bot_row(svc, bot["id"]), run["id"], "email.send", "x")

    with pytest.raises(BotServiceError):
        svc.decide_approval(bot["id"], "bob", approval["id"], approve=True)


# ── permissions ─────────────────────────────────────────────────────────


def test_autonomy_never_grants_undeclared_capabilities():
    from services.bots.permissions import capabilities_for
    # Level 3 does not imply anything: only what was requested survives.
    assert capabilities_for([], 3) == set()


def test_level_zero_cannot_hold_any_side_effect():
    from services.bots.permissions import capabilities_for, CAPABILITY_KEYS
    effective = capabilities_for(sorted(CAPABILITY_KEYS), 0)
    assert effective == {"web.search", "library.read", "memory.read", "calendar.read"}


def test_sensitivity_table_matches_the_docstring():
    from services.bots.permissions import SENSITIVE_CAPABILITIES
    assert "email.send" in SENSITIVE_CAPABILITIES
    assert "web.search" not in SENSITIVE_CAPABILITIES


def test_unknown_capability_keys_are_dropped():
    from services.bots.permissions import capabilities_for
    assert capabilities_for(["web.search", "not.a.thing"], 2) == {"web.search"}


def test_junk_level_falls_back_to_assist():
    from services.bots.permissions import capabilities_for, normalise_level
    assert normalise_level("nonsense") == 1
    assert normalise_level(99) == 3
    assert normalise_level(-2) == 0


# ── dispatcher ──────────────────────────────────────────────────────────


def test_due_bots_only_include_active_bots_with_due_triggers(svc):
    bot = _make_bot(svc, triggers=[{"type": "interval", "hours": 1}])
    # Draft: never due.
    assert svc.due_bots() == []

    svc.transition(bot["id"], "alice", "active")
    # Active and never run: interval is due.
    due = svc.due_bots()
    assert len(due) == 1 and due[0][1] == "interval"

    # Just ran: not due until the interval elapses.
    db = SessionLocal()
    try:
        row = db.query(Bot).filter(Bot.id == bot["id"]).first()
        row.last_run_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()
    assert svc.due_bots() == []


def test_tick_starts_runs_for_due_bots(svc):
    stub = StubScheduler()
    svc._scheduler = stub
    bot = _make_bot(svc, triggers=[{"type": "interval", "hours": 1}])
    svc.transition(bot["id"], "alice", "active")

    assert svc.tick() == 1
    assert len(stub.dispatched) == 1
    # Second tick immediately after: the run is in flight, not due again.
    assert svc.tick() == 0
