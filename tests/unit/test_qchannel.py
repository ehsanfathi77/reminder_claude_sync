"""
Unit tests for gtd/engine/qchannel.py — US-007: Q-channel

All tests use an in-memory sqlite DB and a stub rem_module.
No real Reminders.app is touched.

Test checklist (load-bearing):
  - dispatch dryrun mode → status='dryrun', no create called, jsonl written, state='dryrun'
  - dispatch live → status='dispatched', create called once, state='open'
  - per-command cap: same invocation_id twice → second returns 'cap_per_command'
  - digest=True bypasses per-command cap with same invocation_id
  - q_max_open=3 already → next non-scheduled returns 'cap_open'
  - q_max_open=3, kind='review_agenda' (scheduled) → dispatches anyway
  - q_max_per_day=8 already → next returns 'cap_per_day' (incl. scheduled)
  - quiet hours: dispatch at 23:00 → status='queued_quiet', state='deferred'
  - circuit_breaker: 11 inbox events in last 60s → clarify returns 'circuit_breaker'
  - poll: completed Q with 'Reply: @home' → answered dict with reply_text='@home', state='answered'
  - poll backoff: Q dispatched 73h ago → ttl extended, status='open'
  - poll cancel: Q dispatched 169h ago → status='cancelled', set_complete called
  - archive: marks state='archived', delete called on reminder
  - write_fence: create called with list_name='Questions'
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import gtd.engine.state as state_mod
from gtd.engine.state import (
    init_db,
    insert_question,
    open_questions,
    update_question_status,
)
import gtd.engine.qchannel as qchannel
from gtd.engine.qchannel import (
    DispatchResult,
    archive,
    circuit_breaker_active,
    dispatch,
    open_count,
    per_day_count,
    poll,
)

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
        self._update_notes_calls: list[tuple] = []
        self._set_complete_calls: list[tuple] = []
        self._delete_calls: list[tuple] = []
        self._next_rid = 0

    def create(self, list_name: str, name: str, notes: str = "", due_iso: str = "") -> str:
        self._create_calls.append({"list_name": list_name, "name": name, "notes": notes})
        rid = f"FAKE-RID-{self._next_rid}"
        self._next_rid += 1
        self._reminders.append(
            FakeReminder(id=rid, list=list_name, name=name, body=notes)
        )
        return rid

    def list_all(self, days_done_window: int = 7) -> list[FakeReminder]:
        return list(self._reminders)

    def update_notes(self, rid: str, list_name: str, notes: str) -> None:
        self._update_notes_calls.append((rid, list_name, notes))
        for rem in self._reminders:
            if rem.id == rid:
                rem.body = notes
                break

    def update_field(self, rid: str, field_name: str, value: str) -> None:
        pass

    def set_complete(self, rid: str, list_name: str, completed: bool) -> None:
        self._set_complete_calls.append((rid, list_name, completed))
        for rem in self._reminders:
            if rem.id == rid:
                rem.completed = completed
                break

    def delete(self, rid: str, list_name: str) -> None:
        self._delete_calls.append((rid, list_name))
        self._reminders = [r for r in self._reminders if r.id != rid]


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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dt(hour: int) -> datetime:
    """Return a datetime at the given *local* clock hour today (tz-aware).

    Quiet-hours semantics are local-time (matching the user-facing config); the
    helper builds a local-tz datetime so `_dt(23)` reads as 23:00 user-time.
    """
    local_now = datetime.now().astimezone()
    return local_now.replace(hour=hour, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Helper: fill open Q slots
# ---------------------------------------------------------------------------


def _fill_open_qs(conn, count: int = 3, kind: str = "clarify") -> list[str]:
    """Insert `count` open questions directly into DB."""
    qids = []
    for i in range(count):
        qid = insert_question(conn, kind=kind, dispatched_at=_now().isoformat(),
                              ttl_at=None, status="open", payload_json={})
        qids.append(qid)
    return qids


def _fill_per_day(conn, count: int = 8) -> None:
    """Insert `count` questions with today's date so per_day_count hits cap."""
    today = _now().strftime("%Y-%m-%d")
    for i in range(count):
        insert_question(conn, kind="clarify",
                        dispatched_at=f"{today}T12:0{i % 10}:00+00:00",
                        status="open", payload_json={})


