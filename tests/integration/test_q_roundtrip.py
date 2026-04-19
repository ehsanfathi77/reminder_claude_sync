"""
test_q_roundtrip.py — Integration test for the Q-channel round-trip.

The Q-channel lets the GTD engine ask the user clarifying questions by
creating reminders in the 'Questions' list.  The user answers by completing
the reminder (optionally prefixing their answer with 'Reply: ').

This test exercises the full round-trip:
  1. qchannel.dispatch(dispatch_dryrun=False) → creates a reminder in
     test_list_name (we stub rem_module so it targets test_list_name instead
     of the real 'Questions' list, keeping all writes safely isolated).
  2. We simulate the user answering: edit the reminder's notes to prepend
     'Reply: @home' then mark it complete via reminders-cli.
  3. qchannel.poll() → reads the completed reminder, extracts the reply,
     advances the question's state to 'answered' with reply_text='@home'.

Safety:
  - All Reminders.app writes target test_list_name via the stub rem_module.
  - The real 'Questions' list is never touched.
  - test_list_name fixture cleans up on teardown even if the test fails.

Flakiness note:
  iCloud propagation after write takes 1-3 s.  The time.sleep(3) calls after
  each Reminders.app write guard against stale reads.  Increase to 5 s on
  slow/iCloud-heavy machines.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import bin.lib.reminders as R
import gtd.engine.qchannel as qchannel_mod
from gtd.engine.state import init_db


# ── Helpers ──────────────────────────────────────────────────────────────────

def _open_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "state.db"
    return init_db(db_path)


def _get_rid(list_name: str, title_fragment: str) -> str | None:
    """Return the externalId of the first matching open reminder, or None."""
    rems = R.list_all(days_done_window=0)
    for r in rems:
        if r.list == list_name and title_fragment.lower() in r.name.lower():
            return r.id
    return None


# ── Stub rem_module ──────────────────────────────────────────────────────────

def _make_stub_rem(target_list: str) -> Any:
    """Return a stub rem_module that redirects all writes to target_list.

    qchannel.dispatch calls:
      rem_module.create(list_name=_QUESTIONS_LIST, name=..., notes=...)
      rem_module.update_notes(rid, _QUESTIONS_LIST, notes)   [optional]

    We redirect both to target_list and delegate to the real R module.
    """
    class StubRem:
        @staticmethod
        def create(list_name: str, name: str, notes: str = "", due_iso: str = "") -> str:
            # Always create in target_list regardless of the requested list.
            return R.create(target_list, name, notes=notes)

        @staticmethod
        def update_notes(rid: str, list_name: str, notes: str) -> None:
            R.update_notes(rid, target_list, notes)

        @staticmethod
        def list_all(**kwargs) -> list:
            # poll() calls rem_module.list_all() and filters by r.list == 'Questions'.
            # We intercept: return real reminders but lie about their list name so
            # poll's filter accepts them.
            real = R.list_all(days_done_window=7)
            patched = []
            for r in real:
                if r.list == target_list:
                    # Present this reminder as if it were in 'Questions'.
                    from dataclasses import replace
                    patched.append(replace(r, list="Questions"))
                else:
                    patched.append(r)
            return patched

        @staticmethod
        def set_complete(rid: str, list_name: str, completed: bool) -> None:
            R.set_complete(rid, target_list, completed)

        @staticmethod
        def delete(rid: str, list_name: str) -> None:
            R.delete(rid, target_list)

    return StubRem()


# ── Test ─────────────────────────────────────────────────────────────────────

@pytest.mark.integration
def test_q_dispatch_answer_advance(test_list_name: str, tmp_path: Path, monkeypatch):
    """Q-channel round-trip: dispatch → user answers → poll advances state."""

    conn = _open_db(tmp_path)
    stub_rem = _make_stub_rem(test_list_name)

    # ── Step 1: Dispatch a Q (live, not dryrun) ──────────────────────────────
    # Patch write_fence to allow writes to test_list_name.
    import gtd.engine.write_fence as wf_mod
    extended = wf_mod.DEFAULT_MANAGED_LISTS | {test_list_name}
    monkeypatch.setattr(wf_mod, "DEFAULT_MANAGED_LISTS", extended)

    # Also patch qchannel's _QUESTIONS_LIST reference so assert_writable passes.
    monkeypatch.setattr(qchannel_mod, "_QUESTIONS_LIST", test_list_name)

    # Dispatch outside quiet hours; force a non-quiet time by passing noon UTC.
    from datetime import datetime, timezone
    noon_utc = datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc)

    dispatch_result = qchannel_mod.dispatch(
        conn=conn,
        rem_module=stub_rem,
        kind="clarify",
        prompt="Test clarify Q for integration test",
        payload={"test": True},
        dispatch_dryrun=False,
        quiet_hours=(22, 6),   # noon is outside quiet hours
        now=noon_utc,
        log_dir=tmp_path / "logs",
    )
    assert dispatch_result.status == "dispatched", (
        f"Expected status='dispatched', got {dispatch_result.status!r} "
        f"(reason: {dispatch_result.reason!r})"
    )
    qid = dispatch_result.qid
    assert qid, "dispatch must return a qid"

    # Give Reminders.app time to create the reminder.
    time.sleep(3)

    # Verify the Q-reminder landed in test_list_name.
    rid = _get_rid(test_list_name, "Test clarify Q")
    assert rid is not None, (
        f"Q-reminder not found in '{test_list_name}' after dispatch. "
        "Check Reminders.app permissions and iCloud sync."
    )

    # Verify the question is 'open' in state DB.
    row = conn.execute("SELECT status FROM questions WHERE qid = ?", (qid,)).fetchone()
    assert row is not None, f"qid {qid!r} not found in state DB"
    assert dict(row)["status"] == "open", f"Expected status='open', got {dict(row)['status']!r}"

    # ── Step 2: Simulate user answering ─────────────────────────────────────
    # Fetch current notes so we can prepend the Reply: line.
    rems = R.list_all(days_done_window=0)
    q_rem = next((r for r in rems if r.id == rid), None)
    assert q_rem is not None, f"Could not re-fetch reminder rid={rid!r}"

    reply_notes = f"Reply: @home\n{q_rem.body or ''}"
    subprocess.run(
        [str(ROOT / "bin" / "reminders-cli"), "edit", test_list_name, rid, "--notes", reply_notes],
        check=True,
        capture_output=True,
        timeout=15,
    )
    time.sleep(2)  # brief propagation before marking complete

    R.set_complete(rid, test_list_name, True)
    time.sleep(3)  # iCloud propagation

    # ── Step 3: poll() — verify state advances to 'answered' ─────────────────
    answered = qchannel_mod.poll(
        conn=conn,
        rem_module=stub_rem,
        now=noon_utc,
        log_dir=tmp_path / "logs",
    )

    # poll() should have found exactly our Q as answered.
    assert any(a["qid"] == qid for a in answered), (
        f"poll() did not return qid={qid!r} in answered list.\n"
        f"answered={answered}\n"
        "Possible cause: iCloud didn't propagate the completion in time — "
        "try increasing the time.sleep() values."
    )

    # Extract our answer record.
    our_answer = next(a for a in answered if a["qid"] == qid)
    assert our_answer["reply_text"] == "@home", (
        f"Expected reply_text='@home', got {our_answer['reply_text']!r}"
    )

    # Verify state DB now shows 'answered'.
    row_after = conn.execute("SELECT status FROM questions WHERE qid = ?", (qid,)).fetchone()
    assert row_after is not None
    assert dict(row_after)["status"] == "answered", (
        f"Expected status='answered' in DB after poll, "
        f"got {dict(row_after)['status']!r}"
    )

    conn.close()
