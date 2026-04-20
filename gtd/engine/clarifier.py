"""
clarifier.py — Allen's canonical clarify decision tree.

LAYERING CONTRACT (per AC-CLAR-6 in .omc/plans/gtd-clarifier-brain.md):

This module is layered ON TOP of `gtd.engine.clarify.auto_clarify` (R1–R5
rules in clarify.py:314-357). The contract: callers MUST only call
`evaluate()` on items where `auto_clarify` returned
`ClarifyDecision(kind="needs_user")`. The two functions compose; they do
not compete. The R1–R5 rules handle the deterministic 70% of cases (call/
email/text → @calls; "ask <known person>" → Waiting For; etc.). The
clarifier picks up the remaining ~30% by walking Allen's three canonical
clarify gates and asking ONE socratic question for the first failed gate.

The contract is documentary, NOT enforced in code. The `gtd clarifier
evaluate` CLI subcommand intentionally bypasses the layering for
debug/inspection use — text strings have no `auto_clarify` history.
Future readers should NOT add an assertion that breaks the CLI.

THE GATES (Allen's canonical clarify, in order):

  G1: actionable
      Fails if title is a pure noun phrase with no leading or embedded
      action verb. Heuristic: no token in _ACTION_VERBS appears anywhere
      in the title. Question: "Is X something you actually need to do,
      or is it a reference / interest / someday item?"

  G2: outcome_clear
      Fails if title has a verb but no recipient/object/deliverable
      noun phrase. Heuristic: verb present + sentence ≤ 3 words OR ends
      in a bare verb without an object. Question: "What does 'done'
      look like for X?"

  G3: next_action_concrete
      Fails if title implies multi-step work. Three explicit failure
      paths (any one trips G3):
        G3a: leading verb is in _PROJECT_INDICATOR_VERBS
        G3b: title contains ` and `, ` & `, or comma between verb phrases
        G3c: leading verb is "file" AND object contains lawsuit/court term
      Length is NOT a signal. Question: "What's the very next physical
      step for X? (one specific action)"

OUTPUT:

  ClarifyEvaluation(verdict, failed_gate, reason, proposed_question,
                    recommended_disposition)

  verdict ∈ {ACCEPT, NEEDS_QUESTION, ESCALATE}
    - ACCEPT: all gates pass; item is clarified
    - NEEDS_QUESTION: a gate failed; ask the proposed_question
    - ESCALATE: returned by callers (not by this module) when the
                user has hit the per-item round cap

  recommended_disposition (per AC-CLAR-5):
    - G1 fail → "Someday"
    - G1 pass + G2 fail → "Projects" (multi-step outcome)
    - G3 fail → "Projects"
    - all pass (ACCEPT) → None (caller picks the @ctx)
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Verb lists (per AC-CLAR-4: ≥50 action verbs, ≥7 project-indicator verbs)
# ---------------------------------------------------------------------------

_ACTION_VERBS: frozenset[str] = frozenset({
    # communication
    "call", "email", "text", "ping", "reply", "ask", "message", "schedule",
    "confirm", "remind", "tell", "notify", "respond",
    # commerce
    "buy", "sell", "order", "return", "ship", "pickup", "pick", "dropoff",
    "drop", "purchase", "ups",  # "ups" as verb: ship via UPS
    # maintenance
    "fix", "clean", "replace", "repair", "install", "configure", "setup",
    "wash", "vacuum", "sweep", "change",
    # admin
    "pay", "file", "sign", "register", "submit", "request", "cancel",
    "update", "check", "verify", "review", "approve", "renew", "book",
    # creative
    "write", "draft", "edit", "plan", "design", "sketch", "compose",
    "publish", "post",
    # physical
    "lift", "move", "carry", "throw", "make", "cook", "prepare", "pack",
    "unpack", "load", "unload",
    # project-indicator verbs ARE still verbs — they pass G1, then G3a
    # catches them as project shaped. Listing here keeps G1 from
    # mis-firing on items like "Start CompassionAI".
    "start", "launch", "build", "complete", "finish", "wrap",
    "finalize", "organize", "create",
})

# Multi-word phrases treated as action verbs when they appear at the start.
# Match as substrings; G1 looks for these explicitly before falling through.
_ACTION_PHRASES: tuple[str, ...] = (
    "make sure",  # "make sure crown plaza refunds X" = follow-up action
    "follow up",
    "set up",
    "wrap up",   # also a project-indicator (G3a) — checked for that role first
    "look into",
    "find out",
    "drop off",
    "pick up",
)

_PROJECT_INDICATOR_VERBS: frozenset[str] = frozenset({
    "start", "launch", "build", "complete", "finish", "wrap",
    "finalize", "organize", "create",
})

# G3c: legal-proceedings sub-rule. If leading verb is "file" AND the object
# contains any of these, G3 fails. Future domain patterns should add their
# own G3* sub-rule — do not silently expand this set into other domains.
_LEGAL_KEYWORDS: tuple[str, ...] = (
    "lawsuit", "law suit", "litigation", "court case", "legal proceeding",
    "complaint",
)


# Word-boundary matches "and"; spaces around "&" handle the symbol form
# (\b doesn't recognize & as a word boundary).
_CONJUNCTION_RE = re.compile(r"\band\b|\s&\s|\s&$|^&\s", re.IGNORECASE)
_LEADING_TOKEN_RE = re.compile(r"^\s*(\w+)")
_NON_WORD_RE = re.compile(r"[^\w\s]")


# ---------------------------------------------------------------------------
# Verdict + dataclass
# ---------------------------------------------------------------------------


class ClarifyVerdict(str, Enum):
    ACCEPT = "ACCEPT"
    NEEDS_QUESTION = "NEEDS_QUESTION"
    ESCALATE = "ESCALATE"


@dataclass
class ClarifyEvaluation:
    verdict: ClarifyVerdict
    failed_gate: Optional[str]
    reason: str
    proposed_question: Optional[str]
    recommended_disposition: Optional[str]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["verdict"] = self.verdict.value
        return d


# ---------------------------------------------------------------------------
# Gate helpers
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    """Lowercase + strip extra whitespace."""
    return " ".join(text.lower().split())


def _leading_token(text: str) -> str:
    """Return the first word (lowercase) or '' if empty."""
    m = _LEADING_TOKEN_RE.match(text)
    return m.group(1).lower() if m else ""


def _has_action_verb(text: str) -> tuple[bool, str]:
    """Return (True, matching_token) if any token in _ACTION_VERBS or any
    phrase in _ACTION_PHRASES appears in the text."""
    norm = _normalize(text)

    # Check phrases first (so "make sure" is recognized before falling
    # through to bare "make").
    for phrase in _ACTION_PHRASES:
        if norm.startswith(phrase) or f" {phrase} " in f" {norm} ":
            return True, phrase

    # Token scan — strip punctuation, split on whitespace.
    cleaned = _NON_WORD_RE.sub(" ", norm)
    tokens = cleaned.split()
    for token in tokens:
        if token in _ACTION_VERBS:
            return True, token
    return False, ""


def _gate_actionable(text: str) -> tuple[bool, str]:
    """G1: passes iff text contains an action verb (token or phrase)."""
    has_verb, matched = _has_action_verb(text)
    if has_verb:
        return True, f"action verb {matched!r} present"
    return False, "no action verb in title (looks like a noun/reference)"


_VAGUE_PLACEHOLDERS: frozenset[str] = frozenset({
    "stuff", "things", "thing", "something", "everything", "anything",
})

# `with <CapName> <more>` pattern → outcome depends on someone else.
# Captures titles like "Buy a computer with Eugene list" where the
# user can't act until <Name> delivers their part.
_WITH_NAME_DEPENDENCY_RE = re.compile(
    r"\bwith\s+([A-Z][a-z]+)\s+\S+",
)


def _gate_outcome_clear(text: str) -> tuple[bool, str]:
    """G2: passes iff the action has a recipient/object/deliverable AND no
    vague placeholders ("stuff", "things") AND no dependency-on-other-person
    pattern ("with Eugene <X>").
    """
    norm = _normalize(text)
    cleaned = _NON_WORD_RE.sub(" ", norm)
    tokens = cleaned.split()
    if not tokens:
        return False, "empty title"

    # Vague placeholders → outcome is unclear regardless of token count
    for ph in _VAGUE_PLACEHOLDERS:
        if ph in tokens:
            return False, f"contains vague placeholder {ph!r}"

    # Dependency-on-other-person pattern: "<verb> <object> with <CapName> <more>"
    # — the user can't complete the outcome alone.
    m = _WITH_NAME_DEPENDENCY_RE.search(text)
    if m:
        return False, (
            f"depends on {m.group(1)} — outcome contingent on their input"
        )

    # Drop leading action phrase / verb tokens to find the "object".
    object_start = 0
    for phrase in _ACTION_PHRASES:
        if norm.startswith(phrase):
            object_start = len(phrase.split())
            break
    if object_start == 0 and tokens[0] in _ACTION_VERBS:
        object_start = 1

    object_tokens = tokens[object_start:]

    connectors = {"the", "a", "an", "to", "for", "of", "in", "on", "at",
                  "my", "your", "his", "her", "their", "our", "with"}
    substantive = [t for t in object_tokens if t not in connectors]

    if len(substantive) == 0:
        return False, "title is bare verb with no object"
    if len(substantive) == 1:
        original_token = text.split()[object_start] if object_start < len(text.split()) else ""
        if original_token and original_token[0].isupper() and original_token.lower() == substantive[0]:
            return False, (
                f"verb has only a name/recipient ({substantive[0]!r}) — no topic"
            )
        return True, f"object {substantive[0]!r} reads as a deliverable"

    return True, f"object reads clearly: {' '.join(substantive[:5])!r}"


def _gate_next_action_concrete(text: str) -> tuple[bool, str]:
    """G3: passes iff item is a single physical step.

    Three explicit failure paths (any one trips G3):
      G3a: leading verb is in _PROJECT_INDICATOR_VERBS
      G3b: title contains conjunction (" and ", " & ", comma between phrases)
      G3c: leading verb is "file" AND object contains a legal keyword
    Length is NOT a signal.
    """
    norm = _normalize(text)
    leading = _leading_token(norm)

    # G3a
    if leading in _PROJECT_INDICATOR_VERBS:
        return False, f"G3a: leading verb {leading!r} is a project indicator"

    # Also handle "wrap up" / "follow up" leading phrases that normalize to
    # the multi-word phrase form — "wrap" is in _PROJECT_INDICATOR_VERBS
    # already, so wrap-up is caught above. set up / follow up are NOT
    # project indicators (single tasks); explicitly excluded.

    # G3c (check before G3b so the legal phrase is the named reason)
    if leading == "file":
        for kw in _LEGAL_KEYWORDS:
            if kw in norm:
                return False, f"G3c: 'file' + legal keyword {kw!r} = multi-step legal process"

    # G3b
    if _CONJUNCTION_RE.search(norm):
        return False, "G3b: conjunction in title (multi-step)"
    if "," in text:
        # Comma between two verb-led phrases is multi-step. Single comma
        # in a list ("buy milk, eggs, butter") is still single-action shopping
        # — accepted. Heuristic: comma + a second action verb after it.
        parts = text.split(",")
        if len(parts) >= 2:
            second_lead = _leading_token(parts[1].strip().lower())
            if second_lead in _ACTION_VERBS:
                return False, "G3b: comma separates two verb phrases"

    return True, "single concrete action"


# ---------------------------------------------------------------------------
# Canonical questions (kept here as the single source of truth — the SKILL.md
# anchor references these by gate name, not by inlining the strings)
# ---------------------------------------------------------------------------

_CANONICAL_QUESTIONS: dict[str, str] = {
    "actionable": (
        "Is this something you actually need to do, "
        "or is it a reference / interest / someday item?"
    ),
    "outcome_clear": "What does 'done' look like for this?",
    "next_action_concrete": (
        "What's the very next physical step? (one specific action)"
    ),
}

_DISPOSITION_BY_FAILED_GATE: dict[str, str] = {
    "actionable": "Someday",
    "outcome_clear": "Projects",
    "next_action_concrete": "Projects",
}


def recommend_disposition(failed_gate: Optional[str]) -> Optional[str]:
    """Map a failed-gate name to the default escalation disposition."""
    if failed_gate is None:
        return None
    return _DISPOSITION_BY_FAILED_GATE.get(failed_gate)


def suggest_question(failed_gate: str) -> str:
    """Return the canonical socratic question for the named gate."""
    if failed_gate not in _CANONICAL_QUESTIONS:
        raise KeyError(f"no canonical question for gate {failed_gate!r}")
    return _CANONICAL_QUESTIONS[failed_gate]


# ---------------------------------------------------------------------------
# Public API: evaluate
# ---------------------------------------------------------------------------


def evaluate(item: dict | str) -> ClarifyEvaluation:
    """Walk Allen's gates in order. Return the first failed gate or ACCEPT.

    Accepts either an item dict (with `name` or `title` key) or a raw
    string. Pure function — no I/O, no logging.

    Gate order is G1 → G2 → G3 in Allen's canonical sequence, with one
    short-circuit: if the leading verb is in `_PROJECT_INDICATOR_VERBS`,
    we skip G2 and jump to G3. Rationale: project-indicator verbs
    ("start/launch/build/finish") inherently imply multi-step work — the
    most informative question is "what's the very next physical step?",
    not "what does done look like?". Asking the outcome question first
    here would feel pedantic for an obvious project.
    """
    if isinstance(item, str):
        text = item
    else:
        text = item.get("name", "") or item.get("title", "") or ""

    text = (text or "").strip()
    if not text:
        return ClarifyEvaluation(
            verdict=ClarifyVerdict.NEEDS_QUESTION,
            failed_gate="actionable",
            reason="empty title",
            proposed_question=_CANONICAL_QUESTIONS["actionable"],
            recommended_disposition="Someday",
        )

    # G1 actionable
    passed, reason = _gate_actionable(text)
    if not passed:
        return ClarifyEvaluation(
            verdict=ClarifyVerdict.NEEDS_QUESTION,
            failed_gate="actionable",
            reason=reason,
            proposed_question=_CANONICAL_QUESTIONS["actionable"],
            recommended_disposition="Someday",
        )

    leading = _leading_token(_normalize(text))
    skip_g2 = leading in _PROJECT_INDICATOR_VERBS

    # G2 outcome_clear (skipped for project-indicator-led items — they
    # need the G3 question, not the G2 question)
    if not skip_g2:
        passed, reason = _gate_outcome_clear(text)
        if not passed:
            # Disposition for G2 fail depends on WHY it failed:
            # - "depends on <Name>" pattern → Waiting For
            # - everything else → Projects
            disposition = "Waiting For" if "depends on " in reason else "Projects"
            return ClarifyEvaluation(
                verdict=ClarifyVerdict.NEEDS_QUESTION,
                failed_gate="outcome_clear",
                reason=reason,
                proposed_question=_CANONICAL_QUESTIONS["outcome_clear"],
                recommended_disposition=disposition,
            )

    # G3 next_action_concrete
    passed, reason = _gate_next_action_concrete(text)
    if not passed:
        return ClarifyEvaluation(
            verdict=ClarifyVerdict.NEEDS_QUESTION,
            failed_gate="next_action_concrete",
            reason=reason,
            proposed_question=_CANONICAL_QUESTIONS["next_action_concrete"],
            recommended_disposition="Projects",
        )

    return ClarifyEvaluation(
        verdict=ClarifyVerdict.ACCEPT,
        failed_gate=None,
        reason="all gates pass: actionable, outcome clear, single concrete step",
        proposed_question=None,
        recommended_disposition=None,
    )