# ---------------------------------------------------------------------------
# Tests: dryrun mode
# ---------------------------------------------------------------------------


def test_dispatch_dryrun_no_create_called(db, rem, log_dir):
    result = dispatch(
        conn=db,
        rem_module=rem,
        kind="clarify",
        prompt="Clarify: some thing",
        dispatch_dryrun=True,
        log_dir=log_dir,
    )
    assert result.status == "dryrun"
    assert result.qid is not None
    assert len(rem._create_calls) == 0


def test_dispatch_dryrun_state_is_dryrun(db, rem, log_dir):
    result = dispatch(
        conn=db,
        rem_module=rem,
        kind="clarify",
        prompt="Clarify: some thing",
        dispatch_dryrun=True,
        log_dir=log_dir,
    )
    row = db.execute(
        "SELECT status FROM questions WHERE qid = ?", (result.qid,)
    ).fetchone()
    assert row is not None
    assert dict(row)["status"] == "dryrun"


def test_dispatch_dryrun_jsonl_written(db, rem, log_dir):
    dispatch(
        conn=db,
        rem_module=rem,
        kind="clarify",
        prompt="Clarify: some thing",
        dispatch_dryrun=True,
        log_dir=log_dir,
    )
    jsonl_path = log_dir / "qchannel.jsonl"
    assert jsonl_path.exists()
    lines = [json.loads(l) for l in jsonl_path.read_text().splitlines() if l.strip()]
    assert len(lines) >= 1
    entry = lines[-1]
    assert entry["status"] == "dryrun"
    assert entry["dryrun"] is True


# ---------------------------------------------------------------------------
# Tests: live dispatch
# ---------------------------------------------------------------------------


def test_dispatch_live_status_dispatched(db, rem, log_dir):
    result = dispatch(
        conn=db,
        rem_module=rem,
        kind="clarify",
        prompt="Clarify: live item",
        dispatch_dryrun=False,
        log_dir=log_dir,
    )
    assert result.status == "dispatched"
    assert result.qid is not None


def test_dispatch_live_create_called_once(db, rem, log_dir):
    dispatch(
        conn=db,
        rem_module=rem,
        kind="clarify",
        prompt="Clarify: live item",
        dispatch_dryrun=False,
        log_dir=log_dir,
    )
    assert len(rem._create_calls) == 1


def test_dispatch_live_create_called_with_questions_list(db, rem, log_dir):
    """write_fence enforcement: create must use list_name='Questions'."""
    dispatch(
        conn=db,
        rem_module=rem,
        kind="clarify",
        prompt="Clarify: live item",
        dispatch_dryrun=False,
        log_dir=log_dir,
    )
    assert rem._create_calls[0]["list_name"] == "Questions"


def test_dispatch_live_state_is_open(db, rem, log_dir):
    result = dispatch(
        conn=db,
        rem_module=rem,
        kind="clarify",
        prompt="Clarify: live item",
        dispatch_dryrun=False,
        log_dir=log_dir,
    )
    row = db.execute(
        "SELECT status FROM questions WHERE qid = ?", (result.qid,)
    ).fetchone()
    assert dict(row)["status"] == "open"


# ---------------------------------------------------------------------------
# Tests: per-command cap
# ---------------------------------------------------------------------------


def test_per_command_cap_same_invocation_id(db, rem, log_dir):
    inv_id = "cmd-abc-001"
    r1 = dispatch(
        conn=db, rem_module=rem, kind="clarify", prompt="First Q",
        invocation_id=inv_id, dispatch_dryrun=True, log_dir=log_dir,
    )
    r2 = dispatch(
        conn=db, rem_module=rem, kind="invariant", prompt="Second Q",
        invocation_id=inv_id, dispatch_dryrun=True, log_dir=log_dir,
    )
    assert r1.status in ("dispatched", "dryrun")
    assert r2.status == "cap_per_command"


def test_digest_bypasses_per_command_cap(db, rem, log_dir):
    inv_id = "cmd-digest-001"
    r1 = dispatch(
        conn=db, rem_module=rem, kind="clarify", prompt="First Q",
        invocation_id=inv_id, dispatch_dryrun=True, log_dir=log_dir,
    )
    r2 = dispatch(
        conn=db, rem_module=rem, kind="digest", prompt="Digest Q",
        invocation_id=inv_id, digest=True, dispatch_dryrun=True, log_dir=log_dir,
    )
    assert r1.status in ("dispatched", "dryrun")
    # digest=True bypasses per-command cap
    assert r2.status != "cap_per_command"


