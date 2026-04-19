"""
waiting.py — nudge generation for delegated items.

Reminders in 'Waiting For' list with delegate metadata in fence:
  --- gtd ---
  id: <ulid>
  kind: waiting
  delegate: Dan
  created: <iso>
  --- end ---

nudge() defaults to digest mode: ONE Q with all stale (>7d) items + delegates.
nudge(per_item=True) dispatches one Q per stale item, bounded by remaining
q_max_per_day budget (read from qchannel.per_day_count).

Public API:

@dataclass
class WaitingItem:
    rid: str
    title: str
    delegate: str | None
    created: str       # ISO
    age_days: int

def list_waiting(
    *,
    rem_module=R,
    now: datetime | None = None,
) -> list[WaitingItem]:
    '''Read Waiting For list, parse delegate from fence, compute age.'''

def nudge(
    *,
    conn,
    rem_module=R,
    qchannel_module=Q,
    per_item: bool = False,
    age_threshold_days: int = 7,
    log_dir: Path | None = None,
    dispatch_dryrun: bool = True,
    now: datetime | None = None,
) -> dict:
    '''Default: ONE digest Q listing all stale waitings.
    per_item=True: one Q per stale item, capped by remaining q_max_per_day.
    Returns {'stale_count': N, 'qs_dispatched': N, 'cap_hit': bool}.'''
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from gtd.engine.notes_metadata import parse_metadata

# Import reminders module as the default rem_module.
# Tests inject a stub via the rem_module parameter.
try:
    import bin.lib.reminders as _R  # type: ignore
except ImportError:
    _R = None  # type: ignore

import gtd.engine.qchannel as _Q

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WAITING_LIST = "Waiting For"
_Q_MAX_PER_DAY = 8


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass
class WaitingItem:
    rid: str
    title: str
    delegate: str | None
    created: str       # ISO
    age_days: int


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_waiting(
    *,
    rem_module=None,
    now: datetime | None = None,
) -> list[WaitingItem]:
    """Read Waiting For list, parse delegate from fence, compute age."""
    if rem_module is None:
        rem_module = _R
    if now is None:
        now = datetime.now(timezone.utc)

    all_rems = rem_module.list_all()
    items: list[WaitingItem] = []

    for rem in all_rems:
        if rem.list != _WAITING_LIST:
            continue
        # Skip completed reminders
        if getattr(rem, "completed", False):
            continue

        notes = getattr(rem, "body", "") or ""
        meta, _ = parse_metadata(notes)

        delegate = meta.get("delegate") or None
        created_str = meta.get("created", "")

        # Compute age_days
        age_days = 0
        if created_str:
            try:
                created_dt = datetime.fromisoformat(created_str)
                if created_dt.tzinfo is None:
                    created_dt = created_dt.replace(tzinfo=timezone.utc)
                delta = now - created_dt
                age_days = delta.days
            except (ValueError, TypeError):
                age_days = 0

        items.append(WaitingItem(
            rid=rem.id,
            title=rem.name,
            delegate=delegate,
            created=created_str,
            age_days=age_days,
        ))

    return items


def nudge(
    *,
    conn,
    rem_module=None,
    qchannel_module=None,
    per_item: bool = False,
    age_threshold_days: int = 7,
    log_dir: Path | None = None,
    dispatch_dryrun: bool = True,
    now: datetime | None = None,
) -> dict:
    """Default: ONE digest Q listing all stale waitings.
    per_item=True: one Q per stale item, capped by remaining q_max_per_day.
    Returns {'stale_count': N, 'qs_dispatched': N, 'cap_hit': bool}.
    """
    if rem_module is None:
        rem_module = _R
    if qchannel_module is None:
        qchannel_module = _Q
    if now is None:
        now = datetime.now(timezone.utc)

    all_items = list_waiting(rem_module=rem_module, now=now)
    stale = [item for item in all_items if item.age_days > age_threshold_days]

    stale_count = len(stale)
    qs_dispatched = 0
    cap_hit = False

    if stale_count == 0:
        return {"stale_count": 0, "qs_dispatched": 0, "cap_hit": False}

    if not per_item:
        # Digest mode: one single Q with all stale items
        refs = [
            {"rid": item.rid, "title": item.title, "delegate": item.delegate, "age_days": item.age_days}
            for item in stale
        ]
        prompt = f"Waiting nudge: {stale_count} items overdue — check in?"
        result = qchannel_module.dispatch(
            conn=conn,
            rem_module=rem_module,
            kind="digest_review",
            prompt=prompt,
            payload={"refs": refs},
            digest=True,
            dispatch_dryrun=dispatch_dryrun,
            log_dir=log_dir,
            now=now,
        )
        if result.status in ("dispatched", "dryrun", "queued_quiet"):
            qs_dispatched = 1
    else:
        # Per-item mode: one Q per stale item, capped by remaining daily budget
        today_iso = now.strftime("%Y-%m-%d")
        already_dispatched = qchannel_module.per_day_count(conn=conn, day_iso=today_iso)
        remaining = _Q_MAX_PER_DAY - already_dispatched

        if remaining <= 0:
            return {"stale_count": stale_count, "qs_dispatched": 0, "cap_hit": True}

        for item in stale:
            if qs_dispatched >= remaining:
                cap_hit = True
                break
            delegate_str = f" (delegate: {item.delegate})" if item.delegate else ""
            prompt = f"Still waiting on: {item.title}{delegate_str} — {item.age_days}d"
            result = qchannel_module.dispatch(
                conn=conn,
                rem_module=rem_module,
                kind="digest_review",
                prompt=prompt,
                payload={"rid": item.rid, "delegate": item.delegate, "age_days": item.age_days},
                digest=True,
                dispatch_dryrun=dispatch_dryrun,
                log_dir=log_dir,
                now=now,
            )
            if result.status in ("dispatched", "dryrun", "queued_quiet"):
                qs_dispatched += 1

        # If we hit per-item cap without exhausting remaining budget, check if
        # we dispatched fewer items than stale_count due to cap.
        if not cap_hit and qs_dispatched < stale_count:
            # Check if we actually ran out due to a hard cap (cap_per_day) from
            # dispatch itself
            pass

    return {"stale_count": stale_count, "qs_dispatched": qs_dispatched, "cap_hit": cap_hit}
