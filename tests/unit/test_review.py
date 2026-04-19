"""
Unit tests for gtd/engine/review.py — US-014: weekly review prep + interactive run.

Covers:
- collect_snapshot: 3 inbox, 2 waiting, 1 project (with 1 child), 1 someday → right counts
- render_snapshot_md: snapshot dict → markdown contains section headers
- prepare('friday_prep'): writes memory/reviews/YYYY-MM-DD-friday_prep.md,
  dispatches 1 Q (kind='review_agenda', payload contains snapshot path)
- prepare('sunday_nudge') when Friday Q was answered → 0 Qs, q_skipped_reason='friday_acknowledged'
- prepare('sunday_nudge') when Friday Q open/cancelled → 1 Q dispatched
- prepare('sunday_nudge') with no friday Q this week → 1 Q dispatched
- snapshot file path uses local date YYYY-MM-DD
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from gtd.engine.state import init_db, insert_project, insert_item
import gtd.engine.qchannel as qchannel_mod
from gtd.engine.review import collect_snapshot, render_snapshot_md, prepare

# ---------------------------------------------------------------------------
# Stub reminders module
# ---------------------------------------------------------------------------


@dataclass
class FakeReminder:
    id: str
    list: str
    name: str
    completed: bool = False
    due_date: str = ""
    completion_date: str = ""
    body: str = ""
    priority: int = 0
    last_modified: str = "2026-01-01T10:00:00Z"


class StubRemModule:
    """Minimal stub for bin/lib/reminders."""

    def __init__(self, reminders: list[FakeReminder] | None = None):
        self._reminders: list[FakeReminder] = reminders or []

    def list_all(self, days_done_window: int = 7) -> list[FakeReminder]:
        return list(self._reminders)

    def create(self, list_name: str, name: str, notes: str = "", due_iso: str = "") -> str:
        rid = f"FAKE-{len(self._reminders)}"
        self._reminders.append(FakeReminder(id=rid, list=list_name, name=name))
        return rid

    def update_notes(self, rid: str, list_name: str, notes: str) -> None:
        pass

    def set_complete(self, rid: str, list_name: str, completed: bool) -> None:
        pass

    def delete(self, rid: str, list_name: str) -> None:
        self._reminders = [r for r in self._reminders if r.id != rid]


# ---------------------------------------------------------------------------
# Stub Q channel module
# ---------------------------------------------------------------------------


@dataclass
class FakeDispatchResult:
    qid: str | None
    status: str
    reason: str | None = None


class StubQChannel:
    """Records dispatch() calls and returns configurable results."""

    def __init__(self, status: str = "dryrun"):
        self._dispatch_calls: list[dict] = []
        self._status = status

    def dispatch(self, *, conn, rem_module=None, kind, prompt, payload=None,
                 dispatch_dryrun=True, now=None, log_dir=None, **kwargs) -> FakeDispatchResult:
        self._dispatch_calls.append({
            "kind": kind,
            "prompt": prompt,
            "payload": payload,
        })
        return FakeDispatchResult(qid="FAKE-QID-0", status=self._status)

    @property
    def dispatch_count(self) -> int:
        return len(self._dispatch_calls)

    @property
    def last_call(self) -> dict | None:
        return self._dispatch_calls[-1] if self._dispatch_calls else None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_invocation_registry():
    qchannel_mod._invocation_registry.clear()
    yield
    qchannel_mod._invocation_registry.clear()


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "state.db"
    conn = init_db(db_path)
    yield conn
    conn.close()


@pytest.fixture
def memory_dir(tmp_path):
    d = tmp_path / "memory"
    d.mkdir()
    return d


@pytest.fixture
def log_dir(tmp_path):
    d = tmp_path / "log"
    d.mkdir()
    return d


@pytest.fixture
def fixed_now():
    return datetime(2026, 4, 17, 14, 0, 0, tzinfo=timezone.utc)  # Friday


@pytest.fixture
def stub_rem_with_data():
    """3 inbox, 2 waiting, 1 someday, 2 @home next-actions."""
    rems = [
        FakeReminder(id="r1", list="Inbox", name="Item A"),
        FakeReminder(id="r2", list="Inbox", name="Item B"),
        FakeReminder(id="r3", list="Inbox", name="Item C"),
        FakeReminder(id="r4", list="Waiting For", name="Waiting X",
                     body="Waiting for: Alice"),
        FakeReminder(id="r5", list="Waiting For", name="Waiting Y",
                     body="waiting for: Bob"),
        FakeReminder(id="r6", list="Someday/Maybe", name="Someday idea"),
        FakeReminder(id="r7", list="@home", name="Fix sink"),
        FakeReminder(id="r8", list="@home", name="Paint wall"),
    ]
    return StubRemModule(rems)


# ---------------------------------------------------------------------------
# Tests: collect_snapshot
# ---------------------------------------------------------------------------


def test_collect_snapshot_inbox_count(db, stub_rem_with_data):
    snap = collect_snapshot(rem_module=stub_rem_with_data, conn=db)
    assert len(snap["inbox"]) == 3


def test_collect_snapshot_waiting_count(db, stub_rem_with_data):
    snap = collect_snapshot(rem_module=stub_rem_with_data, conn=db)
    assert len(snap["waiting"]) == 2


def test_collect_snapshot_waiting_has_delegates(db, stub_rem_with_data):
    snap = collect_snapshot(rem_module=stub_rem_with_data, conn=db)
    delegates = [w["delegate"] for w in snap["waiting"]]
    assert "Alice" in delegates
    assert "Bob" in delegates


def test_collect_snapshot_someday_count(db, stub_rem_with_data):
    snap = collect_snapshot(rem_module=stub_rem_with_data, conn=db)
    assert len(snap["someday"]) == 1


def test_collect_snapshot_next_actions_by_ctx(db, stub_rem_with_data):
    snap = collect_snapshot(rem_module=stub_rem_with_data, conn=db)
    assert snap["next_actions_by_ctx"].get("@home") == 2


def test_collect_snapshot_project_with_child(db, stub_rem_with_data):
    """1 project with 1 child next-action → child_count=1, stalled=False."""
    insert_project(db, project_id="proj-alpha", outcome="Launch alpha")
    insert_item(
        db,
        rid="na-001",
        kind="next_action",
        list="@home",
        project="proj-alpha",
    )
    snap = collect_snapshot(rem_module=stub_rem_with_data, conn=db)
    proj = next((p for p in snap["projects"] if p["project_id"] == "proj-alpha"), None)
    assert proj is not None
    assert proj["child_count"] == 1
    assert proj["stalled"] is False


def test_collect_snapshot_project_stalled_when_no_children(db, stub_rem_with_data):
    """Project with 0 next-action children → stalled=True."""
    insert_project(db, project_id="proj-beta", outcome="Beta thing")
    snap = collect_snapshot(rem_module=stub_rem_with_data, conn=db)
    proj = next((p for p in snap["projects"] if p["project_id"] == "proj-beta"), None)
    assert proj is not None
    assert proj["child_count"] == 0
    assert proj["stalled"] is True


def test_collect_snapshot_returns_expected_keys(db, stub_rem_with_data):
    snap = collect_snapshot(rem_module=stub_rem_with_data, conn=db)
    for key in ("inbox", "waiting", "projects", "someday",
                "next_actions_by_ctx", "tickler_due_count", "last_review_iso"):
        assert key in snap, f"Missing key: {key}"


def test_collect_snapshot_completed_items_excluded(db):
    rems = [
        FakeReminder(id="done1", list="Inbox", name="Done item", completed=True),
        FakeReminder(id="open1", list="Inbox", name="Open item", completed=False),
    ]
    rem = StubRemModule(rems)
    snap = collect_snapshot(rem_module=rem, conn=db)
    assert len(snap["inbox"]) == 1
    assert snap["inbox"][0]["title"] == "Open item"


# ---------------------------------------------------------------------------
# Tests: render_snapshot_md
# ---------------------------------------------------------------------------


def _make_snapshot() -> dict:
    return {
        "inbox": [{"rid": "r1", "title": "Item A", "age_days": 3.0}],
        "waiting": [{"rid": "r2", "title": "Wait X", "delegate": "Alice", "age_days": 5.0}],
        "projects": [
            {"project_id": "p1", "name": "p1", "outcome": "Ship it",
             "child_count": 2, "stalled": False},
        ],
        "someday": [{"rid": "r3", "title": "Learn Rust", "age_days": 10.0}],
        "next_actions_by_ctx": {"@home": 3, "@calls": 1},
        "tickler_due_count": 0,
        "last_review_iso": None,
    }


def test_render_snapshot_md_has_inbox_header():
    md = render_snapshot_md(_make_snapshot())
    assert "## Inbox" in md


def test_render_snapshot_md_has_waiting_header():
    md = render_snapshot_md(_make_snapshot())
    assert "## Waiting For" in md


def test_render_snapshot_md_has_projects_header():
    md = render_snapshot_md(_make_snapshot())
    assert "## Projects" in md


def test_render_snapshot_md_has_someday_header():
    md = render_snapshot_md(_make_snapshot())
    assert "## Someday" in md


def test_render_snapshot_md_has_context_header():
    md = render_snapshot_md(_make_snapshot())
    assert "## Next Actions by Context" in md


def test_render_snapshot_md_shows_inbox_items():
    md = render_snapshot_md(_make_snapshot())
    assert "Item A" in md


def test_render_snapshot_md_shows_waiting_delegate():
    md = render_snapshot_md(_make_snapshot())
    assert "Alice" in md


def test_render_snapshot_md_shows_someday_item():
    md = render_snapshot_md(_make_snapshot())
    assert "Learn Rust" in md


def test_render_snapshot_md_shows_context_counts():
    md = render_snapshot_md(_make_snapshot())
    assert "@home" in md
    assert "@calls" in md


def test_render_snapshot_md_date_in_header(fixed_now):
    md = render_snapshot_md(_make_snapshot(), now=fixed_now)
    assert "2026-04-17" in md


# ---------------------------------------------------------------------------
# Tests: prepare('friday_prep')
# ---------------------------------------------------------------------------


def test_prepare_friday_prep_writes_file(db, memory_dir, log_dir, fixed_now):
    rem = StubRemModule()
    q = StubQChannel(status="dryrun")
    result = prepare(
        "friday_prep",
        conn=db,
        rem_module=rem,
        qchannel_module=q,
        memory_dir=memory_dir,
        log_dir=log_dir,
        dispatch_dryrun=True,
        now=fixed_now,
    )
    assert result["snapshot_path"].exists()
    assert result["snapshot_path"].name == "2026-04-17-friday_prep.md"


def test_prepare_friday_prep_dispatches_one_q(db, memory_dir, log_dir, fixed_now):
    rem = StubRemModule()
    q = StubQChannel(status="dryrun")
    result = prepare(
        "friday_prep",
        conn=db,
        rem_module=rem,
        qchannel_module=q,
        memory_dir=memory_dir,
        log_dir=log_dir,
        dispatch_dryrun=True,
        now=fixed_now,
    )
    assert q.dispatch_count == 1
    assert result["q_dispatched"] is True


def test_prepare_friday_prep_q_kind_is_review_agenda(db, memory_dir, log_dir, fixed_now):
    rem = StubRemModule()
    q = StubQChannel(status="dryrun")
    prepare(
        "friday_prep",
        conn=db,
        rem_module=rem,
        qchannel_module=q,
        memory_dir=memory_dir,
        log_dir=log_dir,
        dispatch_dryrun=True,
        now=fixed_now,
    )
    assert q.last_call["kind"] == "review_agenda"


def test_prepare_friday_prep_payload_contains_snapshot_path(db, memory_dir, log_dir, fixed_now):
    rem = StubRemModule()
    q = StubQChannel(status="dryrun")
    result = prepare(
        "friday_prep",
        conn=db,
        rem_module=rem,
        qchannel_module=q,
        memory_dir=memory_dir,
        log_dir=log_dir,
        dispatch_dryrun=True,
        now=fixed_now,
    )
    payload = q.last_call["payload"]
    assert "snapshot_path" in payload
    assert str(result["snapshot_path"]) == payload["snapshot_path"]


def test_prepare_friday_prep_inserts_review_row(db, memory_dir, log_dir, fixed_now):
    rem = StubRemModule()
    q = StubQChannel(status="dryrun")
    prepare(
        "friday_prep",
        conn=db,
        rem_module=rem,
        qchannel_module=q,
        memory_dir=memory_dir,
        log_dir=log_dir,
        dispatch_dryrun=True,
        now=fixed_now,
    )
    rows = db.execute("SELECT * FROM reviews WHERE kind = 'friday_prep'").fetchall()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Tests: prepare('sunday_nudge') — Friday Q answered
# ---------------------------------------------------------------------------


def _insert_review_agenda_q(db, *, dispatched_at: str, status: str) -> str:
    """Insert a review_agenda question directly into the DB."""
    from gtd.engine.state import insert_question
    return insert_question(
        db,
        kind="review_agenda",
        dispatched_at=dispatched_at,
        status=status,
        payload_json={},
    )


def test_prepare_sunday_nudge_skips_when_friday_q_answered(
    db, memory_dir, log_dir, fixed_now
):
    """If friday Q status='answered' → no dispatch, reason='friday_acknowledged'."""
    sunday_now = fixed_now + timedelta(days=2)  # Sunday
    # Insert friday_prep Q from this week, status=answered
    week_start = "2026-04-13"  # Monday of the week containing Apr 17
    _insert_review_agenda_q(
        db,
        dispatched_at=f"{week_start}T09:00:00+00:00",
        status="answered",
    )
    rem = StubRemModule()
    q = StubQChannel(status="dryrun")
    result = prepare(
        "sunday_nudge",
        conn=db,
        rem_module=rem,
        qchannel_module=q,
        memory_dir=memory_dir,
        log_dir=log_dir,
        dispatch_dryrun=True,
        now=sunday_now,
    )
    assert result["q_dispatched"] is False
    assert result["q_skipped_reason"] == "friday_acknowledged"
    assert q.dispatch_count == 0


# ---------------------------------------------------------------------------
# Tests: prepare('sunday_nudge') — Friday Q open/cancelled → dispatch
# ---------------------------------------------------------------------------


def test_prepare_sunday_nudge_dispatches_when_friday_q_open(
    db, memory_dir, log_dir, fixed_now
):
    sunday_now = fixed_now + timedelta(days=2)
    week_start = "2026-04-13"
    _insert_review_agenda_q(
        db,
        dispatched_at=f"{week_start}T09:00:00+00:00",
        status="open",
    )
    rem = StubRemModule()
    q = StubQChannel(status="dryrun")
    result = prepare(
        "sunday_nudge",
        conn=db,
        rem_module=rem,
        qchannel_module=q,
        memory_dir=memory_dir,
        log_dir=log_dir,
        dispatch_dryrun=True,
        now=sunday_now,
    )
    assert result["q_dispatched"] is True
    assert result["q_skipped_reason"] is None
    assert q.dispatch_count == 1


def test_prepare_sunday_nudge_dispatches_when_friday_q_cancelled(
    db, memory_dir, log_dir, fixed_now
):
    sunday_now = fixed_now + timedelta(days=2)
    week_start = "2026-04-13"
    _insert_review_agenda_q(
        db,
        dispatched_at=f"{week_start}T09:00:00+00:00",
        status="cancelled",
    )
    rem = StubRemModule()
    q = StubQChannel(status="dryrun")
    result = prepare(
        "sunday_nudge",
        conn=db,
        rem_module=rem,
        qchannel_module=q,
        memory_dir=memory_dir,
        log_dir=log_dir,
        dispatch_dryrun=True,
        now=sunday_now,
    )
    assert result["q_dispatched"] is True
    assert result["q_skipped_reason"] is None
    assert q.dispatch_count == 1


# ---------------------------------------------------------------------------
# Tests: prepare('sunday_nudge') — no friday Q this week → dispatch
# ---------------------------------------------------------------------------


def test_prepare_sunday_nudge_dispatches_when_no_friday_q_this_week(
    db, memory_dir, log_dir, fixed_now
):
    """No review_agenda Q from this week at all → dispatch as not-acknowledged."""
    sunday_now = fixed_now + timedelta(days=2)
    # Insert a Q from last week (should NOT be found)
    _insert_review_agenda_q(
        db,
        dispatched_at="2026-04-06T09:00:00+00:00",  # last week
        status="answered",
    )
    rem = StubRemModule()
    q = StubQChannel(status="dryrun")
    result = prepare(
        "sunday_nudge",
        conn=db,
        rem_module=rem,
        qchannel_module=q,
        memory_dir=memory_dir,
        log_dir=log_dir,
        dispatch_dryrun=True,
        now=sunday_now,
    )
    assert result["q_dispatched"] is True
    assert result["q_skipped_reason"] is None
    assert q.dispatch_count == 1


# ---------------------------------------------------------------------------
# Tests: snapshot file path uses YYYY-MM-DD of the `now` parameter
# ---------------------------------------------------------------------------


def test_prepare_snapshot_path_uses_now_date(db, memory_dir, log_dir):
    custom_now = datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
    rem = StubRemModule()
    q = StubQChannel(status="dryrun")
    result = prepare(
        "friday_prep",
        conn=db,
        rem_module=rem,
        qchannel_module=q,
        memory_dir=memory_dir,
        log_dir=log_dir,
        dispatch_dryrun=True,
        now=custom_now,
    )
    assert result["snapshot_path"].name == "2026-03-15-friday_prep.md"


def test_prepare_sunday_nudge_snapshot_path_uses_now_date(db, memory_dir, log_dir):
    custom_now = datetime(2026, 3, 17, 10, 0, 0, tzinfo=timezone.utc)  # Sunday
    rem = StubRemModule()
    q = StubQChannel(status="dryrun")
    result = prepare(
        "sunday_nudge",
        conn=db,
        rem_module=rem,
        qchannel_module=q,
        memory_dir=memory_dir,
        log_dir=log_dir,
        dispatch_dryrun=True,
        now=custom_now,
    )
    assert result["snapshot_path"].name == "2026-03-17-sunday_nudge.md"
