"""
engage.py — /gtd:next ranked next-action selector.

Filters across all @context lists. Ranks by:
  1. due-today (or overdue) first
  2. then matching user's current context (if specified) with no due date
  3. then everything else
  4. ties broken by oldest-first (to surface aging items)

Public API:

def next_actions(
    *,
    rem_module=R,
    ctx: str | None = None,           # '@home', '@calls', etc. or None for any
    time_min: int | None = None,      # max minutes available; filter by estimate in notes
    energy: str | None = None,        # 'low' | 'med' | 'high'; filter by tag in notes
    now: datetime | None = None,
) -> list[dict]:
    '''Return ranked list of next-action reminders matching filters.'''

def format_for_chat(actions: list[dict], limit: int = 10) -> str:
    '''Render to a compact chat-friendly string for /gtd:next output.'''
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import bin.lib.reminders as R

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The @-context lists that qualify as next-action lists.
_CONTEXT_LISTS: frozenset[str] = frozenset({
    "@calls",
    "@computer",
    "@errands",
    "@home",
    "@anywhere",
    "@nyc",
    "@jax",
    "@odita",
})

# Regex patterns for parsing notes metadata
_TIME_RE = re.compile(r"(?:time|estimate)\s*:\s*(\d+)\s*m", re.IGNORECASE)
_ENERGY_RE = re.compile(r"energy\s*:\s*(low|med|high)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_estimate(notes: str) -> int | None:
    """Extract time estimate in minutes from notes. Returns None if absent."""
    m = _TIME_RE.search(notes)
    if m:
        return int(m.group(1))
    return None


def _parse_energy(notes: str) -> str | None:
    """Extract energy level from notes. Returns None if absent."""
    m = _ENERGY_RE.search(notes)
    if m:
        return m.group(1).lower()
    return None


def _is_due_today_or_overdue(due_date: str, now: datetime) -> bool:
    """Return True if due_date is today or in the past relative to now."""
    if not due_date:
        return False
    try:
        # due_date is local-naive YYYY-MM-DDTHH:MM:SS
        due_dt = datetime.fromisoformat(due_date)
        # Compare date only: due today or overdue
        return due_dt.date() <= now.date()
    except (ValueError, TypeError):
        return False


def _age_days(last_modified: str, now: datetime) -> float:
    """Return age in days (float) from last_modified ISO string. Larger = older."""
    if not last_modified:
        return 0.0
    try:
        # last_modified ends with Z (UTC)
        s = last_modified
        if s.endswith("Z"):
            s = s[:-1]
        dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        now_utc = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
        delta = now_utc - dt
        return delta.total_seconds() / 86400.0
    except (ValueError, TypeError):
        return 0.0


def _reminder_to_dict(rem: Any) -> dict:
    """Convert a Reminder (dataclass or dict-like) to a plain dict."""
    if isinstance(rem, dict):
        return rem
    return {
        "id": rem.id,
        "list": rem.list,
        "name": rem.name,
        "completed": rem.completed,
        "due_date": rem.due_date,
        "completion_date": rem.completion_date,
        "body": rem.body,
        "priority": rem.priority,
        "last_modified": rem.last_modified,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def next_actions(
    *,
    rem_module=R,
    ctx: str | None = None,
    time_min: int | None = None,
    energy: str | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """Return ranked list of next-action reminders matching filters."""
    if now is None:
        now = datetime.now(timezone.utc)

    # Fetch all incomplete reminders
    raw = rem_module.list_all(days_done_window=0)

    results: list[dict] = []
    for rem in raw:
        d = _reminder_to_dict(rem)

        # Filter: must be in a @-context list
        if d["list"] not in _CONTEXT_LISTS:
            continue

        # Filter: if ctx given, must match exactly
        if ctx is not None and d["list"] != ctx:
            continue

        notes = d.get("body", "") or ""

        # Filter: time_min — if estimate present must be <= time_min
        if time_min is not None:
            est = _parse_estimate(notes)
            if est is not None and est > time_min:
                continue
            # No estimate: default include (est is None)

        # Filter: energy — if energy present in notes must match
        if energy is not None:
            item_energy = _parse_energy(notes)
            if item_energy is not None and item_energy != energy.lower():
                continue
            # No energy tag: default include

        results.append(d)

    # Rank: (due_today_or_overdue DESC, matches_ctx DESC, age_days DESC)
    # Sort key: lower tuple = ranked higher (sorted ascending)
    # - due_today_or_overdue: True → 0, False → 1
    # - matches_ctx: True → 0, False → 1
    # - age_days: negate so older (larger) floats to top
    def sort_key(d: dict) -> tuple:
        due_flag = 0 if _is_due_today_or_overdue(d["due_date"], now) else 1
        ctx_flag = 0 if (ctx is not None and d["list"] == ctx) else 1
        age = _age_days(d.get("last_modified", ""), now)
        return (due_flag, ctx_flag, -age)

    results.sort(key=sort_key)
    return results


def format_for_chat(actions: list[dict], limit: int = 10) -> str:
    """Render to a compact chat-friendly string for /gtd:next output."""
    if not actions:
        return "No next-actions match. Try /gtd:clarify or relax filters."

    shown = actions[:limit]
    lines: list[str] = []

    for i, d in enumerate(shown, start=1):
        list_name = d.get("list", "")
        title = d.get("name", "")
        due = d.get("due_date", "")
        notes = d.get("body", "") or ""
        est = _parse_estimate(notes)

        parts: list[str] = [list_name]

        if due:
            # Format as YYYY-MM-DD only
            due_fmt = due[:10] if len(due) >= 10 else due
            parts.append(f"due: {due_fmt}")

        if est is not None:
            parts.append(f"est: {est}m")

        annotation = ", ".join(parts)
        lines.append(f"{i}. {title} [{annotation}]")

    result = "\n".join(lines)

    remainder = len(actions) - limit
    if remainder > 0:
        result += f"\n... and {remainder} more"

    return result
