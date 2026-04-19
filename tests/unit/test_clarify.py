"""
Unit tests for gtd/engine/clarify.py — US-009: clarify state machine + rules-only auto-clarify.

Covers:
- Each rule (R1-R5) fires on a representative input
- 50-item corpus: load fixture, run auto_clarify on each, count agreement → assert >= 35/50 (70%)
- apply_decision: auto_next_action moves reminder via R.move_to_list, state updated, write_fence enforced
- process_inbox with 5 inbox items, 3 auto-clarified, 2 needs_user → qchannel.dispatch called twice
- process_inbox with circuit-breaker active (mock qchannel.circuit_breaker_active=True) → skips per-item, emits 1 digest Q
- handle_q_answer with reply_text="@home" → moves reminder to @home list, state DONE
- handle_q_answer with reply_text="someday" → moves to Someday
- handle_q_answer with reply_text="delete" → marks reminder complete (cancels)
- handle_q_answer with reply_text="waiting Dan" → moves to Waiting For + sets delegate
"""
from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from gtd.engine.clarify import (
    ClarifyDecision,
    apply_decision,
    auto_clarify,
    handle_q_answer,
    process_inbox,
)
from gtd.engine.state import get_item_by_rid, init_db, insert_item, insert_question
from gtd.engine.write_fence import WriteScopeError


# ---------------------------------------------------------------------------
# Fake reminders dataclass and stub module
# ---------------------------------------------------------------------------


@dataclass
class FakeReminder:
    id: str
    list: str
    name: str
    completed: bool = False
    due_date: str = ""
    completion_date: str = ""
    body: str = ""
    priority: int = 0
    last_modified: str = ""


class StubRemModule:
    """Minimal stub for bin/lib/reminders."""

    def __init__(self, reminders: list[FakeReminder] | None = None):
        self._reminders: list[FakeReminder] = reminders or []
        self.move_calls: list[tuple] = []
        self.update_field_calls: list[tuple] = []
        self.update_notes_calls: list[tuple] = []
        self.complete_calls: list[tuple] = []

    def list_all(self) -> list[FakeReminder]:
        return list(self._reminders)

    def move_to_list(self, rid: str, list_name: str) -> None:
        self.move_calls.append((rid, list_name))
        for r in self._reminders:
            if r.id == rid:
                r.list = list_name

    def update_field(self, rid: str, field: str, value: str) -> None:
        self.update_field_calls.append((rid, field, value))

    def update_notes(self, rid: str, list_name: str, notes: str) -> None:
        self.update_notes_calls.append((rid, list_name, notes))

    def create(self, list_name: str, name: str, notes: str = "", due_iso: str = "") -> str:
        rid = str(uuid.uuid4())
        self._reminders.append(FakeReminder(id=rid, list=list_name, name=name))
        return rid


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_conn(tmp_path):
    db_path = tmp_path / "state.db"
    conn = init_db(db_path)
    yield conn
    conn.close()


@pytest.fixture
def stub_rem():
    return StubRemModule()


@pytest.fixture
def log_dir(tmp_path):
    d = tmp_path / "log"
    d.mkdir()
    return d


@pytest.fixture
def fixed_now():
    # Wednesday 2026-04-22 12:00 UTC
    return datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def memory_dir(tmp_path):
    """Create a memory/people directory with known people."""
    people_dir = tmp_path / "people"
    people_dir.mkdir(parents=True)
    (people_dir / "dan-baker.md").write_text("# Dan Baker")
    (people_dir / "michael-connelly.md").write_text("# Michael Connelly")
    (people_dir / "eugene.md").write_text("# Eugene")
    return tmp_path


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _reminder(name: str, list_name: str = "Inbox") -> dict:
    return {"id": str(uuid.uuid4()), "name": name, "body": "", "list": list_name}


# ---------------------------------------------------------------------------
# R1: 2-minute rule
# ---------------------------------------------------------------------------


