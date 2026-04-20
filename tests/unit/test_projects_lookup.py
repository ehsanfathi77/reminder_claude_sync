"""
Unit tests for projects.lookup_by_name_or_ulid (AC-UX-6).

Three resolution paths:
  1. ULID (regex match) → direct DB lookup
  2. Name (case-insensitive) → walk Projects-list reminders
  3. Miss → ProjectNotFound; multi-match → AmbiguousProjectName
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import gtd.engine.state as state_mod
from gtd.engine.notes_metadata import serialize_metadata
from gtd.engine.projects import (
    AmbiguousProjectName,
    ProjectNotFound,
    lookup_by_name_or_ulid,
)


class StubR:
    def __init__(self, items):
        self.items = items

    def list_all(self, *a, **kw):
        return list(self.items)


def _project_reminder(name: str, project_id: str, outcome: str = "x"):
    notes = serialize_metadata(
        {"id": project_id, "kind": "project", "outcome": outcome, "created": "2026-01-01"},
        "",
    )
    return SimpleNamespace(
        id=f"rid-{project_id}",
        name=name,
        body=notes,
        list="Projects",
        completed=False,
    )


def test_lookup_by_ulid_hits_db(tmp_path):
    db = tmp_path / "state.db"
    conn = state_mod.init_db(db)
    try:
        ulid = "01HZZZZZZZZZZZZZZZZZZZZZZZ"
        state_mod.insert_project(conn, ulid, "Big outcome")
        result = lookup_by_name_or_ulid(ulid, conn=conn)
        assert result["project_id"] == ulid
        assert result["outcome"] == "Big outcome"
    finally:
        conn.close()


def test_lookup_by_ulid_unknown_raises_not_found(tmp_path):
    db = tmp_path / "state.db"
    conn = state_mod.init_db(db)
    try:
        with pytest.raises(ProjectNotFound):
            lookup_by_name_or_ulid("01HXXXXXXXXXXXXXXXXXXXXXXX", conn=conn)
    finally:
        conn.close()


def test_lookup_by_name_walks_reminders(tmp_path):
    db = tmp_path / "state.db"
    conn = state_mod.init_db(db)
    try:
        rems = [
            _project_reminder("Complete IP agreement", "01HABC1234567890ABCDEFGHJK", "Signed"),
            _project_reminder("Another project", "01HDEF1234567890ABCDEFGHJK", "Other"),
        ]
        result = lookup_by_name_or_ulid("Complete IP agreement", conn=conn, rem_module=StubR(rems))
        assert result["project_id"] == "01HABC1234567890ABCDEFGHJK"
        assert result["outcome"] == "Signed"
        assert result["name"] == "Complete IP agreement"
    finally:
        conn.close()


def test_lookup_by_name_case_insensitive(tmp_path):
    db = tmp_path / "state.db"
    conn = state_mod.init_db(db)
    try:
        rems = [_project_reminder("Build the Thing", "01HBBB1234567890ABCDEFGHJK")]
        result = lookup_by_name_or_ulid("build the THING", conn=conn, rem_module=StubR(rems))
        assert result["project_id"] == "01HBBB1234567890ABCDEFGHJK"
    finally:
        conn.close()


def test_lookup_by_name_unknown_raises_not_found(tmp_path):
    db = tmp_path / "state.db"
    conn = state_mod.init_db(db)
    try:
        rems = [_project_reminder("Existing", "01HEEE1234567890ABCDEFGHJK")]
        with pytest.raises(ProjectNotFound):
            lookup_by_name_or_ulid("Nonexistent", conn=conn, rem_module=StubR(rems))
    finally:
        conn.close()


def test_lookup_by_name_multi_match_raises_ambiguous(tmp_path):
    db = tmp_path / "state.db"
    conn = state_mod.init_db(db)
    try:
        rems = [
            _project_reminder("Same Name", "01HAAA1234567890ABCDEFGHJK"),
            _project_reminder("Same Name", "01HBBB1234567890ABCDEFGHJK"),
        ]
        with pytest.raises(AmbiguousProjectName) as exc_info:
            lookup_by_name_or_ulid("Same Name", conn=conn, rem_module=StubR(rems))
        assert len(exc_info.value.matches) == 2
    finally:
        conn.close()


def test_lookup_skips_completed_projects(tmp_path):
    db = tmp_path / "state.db"
    conn = state_mod.init_db(db)
    try:
        completed = _project_reminder("Closed Project", "01HCCC1234567890ABCDEFGHJK")
        completed.completed = True
        with pytest.raises(ProjectNotFound):
            lookup_by_name_or_ulid("Closed Project", conn=conn, rem_module=StubR([completed]))
    finally:
        conn.close()


def test_lookup_skips_non_projects_list_items(tmp_path):
    """A reminder named like the query but not in the Projects list must not match."""
    db = tmp_path / "state.db"
    conn = state_mod.init_db(db)
    try:
        confusing = _project_reminder("My Project", "01HDDD1234567890ABCDEFGHJK")
        confusing.list = "Inbox"  # not in Projects
        with pytest.raises(ProjectNotFound):
            lookup_by_name_or_ulid("My Project", conn=conn, rem_module=StubR([confusing]))
    finally:
        conn.close()


def test_lookup_empty_query_raises():
    with pytest.raises(ProjectNotFound):
        lookup_by_name_or_ulid("", conn=None)
    with pytest.raises(ProjectNotFound):
        lookup_by_name_or_ulid("   ", conn=None)
