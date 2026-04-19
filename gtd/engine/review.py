"""
review.py — weekly GTD review.

Two modes:
  prepare(snapshot_kind='friday_prep' | 'sunday_nudge'):
    - Capture snapshot of all GTD lists (via rem_module.list_all)
    - Write to memory/reviews/YYYY-MM-DD-<kind>.md (markdown summary)
    - Insert reviews row in state.db
    - On 'friday_prep': dispatch 1 Q (kind='review_agenda') linking to snapshot
    - On 'sunday_nudge': dispatch ONLY if Friday Q was not acknowledged
      (status still 'open' OR 'cancelled' OR no friday Q this week)

  run_review() — interactive (called by /gtd:weekly-review):
    Walks the user through process inbox → waiting → projects → someday in chat.
    Returns a structured outcome dict; the chat-side interaction is the user's
    job (this just provides the data + ordering).

Public API:

def prepare(
    snapshot_kind: str,              # 'friday_prep' | 'sunday_nudge'
    *,
    conn,
    rem_module=R,
    qchannel_module=Q,
    memory_dir: Path | None = None,  # default ~/Documents/repos/todo/memory
    log_dir: Path | None = None,
    dispatch_dryrun: bool = True,
    now: datetime | None = None,
) -> dict:
    '''Returns {'snapshot_path': Path, 'q_dispatched': bool, 'q_skipped_reason': str|None}.'''

def render_snapshot_md(snapshot: dict, *, now: datetime | None = None) -> str:
    '''Pure function. Take a snapshot dict, return markdown.'''

def collect_snapshot(*, rem_module=R, conn) -> dict:
    '''Pure-ish (reads via rem_module + conn). Returns:
       {
         'inbox': [{rid, title, age_days}, ...],
         'waiting': [{rid, title, delegate, age_days}, ...],
         'projects': [{project_id, name, outcome, child_count, stalled: bool}, ...],
         'someday': [{rid, title, age_days}, ...],
         'next_actions_by_ctx': {'@home': N, '@calls': N, ...},
         'tickler_due_count': N,
         'last_review_iso': str | None,
       }'''

def run_review() -> dict:
    '''Stub for interactive use. Returns the snapshot dict; chat handles UX.'''
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import gtd.engine.state as state_mod
import gtd.engine.qchannel as _Q_default

# Import reminders module as the default rem_module.
# Tests inject a stub via the rem_module parameter.
try:
    import bin.lib.reminders as _R  # type: ignore
except ImportError:
    _R = None  # type: ignore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MEMORY_DIR = Path.home() / "Documents/repos/todo/memory"

# GTD list names
_INBOX_LIST = "Inbox"
_WAITING_LIST = "Waiting For"
_SOMEDAY_LIST = "Someday/Maybe"

# Context lists for next-actions
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

# Regex for extracting delegate from "Waiting for <person>: <task>" style notes
_DELEGATE_RE = re.compile(r"waiting\s+for\s*:?\s*([^:\n]+)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _age_days(last_modified: str | None, now: datetime) -> float:
    """Return age in days from an ISO timestamp. 0.0 if unparseable."""
    if not last_modified:
        return 0.0
    try:
        s = last_modified.rstrip("Z")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now_utc = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
        return max(0.0, (now_utc - dt).total_seconds() / 86400.0)
    except (ValueError, TypeError):
        return 0.0


def _reminder_attr(rem: Any, attr: str, default: Any = "") -> Any:
    """Safely read an attribute from a reminder object or dict."""
    if isinstance(rem, dict):
        return rem.get(attr, default)
    return getattr(rem, attr, default)


def _parse_delegate(notes: str) -> str | None:
    """Try to extract a delegate name from reminder notes."""
    if not notes:
        return None
    m = _DELEGATE_RE.search(notes)
    if m:
        return m.group(1).strip()
    return None


def _week_start_iso(now: datetime) -> str:
    """Return ISO date string (YYYY-MM-DD) for Monday of the current week."""
    day = now.astimezone(timezone.utc).date()
    monday = day - timedelta(days=day.weekday())
    return monday.isoformat()


def _friday_review_agenda_this_week(*, conn, now: datetime) -> dict | None:
    """Return the most recent 'review_agenda' question from this week, or None."""
    week_start = _week_start_iso(now)
    rows = conn.execute(
        """
        SELECT * FROM questions
        WHERE kind = 'review_agenda'
          AND dispatched_at >= ?
        ORDER BY dispatched_at DESC
        LIMIT 1
        """,
        (week_start,),
    ).fetchall()
    if rows:
        return dict(rows[0])
    return None


def _last_review_iso(*, conn) -> str | None:
    """Return ISO timestamp of the most recent completed review, or None."""
    row = conn.execute(
        "SELECT started_at FROM reviews ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    if row:
        return dict(row)["started_at"]
    return None


# ---------------------------------------------------------------------------
# Public: collect_snapshot
# ---------------------------------------------------------------------------


def collect_snapshot(*, rem_module=None, conn) -> dict:
    """Pure-ish: reads via rem_module.list_all() and state.db.

    Returns:
      {
        'inbox': [{rid, title, age_days}, ...],
        'waiting': [{rid, title, delegate, age_days}, ...],
        'projects': [{project_id, name, outcome, child_count, stalled: bool}, ...],
        'someday': [{rid, title, age_days}, ...],
        'next_actions_by_ctx': {'@home': N, '@calls': N, ...},
        'tickler_due_count': N,
        'last_review_iso': str | None,
      }
    """
    if rem_module is None:
        rem_module = _R

    now = _now_utc()

    # Fetch all reminders (incomplete ones)
    all_rems = rem_module.list_all()

    inbox: list[dict] = []
    waiting: list[dict] = []
    someday: list[dict] = []
    next_actions_by_ctx: dict[str, int] = {}

    for rem in all_rems:
        if _reminder_attr(rem, "completed", False):
            continue

        list_name = _reminder_attr(rem, "list", "")
        rid = _reminder_attr(rem, "id", "")
        title = _reminder_attr(rem, "name", "")
        notes = _reminder_attr(rem, "body", "") or ""
        last_modified = _reminder_attr(rem, "last_modified", "")
        age = _age_days(last_modified, now)

        if list_name == _INBOX_LIST:
            inbox.append({"rid": rid, "title": title, "age_days": age})

        elif list_name == _WAITING_LIST:
            delegate = _parse_delegate(notes)
            waiting.append({"rid": rid, "title": title, "delegate": delegate, "age_days": age})

        elif list_name == _SOMEDAY_LIST:
            someday.append({"rid": rid, "title": title, "age_days": age})

        elif list_name in _CONTEXT_LISTS:
            next_actions_by_ctx[list_name] = next_actions_by_ctx.get(list_name, 0) + 1

    # Projects from state.db
    projects_raw = conn.execute("SELECT * FROM projects").fetchall()
    projects: list[dict] = []
    for row in projects_raw:
        p = dict(row)
        pid = p["project_id"]
        # Count child next-actions
        child_count_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM items WHERE project = ? AND kind = 'next_action'",
            (pid,),
        ).fetchone()
        child_count = dict(child_count_row)["cnt"] if child_count_row else 0
        stalled = child_count == 0
        projects.append({
            "project_id": pid,
            "name": pid,  # project_id doubles as name; no separate name column
            "outcome": p.get("outcome", ""),
            "child_count": child_count,
            "stalled": stalled,
        })

    # Ticklers due today or overdue
    now_iso = now.isoformat()
    due_ticklers = state_mod.due_ticklers(conn, now_iso)
    tickler_due_count = len(due_ticklers)

    last_review = _last_review_iso(conn=conn)

    return {
        "inbox": inbox,
        "waiting": waiting,
        "projects": projects,
        "someday": someday,
        "next_actions_by_ctx": next_actions_by_ctx,
        "tickler_due_count": tickler_due_count,
        "last_review_iso": last_review,
    }


# ---------------------------------------------------------------------------
# Public: render_snapshot_md
# ---------------------------------------------------------------------------


def render_snapshot_md(snapshot: dict, *, now: datetime | None = None) -> str:
    """Pure function. Take a snapshot dict, return a markdown summary."""
    if now is None:
        now = _now_utc()

    date_str = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
    lines: list[str] = [
        f"# GTD Weekly Review — {date_str}",
        "",
    ]

    last_review = snapshot.get("last_review_iso")
    if last_review:
        lines += [f"_Last review: {last_review[:10]}_", ""]

    # Inbox
    inbox = snapshot.get("inbox", [])
    lines += [f"## Inbox ({len(inbox)} items)", ""]
    if inbox:
        for item in inbox:
            age = f"{item['age_days']:.0f}d" if item.get("age_days") else ""
            age_str = f" _{age}_" if age else ""
            lines.append(f"- {item['title']}{age_str}")
    else:
        lines.append("_Empty_")
    lines.append("")

    # Waiting For
    waiting = snapshot.get("waiting", [])
    lines += [f"## Waiting For ({len(waiting)} items)", ""]
    if waiting:
        for item in waiting:
            delegate = item.get("delegate")
            delegate_str = f" → {delegate}" if delegate else ""
            age = f"{item['age_days']:.0f}d" if item.get("age_days") else ""
            age_str = f" _{age}_" if age else ""
            lines.append(f"- {item['title']}{delegate_str}{age_str}")
    else:
        lines.append("_Empty_")
    lines.append("")

    # Projects
    projects = snapshot.get("projects", [])
    stalled = [p for p in projects if p.get("stalled")]
    lines += [f"## Projects ({len(projects)} total, {len(stalled)} stalled)", ""]
    if projects:
        for p in projects:
            stalled_flag = " ⚠ stalled" if p.get("stalled") else ""
            outcome = p.get("outcome", "")
            outcome_str = f": {outcome}" if outcome else ""
            lines.append(f"- **{p['name']}**{outcome_str}{stalled_flag} ({p['child_count']} NAs)")
    else:
        lines.append("_No projects_")
    lines.append("")

    # Someday/Maybe
    someday = snapshot.get("someday", [])
    lines += [f"## Someday ({len(someday)} items)", ""]
    if someday:
        for item in someday:
            age = f"{item['age_days']:.0f}d" if item.get("age_days") else ""
            age_str = f" _{age}_" if age else ""
            lines.append(f"- {item['title']}{age_str}")
    else:
        lines.append("_Empty_")
    lines.append("")

    # Next Actions by Context
    ctx_counts = snapshot.get("next_actions_by_ctx", {})
    lines += ["## Next Actions by Context", ""]
    if ctx_counts:
        for ctx, count in sorted(ctx_counts.items()):
            lines.append(f"- {ctx}: {count}")
    else:
        lines.append("_No next-actions found_")
    lines.append("")

    # Ticklers
    tickler_due = snapshot.get("tickler_due_count", 0)
    if tickler_due:
        lines += [f"## Ticklers Due", "", f"- {tickler_due} item(s) due or overdue", ""]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public: prepare
# ---------------------------------------------------------------------------


def prepare(
    snapshot_kind: str,
    *,
    conn,
    rem_module=None,
    qchannel_module=None,
    memory_dir: Path | None = None,
    log_dir: Path | None = None,
    dispatch_dryrun: bool = True,
    now: datetime | None = None,
) -> dict:
    """Capture a GTD snapshot, write it to disk, insert a reviews row, and
    optionally dispatch a Q.

    Args:
        snapshot_kind: 'friday_prep' | 'sunday_nudge'

    Returns:
        {'snapshot_path': Path, 'q_dispatched': bool, 'q_skipped_reason': str|None}
    """
    if rem_module is None:
        rem_module = _R
    if qchannel_module is None:
        qchannel_module = _Q_default
    if now is None:
        now = _now_utc()
    if memory_dir is None:
        memory_dir = DEFAULT_MEMORY_DIR

    # 1. Collect snapshot
    snapshot = collect_snapshot(rem_module=rem_module, conn=conn)

    # 2. Render markdown
    md = render_snapshot_md(snapshot, now=now)

    # 3. Write to memory/reviews/YYYY-MM-DD-<kind>.md
    date_str = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
    reviews_dir = memory_dir / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = reviews_dir / f"{date_str}-{snapshot_kind}.md"
    snapshot_path.write_text(md, encoding="utf-8")

    # 4. Insert into reviews table
    state_mod.insert_review(conn, kind=snapshot_kind, snapshot=snapshot)

    # 5. Decide whether to dispatch Q
    q_dispatched = False
    q_skipped_reason: str | None = None

    if snapshot_kind == "friday_prep":
        # Always dispatch on friday_prep
        result = qchannel_module.dispatch(
            conn=conn,
            rem_module=rem_module,
            kind="review_agenda",
            prompt="Weekly review ready — run /gtd:weekly-review when you're set",
            payload={"snapshot_path": str(snapshot_path)},
            dispatch_dryrun=dispatch_dryrun,
            now=now,
            log_dir=log_dir,
        )
        q_dispatched = result.status in ("dispatched", "dryrun", "queued_quiet")

    elif snapshot_kind == "sunday_nudge":
        # Only dispatch if friday Q was not acknowledged
        friday_q = _friday_review_agenda_this_week(conn=conn, now=now)

        if friday_q is None:
            # No friday Q this week → treat as not-acknowledged → dispatch
            pass  # fall through to dispatch
        elif friday_q["status"] == "answered":
            q_skipped_reason = "friday_acknowledged"
        # else: status is 'open', 'cancelled', 'dryrun', 'deferred' → dispatch

        if q_skipped_reason is None:
            result = qchannel_module.dispatch(
                conn=conn,
                rem_module=rem_module,
                kind="review_agenda",
                prompt="Sunday nudge: weekly review not done yet — run /gtd:weekly-review",
                payload={"snapshot_path": str(snapshot_path), "nudge": True},
                dispatch_dryrun=dispatch_dryrun,
                now=now,
                log_dir=log_dir,
            )
            q_dispatched = result.status in ("dispatched", "dryrun", "queued_quiet")

    return {
        "snapshot_path": snapshot_path,
        "q_dispatched": q_dispatched,
        "q_skipped_reason": q_skipped_reason,
    }


# ---------------------------------------------------------------------------
# Public: run_review (stub)
# ---------------------------------------------------------------------------


def run_review(*, rem_module=None, conn=None) -> dict:
    """Interactive weekly review stub.

    Returns the snapshot dict so chat-side UX can walk the user through:
      process inbox → waiting → projects → someday

    The order of review sections is:
      1. inbox
      2. waiting
      3. projects
      4. someday
    """
    if rem_module is None:
        rem_module = _R
    if conn is None:
        raise ValueError("conn is required for run_review")
    return collect_snapshot(rem_module=rem_module, conn=conn)
