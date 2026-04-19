"""
state.py — SQLite-backed engine state.

Single file at .gtd/state.db. WAL mode for concurrent reads from sync.py and
supernote-sync. The engine is the only writer.

Schema:
  schema_version (version, applied_at)
  items          (gtd_id PK, rid UNIQUE, kind, list, project, ctx, created, last_seen)
  questions      (qid PK, kind, ref_rid, dispatched_at, ttl_at, status, payload_json)
  projects       (project_id PK, outcome, created, last_review)
  ticklers       (gtd_id PK FK→items, release_at, target_list, created)
  reviews        (review_id PK, kind, started_at, completed_at, snapshot_json)
  events         (event_id PK AUTOINC, ts, stream, payload_json)
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import string
import time
from datetime import datetime, timezone
from pathlib import Path

CURRENT_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# ULID — 26-char Crockford base32 of 48-bit timestamp + 80 random bits.
# No external dependencies.
# ---------------------------------------------------------------------------

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _ulid() -> str:
    """Generate a ULID: 26-char Crockford base32 string."""
    # 48-bit millisecond timestamp
    ts_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF
    # 80 random bits
    rand = random.getrandbits(80)

    # Pack into 128 bits: top 48 = timestamp, bottom 80 = random
    value = (ts_ms << 80) | rand

    # Encode as 26-char Crockford base32 (big-endian, 5 bits per char)
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_DDL_V1 = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    gtd_id      TEXT PRIMARY KEY,
    rid         TEXT UNIQUE NOT NULL,
    kind        TEXT NOT NULL,
    list        TEXT,
    project     TEXT,
    ctx         TEXT,
    created     TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS questions (
    qid             TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    ref_rid         TEXT,
    dispatched_at   TEXT NOT NULL,
    ttl_at          TEXT,
    status          TEXT NOT NULL DEFAULT 'open',
    payload_json    TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS projects (
    project_id  TEXT PRIMARY KEY,
    outcome     TEXT NOT NULL,
    created     TEXT NOT NULL,
    last_review TEXT
);

CREATE TABLE IF NOT EXISTS ticklers (
    gtd_id      TEXT PRIMARY KEY REFERENCES items(gtd_id),
    release_at  TEXT NOT NULL,
    target_list TEXT NOT NULL,
    created     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    review_id       TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    completed_at    TEXT,
    snapshot_json   TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS events (
    event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    stream          TEXT NOT NULL,
    payload_json    TEXT NOT NULL DEFAULT '{}'
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _setup_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row


def _get_schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            "SELECT MAX(version) AS v FROM schema_version"
        ).fetchone()
        return row["v"] if row["v"] is not None else 0
    except sqlite3.OperationalError:
        return 0


def _migrate_to_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL_V1)
    conn.execute(
        "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (1, ?)",
        (_now_iso(),),
    )
    conn.commit()


def _run_migrations(conn: sqlite3.Connection) -> None:
    current = _get_schema_version(conn)
    migrations = {
        1: _migrate_to_v1,
    }
    for version in sorted(migrations):
        if current < version:
            migrations[version](conn)
            current = version


# ---------------------------------------------------------------------------
# Public API: open / connect
# ---------------------------------------------------------------------------


def init_db(path: Path) -> sqlite3.Connection:
    """Open or create. Idempotent. Sets WAL mode. Runs migrations from current
    schema_version. Returns a connection (caller must close)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    _setup_pragmas(conn)
    _run_migrations(conn)
    return conn


def connect(path: Path) -> sqlite3.Connection:
    """Open an existing DB read-write. Asserts schema is initialized."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"DB not found: {path}")
    conn = sqlite3.connect(str(path))
    _setup_pragmas(conn)
    version = _get_schema_version(conn)
    if version < CURRENT_SCHEMA_VERSION:
        raise RuntimeError(
            f"DB schema version {version} < current {CURRENT_SCHEMA_VERSION}; run init_db first."
        )
    return conn


# ---------------------------------------------------------------------------
# Convenience accessors — thin wrappers, one per table
# ---------------------------------------------------------------------------


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return dict(row)


def insert_item(conn: sqlite3.Connection, **fields) -> str:
    """Insert a row into items. Returns gtd_id."""
    gtd_id = fields.get("gtd_id") or _ulid()
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO items (gtd_id, rid, kind, list, project, ctx, created, last_seen)
        VALUES (:gtd_id, :rid, :kind, :list, :project, :ctx, :created, :last_seen)
        """,
        {
            "gtd_id": gtd_id,
            "rid": fields["rid"],
            "kind": fields["kind"],
            "list": fields.get("list"),
            "project": fields.get("project"),
            "ctx": fields.get("ctx"),
            "created": fields.get("created", now),
            "last_seen": fields.get("last_seen", now),
        },
    )
    conn.commit()
    return gtd_id