class TestRuleR1:
    def test_call_verb_routes_to_calls(self, fixed_now):
        r = _reminder("call Michael about tax")
        d = auto_clarify(r, now=fixed_now)
        assert d.kind == "auto_next_action"
        assert d.target_list == "@calls"
        assert "R1" in d.reasoning

    def test_text_verb_routes_to_calls(self, fixed_now):
        r = _reminder("text Zoltan about AC")
        d = auto_clarify(r, now=fixed_now)
        assert d.kind == "auto_next_action"
        assert d.target_list == "@calls"

    def test_ping_verb_routes_to_calls(self, fixed_now):
        r = _reminder("ping Eugene before 1:1")
        d = auto_clarify(r, now=fixed_now)
        assert d.kind == "auto_next_action"
        assert d.target_list == "@calls"

    def test_email_verb_routes_to_computer(self, fixed_now):
        r = _reminder("email Michael about onboarding")
        d = auto_clarify(r, now=fixed_now)
        assert d.kind == "auto_next_action"
        assert d.target_list == "@computer"

    def test_reply_verb_routes_to_computer(self, fixed_now):
        r = _reminder("reply to Peter about IP agreement")
        d = auto_clarify(r, now=fixed_now)
        assert d.kind == "auto_next_action"
        assert d.target_list == "@computer"

    def test_message_verb_routes_to_computer(self, fixed_now):
        r = _reminder("message Dan about lease")
        d = auto_clarify(r, now=fixed_now)
        assert d.kind == "auto_next_action"
        assert d.target_list == "@computer"

    def test_case_insensitive(self, fixed_now):
        r = _reminder("CALL Dan NOW")
        d = auto_clarify(r, now=fixed_now)
        assert d.kind == "auto_next_action"
        assert d.target_list == "@calls"


# ---------------------------------------------------------------------------
# R2: Delegate detection
# ---------------------------------------------------------------------------


class TestRuleR2:
    def test_ask_known_person_routes_to_waiting(self, fixed_now, memory_dir):
        r = _reminder("ask Dan about rental terms")
        d = auto_clarify(r, memory_dir=memory_dir, now=fixed_now)
        assert d.kind == "auto_waiting"
        assert d.target_list == "Waiting For"
        assert d.delegate is not None
        assert d.delegate.lower() == "dan"
        assert "R2" in d.reasoning

    def test_remind_known_person(self, fixed_now, memory_dir):
        r = _reminder("remind Michael to send invoice")
        d = auto_clarify(r, memory_dir=memory_dir, now=fixed_now)
        assert d.kind == "auto_waiting"
        assert d.delegate is not None
        assert d.delegate.lower() == "michael"

    def test_tell_known_person(self, fixed_now, memory_dir):
        r = _reminder("tell Dan about the AC issue")
        d = auto_clarify(r, memory_dir=memory_dir, now=fixed_now)
        assert d.kind == "auto_waiting"

    def test_unknown_person_no_match(self, fixed_now, memory_dir):
        # "Bernadette" is not in memory
        r = _reminder("ask Bernadette about the reservation")
        d = auto_clarify(r, memory_dir=memory_dir, now=fixed_now)
        assert d.kind == "needs_user"

    def test_no_memory_dir_falls_through(self, fixed_now):
        r = _reminder("ask Dan about rental terms")
        d = auto_clarify(r, memory_dir=None, now=fixed_now)
        # Without memory_dir, R2 cannot validate the name → needs_user
        assert d.kind == "needs_user"


# ---------------------------------------------------------------------------
# R3: Reference
# ---------------------------------------------------------------------------


class TestRuleR3:
    def test_read_prefix_routes_to_reference(self, fixed_now):
        r = _reminder("read Financial Samurai")
        d = auto_clarify(r, now=fixed_now)
        assert d.kind == "auto_reference"
        assert d.target_list == "Someday"
        assert "R3" in d.reasoning

    def test_fyi_routes_to_reference(self, fixed_now):
        r = _reminder("fyi article on GTD weekly review")
        d = auto_clarify(r, now=fixed_now)
        assert d.kind == "auto_reference"

    def test_note_prefix_routes_to_reference(self, fixed_now):
        r = _reminder("note: Bank Street AC timeline")
        d = auto_clarify(r, now=fixed_now)
        assert d.kind == "auto_reference"

    def test_article_prefix_routes_to_reference(self, fixed_now):
        r = _reminder("article on property management")
        d = auto_clarify(r, now=fixed_now)
        assert d.kind == "auto_reference"

    def test_link_prefix_routes_to_reference(self, fixed_now):
        r = _reminder("link to Kubernetes O'Reilly")
        d = auto_clarify(r, now=fixed_now)
        assert d.kind == "auto_reference"

    def test_read_with_action_verb_falls_through(self, fixed_now):
        # "read and send" has an action verb — should NOT match R3
        r = _reminder("read and send the report")
        d = auto_clarify(r, now=fixed_now)
        # R3 has action verb guard; may fall through to needs_user
        # (R1 doesn't match since "read" is not in two-min verbs)
        assert d.kind != "auto_reference"


