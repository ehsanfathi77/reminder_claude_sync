"""
test_full_pipeline.py — End-to-end GTD pipeline integration test.

Pipeline exercised:
  1. capture('call Alice') → reminder lands in test_list_name (Inbox surrogate)
  2. auto_clarify routes it via R1 (verb "call" → @calls, auto_next_action)
  3. Because @calls is in _CONTEXT_LISTS, engage.next_actions finds it
  4. Mark complete via reminders-cli
  5. Assert the item is no longer returned by next_actions

Safety:
  - Uses test_list_name fixture (GTD-TEST-<hex>) as the Inbox surrogate.
  - Monkeypatches capture._INBOX and write_fence.DEFAULT_MANAGED_LISTS so
    the engine writes to the test list rather than the real "Inbox".
  - The engine's @calls destination is real (already in DEFAULT_MANAGED_LISTS);
    we intercept apply_decision's move via a stub rem_module so the reminder
    stays in the test list for the duration of the test.
  - All Reminders.app writes use the test list; real user data is never touched.
  - Marked @pytest.mark.integration to exclude from plain unit-test runs.

Flakiness note:
  Reminders.app has ~1-3 s iCloud propagation delay after create/complete.
  time.sleep(3) calls after each write guard against stale reads.
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

# ── Path setup ───────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import bin.lib.reminders as R
import gtd.engine.capture as capture_mod
import gtd.engine.clarify as clarify_mod
import gtd.engine.engage as engage_mod
import gtd.engine.write_fence as wf_mod
from gtd.engine.state import init_db


# ── Helpers ──────────────────────────────────────────────────────────────────

def _open_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "state.db"
    return init_db(db_path)


# ── Test ─────────────────────────────────────────────────────────────────────

@pytest.mark.integration
def test_capture_clarify_engage_complete(test_list_name: str, tmp_path: Path, monkeypatch):
    """Full GTD pipeline: capture → clarify → engage → complete.

    Step 1: capture 'call Alice' into test_list_name (Inbox surrogate).
    Step 2: auto_clarify routes it to @calls via rule R1.
    Step 3: apply_decision would normally move the reminder — we stub that
            move so the reminder stays in test_list_name (avoiding a real
            @calls list write outside the test boundary), but we verify the
            decision kind is correct.
    Step 4: engage.next_actions finds the item when we tell it to look in
            test_list_name instead of @calls.
    Step 5: Mark the reminder complete via reminders-cli; assert it disappears
            from next_actions.
    """

    # ── Patch capture._INBOX to our test list ──────────────────────────────
    monkeypatch.setattr(capture_mod, "_INBOX", test_list_name)

    # ── Patch write_fence to accept our test list ─────────────────────────
    original_managed = wf_mod.DEFAULT_MANAGED_LISTS
    extended_managed = original_managed | {test_list_name}
    monkeypatch.setattr(wf_mod, "DEFAULT_MANAGED_LISTS", extended_managed)

    conn = _open_db(tmp_path)

    # ── Step 1: Capture ──────────────────────────────────────────────────────
    gtd_id = capture_mod.capture(
        "call Alice",
        conn=conn,
        log_dir=tmp_path / "logs",
    )
    assert gtd_id, "capture() must return a non-empty gtd_id"

    # Give Reminders.app time to persist the new reminder.
    time.sleep(3)

    # Verify the reminder landed in the test list.
    all_rems = R.list_all(days_done_window=0)
    test_rems = [r for r in all_rems if r.list == test_list_name and not r.completed]
    assert len(test_rems) == 1, (
        f"Expected 1 open reminder in {test_list_name!r}, got {len(test_rems)}: "
        f"{[r.name for r in test_rems]}"
    )
    rem = test_rems[0]
    assert "alice" in rem.name.lower()
    rid = rem.id

    # ── Step 2: auto_clarify ─────────────────────────────────────────────────
    reminder_dict = {"id": rid, "name": rem.name, "body": rem.body, "list": test_list_name}
    decision = clarify_mod.auto_clarify(reminder_dict)
    assert decision.kind == "auto_next_action", (
        f"Expected R1 to fire → auto_next_action, got {decision.kind!r} "
        f"(reasoning: {decision.reasoning!r})"
    )
    assert decision.target_list == "@calls", (
        f"Expected target_list='@calls', got {decision.target_list!r}"
    )

    # ── Step 3: apply_decision with stubbed rem_module ───────────────────────
    # We do NOT actually move the reminder to @calls (that would write to a
    # non-test list).  Instead, inject a stub that records the move call but
    # does nothing, and patch write_fence so @calls passes the scope check.
    moves: list[tuple[str, str]] = []

    class StubRem:
        """Minimal stub that records move_to_list calls and no-ops them."""

        @staticmethod
        def move_to_list(rid_: str, list_name: str) -> None:
            moves.append((rid_, list_name))

        @staticmethod
        def update_field(rid_: str, field: str, value: str) -> None:
            pass

        @staticmethod
        def update_notes(rid_: str, list_name_: str, notes: str) -> None:
            pass

    clarify_mod.apply_decision(
        decision,
        reminder_dict,
        conn=conn,
        rem_module=StubRem(),
        log_dir=tmp_path / "logs",
    )
    # The stub recorded the intended move.
    assert moves == [(rid, "@calls")], (
        f"apply_decision should have called move_to_list(rid, '@calls'), got {moves}"
    )

    # State DB should now reflect 'next_action' kind.
    from gtd.engine.state import get_item_by_rid
    item = get_item_by_rid(conn, rid)
    assert item is not None, "Item should be in state DB after apply_decision"
    assert item["kind"] == "next_action", (
        f"Expected kind='next_action', got {item['kind']!r}"
    )

    # ── Step 4: engage.next_actions ──────────────────────────────────────────
    # The actual reminder is still in test_list_name (stub didn't move it).
    # Build a minimal stub rem_module that returns our reminder in a list
    # that IS in _CONTEXT_LISTS so next_actions picks it up, by temporarily
    # patching _CONTEXT_LISTS to include test_list_name.
    original_ctx = engage_mod._CONTEXT_LISTS
    monkeypatch.setattr(engage_mod, "_CONTEXT_LISTS", original_ctx | {test_list_name})

    actions = engage_mod.next_actions(ctx=test_list_name)
    matching = [a for a in actions if a["id"] == rid]
    assert len(matching) == 1, (
        f"next_actions should return the reminder; got {len(matching)} matches "
        f"(all actions: {[a['name'] for a in actions]})"
    )

    # ── Step 5: Mark complete + verify it disappears ─────────────────────────
    R.set_complete(rid, test_list_name, True)
    time.sleep(3)  # iCloud propagation

    actions_after = engage_mod.next_actions(ctx=test_list_name)
    still_present = [a for a in actions_after if a["id"] == rid]
    assert len(still_present) == 0, (
        f"Completed reminder should not appear in next_actions, but found: {still_present}"
    )

    conn.close()
