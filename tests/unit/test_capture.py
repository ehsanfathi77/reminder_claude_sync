"""
Unit tests for gtd/engine/capture.py  (US-008)

Covers:
- capture("test") returns a 26-char ULID
- rem_module.create called with list="Inbox", title="test", notes containing fenced block with kind="unclarified"
- After capture: state.db has the row (via state.get_item_by_rid)
- engine.jsonl has one line with op="capture", count=1
- capture_multiline(["a", "", "b", "  ", "c"]) creates exactly 3 reminders (empties stripped),
  returns 3 gtd_ids, engine.jsonl summary line has count=3
- The notes field includes the fence — parse back with notes_metadata.parse_metadata and verify kind="unclarified"
- write_fence is honored: monkeypatch DEFAULT_MANAGED_LISTS to exclude "Inbox" → raises WriteScopeError
"""
from __future__ import annotations

import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import gtd.engine.write_fence as write_fence_mod
from gtd.engine.capture import capture, capture_multiline
from gtd.engine.notes_metadata import parse_metadata
from gtd.engine.state import get_item_by_rid, init_db
from gtd.engine.write_fence import WriteScopeError

CROCKFORD_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


# ---------------------------------------------------------------------------
# Stub rem_module
# ---------------------------------------------------------------------------

class StubReminders:
    """Minimal stub for bin.lib.reminders.  create() records calls and returns a UUID."""

    def __init__(self):
        self.calls: list[dict] = []

    def create(self, list_name: str, name: str, notes: str = "", due_iso: str = "") -> str:
        rid = str(uuid.uuid4())
        self.calls.append({"list": list_name, "name": name, "notes": notes, "due_iso": due_iso})
        return rid


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
def log_dir(tmp_path):
    d = tmp_path / "log"
    d.mkdir()
    return d


@pytest.fixture
def fixed_now():
    return datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# capture() — basic
# ---------------------------------------------------------------------------

def test_capture_returns_26_char_ulid(db_conn, stub_rem, log_dir, fixed_now):
    gtd_id = capture("test", conn=db_conn, rem_module=stub_rem, log_dir=log_dir, now=fixed_now)
    assert len(gtd_id) == 26
    assert CROCKFORD_RE.match(gtd_id), f"Not a valid ULID: {gtd_id}"


def test_capture_calls_rem_create_with_inbox_and_title(db_conn, stub_rem, log_dir, fixed_now):
    capture("buy groceries", conn=db_conn, rem_module=stub_rem, log_dir=log_dir, now=fixed_now)
    assert len(stub_rem.calls) == 1
    call = stub_rem.calls[0]
    assert call["list"] == "Inbox"
    assert call["name"] == "buy groceries"


def test_capture_notes_contains_fenced_metadata(db_conn, stub_rem, log_dir, fixed_now):
    capture("test item", conn=db_conn, rem_module=stub_rem, log_dir=log_dir, now=fixed_now)
    notes = stub_rem.calls[0]["notes"]
    assert "--- gtd ---" in notes
    assert "--- end ---" in notes
    assert "kind: unclarified" in notes


def test_capture_notes_parse_back_kind_unclarified(db_conn, stub_rem, log_dir, fixed_now):
    capture("parse test", conn=db_conn, rem_module=stub_rem, log_dir=log_dir, now=fixed_now)
    notes = stub_rem.calls[0]["notes"]
    meta, prose = parse_metadata(notes)
    assert meta.get("kind") == "unclarified"


def test_capture_notes_id_matches_returned_gtd_id(db_conn, stub_rem, log_dir, fixed_now):
    gtd_id = capture("id check", conn=db_conn, rem_module=stub_rem, log_dir=log_dir, now=fixed_now)
    notes = stub_rem.calls[0]["notes"]
    meta, _ = parse_metadata(notes)
    assert meta.get("id") == gtd_id


# ---------------------------------------------------------------------------
# capture() — state.db persistence
# ---------------------------------------------------------------------------

# Tracking stub that records return values
class TrackingStubReminders:
    """Stub that records the rid (return value) of each create() call."""

    def __init__(self):
        self.calls: list[dict] = []
        self.rids: list[str] = []

    def create(self, list_name: str, name: str, notes: str = "", due_iso: str = "") -> str:
        rid = str(uuid.uuid4())
        self.calls.append({"list": list_name, "name": name, "notes": notes, "due_iso": due_iso})
        self.rids.append(rid)
        return rid


@pytest.fixture
def tracking_rem():
    return TrackingStubReminders()


