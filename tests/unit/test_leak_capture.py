"""
Unit tests for gtd/engine/leak_capture.py — Siri-default-list safety-net.

Covers:
- Untracked item in `Reminders` → moved to Inbox + state.db row created (kind="unclarified").
- Already-tracked item in `Reminders` → skipped (no duplicate move, no duplicate row).
- Empty `Reminders` list → no-op, drained=0.
- R.move_to_list raises → error counted, other items still processed.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from gtd.engine.leak_capture import drain_leak_list
from gtd.engine.state import get_item_by_rid, init_db, insert_item


@dataclass
class FakeReminder:
    id: str
    list: str
    name: str
    completed: bool = False
    body: str = ""


class StubRem:
    """Minimal stub for bin.lib.reminders."""

    def __init__(self, reminders=None, *, raise_on_move=None):
        self._rems = list(reminders or [])
        self.move_calls: list[tuple[str, str]] = []
        self._raise_on_move = raise_on_move or set()

    def list_all(self):
        return list(self._rems)

    def move_to_list(self, rid: str, list_name: str) -> None:
        self.move_calls.append((rid, list_name))
        if rid in self._raise_on_move:
            raise RuntimeError(f"simulated move failure for {rid}")
        for r in self._rems:
            if r.id == rid:
                r.list = list_name


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "state.db"
    c = init_db(db)
    yield c
    c.close()


@pytest.fixture
def log_dir(tmp_path):
    d = tmp_path / "log"
    d.mkdir()
    return d


def test_untracked_item_drained_to_inbox(conn, log_dir):
    rem = StubRem([FakeReminder(id="RID-1", list="Reminders", name="Buy milk")])
    result = drain_leak_list(conn, "Reminders", log_dir=log_dir, rem_module=rem)

    assert result == {"drained": 1, "errors": 0, "skipped": 0}
    assert rem.move_calls == [("RID-1", "Inbox")]
    item = get_item_by_rid(conn, "RID-1")
    assert item is not None
    assert item["kind"] == "unclarified"
    assert item["list"] == "Inbox"


def test_already_tracked_item_skipped(conn, log_dir):
    insert_item(conn, rid="RID-EXIST", kind="next_action", list="@home")
    rem = StubRem([FakeReminder(id="RID-EXIST", list="Reminders", name="Already known")])
    result = drain_leak_list(conn, "Reminders", log_dir=log_dir, rem_module=rem)

    assert result == {"drained": 0, "errors": 0, "skipped": 1}
    assert rem.move_calls == []
    # state.db row unchanged
    item = get_item_by_rid(conn, "RID-EXIST")
    assert item["kind"] == "next_action"
    assert item["list"] == "@home"


def test_empty_leak_list_is_noop(conn, log_dir):
    rem = StubRem([
        FakeReminder(id="OTHER-1", list="@home", name="Already in @home"),
    ])
    result = drain_leak_list(conn, "Reminders", log_dir=log_dir, rem_module=rem)

    assert result == {"drained": 0, "errors": 0, "skipped": 0}
    assert rem.move_calls == []


def test_move_failure_counted_and_other_items_still_processed(conn, log_dir):
    rem = StubRem(
        [
            FakeReminder(id="OK-1", list="Reminders", name="First"),
            FakeReminder(id="FAIL-2", list="Reminders", name="Bad"),
            FakeReminder(id="OK-3", list="Reminders", name="Third"),
        ],
        raise_on_move={"FAIL-2"},
    )
    result = drain_leak_list(conn, "Reminders", log_dir=log_dir, rem_module=rem)

    assert result["drained"] == 2
    assert result["errors"] == 1
    assert result["skipped"] == 0
    assert get_item_by_rid(conn, "OK-1") is not None
    assert get_item_by_rid(conn, "OK-3") is not None
    # FAIL-2's move was attempted but raised; no state.db row inserted.
    assert get_item_by_rid(conn, "FAIL-2") is None


def test_completed_items_in_leak_list_are_ignored(conn, log_dir):
    rem = StubRem([
        FakeReminder(id="DONE-1", list="Reminders", name="done", completed=True),
        FakeReminder(id="OPEN-2", list="Reminders", name="open"),
    ])
    result = drain_leak_list(conn, "Reminders", log_dir=log_dir, rem_module=rem)

    assert result["drained"] == 1
    assert rem.move_calls == [("OPEN-2", "Inbox")]
