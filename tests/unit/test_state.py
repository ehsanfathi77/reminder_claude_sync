"""
Unit tests for gtd/engine/state.py

Covers all cases required by US-004 spec:
  - init_db creates fresh DB with all 7 tables
  - WAL mode is enabled
  - foreign_keys is ON
  - init_db is idempotent (call twice → same DB)
  - insert_item + get_item_by_rid round-trip
  - insert_question + update_question_status + open_questions returns only status='open'
  - insert_project + projects_without_open_next_action
  - park_tickler + due_ticklers
  - insert_event + count_events_in_window
  - ULID format: 26 chars Crockford base32
  - Concurrent readers: 3 read-only + 1 writer, consistent snapshot
"""
from __future__ import annotations

import re
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import gtd.engine.state as state
from gtd.engine.state import (
    connect,
    count_events_in_window,
    due_ticklers,
    get_item_by_rid,
    init_db,
    insert_event,
    insert_item,
    insert_project,
    insert_question,
    insert_review,
    list_items_by_kind,
    open_questions,
    park_tickler,
    projects_without_open_next_action,
    update_question_status,
)

EXPECTED_TABLES = {
    "schema_version",
    "items",
    "questions",
    "projects",
    "ticklers",
    "reviews",
    "events",
}

CROCKFORD_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _past(hours: int = 1) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _future(hours: int = 1) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "state.db"


@pytest.fixture
def conn(db_path):
    c = init_db(db_path)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


def test_init_db_creates_all_tables(db_path):
    c = init_db(db_path)
    rows = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    tables = {r[0] for r in rows}
    assert EXPECTED_TABLES <= tables, f"Missing tables: {EXPECTED_TABLES - tables}"
    c.close()


def test_wal_mode_enabled(db_path):
    c = init_db(db_path)
    row = c.execute("PRAGMA journal_mode").fetchone()
    assert row[0] == "wal"
    c.close()


def test_foreign_keys_on(db_path):
    c = init_db(db_path)
    row = c.execute("PRAGMA foreign_keys").fetchone()
    assert row[0] == 1
    c.close()


def test_init_db_idempotent(db_path):
    c1 = init_db(db_path)
    c1.close()
    # Second call must not raise or corrupt the DB.
    c2 = init_db(db_path)
    rows = c2.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    tables = {r[0] for r in rows}
    assert EXPECTED_TABLES <= tables
    # schema_version should still have exactly 1 row (idempotent INSERT OR IGNORE)
    count = c2.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    assert count == 1
    c2.close()


# ---------------------------------------------------------------------------
# items
# ---------------------------------------------------------------------------


def test_insert_item_get_by_rid_roundtrip(conn):
    gtd_id = insert_item(conn, rid="REM-001", kind="next_action", ctx="@home")
    item = get_item_by_rid(conn, "REM-001")
    assert item is not None
    assert item["gtd_id"] == gtd_id
    assert item["rid"] == "REM-001"
    assert item["kind"] == "next_action"
    assert item["ctx"] == "@home"


def test_get_item_by_rid_missing_returns_none(conn):
    assert get_item_by_rid(conn, "DOES-NOT-EXIST") is None


def test_list_items_by_kind(conn):
    insert_item(conn, rid="REM-A", kind="next_action")
    insert_item(conn, rid="REM-B", kind="next_action")
    insert_item(conn, rid="REM-C", kind="someday")
    results = list_items_by_kind(conn, "next_action")
    assert len(results) == 2
    assert all(r["kind"] == "next_action" for r in results)


# ---------------------------------------------------------------------------
# questions
# ---------------------------------------------------------------------------


def test_insert_question_and_open_questions(conn):
    qid = insert_question(conn, kind="clarify", ref_rid="REM-001")
    qs = open_questions(conn)
    assert any(q["qid"] == qid for q in qs)


def test_update_question_status_filters_open(conn):
    qid_open = insert_question(conn, kind="clarify")
    qid_closed = insert_question(conn, kind="clarify")
    update_question_status(conn, qid_closed, "answered")

    qs = open_questions(conn)
    qids = {q["qid"] for q in qs}
    assert qid_open in qids
    assert qid_closed not in qids


# ---------------------------------------------------------------------------
# projects + projects_without_open_next_action
# ---------------------------------------------------------------------------


