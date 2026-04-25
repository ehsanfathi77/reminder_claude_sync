"""
leak_capture.py — drain Siri/iPhone capture leaks into Inbox.

iPhone Siri's default Reminders list defaults to "Reminders" (legacy, unmanaged
by GTD). Every "Hey Siri, remind me to X" capture lands there and silently
bypasses clarify.process_inbox() — which only scans the "Inbox" list.

drain_leak_list() is a per-tick safety-net: for every reminder in the leak list
that the engine has not already touched, move it to Inbox and insert a
state.db row with kind='unclarified'. The next clarify pass picks it up
normally.

Move uses bin.lib.reminders.move_to_list (same primitive as /gtd:adopt --apply).
The move is permitted by write_fence.assert_writable's narrow leak_source bypass
(source must be in leak_source_lists set, destination MUST be 'Inbox').
"""
from __future__ import annotations

from pathlib import Path

from gtd.engine.observability import log as obs_log
from gtd.engine.write_fence import assert_writable

try:
    import bin.lib.reminders as _R  # type: ignore
except ImportError:
    _R = None  # type: ignore


_INBOX_LIST = "Inbox"


def drain_leak_list(
    conn,
    leak_list: str,
    log_dir: Path | None = None,
    *,
    rem_module=None,
) -> dict:
    """Drain `leak_list` into Inbox.

    For every reminder in `leak_list` whose rid is not already tracked in
    state.db, move it to Inbox via R.move_to_list and insert a state.db
    items row with kind='unclarified'. Already-tracked items are skipped
    (no duplicate move, no duplicate row). Per-item errors are counted and
    surfaced in the return dict but never abort the drain.

    Args:
        conn: open sqlite3 connection to state.db.
        leak_list: source list name (e.g., "Reminders").
        log_dir: directory for engine.jsonl (None → observability default).
        rem_module: injectable bin.lib.reminders stub (tests).

    Returns:
        {"drained": int, "errors": int, "skipped": int}
    """
    from gtd.engine import state as state_mod

    if rem_module is None:
        rem_module = _R

    counters = {"drained": 0, "errors": 0, "skipped": 0}

    if rem_module is None:
        # No reminders backend available (e.g., tests that didn't inject one).
        # Log + return silently — the tick should not blow up.
        obs_log(
            "engine", log_dir=log_dir,
            op="leak_capture", leak_list=leak_list,
            note="rem_module unavailable",
            **counters,
        )
        return counters

    try:
        all_rems = rem_module.list_all()
    except Exception as exc:
        counters["errors"] += 1
        obs_log(
            "engine", log_dir=log_dir,
            op="leak_capture", leak_list=leak_list,
            error=str(exc),
            **counters,
        )
        return counters

    items = [
        r for r in all_rems
        if getattr(r, "list", None) == leak_list
        and not getattr(r, "completed", False)
    ]

    leak_source_lists = frozenset({leak_list})

    for rem in items:
        rid = getattr(rem, "id", "") or ""
        if not rid:
            counters["errors"] += 1
            continue

        if state_mod.get_item_by_rid(conn, rid) is not None:
            counters["skipped"] += 1
            continue

        try:
            assert_writable(
                rid,
                _INBOX_LIST,
                leak_source_lists=leak_source_lists,
                source_list=leak_list,
            )
            rem_module.move_to_list(rid, _INBOX_LIST)
            state_mod.insert_item(
                conn, rid=rid, kind="unclarified", list=_INBOX_LIST,
            )
            counters["drained"] += 1
        except Exception:
            counters["errors"] += 1

    obs_log(
        "engine", log_dir=log_dir,
        op="leak_capture", leak_list=leak_list,
        **counters,
    )
    return counters
