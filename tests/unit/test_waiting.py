"""
Unit tests for gtd/engine/waiting.py — US-013: Waiting-For nudges

All tests use an in-memory sqlite DB and a stub rem_module.
No real Reminders.app is touched.

Test checklist (load-bearing):
  - list_waiting: parses delegate from fenced metadata, computes age_days correctly
  - nudge() default: 8 items > 7d old → 1 digest Q, payload contains all 8 refs+delegates
  - nudge() default with 0 stale → 0 Qs dispatched, returns {'stale_count': 0, 'qs_dispatched': 0}
  - nudge(per_item=True) with 12 stale + remaining q_max_per_day=8 → exactly 8 Qs dispatched, cap_hit logged
  - nudge(per_item=True) with 12 stale + remaining=0 → 0 Qs dispatched, cap_hit=True
  - age_threshold_days=14: items 8-13 days old NOT counted as stale
  - Items younger than threshold: not in payload
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

import gtd.engine.state as state_mod
from gtd.engine.state import init_db, insert_question
import gtd.engine.qchannel as qchannel
from gtd.engine.waiting import WaitingItem, list_waiting, nudge

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
    last_modified: str = ""


class StubRemModule:
    """Minimal stub for bin/lib/reminders that records calls and returns fakes."""

    def __init__(self, reminders: list[FakeReminder] | None = None):
        self._reminders: list[FakeReminder] = reminders or []
        self._create_calls: list[dict] = []

    def create(self, list_name: str, name: str, notes: str = "", due_iso: str = "") -> str:
        self._create_calls.append({"list_name": list_name, "name": name, "notes": notes})
        rid = f"FAKE-RID-{len(self._create_calls)}"
        self._reminders.append(FakeReminder(id=rid, list=list_name, name=name, body=notes))
        return rid

    def list_all(self, days_done_window: int = 7) -> list[FakeReminder]:
        return list(self._reminders)

    def update_notes(self, rid: str, list_name: str, notes: str) -> None:
        for rem in self._reminders:
            if rem.id == rid:
                rem.body = notes
                break

    def set_complete(self, rid: str, list_name: str, completed: bool) -> None:
        for rem in self._reminders:
            if rem.id == rid:
                rem.completed = completed
                break

    def delete(self, rid: str, list_name: str) -> None:
        self._reminders = [r for r in self._reminders if r.id != rid]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WAITING_LIST = "Waiting For"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_waiting_notes(delegate: str | None, created_iso: str) -> str:
    """Build a fenced GTD notes block for a Waiting For item."""
    lines = ["--- gtd ---", "kind: waiting", f"created: {created_iso}"]
    if delegate:
        lines.append(f"delegate: {delegate}")
    lines.append("--- end ---")
    return "\n".join(lines) + "\n"


def _make_waiting_reminder(
    rid: str,
    title: str,
    delegate: str | None,
    age_days: int,
    now: datetime | None = None,
) -> FakeReminder:
    """Create a FakeReminder in the Waiting For list with proper fence metadata."""
    if now is None:
        now = _now()
    created_dt = now - timedelta(days=age_days)
    created_iso = created_dt.isoformat(timespec="seconds")
    notes = _make_waiting_notes(delegate, created_iso)
    return FakeReminder(
        id=rid,
        list=_WAITING_LIST,
        name=title,
        body=notes,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_invocation_registry():
    """Clear the module-level invocation registry before each test."""
    qchannel._invocation_registry.clear()
    yield
    qchannel._invocation_registry.clear()


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "state.db"
    conn = init_db(db_path)
    yield conn
    conn.close()


@pytest.fixture
def rem():
    return StubRemModule()


@pytest.fixture
def log_dir(tmp_path):
    d = tmp_path / "log"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Tests: list_waiting
# ---------------------------------------------------------------------------


def test_list_waiting_parses_delegate(rem):
    now = _now()
    rem._reminders.append(_make_waiting_reminder("R1", "Call Dan back", "Dan", 10, now))
    items = list_waiting(rem_module=rem, now=now)
    assert len(items) == 1
    assert items[0].delegate == "Dan"
    assert items[0].title == "Call Dan back"
    assert items[0].rid == "R1"


def test_list_waiting_no_delegate(rem):
    now = _now()
    rem._reminders.append(_make_waiting_reminder("R2", "Budget approval", None, 5, now))
    items = list_waiting(rem_module=rem, now=now)
    assert len(items) == 1
    assert items[0].delegate is None


def test_list_waiting_age_days_correct(rem):
    now = _now()
    rem._reminders.append(_make_waiting_reminder("R3", "PR review", "Alice", 15, now))
    items = list_waiting(rem_module=rem, now=now)
    assert items[0].age_days == 15


def test_list_waiting_skips_other_lists(rem):
    now = _now()
    rem._reminders.append(_make_waiting_reminder("R4", "Waiting item", "Bob", 10, now))
    # Add item in a different list
    rem._reminders.append(FakeReminder(id="R5", list="Inbox", name="Not waiting", body=""))
    items = list_waiting(rem_module=rem, now=now)
    assert len(items) == 1
    assert items[0].rid == "R4"


def test_list_waiting_skips_completed(rem):
    now = _now()
    completed_rem = _make_waiting_reminder("R6", "Done waiting", "Carol", 10, now)
    completed_rem.completed = True
    rem._reminders.append(completed_rem)
    items = list_waiting(rem_module=rem, now=now)
    assert len(items) == 0


def test_list_waiting_empty_list(rem):
    items = list_waiting(rem_module=rem)
    assert items == []


# ---------------------------------------------------------------------------
# Tests: nudge() default (digest) mode
# ---------------------------------------------------------------------------


def test_nudge_digest_8_stale_dispatches_1_q(db, rem, log_dir):
    """8 items > 7d old → exactly 1 digest Q dispatched."""
    now = _now()
    for i in range(8):
        rem._reminders.append(
            _make_waiting_reminder(f"R{i}", f"Item {i}", f"Person{i}", 10, now)
        )
    result = nudge(
        conn=db, rem_module=rem, qchannel_module=qchannel,
        dispatch_dryrun=True, log_dir=log_dir, now=now,
    )
    assert result["stale_count"] == 8
    assert result["qs_dispatched"] == 1
    assert result["cap_hit"] is False


def test_nudge_digest_payload_contains_all_8_refs(db, rem, log_dir):
    """Digest payload refs must include all 8 stale items with delegates."""
    now = _now()
    delegates = [f"Person{i}" for i in range(8)]
    for i in range(8):
        rem._reminders.append(
            _make_waiting_reminder(f"R{i}", f"Item {i}", delegates[i], 10, now)
        )

    # Capture the payload by inspecting the state DB after nudge
    nudge(
        conn=db, rem_module=rem, qchannel_module=qchannel,
        dispatch_dryrun=True, log_dir=log_dir, now=now,
    )

    # Retrieve the question from DB and check payload
    rows = db.execute("SELECT payload_json FROM questions").fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"] if hasattr(rows[0], "keys") else rows[0][0])
    refs = payload.get("refs", [])
    assert len(refs) == 8
    # All delegates present
    found_delegates = {ref["delegate"] for ref in refs}
    assert found_delegates == set(delegates)


def test_nudge_digest_0_stale_returns_zero(db, rem, log_dir):
    """0 stale items → 0 Qs dispatched."""
    now = _now()
    # Add items that are fresh (within threshold)
    for i in range(3):
        rem._reminders.append(
            _make_waiting_reminder(f"R{i}", f"Fresh {i}", "Dan", 3, now)
        )
    result = nudge(
        conn=db, rem_module=rem, qchannel_module=qchannel,
        dispatch_dryrun=True, log_dir=log_dir, now=now,
    )
    assert result == {"stale_count": 0, "qs_dispatched": 0, "cap_hit": False}


def test_nudge_digest_fresh_items_not_in_payload(db, rem, log_dir):
    """Items under threshold are not included in any Q."""
    now = _now()
    # 2 stale, 3 fresh
    rem._reminders.append(_make_waiting_reminder("R_stale1", "Old 1", "Dan", 8, now))
    rem._reminders.append(_make_waiting_reminder("R_stale2", "Old 2", "Alice", 9, now))
    for i in range(3):
        rem._reminders.append(_make_waiting_reminder(f"R_fresh{i}", f"Fresh {i}", "Bob", 3, now))

    nudge(
        conn=db, rem_module=rem, qchannel_module=qchannel,
        dispatch_dryrun=True, log_dir=log_dir, now=now,
    )

    rows = db.execute("SELECT payload_json FROM questions").fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"] if hasattr(rows[0], "keys") else rows[0][0])
    refs = payload.get("refs", [])
    ref_rids = {ref["rid"] for ref in refs}
    assert "R_stale1" in ref_rids
    assert "R_stale2" in ref_rids
    # Fresh items must not be in payload
    for i in range(3):
        assert f"R_fresh{i}" not in ref_rids


# ---------------------------------------------------------------------------
# Tests: nudge() age_threshold_days
# ---------------------------------------------------------------------------


def test_nudge_threshold_14_excludes_8_to_13_day_items(db, rem, log_dir):
    """With threshold=14, items 8-13 days old are NOT stale."""
    now = _now()
    # 6 items aged 8-13 days — should NOT be stale with threshold=14
    for age in range(8, 14):
        rem._reminders.append(
            _make_waiting_reminder(f"R_age{age}", f"Item {age}d", "Dan", age, now)
        )
    # 2 items aged 15+ days — should be stale
    rem._reminders.append(_make_waiting_reminder("R_15", "Old 15", "Eve", 15, now))
    rem._reminders.append(_make_waiting_reminder("R_20", "Old 20", "Frank", 20, now))

    result = nudge(
        conn=db, rem_module=rem, qchannel_module=qchannel,
        age_threshold_days=14,
        dispatch_dryrun=True, log_dir=log_dir, now=now,
    )
    assert result["stale_count"] == 2
    assert result["qs_dispatched"] == 1


# ---------------------------------------------------------------------------
# Tests: nudge(per_item=True)
# ---------------------------------------------------------------------------


def _fill_per_day(conn, count: int, now: datetime) -> None:
    """Insert `count` questions with today's date so per_day_count hits cap."""
    today = now.strftime("%Y-%m-%d")
    for i in range(count):
        insert_question(
            conn, kind="clarify",
            dispatched_at=f"{today}T12:{i:02d}:00+00:00",
            status="open", payload_json={},
        )


