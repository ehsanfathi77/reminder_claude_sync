"""
qchannel.py — phone-side Q&A protocol over the Reminders 'Questions' list.

Caps (load-bearing):
  q_max_open       = 3   — at most 3 unanswered Qs in the list at once
  q_max_per_day    = 8   — at most 8 dispatches per calendar day
  q_max_per_command = 1  — one Q per command invocation; bulk producers must
                          dispatch a single 'digest' Q with refs in payload

Bypass / carve-out:
  scheduled-nudge Qs (kind in SCHEDULED_NUDGE_KINDS) bypass q_max_open but
  count against q_max_per_day. Allowed transient open count: q_max_open + 2.

Quiet hours (default 22:00–08:00 local): non-urgent dispatches are queued
in state.questions with status='deferred' and released at 08:00 next day
(via tick()).

Dryrun mode (dispatch_dryrun=True, default for first 7 days):
  - DOES NOT create a Reminder.
  - Logs an entry to qchannel.jsonl with {dryrun: true, would_dispatch: {...}}.
  - DOES update state.db with status='dryrun' so /gtd:dryrun-report can read it.

Backoff for unanswered Qs:
  ttl_at = dispatched_at + 72h.
  At 72h: extend to +168h (status stays 'open').
  At 168h: status='cancelled'. Q-reminder marked complete in Reminders with
  notes appended '— Q cancelled by engine after 168h no answer'.

Circuit breaker:
  If >10 reminders appear in Inbox within a 60-second window (per state.events
  count), skip per-item auto-clarify dispatches AND emit a single digest Q
  ('I see N new items — run /gtd:clarify when convenient').

Reminder-side shape:
  Title: a short prompt (≤80 chars), e.g. 'Clarify: NYU Credit Union account'
  Notes:
    --- gtd ---
    id: <ulid>          (gtd_id of the source item, if applicable)
    kind: question
    created: <iso>
    --- end ---
    <!-- qmeta -->
    qid: <ulid>
    qkind: clarify | review_agenda | invariant | health_alert | digest
    ref_rid: <reminder-id> | null
    payload: <json string, ≤256 chars>
    <!-- /qmeta -->

  Parser order: gtd-fence first, then qmeta from the prose remainder.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import gtd.engine.state as state_mod
from gtd.engine.notes_metadata import serialize_metadata
from gtd.engine.observability import log as obs_log
from gtd.engine.write_fence import assert_writable

# Import reminders module as the default rem_module.
# Tests inject a stub via the rem_module parameter.
try:
    import bin.lib.reminders as _R  # type: ignore
except ImportError:
    _R = None  # type: ignore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEDULED_NUDGE_KINDS: frozenset[str] = frozenset({
    "review_agenda", "sunday_nudge", "health_alert", "digest_review",
})

DEFAULT_QUIET_HOURS = (22, 8)  # 22:00 → 08:00

_Q_MAX_OPEN = 3
_Q_MAX_PER_DAY = 8
_QUESTIONS_LIST = "Questions"
_CIRCUIT_BREAKER_WINDOW_SECS = 60
_CIRCUIT_BREAKER_THRESHOLD = 10  # >10 means active
_TTL_FIRST_H = 72
_TTL_SECOND_H = 168

_QMETA_OPEN = "<!-- qmeta -->"
_QMETA_CLOSE = "<!-- /qmeta -->"
_QMETA_RE = re.compile(
    r"<!-- qmeta -->(.*?)<!-- /qmeta -->",
    re.DOTALL,
)

# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass
class DispatchResult:
    qid: str | None
    status: str  # 'dispatched' | 'dryrun' | 'queued_quiet' | 'cap_open' | 'cap_per_day' | 'cap_per_command' | 'circuit_breaker'
    reason: str | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _in_quiet_hours(now: datetime, quiet_start: int, quiet_end: int) -> bool:
    """Return True if now's *local* hour is in the quiet window (wrap-aware).

    quiet_hours represents the user's clock-face night (e.g., 22:00–08:00 local).
    A tz-aware `now` is converted to local time before the comparison; a naive
    `now` is treated as already-local.

    E.g. quiet_start=22, quiet_end=8 → [22, 23, 0, 1, ..., 7] is quiet.
    """
    h = now.astimezone().hour if now.tzinfo is not None else now.hour
    if quiet_start < quiet_end:
        # Simple range: e.g. 2:00–8:00
        return quiet_start <= h < quiet_end
    else:
        # Wrapping range: e.g. 22:00–08:00
        return h >= quiet_start or h < quiet_end


def _build_qmeta_block(
    qid: str,
    qkind: str,
    ref_rid: str | None,
    payload: dict | None,
) -> str:
    payload_str = json.dumps(payload or {})
    if len(payload_str) > 256:
        payload_str = payload_str[:253] + "..."
    lines = [
        _QMETA_OPEN,
        f"qid: {qid}",
        f"qkind: {qkind}",
        f"ref_rid: {ref_rid or 'null'}",
        f"payload: {payload_str}",
        _QMETA_CLOSE,
    ]
    return "\n".join(lines)


def _parse_qmeta(notes: str) -> dict | None:
    """Extract qmeta block from notes, returning dict or None."""
    m = _QMETA_RE.search(notes)
    if m is None:
        return None
    result: dict[str, Any] = {}
    for line in m.group(1).strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if ": " in line:
            k, _, v = line.partition(": ")
            result[k.strip()] = v.strip()
        elif line.endswith(":"):
            result[line[:-1].strip()] = ""
    return result if result else None


def _build_reminder_notes(
    qid: str,
    qkind: str,
    ref_rid: str | None,
    payload: dict | None,
    gtd_id: str | None,
    now: datetime,
) -> str:
    """Build the full notes string for a Q-reminder."""
    meta: dict[str, str] = {"kind": "question", "created": _iso(now)}
    if gtd_id:
        meta["id"] = gtd_id
    gtd_prose = _build_qmeta_block(qid, qkind, ref_rid, payload)
    return serialize_metadata(meta, gtd_prose)


def _log_dispatch(
    *,
    log_dir: Path | None,
    kind: str,
    status: str,
    dryrun: bool,
    qid: str | None,
    open_cnt: int,
    per_day_cnt: int,
    reason: str | None = None,
    extra: dict | None = None,
) -> None:
    fields: dict[str, Any] = {
        "kind": kind,
        "status": status,
        "dryrun": dryrun,
        "qid": qid,
        "open_count": open_cnt,
        "per_day_count": per_day_cnt,
    }
    if reason:
        fields["reason"] = reason
    if extra:
        fields.update(extra)
    kwargs = {"log_dir": log_dir} if log_dir is not None else {}
    obs_log("qchannel", **fields, **kwargs)


# ---------------------------------------------------------------------------
# Public cap-check helpers
# ---------------------------------------------------------------------------


def open_count(*, conn, exclude_scheduled_nudges: bool = True) -> int:
    """For cap checks. Excludes scheduled-nudge kinds by default."""
    rows = conn.execute(
        "SELECT kind FROM questions WHERE status IN ('open', 'deferred')"
    ).fetchall()
    count = 0
    for row in rows:
        kind = dict(row)["kind"] if hasattr(row, "keys") else row[0]
        if exclude_scheduled_nudges and kind in SCHEDULED_NUDGE_KINDS:
            continue
        count += 1
    return count


def per_day_count(*, conn, day_iso: str | None = None) -> int:
    """Count of dispatches today (incl. dryrun).

    day_iso: YYYY-MM-DD (UTC). Defaults to today UTC.
    """
    if day_iso is None:
        day_iso = _now_utc().strftime("%Y-%m-%d")
    # dispatched_at is stored as ISO; prefix-match on YYYY-MM-DD
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM questions WHERE dispatched_at LIKE ? AND status NOT IN ('archived')",
        (f"{day_iso}%",),
    ).fetchone()
    cnt = dict(row)["cnt"] if hasattr(row, "keys") else row[0]
    return cnt


def circuit_breaker_active(*, conn, now: datetime | None = None) -> bool:
    """True if >10 inbox-arrival events in last 60s."""
    if now is None:
        now = _now_utc()
    since = now - timedelta(seconds=_CIRCUIT_BREAKER_WINDOW_SECS)
    count = state_mod.count_events_in_window(conn, "inbox_arrival", _iso(since))
    return count > _CIRCUIT_BREAKER_THRESHOLD


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

# Track per-invocation dispatch within a single process call.
# Maps invocation_id → set of qkinds dispatched.
_invocation_registry: dict[str, set[str]] = {}


def dispatch(
    *,
    conn,
    rem_module=None,
    kind: str,
    prompt: str,
    payload: dict | None = None,
    ref_rid: str | None = None,
    digest: bool = False,
    invocation_id: str | None = None,
    dispatch_dryrun: bool = True,
    quiet_hours: tuple[int, int] = DEFAULT_QUIET_HOURS,
    now: datetime | None = None,
    log_dir: Path | None = None,
    gtd_id: str | None = None,
) -> DispatchResult:
    """Try to dispatch a Q-reminder. Returns DispatchResult."""
    if rem_module is None:
        rem_module = _R

    if now is None:
        now = _now_utc()

    is_scheduled = kind in SCHEDULED_NUDGE_KINDS

    # ---- Cap: per-command dedup (invocation_id already dispatched) ----------
    if not digest and invocation_id is not None:
        already = _invocation_registry.get(invocation_id, set())
        if already:  # any dispatch in this invocation
            _log_dispatch(
                log_dir=log_dir,
                kind=kind,
                status="cap_per_command",
                dryrun=dispatch_dryrun,
                qid=None,
                open_cnt=open_count(conn=conn),
                per_day_cnt=per_day_count(conn=conn),
                reason=f"invocation_id={invocation_id} already dispatched",
            )
            return DispatchResult(qid=None, status="cap_per_command", reason="one Q per command invocation")

    # ---- Cap: circuit breaker (non-scheduled only) --------------------------
    if not is_scheduled and circuit_breaker_active(conn=conn, now=now):
        _log_dispatch(
            log_dir=log_dir,
            kind=kind,
            status="circuit_breaker",
            dryrun=dispatch_dryrun,
            qid=None,
            open_cnt=open_count(conn=conn),
            per_day_cnt=per_day_count(conn=conn),
            reason="circuit breaker active",
        )
        return DispatchResult(qid=None, status="circuit_breaker", reason="circuit breaker: >10 inbox arrivals in 60s")

    # ---- Cap: per-day -------------------------------------------------------
    day_iso = now.strftime("%Y-%m-%d")
    pdc = per_day_count(conn=conn, day_iso=day_iso)
    if pdc >= _Q_MAX_PER_DAY:
        _log_dispatch(
            log_dir=log_dir,
            kind=kind,
            status="cap_per_day",
            dryrun=dispatch_dryrun,
            qid=None,
            open_cnt=open_count(conn=conn),
            per_day_cnt=pdc,
            reason=f"per_day_count={pdc} >= {_Q_MAX_PER_DAY}",
        )
        return DispatchResult(qid=None, status="cap_per_day", reason=f"daily cap of {_Q_MAX_PER_DAY} reached")

    # ---- Cap: open (non-scheduled only) -------------------------------------
    if not is_scheduled:
        oc = open_count(conn=conn, exclude_scheduled_nudges=True)
        if oc >= _Q_MAX_OPEN:
            _log_dispatch(
                log_dir=log_dir,
                kind=kind,
                status="cap_open",
                dryrun=dispatch_dryrun,
                qid=None,
                open_cnt=oc,
                per_day_cnt=pdc,
                reason=f"open_count={oc} >= {_Q_MAX_OPEN}",
            )
            return DispatchResult(qid=None, status="cap_open", reason=f"open cap of {_Q_MAX_OPEN} reached")

    # ---- Quiet hours check --------------------------------------------------
    quiet_start, quiet_end = quiet_hours
    if _in_quiet_hours(now, quiet_start, quiet_end):
        qid = state_mod.insert_question(
            conn,
            kind=kind,
            ref_rid=ref_rid,
            dispatched_at=_iso(now),
            ttl_at=None,
            status="deferred",
            payload_json=payload or {},
        )
        _log_dispatch(
            log_dir=log_dir,
            kind=kind,
            status="queued_quiet",
            dryrun=dispatch_dryrun,
            qid=qid,
            open_cnt=open_count(conn=conn),
            per_day_cnt=per_day_count(conn=conn, day_iso=day_iso),
        )
        if invocation_id is not None:
            _invocation_registry.setdefault(invocation_id, set()).add(kind)
        return DispatchResult(qid=qid, status="queued_quiet", reason="quiet hours active")

    # ---- Truncate prompt to 80 chars ----------------------------------------
    title = prompt[:80]

    # ---- TTL ----------------------------------------------------------------
    ttl_at = _iso(now + timedelta(hours=_TTL_FIRST_H))

    # ---- Dryrun path --------------------------------------------------------
    if dispatch_dryrun:
        qid = state_mod.insert_question(
            conn,
            kind=kind,
            ref_rid=ref_rid,
            dispatched_at=_iso(now),
            ttl_at=ttl_at,
            status="dryrun",
            payload_json=payload or {},
        )
        _log_dispatch(
            log_dir=log_dir,
            kind=kind,
            status="dryrun",
            dryrun=True,
            qid=qid,
            open_cnt=open_count(conn=conn),
            per_day_cnt=per_day_count(conn=conn, day_iso=day_iso),
            extra={"would_dispatch": {"title": title, "payload": payload}},
        )
        if invocation_id is not None:
            _invocation_registry.setdefault(invocation_id, set()).add(kind)
        return DispatchResult(qid=qid, status="dryrun")

    # ---- Live dispatch -------------------------------------------------------
    notes = _build_reminder_notes(
        qid="PENDING",  # placeholder; overwritten after insert
        qkind=kind,
        ref_rid=ref_rid,
        payload=payload,
        gtd_id=gtd_id,
        now=now,
    )

    # write_fence: only allow Questions list
    assert_writable("PENDING_RID", _QUESTIONS_LIST)

    rid = rem_module.create(list_name=_QUESTIONS_LIST, name=title, notes=notes)

    # Re-build notes now that we have rid; update reminder with correct qmeta.
    # First insert into state to get qid.
    qid = state_mod.insert_question(
        conn,
        kind=kind,
        ref_rid=ref_rid,
        dispatched_at=_iso(now),
        ttl_at=ttl_at,
        status="open",
        payload_json=payload or {},
    )

    # Rebuild notes with real qid and update reminder.
    real_notes = _build_reminder_notes(
        qid=qid,
        qkind=kind,
        ref_rid=ref_rid,
        payload=payload,
        gtd_id=gtd_id,
        now=now,
    )
    try:
        rem_module.update_notes(rid, _QUESTIONS_LIST, real_notes)
    except Exception:
        pass  # best-effort; state is already recorded

    oc = open_count(conn=conn)
    pdc2 = per_day_count(conn=conn, day_iso=day_iso)
    _log_dispatch(
        log_dir=log_dir,
        kind=kind,
        status="dispatched",
        dryrun=False,
        qid=qid,
        open_cnt=oc,
        per_day_cnt=pdc2,
        extra={"rid": rid},
    )

    if invocation_id is not None:
        _invocation_registry.setdefault(invocation_id, set()).add(kind)

    return DispatchResult(qid=qid, status="dispatched")


# ---------------------------------------------------------------------------
# poll
# ---------------------------------------------------------------------------


def poll(
    *,
    conn,
    rem_module=None,
    now: datetime | None = None,
    log_dir: Path | None = None,
) -> list[dict]:
    """Read all reminders in 'Questions' list, parse qmeta, advance state machine.

    For each completed reminder with status='open' in state.db:
      - parse Reply: <text> from notes, OR treat mark-complete as 'no specific reply'
      - emit a 'q_answered' event with {qid, qkind, reply_text, raw_completion}
      - update state to 'answered' (caller modules consume from there)
    For each open reminder past ttl_at: extend or cancel per backoff.
    Returns list of newly-answered question dicts.
    """
    if rem_module is None:
        rem_module = _R
    if now is None:
        now = _now_utc()

    # Fetch all reminders (open + recently completed) from Questions list.
    all_rems = rem_module.list_all()
    q_rems = [r for r in all_rems if r.list == _QUESTIONS_LIST]

    # Build lookup: qid → reminder
    qid_to_rem: dict[str, Any] = {}
    for rem in q_rems:
        meta = _parse_qmeta(rem.body)
        if meta and "qid" in meta:
            qid_to_rem[meta["qid"]] = (rem, meta)

    # Fetch all open questions from state.
    open_qs = state_mod.open_questions(conn)
    # Also fetch deferred that may have matured.
    deferred_rows = conn.execute(
        "SELECT * FROM questions WHERE status = 'deferred'"
    ).fetchall()
    deferred_qs = [dict(r) for r in deferred_rows]

    answered: list[dict] = []

    # ---- Process open questions -----
    for q in open_qs:
        qid = q["qid"]

        # Check if the corresponding reminder is completed.
        if qid in qid_to_rem:
            rem, meta = qid_to_rem[qid]
            if rem.completed:
                # Parse Reply: <text> from notes.
                reply_text = _extract_reply(rem.body)
                state_mod.update_question_status(conn, qid, "answered")
                state_mod.insert_event(
                    conn,
                    ts=_iso(now),
                    stream="qchannel",
                    payload={
                        "event": "q_answered",
                        "qid": qid,
                        "qkind": meta.get("qkind", q["kind"]),
                        "reply_text": reply_text,
                        "raw_completion": rem.completion_date,
                    },
                )
                answered.append({
                    "qid": qid,
                    "qkind": meta.get("qkind", q["kind"]),
                    "reply_text": reply_text,
                    "raw_completion": rem.completion_date,
                })
                continue

        # ---- Backoff check ----
        ttl_at_str = q.get("ttl_at")
        if not ttl_at_str:
            continue

        try:
            ttl_dt = datetime.fromisoformat(ttl_at_str)
            if ttl_dt.tzinfo is None:
                ttl_dt = ttl_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue

        if now < ttl_dt:
            continue  # not yet expired

        dispatched_at_str = q.get("dispatched_at", "")
        try:
            dispatched_dt = datetime.fromisoformat(dispatched_at_str)
            if dispatched_dt.tzinfo is None:
                dispatched_dt = dispatched_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            dispatched_dt = now - timedelta(hours=_TTL_FIRST_H)

        age_h = (now - dispatched_dt).total_seconds() / 3600

        if age_h >= _TTL_SECOND_H:
            # Cancel
            state_mod.update_question_status(conn, qid, "cancelled")
            # Also update the Reminder if found.
            if qid in qid_to_rem:
                rem, _ = qid_to_rem[qid]
                cancel_note = rem.body + "\n— Q cancelled by engine after 168h no answer"
                try:
                    rem_module.update_notes(rem.id, _QUESTIONS_LIST, cancel_note)
                    rem_module.set_complete(rem.id, _QUESTIONS_LIST, True)
                except Exception:
                    pass
            obs_log("qchannel", qid=qid, event="q_cancelled", age_h=age_h,
                    **({} if log_dir is None else {"log_dir": log_dir}))
        else:
            # First miss: extend TTL to +168h from dispatch
            new_ttl = _iso(dispatched_dt + timedelta(hours=_TTL_SECOND_H))
            conn.execute(
                "UPDATE questions SET ttl_at = ? WHERE qid = ?",
                (new_ttl, qid),
            )
            conn.commit()
            obs_log("qchannel", qid=qid, event="q_ttl_extended", new_ttl=new_ttl,
                    **({} if log_dir is None else {"log_dir": log_dir}))

    return answered


# ---------------------------------------------------------------------------
# archive
# ---------------------------------------------------------------------------


def archive(*, conn, qid: str, rem_module=None) -> None:
    """Mark Q as 'archived' in state and delete from Questions list."""
    if rem_module is None:
        rem_module = _R

    # Find the reminder in Questions list.
    all_rems = rem_module.list_all()
    for rem in all_rems:
        if rem.list != _QUESTIONS_LIST:
            continue
        meta = _parse_qmeta(rem.body)
        if meta and meta.get("qid") == qid:
            try:
                rem_module.delete(rem.id, _QUESTIONS_LIST)
            except Exception:
                pass
            break

    state_mod.update_question_status(conn, qid, "archived")


# ---------------------------------------------------------------------------
# tick — release deferred Qs, process backoff
# ---------------------------------------------------------------------------


def tick(
    *,
    conn,
    rem_module=None,
    now: datetime | None = None,
    quiet_hours: tuple[int, int] = DEFAULT_QUIET_HOURS,
    dispatch_dryrun: bool = True,
    log_dir: Path | None = None,
) -> None:
    """Release deferred Qs when quiet hours end; process backoff via poll()."""
    if rem_module is None:
        rem_module = _R
    if now is None:
        now = _now_utc()

    quiet_start, quiet_end = quiet_hours
    if _in_quiet_hours(now, quiet_start, quiet_end):
        return  # still quiet; nothing to release

    # Release deferred Qs.
    deferred_rows = conn.execute(
        "SELECT * FROM questions WHERE status = 'deferred'"
    ).fetchall()
    for row in deferred_rows:
        q = dict(row)
        qid = q["qid"]
        kind = q["kind"]
        ref_rid = q.get("ref_rid")
        payload_raw = q.get("payload_json", "{}")
        try:
            payload = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
        except (json.JSONDecodeError, TypeError):
            payload = {}

        # Re-dispatch.
        dispatch(
            conn=conn,
            rem_module=rem_module,
            kind=kind,
            prompt=payload.get("prompt", f"Q: {kind}"),
            payload=payload,
            ref_rid=ref_rid,
            dispatch_dryrun=dispatch_dryrun,
            quiet_hours=quiet_hours,
            now=now,
            log_dir=log_dir,
        )
        # Remove deferred record.
        conn.execute("DELETE FROM questions WHERE qid = ?", (qid,))
        conn.commit()

    # Run backoff via poll.
    poll(conn=conn, rem_module=rem_module, now=now, log_dir=log_dir)


# ---------------------------------------------------------------------------
# Internal: reply extraction
# ---------------------------------------------------------------------------


def _extract_reply(notes: str) -> str | None:
    """Extract 'Reply: <text>' from notes, or return None."""
    if not notes:
        return None
    for line in notes.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("reply:"):
            return stripped[len("reply:"):].strip()
    return None