# ---------------------------------------------------------------------------
# Tests: open cap
# ---------------------------------------------------------------------------


def test_cap_open_at_3_blocks_non_scheduled(db, rem, log_dir):
    _fill_open_qs(db, count=3)
    result = dispatch(
        conn=db, rem_module=rem, kind="clarify", prompt="One more Q",
        dispatch_dryrun=True, log_dir=log_dir,
    )
    assert result.status == "cap_open"


def test_cap_open_at_3_scheduled_kind_dispatches(db, rem, log_dir):
    """Scheduled-nudge kinds bypass q_max_open."""
    _fill_open_qs(db, count=3)
    result = dispatch(
        conn=db, rem_module=rem, kind="review_agenda", prompt="Weekly review?",
        dispatch_dryrun=True, log_dir=log_dir,
    )
    assert result.status in ("dispatched", "dryrun")


# ---------------------------------------------------------------------------
# Tests: per-day cap
# ---------------------------------------------------------------------------


def test_cap_per_day_at_8_blocks_all(db, rem, log_dir):
    _fill_per_day(db, count=8)
    result = dispatch(
        conn=db, rem_module=rem, kind="clarify", prompt="One more today",
        dispatch_dryrun=True, log_dir=log_dir,
    )
    assert result.status == "cap_per_day"


def test_cap_per_day_at_8_blocks_scheduled_too(db, rem, log_dir):
    """Per-day cap applies to all kinds including scheduled nudges."""
    _fill_per_day(db, count=8)
    result = dispatch(
        conn=db, rem_module=rem, kind="review_agenda", prompt="Review?",
        dispatch_dryrun=True, log_dir=log_dir,
    )
    assert result.status == "cap_per_day"


# ---------------------------------------------------------------------------
# Tests: quiet hours
# ---------------------------------------------------------------------------


def test_quiet_hours_dispatch_returns_queued_quiet(db, rem, log_dir):
    night_now = _dt(23)  # 23:00 UTC → inside 22:00–08:00 quiet window
    result = dispatch(
        conn=db, rem_module=rem, kind="clarify", prompt="Night Q",
        dispatch_dryrun=True, quiet_hours=(22, 8), now=night_now, log_dir=log_dir,
    )
    assert result.status == "queued_quiet"


def test_quiet_hours_state_is_deferred(db, rem, log_dir):
    night_now = _dt(23)
    result = dispatch(
        conn=db, rem_module=rem, kind="clarify", prompt="Night Q",
        dispatch_dryrun=True, quiet_hours=(22, 8), now=night_now, log_dir=log_dir,
    )
    row = db.execute(
        "SELECT status FROM questions WHERE qid = ?", (result.qid,)
    ).fetchone()
    assert dict(row)["status"] == "deferred"


def test_quiet_hours_no_create_called(db, rem, log_dir):
    night_now = _dt(23)
    dispatch(
        conn=db, rem_module=rem, kind="clarify", prompt="Night Q",
        dispatch_dryrun=False, quiet_hours=(22, 8), now=night_now, log_dir=log_dir,
    )
    assert len(rem._create_calls) == 0


# ---------------------------------------------------------------------------
# Tests: circuit breaker
# ---------------------------------------------------------------------------


def _insert_inbox_events(conn, count: int, within_secs: int = 30) -> None:
    """Insert `count` inbox_arrival events within the last `within_secs` seconds."""
    now = _now()
    for i in range(count):
        ts = (now - timedelta(seconds=within_secs - i)).isoformat()
        state_mod.insert_event(conn, ts=ts, stream="inbox_arrival", payload={"i": i})


def test_circuit_breaker_active_with_11_events(db):
    _insert_inbox_events(db, count=11)
    assert circuit_breaker_active(conn=db, now=_now()) is True


def test_circuit_breaker_inactive_with_10_events(db):
    _insert_inbox_events(db, count=10)
    assert circuit_breaker_active(conn=db, now=_now()) is False


