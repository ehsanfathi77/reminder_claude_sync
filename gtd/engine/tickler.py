"""
tickler.py — park items in a future-dated holding pen until they're due.

Tickler reminders live in the 'Tickler' list. Each tick():
  1. release_at <= now → move reminder back to its target_list (default 'Inbox'),
     remove tickler row from state
  2. release_at < now - 24h → past-due cleanup: emit ONE digest Q listing all
     past-due ticklers (don't move them; user decides). Bulk-producer mode.

Public API:

def park(
    rid: str,
    list_name: str,                  # current list (must be in managed; pre-park)
    release_at: str,                 # ISO local
    *,
    conn,
    target_list: str = 'Inbox',
    rem_module=R,
    log_dir: Path | None = None,
    now: datetime | None = None,
) -> None:
    '''Move reminder to Tickler, record release_at + target_list in state.'''

def release(
    *,
    conn,
    rem_module=R,
    log_dir: Path | None = None,
    qchannel_module=Q,
    dispatch_dryrun: bool = True,
    now: datetime | None = None,
) -> dict:
    '''Process all due ticklers. Returns {'released': N, 'past_due_q': bool}.'''
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import gtd.engine.qchannel as Q
import gtd.engine.state as state_mod
from gtd.engine.observability import log as obs_log
from gtd.engine.write_fence import assert_writable

# Import reminders module as the default rem_module.
# Tests inject a stub via the rem_module parameter.
try:
    import bin.lib.reminders as R  # type: ignore
except ImportError:
    R = None  # type: ignore

_TICKLER_LIST = "Tickler"
_PAST_DUE_THRESHOLD_H = 24

# Date-input validation for the user-facing /gtd:tickler command.
# Accepts YYYY-MM-DD (date-only, defaulted to 09:00 in the user's local
# timezone) and YYYY-MM-DDTHH:MM:SS (offset-naive → local; offset-aware →
# preserved as given). Anything else raises InvalidReleaseDate so the CLI
# can translate to a friendly error.
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}([+-]\d{2}:\d{2}|Z)?$"
)
_PARSE_HINT = "Use ISO YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS."


class InvalidReleaseDate(ValueError):
    """Raised when a tickler release date can't be parsed."""

    def __init__(self, raw: str):
        self.raw = raw
        super().__init__(f"invalid date {raw!r}. {_PARSE_HINT}")


def _local_tz_for(dt_naive: datetime) -> timezone:
    """Return a fixed-offset tzinfo matching the user's local TZ AT THAT moment.

    Critical: the offset must be computed for the parsed date, not for `now`.
    Otherwise a January date parsed in July (DST active) would get an EDT
    offset instead of EST. Uses `time.mktime` + `localtime.tm_gmtoff` so DST
    transitions are honored correctly.
    """
    import time as _time
    ts = _time.mktime(dt_naive.timetuple())
    lt = _time.localtime(ts)
    return timezone(timedelta(seconds=lt.tm_gmtoff))