# ---------------------------------------------------------------------------
# R4: Books
# ---------------------------------------------------------------------------


class TestRuleR4:
    def test_title_contains_book_word(self, fixed_now):
        r = _reminder("Essentialism book summary")
        d = auto_clarify(r, now=fixed_now)
        assert d.kind == "auto_someday"
        assert d.target_list == "Someday"
        assert "R4" in d.reasoning

    def test_book_at_start(self, fixed_now):
        r = _reminder("book: Thinking in Bets")
        d = auto_clarify(r, now=fixed_now)
        assert d.kind == "auto_someday"

    def test_book_in_middle(self, fixed_now):
        r = _reminder("Shoe Dog book by Phil Knight")
        d = auto_clarify(r, now=fixed_now)
        assert d.kind == "auto_someday"

    def test_case_insensitive_book(self, fixed_now):
        r = _reminder("BOOK recommendation from Dan")
        d = auto_clarify(r, now=fixed_now)
        assert d.kind == "auto_someday"


# ---------------------------------------------------------------------------
# R5: Date-anchored
# ---------------------------------------------------------------------------


class TestRuleR5:
    def test_next_weekday(self, fixed_now):
        r = _reminder("buy chairs next Tuesday")
        d = auto_clarify(r, now=fixed_now)
        assert d.kind == "auto_tickler"
        assert d.target_list == "Tickler"
        assert d.release_at is not None
        assert "2026" in d.release_at
        assert "R5" in d.reasoning

    def test_tomorrow(self, fixed_now):
        r = _reminder("dentist appointment tomorrow")
        d = auto_clarify(r, now=fixed_now)
        assert d.kind == "auto_tickler"
        assert d.release_at == "2026-04-23"

    def test_slash_date(self, fixed_now):
        r = _reminder("Amazon return by 4/25")
        d = auto_clarify(r, now=fixed_now)
        assert d.kind == "auto_tickler"
        assert d.release_at == "2026-04-25"

    def test_month_day(self, fixed_now):
        r = _reminder("submit tax forms by April 30")
        d = auto_clarify(r, now=fixed_now)
        assert d.kind == "auto_tickler"
        assert d.release_at == "2026-04-30"

    def test_this_friday(self, fixed_now):
        # fixed_now is Wednesday; this Friday = 2026-04-24
        r = _reminder("renew lease this Friday")
        d = auto_clarify(r, now=fixed_now)
        assert d.kind == "auto_tickler"
        assert d.release_at == "2026-04-24"


# ---------------------------------------------------------------------------
# R1 beats R5 when call verb is present with date
# ---------------------------------------------------------------------------


def test_r1_fires_before_r5(fixed_now):
    """call + date: R1 fires first → @calls (not tickler)."""
    r = _reminder("call Michael next Monday about taxes")
    d = auto_clarify(r, now=fixed_now)
    assert d.kind == "auto_next_action"
    assert d.target_list == "@calls"


# ---------------------------------------------------------------------------
# Needs-user fallback
# ---------------------------------------------------------------------------


def test_ambiguous_item_needs_user(fixed_now):
    r = _reminder("Buy butt plug for Adelya")
    d = auto_clarify(r, now=fixed_now)
    assert d.kind == "needs_user"
    assert d.target_list is None


# ---------------------------------------------------------------------------
# 50-item corpus: ≥ 70% accuracy
# ---------------------------------------------------------------------------


CORPUS_PATH = ROOT / "tests" / "fixtures" / "clarify_corpus.json"