def test_circuit_breaker_blocks_clarify_dispatch(db, rem, log_dir):
    _insert_inbox_events(db, count=11)
    result = dispatch(
        conn=db, rem_module=rem, kind="clarify", prompt="Some clarify",
        dispatch_dryrun=True, log_dir=log_dir,
    )
    assert result.status == "circuit_breaker"


def test_circuit_breaker_does_not_block_scheduled(db, rem, log_dir):
    """Scheduled-nudge kinds bypass the circuit breaker."""
    _insert_inbox_events(db, count=11)
    result = dispatch(
        conn=db, rem_module=rem, kind="review_agenda", prompt="Review?",
        dispatch_dryrun=True, log_dir=log_dir,
    )
    # Should not be circuit_breaker (scheduled kinds bypass it)
    assert result.status != "circuit_breaker"


# ---------------------------------------------------------------------------
# Tests: poll — answered Q
# ---------------------------------------------------------------------------


def _make_qmeta_notes(qid: str, qkind: str) -> str:
    return f"<!-- qmeta -->\nqid: {qid}\nqkind: {qkind}\nref_rid: null\npayload: {{}}\n<!-- /qmeta -->\nReply: @home"


def test_poll_answered_q_with_reply_text(db, rem, log_dir):
    qid = insert_question(db, kind="clarify", status="open",
                          dispatched_at=_now().isoformat(), payload_json={})
    notes = _make_qmeta_notes(qid, "clarify")
    fake_rem = FakeReminder(
        id="REM-ANS-1",
        list="Questions",
        name="Clarify?",
        completed=True,
        completion_date=_now().isoformat(),
        body=notes,
    )
    rem._reminders.append(fake_rem)

    answered = poll(conn=db, rem_module=rem, now=_now(), log_dir=log_dir)

    assert len(answered) == 1
    assert answered[0]["qid"] == qid
    assert answered[0]["reply_text"] == "@home"


def test_poll_answered_state_updated_to_answered(db, rem, log_dir):
    qid = insert_question(db, kind="clarify", status="open",
                          dispatched_at=_now().isoformat(), payload_json={})
    notes = _make_qmeta_notes(qid, "clarify")
    fake_rem = FakeReminder(
        id="REM-ANS-2",
        list="Questions",
        name="Clarify?",
        completed=True,
        completion_date=_now().isoformat(),
        body=notes,
    )
    rem._reminders.append(fake_rem)

    poll(conn=db, rem_module=rem, now=_now(), log_dir=log_dir)

    row = db.execute("SELECT status FROM questions WHERE qid = ?", (qid,)).fetchone()
    assert dict(row)["status"] == "answered"


# ---------------------------------------------------------------------------
# Tests: poll — backoff (extend TTL at 73h)
# ---------------------------------------------------------------------------


def test_poll_backoff_extends_ttl_at_73h(db, rem, log_dir):
    dispatched_at = _now() - timedelta(hours=73)
    ttl_at = _now() - timedelta(hours=1)  # already past first TTL (72h)
    qid = insert_question(
        db,
        kind="clarify",
        status="open",
        dispatched_at=dispatched_at.isoformat(),
        ttl_at=ttl_at.isoformat(),
        payload_json={},
    )
    # No matching completed reminder.
    poll(conn=db, rem_module=rem, now=_now(), log_dir=log_dir)

    row = db.execute("SELECT ttl_at, status FROM questions WHERE qid = ?", (qid,)).fetchone()
    d = dict(row)
    assert d["status"] == "open"
    # new TTL should be extended (168h from dispatch)
    expected_new_ttl = dispatched_at + timedelta(hours=168)
    new_ttl_dt = datetime.fromisoformat(d["ttl_at"]).replace(tzinfo=timezone.utc)
    # Allow 60s slack for test timing.
    assert abs((new_ttl_dt - expected_new_ttl).total_seconds()) < 60


# ---------------------------------------------------------------------------
# Tests: poll — cancel at 169h
# ---------------------------------------------------------------------------


def _make_qmeta_notes_no_reply(qid: str, qkind: str) -> str:
    return f"<!-- qmeta -->\nqid: {qid}\nqkind: {qkind}\nref_rid: null\npayload: {{}}\n<!-- /qmeta -->"


