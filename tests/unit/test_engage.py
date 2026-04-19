"""
Unit tests for gtd/engine/engage.py  (US-010)

Stubs rem_module with a fixed list of Reminder-like dicts. Covers:
- next_actions(ctx='@home') returns only @home items
- next_actions(ctx=None) returns all @-context items, no Inbox/Waiting/Someday
- time_min=15 filters out items with notes 'time:30m'
- energy='low' filters to low-energy items
- Due-today items rank before no-due
- Within same rank tier: older items rank first
- format_for_chat with 12 items, limit=10 → 10 lines + '... and 2 more'
- Empty result → helpful message
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from gtd.engine.engage import next_actions, format_for_chat


# ---------------------------------------------------------------------------
# Stub rem_module
# ---------------------------------------------------------------------------

def _make_reminder(
    *,
    rid: str = "test-id",
    list_name: str = "@home",
    name: str = "test item",
    due_date: str = "",
    body: str = "",
    last_modified: str = "2026-04-10T12:00:00Z",
) -> dict:
    """Build a Reminder-like dict matching the shape _reminder_to_dict produces."""
    return {
        "id": rid,
        "list": list_name,
        "name": name,
        "completed": False,
        "due_date": due_date,
        "completion_date": "",
        "body": body,
        "priority": 0,
        "last_modified": last_modified,
    }


class StubRemModule:
    """Minimal stub for bin.lib.reminders; returns a fixed list on list_all()."""

    def __init__(self, reminders: list[dict]):
        self._reminders = reminders

    def list_all(self, days_done_window: int = 0) -> list[dict]:
        return list(self._reminders)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

NOW = datetime(2026, 4, 18, 14, 0, 0, tzinfo=timezone.utc)
TODAY_STR = "2026-04-18T09:00:00"   # local naive, today
PAST_STR = "2026-04-15T09:00:00"    # overdue
FUTURE_STR = "2026-04-25T09:00:00"  # future


@pytest.fixture
def mixed_reminders():
    """A list spanning multiple lists including non-@-context lists."""
    return [
        _make_reminder(rid="h1", list_name="@home", name="Fix lightbulb", last_modified="2026-04-01T00:00:00Z"),
        _make_reminder(rid="h2", list_name="@home", name="Water plants", last_modified="2026-04-05T00:00:00Z"),
        _make_reminder(rid="c1", list_name="@calls", name="Call dentist"),
        _make_reminder(rid="co1", list_name="@computer", name="Send invoice"),
        _make_reminder(rid="e1", list_name="@errands", name="Buy milk"),
        _make_reminder(rid="i1", list_name="Inbox", name="Clarify this"),
        _make_reminder(rid="w1", list_name="Waiting For", name="Waiting on reply"),
        _make_reminder(rid="s1", list_name="Someday/Maybe", name="Learn guitar"),
    ]


# ---------------------------------------------------------------------------
# Tests: filtering by ctx
# ---------------------------------------------------------------------------

def test_next_actions_ctx_home_returns_only_home_items(mixed_reminders):
    stub = StubRemModule(mixed_reminders)
    actions = next_actions(rem_module=stub, ctx="@home", now=NOW)
    assert len(actions) == 2
    assert all(a["list"] == "@home" for a in actions)


def test_next_actions_ctx_none_returns_all_context_lists(mixed_reminders):
    stub = StubRemModule(mixed_reminders)
    actions = next_actions(rem_module=stub, ctx=None, now=NOW)
    lists = {a["list"] for a in actions}
    # All returned items must be @-context lists
    non_context = {"Inbox", "Waiting For", "Someday/Maybe"}
    assert lists.isdisjoint(non_context), f"Got non-context lists: {lists & non_context}"
    # Must include the @-context items
    assert "@home" in lists
    assert "@calls" in lists
    assert "@computer" in lists
    assert "@errands" in lists


def test_next_actions_ctx_none_excludes_inbox_waiting_someday(mixed_reminders):
    stub = StubRemModule(mixed_reminders)
    actions = next_actions(rem_module=stub, ctx=None, now=NOW)
    ids = {a["id"] for a in actions}
    assert "i1" not in ids   # Inbox
    assert "w1" not in ids   # Waiting For
    assert "s1" not in ids   # Someday/Maybe


# ---------------------------------------------------------------------------
# Tests: time_min filter
# ---------------------------------------------------------------------------

def test_time_min_filters_out_long_items():
    rems = [
        _make_reminder(rid="a", list_name="@home", name="Quick task", body="time:10m"),
        _make_reminder(rid="b", list_name="@home", name="Long task", body="time:30m"),
        _make_reminder(rid="c", list_name="@home", name="No estimate"),
    ]
    stub = StubRemModule(rems)
    actions = next_actions(rem_module=stub, time_min=15, now=NOW)
    ids = {a["id"] for a in actions}
    assert "a" in ids      # 10m <= 15 → included
    assert "b" not in ids  # 30m > 15 → excluded
    assert "c" in ids      # no estimate → default include


def test_time_min_with_estimate_keyword():
    rems = [
        _make_reminder(rid="x", list_name="@computer", name="Write docs", body="estimate:20m"),
    ]
    stub = StubRemModule(rems)
    actions = next_actions(rem_module=stub, time_min=15, now=NOW)
    assert len(actions) == 0  # 20m > 15


def test_time_min_include_when_estimate_missing():
    rems = [
        _make_reminder(rid="y", list_name="@anywhere", name="Think", body=""),
    ]
    stub = StubRemModule(rems)
    actions = next_actions(rem_module=stub, time_min=5, now=NOW)
    assert len(actions) == 1


# ---------------------------------------------------------------------------
# Tests: energy filter
# ---------------------------------------------------------------------------

def test_energy_low_filters_to_low_items():
    rems = [
        _make_reminder(rid="l", list_name="@home", name="Low task", body="energy: low"),
        _make_reminder(rid="m", list_name="@home", name="Med task", body="energy: med"),
        _make_reminder(rid="h", list_name="@home", name="High task", body="energy: high"),
        _make_reminder(rid="n", list_name="@home", name="No energy tag"),
    ]
    stub = StubRemModule(rems)
    actions = next_actions(rem_module=stub, energy="low", now=NOW)
    ids = {a["id"] for a in actions}
    assert "l" in ids   # low == low → included
    assert "m" not in ids
    assert "h" not in ids
    assert "n" in ids   # no energy tag → default include


def test_energy_filter_case_insensitive():
    rems = [
        _make_reminder(rid="p", list_name="@calls", name="Low call", body="Energy: LOW"),
    ]
    stub = StubRemModule(rems)
    actions = next_actions(rem_module=stub, energy="low", now=NOW)
    assert len(actions) == 1


# ---------------------------------------------------------------------------
# Tests: ranking
# ---------------------------------------------------------------------------

def test_due_today_ranks_before_no_due():
    rems = [
        _make_reminder(rid="nodUe", list_name="@home", name="No due date", last_modified="2026-01-01T00:00:00Z"),
        _make_reminder(rid="due", list_name="@home", name="Due today", due_date=TODAY_STR, last_modified="2026-04-17T00:00:00Z"),
    ]
    stub = StubRemModule(rems)
    actions = next_actions(rem_module=stub, now=NOW)
    assert actions[0]["id"] == "due"


def test_overdue_ranks_first():
    rems = [
        _make_reminder(rid="future", list_name="@home", name="Future task", due_date=FUTURE_STR),
        _make_reminder(rid="past", list_name="@home", name="Overdue task", due_date=PAST_STR),
        _make_reminder(rid="none", list_name="@home", name="No due"),
    ]
    stub = StubRemModule(rems)
    actions = next_actions(rem_module=stub, now=NOW)
    assert actions[0]["id"] == "past"


def test_older_items_rank_first_within_same_tier():
    rems = [
        _make_reminder(rid="newer", list_name="@home", name="Newer item", last_modified="2026-04-16T00:00:00Z"),
        _make_reminder(rid="older", list_name="@home", name="Older item", last_modified="2026-03-01T00:00:00Z"),
    ]
    stub = StubRemModule(rems)
    actions = next_actions(rem_module=stub, now=NOW)
    assert actions[0]["id"] == "older"


def test_due_today_older_beats_due_today_newer():
    """Among due-today items, older last_modified comes first."""
    rems = [
        _make_reminder(rid="due_new", list_name="@home", name="Due newer", due_date=TODAY_STR, last_modified="2026-04-17T00:00:00Z"),
        _make_reminder(rid="due_old", list_name="@home", name="Due older", due_date=TODAY_STR, last_modified="2026-03-10T00:00:00Z"),
    ]
    stub = StubRemModule(rems)
    actions = next_actions(rem_module=stub, now=NOW)
    assert actions[0]["id"] == "due_old"


# ---------------------------------------------------------------------------
# Tests: format_for_chat
# ---------------------------------------------------------------------------

def _make_12_actions() -> list[dict]:
    return [
        _make_reminder(rid=f"i{n}", list_name="@home", name=f"Task {n}")
        for n in range(1, 13)
    ]


def test_format_for_chat_limit_10_shows_10_lines_and_more():
    actions = _make_12_actions()
    output = format_for_chat(actions, limit=10)
    lines = output.strip().splitlines()
    # Last line is "... and 2 more"
    assert lines[-1] == "... and 2 more"
    # 10 numbered lines + 1 "more" line
    assert len(lines) == 11


def test_format_for_chat_numbered_correctly():
    actions = _make_12_actions()
    output = format_for_chat(actions, limit=10)
    lines = output.strip().splitlines()
    assert lines[0].startswith("1. ")
    assert lines[9].startswith("10. ")


def test_format_for_chat_includes_list_name():
    actions = [_make_reminder(rid="x", list_name="@calls", name="Call mom")]
    output = format_for_chat(actions, limit=10)
    assert "@calls" in output


def test_format_for_chat_includes_due_date():
    actions = [_make_reminder(rid="x", list_name="@home", name="Task", due_date=TODAY_STR)]
    output = format_for_chat(actions, limit=10)
    assert "due: 2026-04-18" in output


def test_format_for_chat_includes_estimate():
    actions = [_make_reminder(rid="x", list_name="@home", name="Task", body="time:20m")]
    output = format_for_chat(actions, limit=10)
    assert "est: 20m" in output


def test_format_for_chat_no_more_when_fits():
    actions = _make_12_actions()
    output = format_for_chat(actions, limit=20)
    assert "more" not in output


def test_format_for_chat_empty_returns_helpful_message():
    output = format_for_chat([], limit=10)
    assert "No next-actions match" in output
    assert "/gtd:clarify" in output


def test_format_for_chat_exactly_limit_no_more():
    actions = _make_12_actions()[:10]
    output = format_for_chat(actions, limit=10)
    assert "more" not in output
    lines = [l for l in output.strip().splitlines() if l.strip()]
    assert len(lines) == 10