def test_corpus_accuracy(fixed_now, memory_dir):
    corpus = json.loads(CORPUS_PATH.read_text())
    assert len(corpus) == 50, f"Expected 50 items, got {len(corpus)}"

    agreed = 0
    failures = []
    for item in corpus:
        title = item["title"]
        expected_kind = item["expected_kind"]
        expected_list = item["expected_list"]  # may be None

        r = {"id": str(uuid.uuid4()), "name": title, "body": "", "list": "Inbox"}
        d = auto_clarify(r, memory_dir=memory_dir, now=fixed_now)

        kind_match = d.kind == expected_kind
        list_match = (d.target_list == expected_list)

        if kind_match and list_match:
            agreed += 1
        else:
            failures.append(
                f"  title={title!r}\n"
                f"    expected: kind={expected_kind}, list={expected_list}\n"
                f"    got:      kind={d.kind}, list={d.target_list}\n"
                f"    reasoning: {d.reasoning}"
            )

    accuracy = agreed / len(corpus)
    failure_detail = "\n".join(failures)
    assert agreed >= 35, (
        f"Corpus accuracy {agreed}/50 ({accuracy:.0%}) < 70%.\n"
        f"Failures:\n{failure_detail}"
    )


# ---------------------------------------------------------------------------
# apply_decision
# ---------------------------------------------------------------------------


class TestApplyDecision:
    def test_auto_next_action_moves_reminder(self, db_conn, stub_rem, log_dir, fixed_now):
        rid = str(uuid.uuid4())
        insert_item(db_conn, rid=rid, kind="unclarified", list="Inbox")
        reminder = {"id": rid, "name": "call Dan", "body": "", "list": "Inbox"}
        decision = ClarifyDecision(
            kind="auto_next_action", target_list="@calls", reasoning="R1"
        )
        apply_decision(
            decision, reminder, conn=db_conn, rem_module=stub_rem,
            log_dir=log_dir, now=fixed_now,
        )
        assert (rid, "@calls") in stub_rem.move_calls
        item = get_item_by_rid(db_conn, rid)
        assert item["kind"] == "next_action"
        assert item["list"] == "@calls"
        assert item["ctx"] == "@calls"

    def test_write_fence_enforced(self, db_conn, stub_rem, log_dir, fixed_now):
        """Attempting to move to a non-managed list raises WriteScopeError."""
        rid = str(uuid.uuid4())
        insert_item(db_conn, rid=rid, kind="unclarified", list="Inbox")
        reminder = {"id": rid, "name": "test", "body": "", "list": "Inbox"}
        decision = ClarifyDecision(
            kind="auto_next_action", target_list="NonManagedList", reasoning="test"
        )
        with pytest.raises(WriteScopeError):
            apply_decision(
                decision, reminder, conn=db_conn, rem_module=stub_rem,
                log_dir=log_dir, now=fixed_now,
            )

    def test_auto_waiting_updates_state(self, db_conn, stub_rem, log_dir, fixed_now):
        rid = str(uuid.uuid4())
        insert_item(db_conn, rid=rid, kind="unclarified", list="Inbox")
        reminder = {"id": rid, "name": "ask Dan about lease", "body": "", "list": "Inbox"}
        decision = ClarifyDecision(
            kind="auto_waiting", target_list="Waiting For",
            delegate="Dan", reasoning="R2"
        )
        apply_decision(
            decision, reminder, conn=db_conn, rem_module=stub_rem,
            log_dir=log_dir, now=fixed_now,
        )
        assert (rid, "Waiting For") in stub_rem.move_calls
        item = get_item_by_rid(db_conn, rid)
        assert item["kind"] == "waiting_for"

    def test_auto_someday_updates_state(self, db_conn, stub_rem, log_dir, fixed_now):
        rid = str(uuid.uuid4())
        insert_item(db_conn, rid=rid, kind="unclarified", list="Inbox")
        reminder = {"id": rid, "name": "Essentialism book", "body": "", "list": "Inbox"}
        decision = ClarifyDecision(
            kind="auto_someday", target_list="Someday", reasoning="R4"
        )
        apply_decision(
            decision, reminder, conn=db_conn, rem_module=stub_rem,
            log_dir=log_dir, now=fixed_now,
        )
        item = get_item_by_rid(db_conn, rid)
        assert item["kind"] == "someday"

    def test_needs_user_does_nothing(self, db_conn, stub_rem, log_dir, fixed_now):
        rid = str(uuid.uuid4())
        insert_item(db_conn, rid=rid, kind="unclarified", list="Inbox")
        reminder = {"id": rid, "name": "ambiguous item", "body": "", "list": "Inbox"}
        decision = ClarifyDecision(
            kind="needs_user", target_list=None, reasoning="no rule"
        )
        apply_decision(
            decision, reminder, conn=db_conn, rem_module=stub_rem,
            log_dir=log_dir, now=fixed_now,
        )
        # Nothing moved
        assert stub_rem.move_calls == []

    def test_log_file_written(self, db_conn, stub_rem, log_dir, fixed_now):
        rid = str(uuid.uuid4())
        insert_item(db_conn, rid=rid, kind="unclarified", list="Inbox")
        reminder = {"id": rid, "name": "call Michael", "body": "", "list": "Inbox"}
        decision = ClarifyDecision(
            kind="auto_next_action", target_list="@calls", reasoning="R1"
        )
        apply_decision(
            decision, reminder, conn=db_conn, rem_module=stub_rem,
            log_dir=log_dir, now=fixed_now,
        )
        log_file = log_dir / "clarify.jsonl"
        assert log_file.exists()
        line = json.loads(log_file.read_text().strip().splitlines()[-1])
        assert line["op"] == "apply_decision"
        assert line["decision_kind"] == "auto_next_action"