def test_capture_state_db_row_queryable_by_rid(db_conn, tracking_rem, log_dir, fixed_now):
    capture("db persistence", conn=db_conn, rem_module=tracking_rem, log_dir=log_dir, now=fixed_now)
    rid = tracking_rem.rids[0]
    item = get_item_by_rid(db_conn, rid)
    assert item is not None
    assert item["kind"] == "unclarified"
    assert item["list"] == "Inbox"


def test_capture_state_db_gtd_id_matches_return(db_conn, tracking_rem, log_dir, fixed_now):
    gtd_id = capture("gtd_id match", conn=db_conn, rem_module=tracking_rem, log_dir=log_dir, now=fixed_now)
    rid = tracking_rem.rids[0]
    item = get_item_by_rid(db_conn, rid)
    assert item["gtd_id"] == gtd_id


# ---------------------------------------------------------------------------
# capture() — engine.jsonl logging
# ---------------------------------------------------------------------------

def test_capture_logs_engine_jsonl(db_conn, tracking_rem, log_dir, fixed_now):
    gtd_id = capture("log test", conn=db_conn, rem_module=tracking_rem, log_dir=log_dir, now=fixed_now)
    log_file = log_dir / "engine.jsonl"
    assert log_file.exists()
    lines = [json.loads(l) for l in log_file.read_text().splitlines() if l.strip()]
    assert len(lines) >= 1
    last = lines[-1]
    assert last["op"] == "capture"
    assert last["count"] == 1
    assert gtd_id in last["gtd_ids"]


# ---------------------------------------------------------------------------
# capture_multiline() — empty line filtering
# ---------------------------------------------------------------------------

def test_capture_multiline_strips_empties(db_conn, tracking_rem, log_dir, fixed_now):
    gtd_ids = capture_multiline(
        ["a", "", "b", "  ", "c"],
        conn=db_conn,
        rem_module=tracking_rem,
        log_dir=log_dir,
        now=fixed_now,
    )
    assert len(gtd_ids) == 3
    assert len(tracking_rem.rids) == 3


def test_capture_multiline_returns_ulids(db_conn, tracking_rem, log_dir, fixed_now):
    gtd_ids = capture_multiline(
        ["x", "y", "z"],
        conn=db_conn,
        rem_module=tracking_rem,
        log_dir=log_dir,
        now=fixed_now,
    )
    assert all(CROCKFORD_RE.match(gid) for gid in gtd_ids), f"Non-ULID in {gtd_ids}"


def test_capture_multiline_all_empty_returns_empty(db_conn, tracking_rem, log_dir, fixed_now):
    gtd_ids = capture_multiline(
        ["", "   ", "\t"],
        conn=db_conn,
        rem_module=tracking_rem,
        log_dir=log_dir,
        now=fixed_now,
    )
    assert gtd_ids == []
    assert tracking_rem.rids == []


def test_capture_multiline_summary_log_count_3(db_conn, tracking_rem, log_dir, fixed_now):
    gtd_ids = capture_multiline(
        ["a", "", "b", "  ", "c"],
        conn=db_conn,
        rem_module=tracking_rem,
        log_dir=log_dir,
        now=fixed_now,
    )
    log_file = log_dir / "engine.jsonl"
    lines = [json.loads(l) for l in log_file.read_text().splitlines() if l.strip()]
    # The last line is the summary
    summary = lines[-1]
    assert summary["op"] == "capture"
    assert summary["count"] == 3
    assert set(gtd_ids) == set(summary["gtd_ids"])


# ---------------------------------------------------------------------------
# write_fence — WriteScopeError when Inbox excluded from managed lists
# ---------------------------------------------------------------------------

def test_capture_raises_write_scope_error_when_inbox_excluded(
    db_conn, tracking_rem, log_dir, fixed_now, monkeypatch
):
    """Monkeypatch DEFAULT_MANAGED_LISTS to not include 'Inbox'; capture must raise."""
    original = write_fence_mod.DEFAULT_MANAGED_LISTS
    monkeypatch.setattr(
        write_fence_mod,
        "DEFAULT_MANAGED_LISTS",
        frozenset(original - {"Inbox"}),
    )
    with pytest.raises(WriteScopeError) as exc_info:
        capture("should fail", conn=db_conn, rem_module=tracking_rem, log_dir=log_dir, now=fixed_now)
    assert exc_info.value.attempted_list == "Inbox"
    # No reminder should have been created
    assert tracking_rem.rids == []