def test_poll_cancel_at_169h(db, rem, log_dir):
    dispatched_at = _now() - timedelta(hours=169)
    # TTL set past 168h threshold (second miss)
    ttl_at = _now() - timedelta(hours=1)
    qid = insert_question(
        db,
        kind="clarify",
        status="open",
        dispatched_at=dispatched_at.isoformat(),
        ttl_at=ttl_at.isoformat(),
        payload_json={},
    )
    notes = _make_qmeta_notes_no_reply(qid, "clarify")
    fake_rem = FakeReminder(
        id="REM-OLD-1",
        list="Questions",
        name="Old Q",
        completed=False,
        body=notes,
    )
    rem._reminders.append(fake_rem)

    poll(conn=db, rem_module=rem, now=_now(), log_dir=log_dir)

    row = db.execute("SELECT status FROM questions WHERE qid = ?", (qid,)).fetchone()
    assert dict(row)["status"] == "cancelled"
    # set_complete should be called on the reminder.
    assert len(rem._set_complete_calls) >= 1
    rids = [c[0] for c in rem._set_complete_calls]
    assert "REM-OLD-1" in rids


# ---------------------------------------------------------------------------
# Tests: archive
# ---------------------------------------------------------------------------


def test_archive_marks_state_archived(db, rem, log_dir):
    qid = insert_question(db, kind="clarify", status="open",
                          dispatched_at=_now().isoformat(), payload_json={})
    notes = _make_qmeta_notes_no_reply(qid, "clarify")
    fake_rem = FakeReminder(
        id="REM-ARC-1",
        list="Questions",
        name="Arc Q",
        body=notes,
    )
    rem._reminders.append(fake_rem)

    archive(conn=db, qid=qid, rem_module=rem)

    row = db.execute("SELECT status FROM questions WHERE qid = ?", (qid,)).fetchone()
    assert dict(row)["status"] == "archived"


def test_archive_deletes_from_questions_list(db, rem, log_dir):
    qid = insert_question(db, kind="clarify", status="open",
                          dispatched_at=_now().isoformat(), payload_json={})
    notes = _make_qmeta_notes_no_reply(qid, "clarify")
    fake_rem = FakeReminder(
        id="REM-ARC-2",
        list="Questions",
        name="Arc Q",
        body=notes,
    )
    rem._reminders.append(fake_rem)

    archive(conn=db, qid=qid, rem_module=rem)

    assert len(rem._delete_calls) == 1
    assert rem._delete_calls[0][0] == "REM-ARC-2"
    assert rem._delete_calls[0][1] == "Questions"


# ---------------------------------------------------------------------------
# Tests: cap helpers
# ---------------------------------------------------------------------------


def test_open_count_excludes_scheduled_nudges_by_default(db):
    insert_question(db, kind="clarify", status="open",
                    dispatched_at=_now().isoformat(), payload_json={})
    insert_question(db, kind="review_agenda", status="open",
                    dispatched_at=_now().isoformat(), payload_json={})
    assert open_count(conn=db) == 1  # clarify only


def test_open_count_includes_scheduled_when_not_excluded(db):
    insert_question(db, kind="clarify", status="open",
                    dispatched_at=_now().isoformat(), payload_json={})
    insert_question(db, kind="review_agenda", status="open",
                    dispatched_at=_now().isoformat(), payload_json={})
    assert open_count(conn=db, exclude_scheduled_nudges=False) == 2


def test_per_day_count_counts_today_only(db):
    today = _now().strftime("%Y-%m-%d")
    insert_question(db, kind="clarify", status="open",
                    dispatched_at=f"{today}T10:00:00+00:00", payload_json={})
    insert_question(db, kind="clarify", status="open",
                    dispatched_at="2020-01-01T10:00:00+00:00", payload_json={})
    assert per_day_count(conn=db, day_iso=today) == 1


# ---------------------------------------------------------------------------
# Tests: write_fence enforcement
# ---------------------------------------------------------------------------


def test_dispatch_live_write_fence_questions_list(db, rem, log_dir):
    """Verify that create() is called exclusively with list_name='Questions'."""
    dispatch(
        conn=db, rem_module=rem, kind="clarify", prompt="Write fence check",
        dispatch_dryrun=False, log_dir=log_dir,
    )
    for call in rem._create_calls:
        assert call["list_name"] == "Questions", (
            f"Expected list_name='Questions', got {call['list_name']!r}"
        )
