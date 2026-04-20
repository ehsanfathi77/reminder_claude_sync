"""
write_fence.py — single chokepoint for engine writes to Reminders.

Every engine module that writes to a reminder (capture, clarify, projects,
tickler, etc.) MUST go through write_fence(). This is the only place that
checks 'is this reminder in a list the GTD engine is allowed to touch?'

Rationale: 132 user reminders live in legacy lists (Books to Read, Personal,
wine, Johnny, etc.). Stamping engine metadata into those would silently
mutate user data — we draw a hard write-scope boundary instead.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

DEFAULT_MANAGED_LISTS: frozenset[str] = frozenset({
    "Inbox",
    "@calls", "@computer", "@errands", "@home", "@anywhere", "@agenda",
    "@nyc", "@jax", "@odita", "@health", "@financials",
    "Waiting For", "Someday", "Projects", "Tickler", "Questions",
})


class WriteScopeError(RuntimeError):
    """Raised when an engine write targets a reminder outside the managed list set."""

    def __init__(self, rid: str, attempted_list: str, allowed: set[str] | frozenset[str]):
        self.rid = rid
        self.attempted_list = attempted_list
        self.allowed = set(allowed)
        super().__init__(
            f"WriteScopeError: rid={rid} list={attempted_list!r} not in managed set "
            f"({len(self.allowed)} allowed lists)"
        )


def assert_writable(
    rid: str,
    list_name: str,
    *,
    allowed: set[str] | frozenset[str] | None = None,
    invariants_log: Path | None = None,
) -> None:
    """Raise WriteScopeError if list_name not in allowed (default: DEFAULT_MANAGED_LISTS).

    Log the violation to invariants_log (if provided) before raising. The log
    receives a JSONL line with {ts, kind, rid, attempted_list, allowed_count}.
    If the log's parent directory does not exist it is created automatically.
    """
    effective = DEFAULT_MANAGED_LISTS if allowed is None else allowed
    if list_name not in effective:
        if invariants_log is not None:
            invariants_log.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(
                {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "kind": "write_scope_violation",
                    "rid": rid,
                    "attempted_list": list_name,
                    "allowed_count": len(effective),
                },
                default=str,
            )
            with invariants_log.open("a") as fh:
                fh.write(line + "\n")
        raise WriteScopeError(rid, list_name, effective)


def is_writable(
    list_name: str,
    *,
    allowed: set[str] | frozenset[str] | None = None,
) -> bool:
    """Non-raising check; useful for advisory paths."""
    effective = DEFAULT_MANAGED_LISTS if allowed is None else allowed
    return list_name in effective