def test_nudge_per_item_12_stale_remaining_8_dispatches_8(db, rem, log_dir):
    """12 stale + remaining=8 → exactly 8 Qs dispatched, cap_hit=True (4 skipped)."""
    now = _now()
    for i in range(12):
        rem._reminders.append(
            _make_waiting_reminder(f"R{i}", f"Item {i}", f"Person{i}", 10, now)
        )
    # No prior dispatches → remaining = 8

    result = nudge(
        conn=db, rem_module=rem, qchannel_module=qchannel,
        per_item=True,
        dispatch_dryrun=True, log_dir=log_dir, now=now,
    )
    assert result["stale_count"] == 12
    assert result["qs_dispatched"] == 8
    assert result["cap_hit"] is True


def test_nudge_per_item_12_stale_remaining_0_dispatches_0(db, rem, log_dir):
    """12 stale + remaining=0 → 0 Qs dispatched, cap_hit=True."""
    now = _now()
    for i in range(12):
        rem._reminders.append(
            _make_waiting_reminder(f"R{i}", f"Item {i}", f"Person{i}", 10, now)
        )
    # Fill the daily budget completely
    _fill_per_day(db, count=8, now=now)

    result = nudge(
        conn=db, rem_module=rem, qchannel_module=qchannel,
        per_item=True,
        dispatch_dryrun=True, log_dir=log_dir, now=now,
    )
    assert result["stale_count"] == 12
    assert result["qs_dispatched"] == 0
    assert result["cap_hit"] is True


def test_nudge_per_item_fewer_stale_than_remaining(db, rem, log_dir):
    """5 stale + remaining=8 → 5 Qs dispatched, cap_hit=False."""
    now = _now()
    for i in range(5):
        rem._reminders.append(
            _make_waiting_reminder(f"R{i}", f"Item {i}", f"Person{i}", 10, now)
        )
    result = nudge(
        conn=db, rem_module=rem, qchannel_module=qchannel,
        per_item=True,
        dispatch_dryrun=True, log_dir=log_dir, now=now,
    )
    assert result["stale_count"] == 5
    assert result["qs_dispatched"] == 5
    assert result["cap_hit"] is False
