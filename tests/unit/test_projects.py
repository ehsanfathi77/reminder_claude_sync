"""
Unit tests for gtd/engine/projects.py — US-011: projects + next-action invariant.

All tests use a real state.db on tmp_path and a stub rem_module.
No real Reminders.app is touched.

Covers:
- create_project: state row exists, Projects-list reminder created via
  rem_module.create with outcome in notes (fenced)
- add_next_action: rem_module.create called with @ctx list, notes contain
  `project: <project-id>` in fence
- project_children: 3 next-actions for one project, 1 for another → 3 + 1
- stalled_projects: project with 0 children → in result; project with 1 → not
- check_invariants with 0 stalled → no Q dispatched, returns {'stalled_count': 0, 'q_dispatched': False}
- check_invariants with 1 stalled → 1 digest Q dispatched (qchannel.dispatch called with digest=True, payload includes project)
- check_invariants with 3 stalled → exactly 1 digest Q dispatched (NOT 3 separate Qs)
- write_fence enforced: unmanaged ctx string raises WriteScopeError
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from gtd.engine.state import init_db, insert_project as state_insert_project
import gtd.engine.state as state_mod
from gtd.engine.notes_metadata import parse_metadata, serialize_metadata
from gtd.engine.write_fence import WriteScopeError
from gtd.engine.projects import (
    add_next_action,
    check_invariants,
    create_project,
    project_children,
    stalled_projects,
)


# ---------------------------------------------------------------------------
# Stub reminders module (mirrors qchannel test stub)
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
    """Minimal stub for bin/lib/reminders that records calls and returns fakes."""

    def __init__(self, reminders: list[FakeReminder] | None = None):
        self._reminders: list[FakeReminder] = reminders or []
        self._create_calls: list[dict] = []
        self._update_notes_calls: list[tuple] = []
        self._next_rid = 0

    def create(self, list_name: str, name: str, notes: str = "", due_iso: str = "") -> str:
        self._create_calls.append({"list_name": list_name, "name": name, "notes": notes})
        rid = f"FAKE-RID-{self._next_rid}"
        self._next_rid += 1
        self._reminders.append(
            FakeReminder(id=rid, list=list_name, name=name, body=notes)
        )
        return rid

    def list_all(self, days_done_window: int = 7) -> list[FakeReminder]:
        return list(self._reminders)

    def update_notes(self, rid: str, list_name: str, notes: str) -> None:
        self._update_notes_calls.append((rid, list_name, notes))
        for rem in self._reminders:
            if rem.id == rid:
                rem.body = notes
                break


# ---------------------------------------------------------------------------
# Stub qchannel module
# ---------------------------------------------------------------------------


class StubQChannel:
    """Minimal stub for gtd.engine.qchannel that records dispatch calls."""

    def __init__(self):
        self._dispatch_calls: list[dict] = []
        self._dispatch_results: list = []
        self._next_status = "dryrun"

    def dispatch(self, *, conn, rem_module=None, kind, prompt, payload=None,
                 digest=False, dispatch_dryrun=True, now=None, log_dir=None,
                 invocation_id=None, ref_rid=None, quiet_hours=None, gtd_id=None,
                 **kwargs) -> "DispatchResultStub":
        self._dispatch_calls.append({
            "kind": kind,
            "prompt": prompt,
            "payload": payload,
            "digest": digest,
            "dispatch_dryrun": dispatch_dryrun,
        })
        return DispatchResultStub(qid=f"FAKE-QID-{len(self._dispatch_calls)}", status=self._next_status)


@dataclass
class DispatchResultStub:
    qid: str | None
    status: str
    reason: str | None = None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "state.db"
    conn = init_db(db_path)
    yield conn
    conn.close()


@pytest.fixture
def rem():
    return StubRemModule()


@pytest.fixture
def qch():
    return StubQChannel()


@pytest.fixture
def log_dir(tmp_path):
    d = tmp_path / "log"
    d.mkdir()
    return d


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# create_project
# ---------------------------------------------------------------------------


def test_create_project_returns_project_id(db, rem):
    pid = create_project("Launch Site", "Ship the marketing site by Q3", conn=db, rem_module=rem)
    assert pid is not None
    assert len(pid) == 26  # ULID length


def test_create_project_state_row_exists(db, rem):
    pid = create_project("Launch Site", "Ship the marketing site", conn=db, rem_module=rem)
    row = db.execute(
        "SELECT * FROM projects WHERE project_id = ?", (pid,)
    ).fetchone()
    assert row is not None
    assert dict(row)["outcome"] == "Ship the marketing site"


def test_create_project_reminder_created_in_projects_list(db, rem):
    create_project("Launch Site", "Ship the marketing site", conn=db, rem_module=rem)
    assert len(rem._create_calls) == 1
    call = rem._create_calls[0]
    assert call["list_name"] == "Projects"
    assert call["name"] == "Launch Site"


def test_create_project_notes_contain_fenced_outcome(db, rem):
    outcome = "Ship the marketing site by Q3"
    create_project("Launch Site", outcome, conn=db, rem_module=rem)
    notes = rem._create_calls[0]["notes"]
    meta, _ = parse_metadata(notes)
    assert meta.get("kind") == "project"
    assert meta.get("outcome") == outcome


def test_create_project_notes_contain_project_id(db, rem):
    pid = create_project("Launch Site", "Ship it", conn=db, rem_module=rem)
    notes = rem._create_calls[0]["notes"]
    meta, _ = parse_metadata(notes)
    assert meta.get("id") == pid


# ---------------------------------------------------------------------------
# add_next_action
# ---------------------------------------------------------------------------


def test_add_next_action_returns_rid(db, rem):
    pid = create_project("My Project", "Finish it", conn=db, rem_module=rem)
    rid = add_next_action(pid, "@home", "Buy supplies", conn=db, rem_module=rem)
    assert rid is not None
    assert rid.startswith("FAKE-RID-")


def test_add_next_action_create_called_with_ctx_list(db, rem):
    pid = create_project("My Project", "Finish it", conn=db, rem_module=rem)
    add_next_action(pid, "@home", "Buy supplies", conn=db, rem_module=rem)
    # Second create call (first is for project)
    na_call = rem._create_calls[1]
    assert na_call["list_name"] == "@home"
    assert na_call["name"] == "Buy supplies"


def test_add_next_action_notes_contain_project_id_in_fence(db, rem):
    pid = create_project("My Project", "Finish it", conn=db, rem_module=rem)
    add_next_action(pid, "@home", "Buy supplies", conn=db, rem_module=rem)
    na_call = rem._create_calls[1]
    notes = na_call["notes"]
    meta, _ = parse_metadata(notes)
    assert meta.get("project") == pid


def test_add_next_action_notes_contain_kind_next_action(db, rem):
    pid = create_project("My Project", "Finish it", conn=db, rem_module=rem)
    add_next_action(pid, "@home", "Buy supplies", conn=db, rem_module=rem)
    notes = rem._create_calls[1]["notes"]
    meta, _ = parse_metadata(notes)
    assert meta.get("kind") == "next_action"


def test_add_next_action_notes_contain_ctx(db, rem):
    pid = create_project("My Project", "Finish it", conn=db, rem_module=rem)
    add_next_action(pid, "@calls", "Call Alice", conn=db, rem_module=rem)
    notes = rem._create_calls[1]["notes"]
    meta, _ = parse_metadata(notes)
    assert meta.get("ctx") == "@calls"


def test_add_next_action_state_item_persisted(db, rem):
    pid = create_project("My Project", "Finish it", conn=db, rem_module=rem)
    rid = add_next_action(pid, "@home", "Buy supplies", conn=db, rem_module=rem)
    item = state_mod.get_item_by_rid(db, rid)
    assert item is not None
    assert item["kind"] == "next_action"
    assert item["project"] == pid
    assert item["ctx"] == "@home"


def test_add_next_action_unmanaged_ctx_raises_write_scope_error(db, rem):
    pid = create_project("My Project", "Finish it", conn=db, rem_module=rem)
    with pytest.raises(WriteScopeError):
        add_next_action(pid, "MyPersonalList", "Do thing", conn=db, rem_module=rem)


# ---------------------------------------------------------------------------
# project_children
# ---------------------------------------------------------------------------


def _create_project_with_children(db, rem, outcome, ctx_list, n_children):
    """Helper: create a project + n_children next-actions. Returns project_id."""
    pid = create_project(f"Project {outcome}", outcome, conn=db, rem_module=rem)
    for i in range(n_children):
        add_next_action(pid, ctx_list, f"Action {i}", conn=db, rem_module=rem)
    return pid


def test_project_children_returns_3_for_project_with_3_actions(db, rem):
    pid = _create_project_with_children(db, rem, "Big outcome", "@home", 3)
    children = project_children(pid, conn=db, rem_module=rem)
    assert len(children) == 3


def test_project_children_returns_1_for_project_with_1_action(db, rem):
    pid = _create_project_with_children(db, rem, "Small outcome", "@calls", 1)
    children = project_children(pid, conn=db, rem_module=rem)
    assert len(children) == 1


def test_project_children_does_not_cross_contaminate(db, rem):
    """3 children for A, 1 for B: each returns correct count."""
    pid_a = _create_project_with_children(db, rem, "Project A outcome", "@home", 3)
    pid_b = _create_project_with_children(db, rem, "Project B outcome", "@calls", 1)
    children_a = project_children(pid_a, conn=db, rem_module=rem)
    children_b = project_children(pid_b, conn=db, rem_module=rem)
    assert len(children_a) == 3
    assert len(children_b) == 1


def test_project_children_excludes_completed_reminders(db, rem):
    pid = create_project("Done project", "Get done", conn=db, rem_module=rem)
    add_next_action(pid, "@home", "Open action", conn=db, rem_module=rem)
    # Simulate a completed reminder by marking it in the stub
    for r in rem._reminders:
        if r.name == "Open action":
            r.completed = True
    children = project_children(pid, conn=db, rem_module=rem)
    assert len(children) == 0


def test_project_children_returns_rid_and_name(db, rem):
    pid = create_project("My project", "My outcome", conn=db, rem_module=rem)
    add_next_action(pid, "@home", "Pick up package", conn=db, rem_module=rem)
    children = project_children(pid, conn=db, rem_module=rem)
    assert len(children) == 1
    child = children[0]
    assert "rid" in child
    assert child["name"] == "Pick up package"


# ---------------------------------------------------------------------------
# stalled_projects
# ---------------------------------------------------------------------------


def test_stalled_projects_no_children_in_result(db, rem):
    pid = create_project("Orphan project", "Accomplish nothing", conn=db, rem_module=rem)
    stalled = stalled_projects(conn=db, rem_module=rem)
    stalled_ids = {p["project_id"] for p in stalled}
    assert pid in stalled_ids


def test_stalled_projects_with_child_not_in_result(db, rem):
    pid = create_project("Active project", "Do things", conn=db, rem_module=rem)
    add_next_action(pid, "@home", "First action", conn=db, rem_module=rem)
    stalled = stalled_projects(conn=db, rem_module=rem)
    stalled_ids = {p["project_id"] for p in stalled}
    assert pid not in stalled_ids


def test_stalled_projects_mixed(db, rem):
    """One stalled, one active: only stalled appears."""
    pid_stalled = create_project("Stalled proj", "Outcome A", conn=db, rem_module=rem)
    pid_active = create_project("Active proj", "Outcome B", conn=db, rem_module=rem)
    add_next_action(pid_active, "@home", "Do B", conn=db, rem_module=rem)

    stalled = stalled_projects(conn=db, rem_module=rem)
    stalled_ids = {p["project_id"] for p in stalled}
    assert pid_stalled in stalled_ids
    assert pid_active not in stalled_ids


def test_stalled_projects_returns_project_dict_with_outcome(db, rem):
    create_project("Stalled", "Important outcome", conn=db, rem_module=rem)
    stalled = stalled_projects(conn=db, rem_module=rem)
    assert len(stalled) >= 1
    p = stalled[0]
    assert "project_id" in p
    assert "outcome" in p
    assert p["outcome"] == "Important outcome"


# ---------------------------------------------------------------------------
# check_invariants — 0 stalled
# ---------------------------------------------------------------------------


def test_check_invariants_zero_stalled_no_q_dispatched(db, rem, qch):
    pid = create_project("Active", "Stay active", conn=db, rem_module=rem)
    add_next_action(pid, "@home", "Keep going", conn=db, rem_module=rem)
    result = check_invariants(conn=db, rem_module=rem, qchannel_module=qch)
    assert result == {"stalled_count": 0, "q_dispatched": False}
    assert len(qch._dispatch_calls) == 0


# ---------------------------------------------------------------------------
# check_invariants — 1 stalled
# ---------------------------------------------------------------------------


def test_check_invariants_one_stalled_dispatches_digest_q(db, rem, qch):
    pid = create_project("Stalled", "Build the thing", conn=db, rem_module=rem)
    result = check_invariants(conn=db, rem_module=rem, qchannel_module=qch)
    assert result["stalled_count"] == 1
    assert result["q_dispatched"] is True
    assert len(qch._dispatch_calls) == 1


def test_check_invariants_one_stalled_dispatch_has_digest_true(db, rem, qch):
    create_project("Stalled", "Build the thing", conn=db, rem_module=rem)
    check_invariants(conn=db, rem_module=rem, qchannel_module=qch)
    call = qch._dispatch_calls[0]
    assert call["digest"] is True


def test_check_invariants_one_stalled_payload_includes_project(db, rem, qch):
    pid = create_project("Stalled", "Build the thing", conn=db, rem_module=rem)
    check_invariants(conn=db, rem_module=rem, qchannel_module=qch)
    call = qch._dispatch_calls[0]
    payload = call["payload"]
    assert "stalled_projects" in payload
    project_ids = [p["project_id"] for p in payload["stalled_projects"]]
    assert pid in project_ids


def test_check_invariants_one_stalled_payload_includes_outcome(db, rem, qch):
    create_project("Stalled", "Build the thing", conn=db, rem_module=rem)
    check_invariants(conn=db, rem_module=rem, qchannel_module=qch)
    call = qch._dispatch_calls[0]
    outcomes = [p["outcome"] for p in call["payload"]["stalled_projects"]]
    assert "Build the thing" in outcomes


# ---------------------------------------------------------------------------
# check_invariants — 3 stalled → exactly 1 Q dispatched (digest/bulk)
# ---------------------------------------------------------------------------


def test_check_invariants_three_stalled_exactly_one_q_dispatched(db, rem, qch):
    create_project("Project A", "Outcome A", conn=db, rem_module=rem)
    create_project("Project B", "Outcome B", conn=db, rem_module=rem)
    create_project("Project C", "Outcome C", conn=db, rem_module=rem)
    result = check_invariants(conn=db, rem_module=rem, qchannel_module=qch)
    assert result["stalled_count"] == 3
    assert result["q_dispatched"] is True
    # Critical: bulk-producer mode — NOT 3 separate Qs
    assert len(qch._dispatch_calls) == 1


def test_check_invariants_three_stalled_all_projects_in_payload(db, rem, qch):
    pids = []
    for i in range(3):
        pid = create_project(f"Project {i}", f"Outcome {i}", conn=db, rem_module=rem)
        pids.append(pid)
    check_invariants(conn=db, rem_module=rem, qchannel_module=qch)
    call = qch._dispatch_calls[0]
    payload_pids = {p["project_id"] for p in call["payload"]["stalled_projects"]}
    for pid in pids:
        assert pid in payload_pids


def test_check_invariants_three_stalled_digest_flag_true(db, rem, qch):
    for i in range(3):
        create_project(f"Project {i}", f"Outcome {i}", conn=db, rem_module=rem)
    check_invariants(conn=db, rem_module=rem, qchannel_module=qch)
    assert qch._dispatch_calls[0]["digest"] is True


# ---------------------------------------------------------------------------
# check_invariants — q_dispatched flag from dispatch status
# ---------------------------------------------------------------------------


def test_check_invariants_returns_q_dispatched_false_on_cap(db, rem):
    """If qchannel returns a non-dispatching status, q_dispatched is False."""

    class CapQChannel:
        def dispatch(self, **kwargs) -> DispatchResultStub:
            return DispatchResultStub(qid=None, status="cap_open")

    create_project("Stalled", "Outcome", conn=db, rem_module=rem)
    result = check_invariants(conn=db, rem_module=rem, qchannel_module=CapQChannel())
    assert result["stalled_count"] == 1
    assert result["q_dispatched"] is False