def get_item_by_rid(conn: sqlite3.Connection, rid: str) -> dict | None:
    row = conn.execute("SELECT * FROM items WHERE rid = ?", (rid,)).fetchone()
    return _row_to_dict(row)


def list_items_by_kind(conn: sqlite3.Connection, kind: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM items WHERE kind = ?", (kind,)).fetchall()
    return [dict(r) for r in rows]


def insert_question(conn: sqlite3.Connection, **fields) -> str:
    """Insert a row into questions. Returns qid."""
    qid = fields.get("qid") or _ulid()
    now = _now_iso()
    payload = fields.get("payload_json", {})
    if isinstance(payload, dict):
        payload = json.dumps(payload)
    conn.execute(
        """
        INSERT INTO questions (qid, kind, ref_rid, dispatched_at, ttl_at, status, payload_json)
        VALUES (:qid, :kind, :ref_rid, :dispatched_at, :ttl_at, :status, :payload_json)
        """,
        {
            "qid": qid,
            "kind": fields["kind"],
            "ref_rid": fields.get("ref_rid"),
            "dispatched_at": fields.get("dispatched_at", now),
            "ttl_at": fields.get("ttl_at"),
            "status": fields.get("status", "open"),
            "payload_json": payload,
        },
    )
    conn.commit()
    return qid


def update_question_status(conn: sqlite3.Connection, qid: str, status: str) -> None:
    conn.execute("UPDATE questions SET status = ? WHERE qid = ?", (status, qid))
    conn.commit()


def open_questions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM questions WHERE status = 'open'"
    ).fetchall()
    return [dict(r) for r in rows]


def insert_project(conn: sqlite3.Connection, project_id: str, outcome: str) -> None:
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO projects (project_id, outcome, created, last_review)
        VALUES (?, ?, ?, NULL)
        """,
        (project_id, outcome, now),
    )
    conn.commit()


def projects_without_open_next_action(conn: sqlite3.Connection) -> list[dict]:
    """For invariant check: returns projects with zero open child items in any @ctx list."""
    rows = conn.execute(
        """
        SELECT p.*
        FROM projects p
        WHERE NOT EXISTS (
            SELECT 1 FROM items i
            WHERE i.project = p.project_id
              AND i.kind = 'next_action'
              AND i.ctx IS NOT NULL
        )
        """
    ).fetchall()
    return [dict(r) for r in rows]


def park_tickler(
    conn: sqlite3.Connection, gtd_id: str, release_at: str, target_list: str
) -> None:
    now = _now_iso()
    conn.execute(
        """
        INSERT OR REPLACE INTO ticklers (gtd_id, release_at, target_list, created)
        VALUES (?, ?, ?, ?)
        """,
        (gtd_id, release_at, target_list, now),
    )
    conn.commit()


def due_ticklers(conn: sqlite3.Connection, now_iso: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM ticklers WHERE release_at <= ?", (now_iso,)
    ).fetchall()
    return [dict(r) for r in rows]


def insert_review(conn: sqlite3.Connection, kind: str, snapshot: dict) -> str:
    review_id = _ulid()
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO reviews (review_id, kind, started_at, completed_at, snapshot_json)
        VALUES (?, ?, ?, NULL, ?)
        """,
        (review_id, kind, now, json.dumps(snapshot)),
    )
    conn.commit()
    return review_id


def insert_event(conn: sqlite3.Connection, ts: str, stream: str, payload: dict) -> int:
    cur = conn.execute(
        """
        INSERT INTO events (ts, stream, payload_json)
        VALUES (?, ?, ?)
        """,
        (ts, stream, json.dumps(payload)),
    )
    conn.commit()
    return cur.lastrowid


def count_events_in_window(
    conn: sqlite3.Connection, stream: str, since_iso: str
) -> int:
    """Used for circuit-breaker logic in qchannel."""
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM events WHERE stream = ? AND ts >= ?",
        (stream, since_iso),
    ).fetchone()
    return row["cnt"] if row else 0
