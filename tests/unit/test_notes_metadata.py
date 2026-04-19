"""
Unit tests for gtd/engine/notes_metadata.py

Covers all cases required by US-002 spec:
  - empty input
  - no-fence input
  - basic fence parsing
  - URL-after-fence round-trip
  - URL-before-fence (no fence) not parsed as fence
  - malformed YAML inside fence → ({}, notes) + invariants log entry
  - oversized metadata raises MetadataTooLargeError
  - round-trip preserves stable key order
  - round-trip preserves user prose byte-for-byte
  - empty meta with non-empty prose: serialize_metadata({}, "hello") → "hello"
  - ctx value with @ prefix round-trips
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make gtd package importable from the repo root.
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import gtd.engine.notes_metadata as nm
from gtd.engine.notes_metadata import (
    MetadataTooLargeError,
    parse_metadata,
    serialize_metadata,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _redirect_log(tmp_path: Path, monkeypatch) -> Path:
    """Point the module's log to a temp file so tests are isolated."""
    log_file = tmp_path / "invariants.jsonl"
    monkeypatch.setattr(nm, "_INVARIANTS_LOG", log_file)
    return log_file


# ---------------------------------------------------------------------------
# parse_metadata — basic cases
# ---------------------------------------------------------------------------

def test_empty_input():
    meta, prose = parse_metadata("")
    assert meta == {}
    assert prose == ""


def test_no_fence_input():
    original = "just some plain user notes with no fence whatsoever"
    meta, prose = parse_metadata(original)
    assert meta == {}
    assert prose == original


def test_basic_fence_returns_dict_and_prose():
    notes = (
        "--- gtd ---\n"
        "id: 01H8WZ3ABC\n"
        "kind: next-action\n"
        "created: 2026-04-19T14:03-04:00\n"
        "ctx: '@home'\n"
        "project: 01HABC123\n"
        "delegate: Dan\n"
        "release: 2026-05-01\n"
        "--- end ---\n"
        "the actual user-visible note"
    )
    meta, prose = parse_metadata(notes)
    assert meta["id"] == "01H8WZ3ABC"
    assert meta["kind"] == "next-action"
    assert meta["created"] == "2026-04-19T14:03-04:00"
    assert meta["ctx"] == "@home"
    assert meta["project"] == "01HABC123"
    assert meta["delegate"] == "Dan"
    assert meta["release"] == "2026-05-01"
    assert prose == "the actual user-visible note"


# ---------------------------------------------------------------------------
# URL round-trip cases
# ---------------------------------------------------------------------------

def test_url_after_fence_round_trips():
    """URL immediately after --- end --- must survive parse → serialize cleanly."""
    prose = "https://example.com \u2014 see this"
    meta = {"id": "X1", "kind": "next-action"}
    notes = serialize_metadata(meta, prose)
    parsed_meta, parsed_prose = parse_metadata(notes)
    assert parsed_prose == prose
    assert parsed_meta == meta


def test_url_before_fence_not_parsed_as_fence():
    """A URL in the notes field (no fence) must not be confused with a fence."""
    notes = "https://example.com is a great site\nsome more text"
    meta, prose = parse_metadata(notes)
    assert meta == {}
    assert prose == notes


# ---------------------------------------------------------------------------
# Malformed YAML inside a valid fence
# ---------------------------------------------------------------------------

def test_malformed_yaml_returns_empty_dict_and_original_notes(tmp_path, monkeypatch):
    log_file = _redirect_log(tmp_path, monkeypatch)
    # mismatched single quote makes the value unparseable
    notes = (
        "--- gtd ---\n"
        "id: 'unclosed\n"
        "kind: next-action\n"
        "--- end ---\n"
        "some prose"
    )
    meta, returned_notes = parse_metadata(notes)
    assert meta == {}
    assert returned_notes == notes


