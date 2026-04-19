"""
Unit tests for gtd/engine/tickler.py  (US-012)

Covers:
- park: state row created with right release_at + target_list, R.move_to_list called with 'Tickler'
- release with no due: returns {'released': 0, 'past_due_q': False}
- release with 1 due (release_at = now-1s): R.move_to_list called with target_list, state row deleted
- release with 5 past-due (release_at < now-24h): 1 digest Q dispatched, refs included in payload,
  original reminders NOT moved (user decides)
- write_fence: park to Tickler is allowed; releasing back to Inbox is allowed
- Ordering: park 3 ticklers with release_at = past, present, future; release() handles the 2 due ones,
  ignores future
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import gtd.engine.write_fence as write_fence_mod
from gtd.engine.state import get_item_by_rid, init_db, insert_item
from gtd.engine.tickler import park, release
from gtd.engine.write_fence import WriteScopeError


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

class StubReminders:
    """Stub for bin.lib.reminders. Tracks move_to_list and create calls."""

    def __init__(self):
        self.move_calls: list[dict] = []  # {'rid': ..., 'list': ...}
        self.create_calls: list[dict] = []

    def move_to_list(self, rid: str, list_name: str) -> None:
        self.move_calls.append({"rid": rid, "list": list_name})

    def create(self, list_name: str, name: str, notes: str = "", due_iso: str = "") -> str:
        rid = str(uuid.uuid4())
        self.create_calls.append({"list": list_name, "name": name, "notes": notes})
        return rid


class StubQChannel:
    """Stub for gtd.engine.qchannel dispatch."""

    def __init__(self, status: str = "dryrun"):
        self.dispatch_calls: list[dict] = []
        self._status = status

    def dispatch(self, *, conn, rem_module=None, kind, prompt, payload=None,
                 digest=False, dispatch_dryrun=True, log_dir=None, now=None,
                 ref_rid=None, invocation_id=None, quiet_hours=None,
                 gtd_id=None, **kwargs):
        self.dispatch_calls.append({
            "kind": kind,
            "prompt": prompt,
            "payload": payload,
            "digest": digest,
        })
        # Return a simple object mimicking DispatchResult
        return _FakeResult(self._status)


class _FakeResult:
    def __init__(self, status: str):
        self.status = status
        self.qid = "fake-qid"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_conn(tmp_path):
    db_path = tmp_path / "state.db"
    conn = init_db(db_path)
    yield conn
    conn.close()


@pytest.fixture
def stub_rem():
    return StubReminders()


@pytest.fixture
def stub_q():
    return StubQChannel(status="dryrun")


@pytest.fixture
def log_dir(tmp_path):
    d = tmp_path / "log"
    d.mkdir()
    return d


@pytest.fixture
def fixed_now():
    return datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_item(conn, list_name: str = "Inbox") -> tuple[str, str]:
    """Insert a fake item into state.db. Returns (gtd_id, rid)."""
    rid = str(uuid.uuid4())
    gtd_id = insert_item(conn, rid=rid, kind="unclarified", list=list_name)
    return gtd_id, rid


# ---------------------------------------------------------------------------
# park()
# ---------------------------------------------------------------------------

def test_park_moves_reminder_to_tickler_list(db_conn, stub_rem, log_dir, fixed_now):
    gtd_id, rid = _seed_item(db_conn, "Inbox")
    park(rid, "Inbox", "2026-04-20T10:00:00",
         conn=db_conn, rem_module=stub_rem, log_dir=log_dir, now=fixed_now)
    assert len(stub_rem.move_calls) == 1
    assert stub_rem.move_calls[0]["rid"] == rid
    assert stub_rem.move_calls[0]["list"] == "Tickler"


def test_park_creates_state_tickler_row_with_correct_release_at(db_conn, stub_rem, log_dir, fixed_now):
    gtd_id, rid = _seed_item(db_conn, "Inbox")
    release_at = "2026-04-20T10:00:00"
    park(rid, "Inbox", release_at,
         conn=db_conn, rem_module=stub_rem, log_dir=log_dir, now=fixed_now)
    row = db_conn.execute("SELECT * FROM ticklers WHERE gtd_id = ?", (gtd_id,)).fetchone()
    assert row is not None
    assert dict(row)["release_at"] == release_at


def test_park_state_row_has_correct_target_list(db_conn, stub_rem, log_dir, fixed_now):
    gtd_id, rid = _seed_item(db_conn, "Inbox")
    park(rid, "Inbox", "2026-04-20T10:00:00", target_list="@errands",
         conn=db_conn, rem_module=stub_rem, log_dir=log_dir, now=fixed_now)
    row = db_conn.execute("SELECT * FROM ticklers WHERE gtd_id = ?", (gtd_id,)).fetchone()
    assert dict(row)["target_list"] == "@errands"


def test_park_default_target_list_is_inbox(db_conn, stub_rem, log_dir, fixed_now):
    gtd_id, rid = _seed_item(db_conn, "Inbox")
    park(rid, "Inbox", "2026-04-20T10:00:00",
         conn=db_conn, rem_module=stub_rem, log_dir=log_dir, now=fixed_now)
    row = db_conn.execute("SELECT * FROM ticklers WHERE gtd_id = ?", (gtd_id,)).fetchone()
    assert dict(row)["target_list"] == "Inbox"


def test_park_updates_items_list_to_tickler(db_conn, stub_rem, log_dir, fixed_now):
    gtd_id, rid = _seed_item(db_conn, "Inbox")
    park(rid, "Inbox", "2026-04-20T10:00:00",
         conn=db_conn, rem_module=stub_rem, log_dir=log_dir, now=fixed_now)
    item = get_item_by_rid(db_conn, rid)
    assert item["list"] == "Tickler"


# ---------------------------------------------------------------------------
# release() — no due ticklers
# ---------------------------------------------------------------------------

def test_release_no_due_returns_zero(db_conn, stub_rem, stub_q, log_dir, fixed_now):
    # Park a future tickler — should not be released
    gtd_id, rid = _seed_item(db_conn, "Inbox")
    future = fixed_now + timedelta(hours=2)
    park(rid, "Inbox", future.isoformat(),
         conn=db_conn, rem_module=stub_rem, log_dir=log_dir, now=fixed_now)
    stub_rem.move_calls.clear()  # clear park move

    result = release(conn=db_conn, rem_module=stub_rem, log_dir=log_dir,
                     qchannel_module=stub_q, dispatch_dryrun=True, now=fixed_now)
    assert result == {"released": 0, "past_due_q": False}
    assert stub_rem.move_calls == []


# ---------------------------------------------------------------------------
# release() — 1 due tickler (release_at = now-1s)
# ---------------------------------------------------------------------------

def test_release_one_due_moves_to_target_list(db_conn, stub_rem, stub_q, log_dir, fixed_now):
    gtd_id, rid = _seed_item(db_conn, "Inbox")
    release_at = (fixed_now - timedelta(seconds=1)).isoformat()
    park(rid, "Inbox", release_at,
         conn=db_conn, rem_module=stub_rem, log_dir=log_dir, now=fixed_now)
    stub_rem.move_calls.clear()

    result = release(conn=db_conn, rem_module=stub_rem, log_dir=log_dir,
                     qchannel_module=stub_q, dispatch_dryrun=True, now=fixed_now)
    assert result["released"] == 1
    assert result["past_due_q"] is False
    assert len(stub_rem.move_calls) == 1
    assert stub_rem.move_calls[0]["rid"] == rid
    assert stub_rem.move_calls[0]["list"] == "Inbox"


def test_release_one_due_deletes_tickler_state_row(db_conn, stub_rem, stub_q, log_dir, fixed_now):
    gtd_id, rid = _seed_item(db_conn, "Inbox")
    release_at = (fixed_now - timedelta(seconds=1)).isoformat()
    park(rid, "Inbox", release_at,
         conn=db_conn, rem_module=stub_rem, log_dir=log_dir, now=fixed_now)

    release(conn=db_conn, rem_module=stub_rem, log_dir=log_dir,
            qchannel_module=stub_q, dispatch_dryrun=True, now=fixed_now)

    row = db_conn.execute("SELECT * FROM ticklers WHERE gtd_id = ?", (gtd_id,)).fetchone()
    assert row is None


def test_release_one_due_updates_items_list(db_conn, stub_rem, stub_q, log_dir, fixed_now):
    gtd_id, rid = _seed_item(db_conn, "Inbox")
    release_at = (fixed_now - timedelta(seconds=1)).isoformat()
    park(rid, "Inbox", release_at, target_list="@calls",
         conn=db_conn, rem_module=stub_rem, log_dir=log_dir, now=fixed_now)

    release(conn=db_conn, rem_module=stub_rem, log_dir=log_dir,
            qchannel_module=stub_q, dispatch_dryrun=True, now=fixed_now)

    item = get_item_by_rid(db_conn, rid)
    assert item["list"] == "@calls"


# ---------------------------------------------------------------------------
# release() — 5 past-due (release_at < now - 24h)
# ---------------------------------------------------------------------------

def test_release_past_due_emits_single_digest_q(db_conn, stub_rem, stub_q, log_dir, fixed_now):
    rids = []
    for _ in range(5):
        gtd_id, rid = _seed_item(db_conn, "Inbox")
        past = (fixed_now - timedelta(hours=25)).isoformat()
        park(rid, "Inbox", past,
             conn=db_conn, rem_module=stub_rem, log_dir=log_dir, now=fixed_now)
        rids.append(rid)
    stub_rem.move_calls.clear()

    result = release(conn=db_conn, rem_module=stub_rem, log_dir=log_dir,
                     qchannel_module=stub_q, dispatch_dryrun=True, now=fixed_now)

    assert result["past_due_q"] is True
    assert len(stub_q.dispatch_calls) == 1
    call = stub_q.dispatch_calls[0]
    assert call["kind"] == "digest"
    assert call["digest"] is True


def test_release_past_due_payload_contains_all_refs(db_conn, stub_rem, stub_q, log_dir, fixed_now):
    rids = []
    for _ in range(5):
        gtd_id, rid = _seed_item(db_conn, "Inbox")
        past = (fixed_now - timedelta(hours=25)).isoformat()
        park(rid, "Inbox", past,
             conn=db_conn, rem_module=stub_rem, log_dir=log_dir, now=fixed_now)
        rids.append(rid)
    stub_rem.move_calls.clear()

    release(conn=db_conn, rem_module=stub_rem, log_dir=log_dir,
            qchannel_module=stub_q, dispatch_dryrun=True, now=fixed_now)

    payload = stub_q.dispatch_calls[0]["payload"]
    assert "ticklers" in payload
    for rid in rids:
        assert rid in payload["ticklers"]


def test_release_past_due_does_not_move_reminders(db_conn, stub_rem, stub_q, log_dir, fixed_now):
    """Past-due items are NOT moved — user decides."""
    for _ in range(5):
        gtd_id, rid = _seed_item(db_conn, "Inbox")
        past = (fixed_now - timedelta(hours=25)).isoformat()
        park(rid, "Inbox", past,
             conn=db_conn, rem_module=stub_rem, log_dir=log_dir, now=fixed_now)
    stub_rem.move_calls.clear()

    release(conn=db_conn, rem_module=stub_rem, log_dir=log_dir,
            qchannel_module=stub_q, dispatch_dryrun=True, now=fixed_now)

    assert stub_rem.move_calls == []


def test_release_past_due_released_count_is_zero(db_conn, stub_rem, stub_q, log_dir, fixed_now):
    for _ in range(5):
        gtd_id, rid = _seed_item(db_conn, "Inbox")
        past = (fixed_now - timedelta(hours=25)).isoformat()
        park(rid, "Inbox", past,
             conn=db_conn, rem_module=stub_rem, log_dir=log_dir, now=fixed_now)
    stub_rem.move_calls.clear()

    result = release(conn=db_conn, rem_module=stub_rem, log_dir=log_dir,
                     qchannel_module=stub_q, dispatch_dryrun=True, now=fixed_now)

    assert result["released"] == 0


# ---------------------------------------------------------------------------
# write_fence
# ---------------------------------------------------------------------------

def test_park_to_tickler_is_allowed(db_conn, stub_rem, log_dir, fixed_now):
    """Tickler is in DEFAULT_MANAGED_LISTS — no WriteScopeError."""
    gtd_id, rid = _seed_item(db_conn, "Inbox")
    # Should not raise
    park(rid, "Inbox", "2026-04-20T10:00:00",
         conn=db_conn, rem_module=stub_rem, log_dir=log_dir, now=fixed_now)


def test_park_raises_when_tickler_excluded(db_conn, stub_rem, log_dir, fixed_now, monkeypatch):
    """If Tickler is removed from managed lists, park must raise WriteScopeError."""
    gtd_id, rid = _seed_item(db_conn, "Inbox")
    monkeypatch.setattr(
        write_fence_mod,
        "DEFAULT_MANAGED_LISTS",
        frozenset(write_fence_mod.DEFAULT_MANAGED_LISTS - {"Tickler"}),
    )
    with pytest.raises(WriteScopeError) as exc_info:
        park(rid, "Inbox", "2026-04-20T10:00:00",
             conn=db_conn, rem_module=stub_rem, log_dir=log_dir, now=fixed_now)
    assert exc_info.value.attempted_list == "Tickler"


def test_release_back_to_inbox_is_allowed(db_conn, stub_rem, stub_q, log_dir, fixed_now):
    """Releasing to Inbox (default) should not raise WriteScopeError."""
    gtd_id, rid = _seed_item(db_conn, "Inbox")
    release_at = (fixed_now - timedelta(seconds=1)).isoformat()
    park(rid, "Inbox", release_at,
         conn=db_conn, rem_module=stub_rem, log_dir=log_dir, now=fixed_now)
    # Should not raise
    result = release(conn=db_conn, rem_module=stub_rem, log_dir=log_dir,
                     qchannel_module=stub_q, dispatch_dryrun=True, now=fixed_now)
    assert result["released"] == 1


# ---------------------------------------------------------------------------
# Ordering: past, present (due-1s), future — release handles 2, ignores future
# ---------------------------------------------------------------------------

def test_ordering_release_handles_due_ignores_future(db_conn, stub_rem, stub_q, log_dir, fixed_now):
    """Park 3 ticklers: past-due, due (now-1s), future. release() should:
    - past-due: emit digest Q, no move
    - due (now-1s): move to target, delete row
    - future: leave untouched
    """
    # Past-due (> 24h ago)
    _, rid_past = _seed_item(db_conn, "Inbox")
    park(rid_past, "Inbox", (fixed_now - timedelta(hours=25)).isoformat(),
         conn=db_conn, rem_module=stub_rem, log_dir=log_dir, now=fixed_now)

    # Due (1 second ago — normal release)
    _, rid_due = _seed_item(db_conn, "Inbox")
    park(rid_due, "Inbox", (fixed_now - timedelta(seconds=1)).isoformat(),
         conn=db_conn, rem_module=stub_rem, log_dir=log_dir, now=fixed_now)

    # Future (not yet due)
    _, rid_future = _seed_item(db_conn, "Inbox")
    park(rid_future, "Inbox", (fixed_now + timedelta(hours=2)).isoformat(),
         conn=db_conn, rem_module=stub_rem, log_dir=log_dir, now=fixed_now)

    stub_rem.move_calls.clear()

    result = release(conn=db_conn, rem_module=stub_rem, log_dir=log_dir,
                     qchannel_module=stub_q, dispatch_dryrun=True, now=fixed_now)

    # 1 normal release (rid_due), 1 past-due (no move), 1 future (untouched)
    assert result["released"] == 1
    assert result["past_due_q"] is True

    # Only rid_due should have been moved
    moved_rids = [c["rid"] for c in stub_rem.move_calls]
    assert rid_due in moved_rids
    assert rid_past not in moved_rids
    assert rid_future not in moved_rids

    # Future tickler row still exists
    future_item = db_conn.execute(
        "SELECT t.* FROM ticklers t JOIN items i ON t.gtd_id = i.gtd_id WHERE i.rid = ?",
        (rid_future,),
    ).fetchone()
    assert future_item is not None

    # Due tickler row deleted
    due_item_row = db_conn.execute(
        "SELECT t.* FROM ticklers t JOIN items i ON t.gtd_id = i.gtd_id WHERE i.rid = ?",
        (rid_due,),
    ).fetchone()
    assert due_item_row is None

    # digest Q dispatched (for past-due)
    assert len(stub_q.dispatch_calls) == 1
    assert stub_q.dispatch_calls[0]["kind"] == "digest"