# ---------------------------------------------------------------------------
# process_inbox
# ---------------------------------------------------------------------------


class TestProcessInbox:
    def _make_stub_qchannel(self, cb_active: bool = False):
        """Return a stub qchannel module."""
        q = MagicMock()
        q.circuit_breaker_active.return_value = cb_active
        q.dispatch.return_value = MagicMock(qid="QID1", status="dryrun")
        return q

    def _make_inbox_reminders(self):
        """5 inbox reminders: 3 auto-clarifiable, 2 ambiguous."""
        return [
            FakeReminder(id="r1", list="Inbox", name="call Michael about taxes"),
            FakeReminder(id="r2", list="Inbox", name="email Dan re: lease"),
            FakeReminder(id="r3", list="Inbox", name="Essentialism book"),
            FakeReminder(id="r4", list="Inbox", name="Buy chairs for apartment"),
            FakeReminder(id="r5", list="Inbox", name="Fix AC at Bank Street"),
        ]

    def test_three_auto_two_dispatched(self, db_conn, log_dir, fixed_now):
        stub_rem = StubRemModule(self._make_inbox_reminders())
        stub_q = self._make_stub_qchannel()

        result = process_inbox(
            conn=db_conn,
            rem_module=stub_rem,
            memory_dir=None,
            log_dir=log_dir,
            qchannel_module=stub_q,
            dispatch_dryrun=True,
            now=fixed_now,
        )

        assert result["auto"] == 3
        assert result["dispatched"] == 2
        assert result["skipped"] == 0
        # dispatch called twice for the 2 needs_user items
        assert stub_q.dispatch.call_count == 2

    def test_circuit_breaker_emits_digest(self, db_conn, log_dir, fixed_now):
        stub_rem = StubRemModule(self._make_inbox_reminders())
        stub_q = self._make_stub_qchannel(cb_active=True)

        result = process_inbox(
            conn=db_conn,
            rem_module=stub_rem,
            memory_dir=None,
            log_dir=log_dir,
            qchannel_module=stub_q,
            dispatch_dryrun=True,
            now=fixed_now,
        )

        # Auto items still processed; needs_user items skipped (one digest Q)
        assert result["auto"] == 3
        assert result["skipped"] == 2
        # Exactly one digest dispatch call
        assert stub_q.dispatch.call_count == 1
        call_kwargs = stub_q.dispatch.call_args[1]
        assert call_kwargs.get("digest") is True
        payload = call_kwargs.get("payload", {})
        assert payload.get("digest") is True

    def test_already_clarified_items_skipped(self, db_conn, log_dir, fixed_now):
        rid = "r1"
        insert_item(db_conn, rid=rid, kind="next_action", list="@calls")
        stub_rem = StubRemModule([
            FakeReminder(id=rid, list="Inbox", name="call Michael about taxes"),
        ])
        stub_q = self._make_stub_qchannel()

        result = process_inbox(
            conn=db_conn,
            rem_module=stub_rem,
            memory_dir=None,
            log_dir=log_dir,
            qchannel_module=stub_q,
            dispatch_dryrun=True,
            now=fixed_now,
        )
        assert result["skipped"] == 1
        assert result["auto"] == 0