def test_projects_without_open_next_action_empty_child(conn):
    pid = state._ulid()
    insert_project(conn, pid, outcome="Launch v2")
    stale = projects_without_open_next_action(conn)
    assert any(p["project_id"] == pid for p in stale)


def test_projects_without_open_next_action_has_child(conn):
    pid = state._ulid()
    insert_project(conn, pid, outcome="Launch v3")
    # Insert a next_action item linked to this project with a @ctx
    insert_item(
        conn,
        rid="REM-NA-001",
        kind="next_action",
        project=pid,
        ctx="@computer",
    )
    stale = projects_without_open_next_action(conn)
    assert not any(p["project_id"] == pid for p in stale)


# ---------------------------------------------------------------------------
# ticklers
# ---------------------------------------------------------------------------


def test_due_ticklers_past_release_in_list(conn):
    gtd_id = insert_item(conn, rid="REM-T1", kind="tickler")
    park_tickler(conn, gtd_id, release_at=_past(2), target_list="Inbox")
    due = due_ticklers(conn, _now())
    assert any(t["gtd_id"] == gtd_id for t in due)


def test_due_ticklers_future_release_not_in_list(conn):
    gtd_id = insert_item(conn, rid="REM-T2", kind="tickler")
    park_tickler(conn, gtd_id, release_at=_future(2), target_list="Inbox")
    due = due_ticklers(conn, _now())
    assert not any(t["gtd_id"] == gtd_id for t in due)


# ---------------------------------------------------------------------------
# events + count_events_in_window
# ---------------------------------------------------------------------------


def test_insert_event_and_count_in_window(conn):
    stream = "sync"
    # 5 events: 3 in the window, 2 before it
    window_start = _now()
    import time as _time
    _time.sleep(0.01)  # ensure timestamps differ

    for i in range(3):
        insert_event(conn, ts=_now(), stream=stream, payload={"i": i})

    old_ts = _past(24)
    for i in range(2):
        insert_event(conn, ts=old_ts, stream=stream, payload={"old": i})

    count = count_events_in_window(conn, stream, window_start)
    assert count == 3


def test_count_events_different_stream(conn):
    ts = _now()
    insert_event(conn, ts=ts, stream="sync", payload={})
    insert_event(conn, ts=ts, stream="other", payload={})
    assert count_events_in_window(conn, "sync", _past(1)) == 1


# ---------------------------------------------------------------------------
# ULID format
# ---------------------------------------------------------------------------


def test_ulid_format():
    uid = state._ulid()
    assert len(uid) == 26, f"Expected 26 chars, got {len(uid)}: {uid}"
    assert CROCKFORD_RE.match(uid), f"Invalid Crockford base32: {uid}"


def test_insert_item_gtd_id_is_ulid(conn):
    gtd_id = insert_item(conn, rid="REM-ULID", kind="next_action")
    assert CROCKFORD_RE.match(gtd_id), f"gtd_id not Crockford base32: {gtd_id}"


def test_insert_question_qid_is_ulid(conn):
    qid = insert_question(conn, kind="clarify")
    assert CROCKFORD_RE.match(qid), f"qid not Crockford base32: {qid}"


def test_insert_review_review_id_is_ulid(conn):
    rid = insert_review(conn, kind="weekly", snapshot={"items": 5})
    assert CROCKFORD_RE.match(rid), f"review_id not Crockford base32: {rid}"


# ---------------------------------------------------------------------------
# Concurrent readers
# ---------------------------------------------------------------------------


def test_concurrent_readers_consistent_snapshot(db_path):
    """3 read-only connections + 1 writer; all reads see consistent snapshot."""
    writer = init_db(db_path)
    insert_item(writer, rid="REM-CONC-1", kind="next_action")
    insert_item(writer, rid="REM-CONC-2", kind="next_action")

    results: list[int] = []
    errors: list[Exception] = []

    def reader_thread():
        try:
            c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            c.row_factory = sqlite3.Row
            rows = c.execute("SELECT COUNT(*) AS cnt FROM items").fetchone()
            results.append(rows["cnt"])
            c.close()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=reader_thread) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Reader errors: {errors}"
    # All 3 readers should see 2 items (consistent snapshot)
    assert all(r == 2 for r in results), f"Inconsistent reads: {results}"

    writer.close()