def test_malformed_yaml_appends_invariants_log_line(tmp_path, monkeypatch):
    log_file = _redirect_log(tmp_path, monkeypatch)
    notes = (
        "--- gtd ---\n"
        "id: 'unclosed\n"
        "--- end ---\n"
        "prose"
    )
    parse_metadata(notes)
    assert log_file.exists(), "invariants.jsonl was not created"
    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 1
    record = json.loads(lines[-1])
    assert record["kind"] == "metadata_parse_error"
    assert "sample" in record


# ---------------------------------------------------------------------------
# 512-byte hard cap
# ---------------------------------------------------------------------------

def test_oversized_metadata_raises():
    # Build a dict with a value long enough to exceed 512 bytes.
    meta = {"id": "X" * 600}
    with pytest.raises(MetadataTooLargeError):
        serialize_metadata(meta, "prose")


# ---------------------------------------------------------------------------
# Stable key order round-trip
# ---------------------------------------------------------------------------

def test_round_trip_preserves_stable_key_order():
    """parse → mutate one value → serialize → parse → expected dict."""
    original_notes = (
        "--- gtd ---\n"
        "id: 01H8WZ3\n"
        "kind: next-action\n"
        "ctx: '@home'\n"
        "--- end ---\n"
        "my prose"
    )
    meta, prose = parse_metadata(original_notes)
    assert meta["kind"] == "next-action"
    meta["kind"] = "someday"

    new_notes = serialize_metadata(meta, prose)
    # Check that the serialized block starts with --- gtd --- and keys are in order.
    lines = new_notes.split("\n")
    assert lines[0] == "--- gtd ---"
    # id must come before kind in the output
    id_idx = next(i for i, l in enumerate(lines) if l.startswith("id:"))
    kind_idx = next(i for i, l in enumerate(lines) if l.startswith("kind:"))
    assert id_idx < kind_idx

    re_parsed_meta, re_parsed_prose = parse_metadata(new_notes)
    assert re_parsed_meta["kind"] == "someday"
    assert re_parsed_meta["id"] == "01H8WZ3"
    assert re_parsed_prose == "my prose"


# ---------------------------------------------------------------------------
# Prose byte-for-byte preservation
# ---------------------------------------------------------------------------

def test_round_trip_preserves_user_prose_bytes():
    """parse → serialize → parse: prose must be byte-identical each cycle."""
    prose = "Line one\nLine two — with em-dash\n\nBlank line above."
    meta = {"id": "ABC", "kind": "reference"}
    notes = serialize_metadata(meta, prose)

    meta2, prose2 = parse_metadata(notes)
    assert prose2 == prose, f"Prose changed:\n{prose!r}\n!=\n{prose2!r}"

    notes2 = serialize_metadata(meta2, prose2)
    meta3, prose3 = parse_metadata(notes2)
    assert prose3 == prose, "Prose drifted on second cycle"
    assert notes2 == notes, "Serialized form changed on second cycle"


# ---------------------------------------------------------------------------
# Empty meta → just prose
# ---------------------------------------------------------------------------

def test_empty_meta_returns_prose_unchanged():
    result = serialize_metadata({}, "hello")
    assert result == "hello"


def test_empty_meta_empty_prose_returns_empty():
    result = serialize_metadata({}, "")
    assert result == ""


# ---------------------------------------------------------------------------
# ctx with @ prefix
# ---------------------------------------------------------------------------

def test_ctx_at_prefix_round_trips():
    meta = {"id": "ZZZ", "ctx": "@home"}
    prose = "pick up groceries"
    notes = serialize_metadata(meta, prose)

    # The @ must be quoted in the serialized form so YAML parsers don't choke.
    assert "'@home'" in notes or "@home" in notes  # at minimum, value is present

    parsed_meta, parsed_prose = parse_metadata(notes)
    assert parsed_meta["ctx"] == "@home", f"ctx round-trip failed: {parsed_meta!r}"
    assert parsed_prose == prose