def parse_release_date(s: str) -> str:
    """Normalize a user-supplied release date to ISO 8601 with offset.

    Accepts:
      - 'YYYY-MM-DD'         → defaults to 09:00 in user's local timezone
      - 'YYYY-MM-DDTHH:MM:SS' (offset-naive) → interpreted as local time
      - 'YYYY-MM-DDTHH:MM:SS±HH:MM' or '...Z' (offset-aware) → preserved

    Returns an offset-aware ISO string like '2026-06-01T09:00:00-04:00'.
    Raises InvalidReleaseDate on anything else.
    """
    if not isinstance(s, str) or not s.strip():
        raise InvalidReleaseDate(s if isinstance(s, str) else repr(s))
    raw = s.strip()

    if _DATE_ONLY_RE.match(raw):
        try:
            d = datetime.strptime(raw, "%Y-%m-%d")
        except ValueError as exc:
            raise InvalidReleaseDate(raw) from exc
        naive = d.replace(hour=9)
        dt = naive.replace(tzinfo=_local_tz_for(naive))
        return dt.isoformat(timespec="seconds")

    if _DATETIME_RE.match(raw):
        normalized = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
        try:
            dt = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise InvalidReleaseDate(raw) from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_local_tz_for(dt))
        return dt.isoformat(timespec="seconds")

    raise InvalidReleaseDate(raw)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def park(
    rid: str,
    list_name: str,
    release_at: str,
    *,
    conn,
    target_list: str = "Inbox",
    rem_module=None,
    log_dir: Path | None = None,
    now: datetime | None = None,
) -> None:
    """Move reminder to Tickler, record release_at + target_list in state."""
    if rem_module is None:
        rem_module = R
    if now is None:
        now = _now_utc()

    # Assert the source list is writable (pre-park check)
    assert_writable(rid, list_name)

    # Assert the Tickler list is writable (destination)
    assert_writable(rid, _TICKLER_LIST)

    # Move the reminder into the Tickler list
    rem_module.move_to_list(rid, _TICKLER_LIST)

    # Look up the item in state by rid to get gtd_id
    item = state_mod.get_item_by_rid(conn, rid)
    if item is None:
        raise ValueError(f"No state row found for rid={rid!r}; item must be captured before parking")

    gtd_id = item["gtd_id"]

    # Update the list in the items table to reflect move to Tickler
    conn.execute("UPDATE items SET list = ? WHERE gtd_id = ?", (_TICKLER_LIST, gtd_id))
    conn.commit()

    # Record tickler row
    state_mod.park_tickler(conn, gtd_id, release_at, target_list)

    obs_log(
        "engine",
        log_dir=log_dir,
        op="tickler_park",
        rid=rid,
        gtd_id=gtd_id,
        release_at=release_at,
        target_list=target_list,
    )


def release(
    *,
    conn,
    rem_module=None,
    log_dir: Path | None = None,
    qchannel_module=None,
    dispatch_dryrun: bool = True,
    now: datetime | None = None,
) -> dict:
    """Process all due ticklers. Returns {'released': N, 'past_due_q': bool}."""
    if rem_module is None:
        rem_module = R
    if qchannel_module is None:
        qchannel_module = Q
    if now is None:
        now = _now_utc()

    now_iso = _iso(now)
    past_due_cutoff = _iso(now - timedelta(hours=_PAST_DUE_THRESHOLD_H))

    # Fetch all due ticklers (release_at <= now)
    due = state_mod.due_ticklers(conn, now_iso)

    released_count = 0
    past_due_refs: list[str] = []

    for row in due:
        gtd_id = row["gtd_id"]
        release_at = row["release_at"]
        target_list = row["target_list"]

        # Determine if past-due (release_at < now - 24h)
        if release_at < past_due_cutoff:
            # Look up rid for digest payload
            item = conn.execute("SELECT rid FROM items WHERE gtd_id = ?", (gtd_id,)).fetchone()
            ref = dict(item)["rid"] if item else gtd_id
            past_due_refs.append(ref)
            # Do NOT move — user decides
            continue

        # Normal release: assert target list is writable, move back
        item = conn.execute("SELECT rid FROM items WHERE gtd_id = ?", (gtd_id,)).fetchone()
        if item is None:
            continue
        rid = dict(item)["rid"]

        assert_writable(rid, target_list)
        rem_module.move_to_list(rid, target_list)

        # Update items table
        conn.execute("UPDATE items SET list = ? WHERE gtd_id = ?", (target_list, gtd_id))
        conn.commit()

        # Remove tickler row
        conn.execute("DELETE FROM ticklers WHERE gtd_id = ?", (gtd_id,))
        conn.commit()

        obs_log(
            "engine",
            log_dir=log_dir,
            op="tickler_release",
            gtd_id=gtd_id,
            rid=rid,
            target_list=target_list,
            release_at=release_at,
        )
        released_count += 1

    # Emit ONE digest Q for all past-due ticklers
    past_due_q = False
    if past_due_refs:
        result = qchannel_module.dispatch(
            conn=conn,
            rem_module=rem_module,
            kind="digest",
            prompt=f"Past-due ticklers ({len(past_due_refs)}): review and decide",
            payload={"ticklers": past_due_refs},
            digest=True,
            dispatch_dryrun=dispatch_dryrun,
            log_dir=log_dir,
            now=now,
        )
        past_due_q = result.status in ("dispatched", "dryrun", "queued_quiet")

    return {"released": released_count, "past_due_q": past_due_q}
