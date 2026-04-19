"""
Unit tests for gtd/engine/write_fence.py.

Covers:
- assert_writable with managed lists (returns None)
- assert_writable with legacy lists (raises WriteScopeError)
- WriteScopeError fields (rid, attempted_list, allowed)
- invariants_log JSONL line written before raise
- invariants_log auto-creates missing parent directory
- is_writable with default and custom allowed sets
- Custom allowed set overrides default
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from gtd.engine.write_fence import (  # noqa: E402
    DEFAULT_MANAGED_LISTS,
    WriteScopeError,
    assert_writable,
    is_writable,
)


# ────────────────────────────────────────────────────────────────────────
# assert_writable — happy paths (managed lists)
# ────────────────────────────────────────────────────────────────────────

def test_assert_writable_inbox_returns_none():
    result = assert_writable("rid-001", "Inbox")
    assert result is None


def test_assert_writable_at_home_returns_none():
    result = assert_writable("rid-002", "@home")
    assert result is None


# ────────────────────────────────────────────────────────────────────────
# assert_writable — raises on legacy lists
# ────────────────────────────────────────────────────────────────────────

def test_assert_writable_legacy_list_raises():
    with pytest.raises(WriteScopeError) as exc_info:
        assert_writable("rid-003", "Books to Read")
    err = exc_info.value
    assert err.rid == "rid-003"
    assert err.attempted_list == "Books to Read"
    assert isinstance(err.allowed, set)
    assert "Inbox" in err.allowed


def test_assert_writable_error_message_contains_key_fields():
    with pytest.raises(WriteScopeError) as exc_info:
        assert_writable("rid-abc", "wine")
    msg = str(exc_info.value)
    assert "rid-abc" in msg
    assert "wine" in msg
    assert "allowed lists" in msg


# ────────────────────────────────────────────────────────────────────────
# WriteScopeError fields are preserved on the exception
# ────────────────────────────────────────────────────────────────────────

def test_write_scope_error_fields_readable():
    err = WriteScopeError("rid-X", "Personal", {"Inbox", "Someday"})
    assert err.rid == "rid-X"
    assert err.attempted_list == "Personal"
    assert err.allowed == {"Inbox", "Someday"}


# ────────────────────────────────────────────────────────────────────────
# invariants_log — JSONL line written before raise
# ────────────────────────────────────────────────────────────────────────

def test_invariants_log_written_then_raises(tmp_path: Path):
    log_file = tmp_path / "violations.jsonl"
    with pytest.raises(WriteScopeError):
        assert_writable("rid-log", "Books to Read", invariants_log=log_file)

    assert log_file.exists(), "log file should have been created"
    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["kind"] == "write_scope_violation"
    assert entry["rid"] == "rid-log"
    assert entry["attempted_list"] == "Books to Read"
    assert "ts" in entry
    assert entry["allowed_count"] == len(DEFAULT_MANAGED_LISTS)


def test_invariants_log_multiple_violations_appended(tmp_path: Path):
    log_file = tmp_path / "violations.jsonl"
    for rid in ("r1", "r2"):
        with pytest.raises(WriteScopeError):
            assert_writable(rid, "Johnny", invariants_log=log_file)

    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 2


# ────────────────────────────────────────────────────────────────────────
# invariants_log — auto-creates missing parent directory
# ────────────────────────────────────────────────────────────────────────

def test_invariants_log_creates_missing_dir(tmp_path: Path):
    nested_log = tmp_path / "deeply" / "nested" / "dir" / "violations.jsonl"
    assert not nested_log.parent.exists()
    with pytest.raises(WriteScopeError):
        assert_writable("rid-dir", "wine", invariants_log=nested_log)
    assert nested_log.exists(), "log file (and parent dirs) should be created"


# ────────────────────────────────────────────────────────────────────────
# is_writable — default allowed set
# ────────────────────────────────────────────────────────────────────────

def test_is_writable_inbox_true():
    assert is_writable("Inbox") is True


def test_is_writable_legacy_false():
    assert is_writable("Books to Read") is False


def test_is_writable_all_default_managed_lists():
    for lst in DEFAULT_MANAGED_LISTS:
        assert is_writable(lst) is True, f"Expected {lst!r} to be writable"


# ────────────────────────────────────────────────────────────────────────
# Custom allowed set overrides default
# ────────────────────────────────────────────────────────────────────────

def test_custom_allowed_foo_writable():
    assert is_writable("foo", allowed={"foo"}) is True


def test_custom_allowed_inbox_not_writable():
    assert is_writable("Inbox", allowed={"foo"}) is False


def test_assert_writable_custom_allowed_passes():
    result = assert_writable("rid-custom", "foo", allowed={"foo"})
    assert result is None


def test_assert_writable_custom_allowed_raises_for_inbox():
    with pytest.raises(WriteScopeError) as exc_info:
        assert_writable("rid-custom2", "Inbox", allowed={"foo"})
    assert exc_info.value.attempted_list == "Inbox"
    assert exc_info.value.allowed == {"foo"}
