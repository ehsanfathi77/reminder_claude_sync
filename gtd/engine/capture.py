"""
capture.py — capture flow.

Drops items into the 'Inbox' list, stamps engine metadata, records in state.db.
This is the ONLY GTD step that's strictly synchronous (capture must be
instantaneous so it can be used from chat without thinking).

Public API:

def capture(
    text: str,
    *,
    conn,                    # sqlite3 connection
    rem_module=R,            # injectable for tests
    log_dir: Path | None = None,
    now: datetime | None = None,
) -> str:
    '''Create one Inbox reminder. Returns gtd_id.'''

def capture_multiline(
    lines: list[str],
    *,
    conn,
    rem_module=R,
    log_dir: Path | None = None,
    now: datetime | None = None,
) -> list[str]:
    '''Create one reminder per non-empty line. Returns list of gtd_ids.'''
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import bin.lib.reminders as R
from gtd.engine.notes_metadata import serialize_metadata
from gtd.engine.observability import log
from gtd.engine.state import _ulid, insert_item
from gtd.engine.write_fence import assert_writable

_INBOX = "Inbox"


def capture(
    text: str,
    *,
    conn,
    rem_module=R,
    log_dir: Path | None = None,
    now: datetime | None = None,
) -> str:
    """Create one Inbox reminder. Returns gtd_id."""
    if now is None:
        now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")

    # Assert write scope on the list (rid placeholder for new item)
    assert_writable("<new>", _INBOX)

    # Generate ULID for this item
    gtd_id = _ulid()

    # Build fenced metadata notes body (empty prose at first)
    notes = serialize_metadata(
        {"id": gtd_id, "kind": "unclarified", "created": now_iso},
        "",
    )

    # Create the reminder in Reminders.app
    new_rid = rem_module.create(_INBOX, text, notes=notes)

    # Persist to state.db
    insert_item(
        conn,
        gtd_id=gtd_id,
        rid=new_rid,
        kind="unclarified",
        list=_INBOX,
        created=now_iso,
    )

    # Log to engine.jsonl
    log("engine", log_dir=log_dir, op="capture", count=1, gtd_ids=[gtd_id])

    return gtd_id


def capture_multiline(
    lines: list[str],
    *,
    conn,
    rem_module=R,
    log_dir: Path | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Create one reminder per non-empty line. Returns list of gtd_ids."""
    if now is None:
        now = datetime.now(timezone.utc)

    # Strip blank/whitespace-only lines
    filtered = [line for line in lines if line.strip()]

    gtd_ids: list[str] = []
    for line in filtered:
        gtd_id = capture(
            line.strip(),
            conn=conn,
            rem_module=rem_module,
            log_dir=log_dir,
            now=now,
        )
        gtd_ids.append(gtd_id)

    # Overwrite the per-item log entries with a single summary line
    log("engine", log_dir=log_dir, op="capture", count=len(gtd_ids), gtd_ids=gtd_ids)

    return gtd_ids