# ---------------------------------------------------------------------------
# handle_q_answer
# ---------------------------------------------------------------------------


class TestHandleQAnswer:
    def _setup_question(self, db_conn, ref_rid: str) -> str:
        """Insert a clarify question linked to ref_rid. Returns qid."""
        insert_item(db_conn, rid=ref_rid, kind="unclarified", list="Inbox")
        qid = insert_question(
            db_conn,
            kind="clarify",
            ref_rid=ref_rid,
            status="open",
            payload_json={"ref_rid": ref_rid},
        )
        return qid

    def test_at_home_moves_to_home_list(self, db_conn, log_dir):
        rid = str(uuid.uuid4())
        stub_rem = StubRemModule()
        qid = self._setup_question(db_conn, rid)

        handle_q_answer(qid, "@home", conn=db_conn, rem_module=stub_rem, log_dir=log_dir)

        assert (rid, "@home") in stub_rem.move_calls
        item = get_item_by_rid(db_conn, rid)
        assert item["kind"] == "next_action"
        row = db_conn.execute("SELECT status FROM questions WHERE qid = ?", (qid,)).fetchone()
        assert dict(row)["status"] == "answered"

    def test_someday_moves_to_someday(self, db_conn, log_dir):
        rid = str(uuid.uuid4())
        stub_rem = StubRemModule()
        qid = self._setup_question(db_conn, rid)

        handle_q_answer(qid, "someday", conn=db_conn, rem_module=stub_rem, log_dir=log_dir)

        assert (rid, "Someday") in stub_rem.move_calls
        item = get_item_by_rid(db_conn, rid)
        assert item["kind"] == "someday"

    def test_delete_marks_item_deleted(self, db_conn, log_dir):
        rid = str(uuid.uuid4())
        stub_rem = StubRemModule()
        qid = self._setup_question(db_conn, rid)

        handle_q_answer(qid, "delete", conn=db_conn, rem_module=stub_rem, log_dir=log_dir)

        item = get_item_by_rid(db_conn, rid)
        assert item["kind"] == "deleted"
        row = db_conn.execute("SELECT status FROM questions WHERE qid = ?", (qid,)).fetchone()
        assert dict(row)["status"] == "answered"
        # update_field called with isCompleted
        assert any(
            c[0] == rid and c[1] == "isCompleted"
            for c in stub_rem.update_field_calls
        )

    def test_waiting_dan_moves_to_waiting_for(self, db_conn, log_dir):
        rid = str(uuid.uuid4())
        stub_rem = StubRemModule()
        qid = self._setup_question(db_conn, rid)

        handle_q_answer(qid, "waiting Dan", conn=db_conn, rem_module=stub_rem, log_dir=log_dir)

        assert (rid, "Waiting For") in stub_rem.move_calls
        item = get_item_by_rid(db_conn, rid)
        assert item["kind"] == "waiting_for"
        row = db_conn.execute("SELECT status FROM questions WHERE qid = ?", (qid,)).fetchone()
        assert dict(row)["status"] == "answered"

    def test_waiting_for_dan_variant(self, db_conn, log_dir):
        rid = str(uuid.uuid4())
        stub_rem = StubRemModule()
        qid = self._setup_question(db_conn, rid)

        handle_q_answer(qid, "waiting for Dan", conn=db_conn, rem_module=stub_rem, log_dir=log_dir)

        assert (rid, "Waiting For") in stub_rem.move_calls

    def test_trash_keyword_also_deletes(self, db_conn, log_dir):
        rid = str(uuid.uuid4())
        stub_rem = StubRemModule()
        qid = self._setup_question(db_conn, rid)

        handle_q_answer(qid, "trash", conn=db_conn, rem_module=stub_rem, log_dir=log_dir)

        item = get_item_by_rid(db_conn, rid)
        assert item["kind"] == "deleted"

    def test_missing_question_is_noop(self, db_conn, log_dir):
        stub_rem = StubRemModule()
        # Should not raise even with a bogus qid
        handle_q_answer("NONEXISTENT_QID", "@home", conn=db_conn, rem_module=stub_rem, log_dir=log_dir)
        assert stub_rem.move_calls == []
