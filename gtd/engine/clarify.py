"""
clarify.py — Inbox processing state machine + rules-only auto-clarify.

V1 uses RULES, not an LLM. The rules:
  R1. 2-minute rule: title matches /^(call|email|text|reply|message|ping)/i
      AND no time estimate >2m → auto-classify as next-action @calls / @computer
      depending on title verb (call/text/ping → @calls; email/reply/message → @computer).
  R2. Delegate detection: title matches /\\b(ask|tell|remind)\\s+(\\w+)/i AND
      that person matches a name in memory/people/*.md → mark waiting-for, delegate=name.
  R3. Reference: title matches /^(read|reference|fyi|note|article|link)/i AND no verb of
      action → move to Reference list (if exists; else Someday).
  R4. Books: title contains /\\bbook\\b/ or matches a known title pattern → Someday/Books.
  R5. Date-anchored: title contains a parseable future date (e.g. "next Tuesday") →
      tickler with that date as release_at.
  Else: NEEDS_USER → Q_DISPATCHED.

Rules are deliberately conservative. Better to dispatch a Q than auto-misroute.
Goal: ≥70% accuracy on a 50-item labeled corpus (test fixture).

State machine:
  NEW → CLAUDE_ASSESSING → AUTO_CLARIFIED | NEEDS_USER → Q_DISPATCHED → Q_ANSWERED
      → CLAUDE_APPLYING → DONE
                        ↘ Q_EXPIRED → NEEDS_USER (re-dispatch with backoff)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from gtd.engine.observability import log as obs_log
from gtd.engine.write_fence import assert_writable

# ---------------------------------------------------------------------------
# Try importing default modules; tests inject stubs via parameters.
# ---------------------------------------------------------------------------

try:
    import bin.lib.reminders as _R  # type: ignore
except ImportError:
    _R = None  # type: ignore

try:
    import gtd.engine.qchannel as _Q  # type: ignore
except ImportError:
    _Q = None  # type: ignore


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# R1 verb → context list mapping
_TWO_MIN_CALLS_VERBS = frozenset({"call", "text", "ping"})
_TWO_MIN_COMPUTER_VERBS = frozenset({"email", "reply", "message"})
_TWO_MIN_RE = re.compile(
    r"^(call|email|text|reply|message|ping)\b",
    re.IGNORECASE,
)

# R2 delegation
_DELEGATE_RE = re.compile(
    r"\b(ask|tell|remind)\s+(\w+)\b",
    re.IGNORECASE,
)

# R3 reference
_REFERENCE_RE = re.compile(
    r"^(read|reference|fyi|note|article|link)\b",
    re.IGNORECASE,
)
# Action verbs that disqualify R3 (the item has a clear action component).
# We deliberately exclude 'email', 'review', 'read', 'reply' here because those
# appear in reference-style titles like "fyi email from…" or "note: review of…".
_ACTION_VERBS_RE = re.compile(
    r"\b(send|write|call|text|buy|order|schedule|plan|make|do|fix|create|build|submit|register)\b",
    re.IGNORECASE,
)

# R4 books
_BOOK_RE = re.compile(r"\bbook\b", re.IGNORECASE)

# R5 date anchors
_WEEKDAY_NAMES = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)
_DATE_PATTERNS = [
    re.compile(r"\bnext\s+(" + "|".join(_WEEKDAY_NAMES) + r")\b", re.IGNORECASE),
    re.compile(r"\bthis\s+(" + "|".join(_WEEKDAY_NAMES) + r")\b", re.IGNORECASE),
    re.compile(r"\btomorrow\b", re.IGNORECASE),
    re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"),
    re.compile(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}\b", re.IGNORECASE),
]

# Weekday name → offset from Monday (weekday() returns 0=Mon)
_WEEKDAY_OFFSETS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

# Default destination lists
_WAITING_FOR_LIST = "Waiting For"
_SOMEDAY_LIST = "Someday"
_REFERENCE_LIST = "Someday"  # no separate Reference list in managed set; use Someday
_TICKLER_LIST = "Tickler"


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass
class ClarifyDecision:
    kind: str  # 'auto_next_action' | 'auto_waiting' | 'auto_reference' | 'auto_someday' | 'auto_tickler' | 'needs_user'
    target_list: str | None        # destination Reminders list (if auto)
    delegate: str | None = None    # for waiting
    release_at: str | None = None  # for tickler (ISO date string)
    reasoning: str = ""            # which rule fired


# ---------------------------------------------------------------------------
# People-memory loader
# ---------------------------------------------------------------------------


def _load_known_people(memory_dir: Path | None) -> set[str]:
    """Return a set of lowercase first names from memory/people/*.md files."""
    names: set[str] = set()
    if memory_dir is None:
        return names
    people_dir = memory_dir / "people"
    if not people_dir.is_dir():
        return names
    for md_file in people_dir.glob("*.md"):
        # Filename is often firstname-lastname.md or just firstname.md
        stem = md_file.stem  # e.g. "dan-baker" or "michael-connelly"
        parts = stem.split("-")
        if parts:
            names.add(parts[0].lower())
    return names


# ---------------------------------------------------------------------------
# Date parsing helpers
# ---------------------------------------------------------------------------


def _next_weekday_date(weekday_name: str, now: datetime) -> str:
    """Return ISO date string for the next occurrence of the named weekday."""
    target = _WEEKDAY_OFFSETS.get(weekday_name.lower())
    if target is None:
        return ""
    today_wd = now.weekday()
    days_ahead = (target - today_wd) % 7
    if days_ahead == 0:
        days_ahead = 7  # 'next Monday' when today is Monday → next week
    dt = now + timedelta(days=days_ahead)
    return dt.strftime("%Y-%m-%d")


def _parse_date_hint(title: str, now: datetime) -> str | None:
    """Try to extract a future date from the title. Return ISO date or None."""
    # "tomorrow"
    if re.search(r"\btomorrow\b", title, re.IGNORECASE):
        return (now + timedelta(days=1)).strftime("%Y-%m-%d")

    # "next <weekday>" or "this <weekday>"
    m = re.search(
        r"\b(next|this)\s+(" + "|".join(_WEEKDAY_NAMES) + r")\b",
        title,
        re.IGNORECASE,
    )
    if m:
        weekday_name = m.group(2).lower()
        return _next_weekday_date(weekday_name, now)

    # MM/DD or MM/DD/YY or MM/DD/YYYY
    m2 = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", title)
    if m2:
        month, day = int(m2.group(1)), int(m2.group(2))
        year_raw = m2.group(3)
        year = now.year
        if year_raw:
            y = int(year_raw)
            year = y if y > 99 else 2000 + y
        try:
            dt = datetime(year, month, day)
            if dt.date() >= now.date():
                return dt.strftime("%Y-%m-%d")
            # Past date in current year → assume next year
            dt = datetime(year + 1, month, day)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    # "Jan 15", "April 3", etc.
    m3 = re.search(
        r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
        r"\s+(\d{1,2})\b",
        title,
        re.IGNORECASE,
    )
    if m3:
        month_abbr = m3.group(1)[:3].lower()
        month_map = {
            "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
            "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        }
        month = month_map.get(month_abbr)
        day = int(m3.group(2))
        if month:
            try:
                dt = datetime(now.year, month, day)
                if dt.date() >= now.date():
                    return dt.strftime("%Y-%m-%d")
                dt = datetime(now.year + 1, month, day)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                pass

    return None


# ---------------------------------------------------------------------------
# Rule implementations
# ---------------------------------------------------------------------------


def _rule_r1_two_minute(title: str) -> ClarifyDecision | None:
    """R1: 2-minute rule — call/text/ping → @calls; email/reply/message → @computer."""
    m = _TWO_MIN_RE.match(title.strip())
    if not m:
        return None
    verb = m.group(1).lower()
    if verb in _TWO_MIN_CALLS_VERBS:
        return ClarifyDecision(
            kind="auto_next_action",
            target_list="@calls",
            reasoning=f"R1: 2-min rule, verb={verb!r} → @calls",
        )
    if verb in _TWO_MIN_COMPUTER_VERBS:
        return ClarifyDecision(
            kind="auto_next_action",
            target_list="@computer",
            reasoning=f"R1: 2-min rule, verb={verb!r} → @computer",
        )
    return None


def _rule_r2_delegate(title: str, known_people: set[str]) -> ClarifyDecision | None:
    """R2: Delegate detection — ask/tell/remind <name> where name is in people memory."""
    m = _DELEGATE_RE.search(title)
    if not m:
        return None
    name = m.group(2).lower()
    if name not in known_people:
        return None
    return ClarifyDecision(
        kind="auto_waiting",
        target_list=_WAITING_FOR_LIST,
        delegate=m.group(2),  # preserve original capitalisation
        reasoning=f"R2: delegate detected, name={m.group(2)!r}",
    )


def _rule_r3_reference(title: str) -> ClarifyDecision | None:
    """R3: Reference — read/fyi/note/article/link with no action verb."""
    if not _REFERENCE_RE.match(title.strip()):
        return None
    # If the title also contains a strong action verb, defer to user
    if _ACTION_VERBS_RE.search(title):
        return None
    return ClarifyDecision(
        kind="auto_reference",
        target_list=_REFERENCE_LIST,
        reasoning="R3: reference item",
    )


def _rule_r4_book(title: str) -> ClarifyDecision | None:
    """R4: Books — title contains 'book' or title starts with a known book-like prefix."""
    if _BOOK_RE.search(title):
        return ClarifyDecision(
            kind="auto_someday",
            target_list=_SOMEDAY_LIST,
            reasoning="R4: book detected",
        )
    return None


def _rule_r5_date_anchored(title: str, now: datetime) -> ClarifyDecision | None:
    """R5: Date-anchored — title contains a parseable future date."""
    release_at = _parse_date_hint(title, now)
    if release_at is None:
        return None
    return ClarifyDecision(
        kind="auto_tickler",
        target_list=_TICKLER_LIST,
        release_at=release_at,
        reasoning=f"R5: date-anchored, release_at={release_at}",
    )


# ---------------------------------------------------------------------------
# Public API: auto_clarify
# ---------------------------------------------------------------------------


def auto_clarify(
    reminder: dict,
    *,
    memory_dir: Path | None = None,
    now: datetime | None = None,
) -> ClarifyDecision:
    """Pure function — no I/O. Apply rules R1-R5 in order. Return first match or NEEDS_USER."""
    title: str = reminder.get("name", "") or reminder.get("title", "") or ""
    if now is None:
        now = datetime.now(timezone.utc)

    known_people = _load_known_people(memory_dir)

    # R1
    decision = _rule_r1_two_minute(title)
    if decision is not None:
        return decision

    # R2
    decision = _rule_r2_delegate(title, known_people)
    if decision is not None:
        return decision

    # R3
    decision = _rule_r3_reference(title)
    if decision is not None:
        return decision

    # R4
    decision = _rule_r4_book(title)
    if decision is not None:
        return decision

    # R5
    decision = _rule_r5_date_anchored(title, now)
    if decision is not None:
        return decision

    # Fallback
    return ClarifyDecision(
        kind="needs_user",
        target_list=None,
        reasoning="no rule matched",
    )


# ---------------------------------------------------------------------------
# Public API: apply_decision
# ---------------------------------------------------------------------------


def apply_decision(
    decision: ClarifyDecision,
    reminder: dict,
    *,
    conn,
    rem_module=None,
    log_dir: Path | None = None,
    now: datetime | None = None,
) -> None:
    """Move/update reminder per decision. Updates state.db. Logs to clarify.jsonl.
    Calls write_fence.assert_writable on every move."""
    if rem_module is None:
        rem_module = _R
    if now is None:
        now = datetime.now(timezone.utc)

    rid = reminder.get("id", "")
    name = reminder.get("name", "") or reminder.get("title", "") or ""
    target_list = decision.target_list

    if decision.kind == "needs_user":
        # Nothing to move; caller handles dispatch
        return

    # Write-fence check before every move
    if target_list:
        assert_writable(rid, target_list)

    # Move via reminders module
    if target_list and rem_module is not None:
        rem_module.move_to_list(rid, target_list)

    # For ticklers, set a due date / release_at annotation
    if decision.kind == "auto_tickler" and decision.release_at and rem_module is not None:
        try:
            rem_module.update_field(rid, "dueDate", decision.release_at)
        except Exception:
            pass  # best-effort; tickler date is advisory

    # For delegations, annotate notes with delegate name
    if decision.kind == "auto_waiting" and decision.delegate and rem_module is not None:
        try:
            existing_notes = reminder.get("body", "") or ""
            new_notes = f"delegate: {decision.delegate}\n{existing_notes}".strip()
            rem_module.update_notes(rid, target_list or _WAITING_FOR_LIST, new_notes)
        except Exception:
            pass

    # Update state.db item kind to reflect new classification
    from gtd.engine import state as state_mod
    item = state_mod.get_item_by_rid(conn, rid)
    if item is not None:
        kind_map = {
            "auto_next_action": "next_action",
            "auto_waiting": "waiting_for",
            "auto_reference": "reference",
            "auto_someday": "someday",
            "auto_tickler": "tickler",
        }
        new_kind = kind_map.get(decision.kind, "unclarified")
        ctx = None
        if target_list and target_list.startswith("@"):
            ctx = target_list
        conn.execute(
            "UPDATE items SET kind = ?, list = ?, ctx = ? WHERE rid = ?",
            (new_kind, target_list, ctx, rid),
        )
        conn.commit()

    # Log
    log_fields: dict[str, Any] = {
        "op": "apply_decision",
        "rid": rid,
        "name": name,
        "decision_kind": decision.kind,
        "target_list": target_list,
        "reasoning": decision.reasoning,
    }
    if decision.delegate:
        log_fields["delegate"] = decision.delegate
    if decision.release_at:
        log_fields["release_at"] = decision.release_at
    obs_log(
        "clarify",
        **log_fields,
        **({"log_dir": log_dir} if log_dir is not None else {}),
    )


# ---------------------------------------------------------------------------
# Public API: process_inbox
# ---------------------------------------------------------------------------


def process_inbox(
    *,
    conn,
    rem_module=None,
    memory_dir: Path | None = None,
    log_dir: Path | None = None,
    qchannel_module=None,
    dispatch_dryrun: bool = True,
    now: datetime | None = None,
) -> dict:
    """Walk current Inbox via R.list_all. For each unclarified reminder:
       1. auto_clarify → if decision != needs_user → apply_decision, mark DONE in state
       2. Else → qchannel_module.dispatch(kind="clarify", prompt=..., ref_rid=...,
                                          payload={suggestions: [...]})
    Returns {'auto': N, 'dispatched': N, 'skipped': N}. Honors circuit_breaker
    (if active, skip per-item Q dispatch and emit one digest Q)."""
    if rem_module is None:
        rem_module = _R
    if qchannel_module is None:
        qchannel_module = _Q
    if now is None:
        now = datetime.now(timezone.utc)

    from gtd.engine import state as state_mod

    counters = {"auto": 0, "dispatched": 0, "skipped": 0}

    # Fetch all inbox reminders
    all_rems = rem_module.list_all()
    inbox_items = [r for r in all_rems if getattr(r, "list", None) == "Inbox"]

    # Determine if circuit breaker is active
    cb_active = False
    if qchannel_module is not None and hasattr(qchannel_module, "circuit_breaker_active"):
        cb_active = qchannel_module.circuit_breaker_active(conn=conn, now=now)

    needs_user_items: list[dict] = []

    for rem in inbox_items:
        rid = getattr(rem, "id", "")
        name = getattr(rem, "name", "") or getattr(rem, "title", "") or ""
        body = getattr(rem, "body", "") or ""

        # Check if already clarified in state
        existing = state_mod.get_item_by_rid(conn, rid)
        if existing and existing.get("kind", "unclarified") != "unclarified":
            counters["skipped"] += 1
            continue

        reminder_dict = {"id": rid, "name": name, "body": body, "list": "Inbox"}

        decision = auto_clarify(reminder_dict, memory_dir=memory_dir, now=now)

        if decision.kind != "needs_user":
            # Ensure item is in state DB
            if existing is None:
                state_mod.insert_item(conn, rid=rid, kind="unclarified", list="Inbox")
            apply_decision(
                decision,
                reminder_dict,
                conn=conn,
                rem_module=rem_module,
                log_dir=log_dir,
                now=now,
            )
            counters["auto"] += 1
        else:
            needs_user_items.append(reminder_dict)

    # Handle needs_user items
    if needs_user_items:
        if cb_active:
            # Circuit breaker active: emit one digest Q instead of per-item
            n = len(needs_user_items)
            if qchannel_module is not None:
                qchannel_module.dispatch(
                    conn=conn,
                    rem_module=rem_module,
                    kind="clarify",
                    prompt=f"Clarify: {n} new Inbox items need review",
                    payload={"digest": True, "count": n},
                    digest=True,
                    dispatch_dryrun=dispatch_dryrun,
                    now=now,
                    log_dir=log_dir,
                )
            counters["skipped"] += len(needs_user_items)
        else:
            for reminder_dict in needs_user_items:
                rid = reminder_dict["id"]
                name = reminder_dict["name"]

                # Ensure in state
                existing = state_mod.get_item_by_rid(conn, rid)
                if existing is None:
                    state_mod.insert_item(conn, rid=rid, kind="unclarified", list="Inbox")

                if qchannel_module is not None:
                    suggestions = _build_suggestions(name)
                    qchannel_module.dispatch(
                        conn=conn,
                        rem_module=rem_module,
                        kind="clarify",
                        prompt=f"Clarify: {name}"[:80],
                        payload={"ref_rid": rid, "suggestions": suggestions},
                        ref_rid=rid,
                        dispatch_dryrun=dispatch_dryrun,
                        now=now,
                        log_dir=log_dir,
                    )
                counters["dispatched"] += 1

    return counters


def _build_suggestions(title: str) -> list[str]:
    """Build a short list of plausible GTD destinations for the Q payload."""
    suggestions = ["@home", "@computer", "@errands", "Someday", "Waiting For", "delete"]
    return suggestions


# ---------------------------------------------------------------------------
# Public API: handle_q_answer
# ---------------------------------------------------------------------------


_CONTEXT_LISTS = frozenset({
    "@home", "@computer", "@errands", "@calls", "@anywhere", "@agenda",
    "@nyc", "@jax", "@odita",
})

_SOMEDAY_KEYWORDS = frozenset({"someday", "maybe", "later", "someday/maybe"})
_DELETE_KEYWORDS = frozenset({"delete", "trash", "cancel", "discard", "remove"})
_WAITING_KEYWORDS = frozenset({"waiting", "waiting for"})


def handle_q_answer(
    qid: str,
    reply_text: str,
    *,
    conn,
    rem_module=None,
    log_dir: Path | None = None,
) -> None:
    """Called by qchannel.poll when a clarify Q is answered. Parse reply
    (e.g., "@home", "waiting Dan", "someday", "delete") and apply."""
    if rem_module is None:
        rem_module = _R

    from gtd.engine import state as state_mod

    reply = (reply_text or "").strip().lower()

    # Look up the question to find ref_rid
    row = conn.execute("SELECT * FROM questions WHERE qid = ?", (qid,)).fetchone()
    if row is None:
        return
    q = dict(row)
    ref_rid = q.get("ref_rid")

    if not ref_rid:
        # Mark answered and return
        state_mod.update_question_status(conn, qid, "answered")
        return

    # Find the item in state
    item = state_mod.get_item_by_rid(conn, ref_rid)

    # Parse reply_text
    decision: ClarifyDecision | None = None

    # "@context" → next action in that context
    if reply.startswith("@"):
        ctx_name = reply.split()[0]  # e.g. "@home"
        decision = ClarifyDecision(
            kind="auto_next_action",
            target_list=ctx_name,
            reasoning=f"Q answer: context={ctx_name}",
        )

    # "waiting <name>" or "waiting for <name>"
    elif reply.startswith("waiting"):
        parts = reply.split()
        # "waiting Dan" → parts[1] is the name (if present)
        if len(parts) >= 2:
            # strip "for" if present
            name_parts = [p for p in parts[1:] if p.lower() != "for"]
            delegate = " ".join(name_parts).title() if name_parts else None
        else:
            delegate = None
        decision = ClarifyDecision(
            kind="auto_waiting",
            target_list=_WAITING_FOR_LIST,
            delegate=delegate,
            reasoning=f"Q answer: waiting for {delegate}",
        )

    # "someday" / "maybe" / "later"
    elif any(kw in reply for kw in _SOMEDAY_KEYWORDS):
        decision = ClarifyDecision(
            kind="auto_someday",
            target_list=_SOMEDAY_LIST,
            reasoning="Q answer: someday",
        )

    # "delete" / "trash" / "cancel"
    elif any(kw in reply for kw in _DELETE_KEYWORDS):
        # Mark complete (cancels) via reminders module
        if rem_module is not None:
            try:
                # Get current list from state or fallback to Inbox
                current_list = "Inbox"
                if item:
                    current_list = item.get("list") or "Inbox"
                rem_module.update_field(ref_rid, "isCompleted", "true")
            except Exception:
                pass
        # Update state to deleted/done
        if item is not None:
            conn.execute(
                "UPDATE items SET kind = ? WHERE rid = ?",
                ("deleted", ref_rid),
            )
            conn.commit()
        state_mod.update_question_status(conn, qid, "answered")
        obs_log(
            "clarify",
            op="handle_q_answer",
            qid=qid,
            ref_rid=ref_rid,
            reply=reply_text,
            action="delete",
            **({"log_dir": log_dir} if log_dir is not None else {}),
        )
        return

    if decision is not None and rem_module is not None:
        # Build minimal reminder dict from state item
        reminder_dict = {
            "id": ref_rid,
            "name": item.get("list", "") if item else "",
            "body": "",
            "list": item.get("list", "Inbox") if item else "Inbox",
        }
        apply_decision(
            decision,
            reminder_dict,
            conn=conn,
            rem_module=rem_module,
            log_dir=log_dir,
        )

    state_mod.update_question_status(conn, qid, "answered")
    obs_log(
        "clarify",
        op="handle_q_answer",
        qid=qid,
        ref_rid=ref_rid,
        reply=reply_text,
        decision_kind=decision.kind if decision else "unknown",
        **({"log_dir": log_dir} if log_dir is not None else {}),
    )
