"""
Unit tests for cmd_next empty-state messaging (AC-TEST-11).

Goal: an empty result must print actionable guidance, not silence. A user
typing `/gtd:next --ctx @home` with zero items currently received an empty
chat reply — indistinguishable from a CLI hang.
"""
from __future__ import annotations

import io
import sys
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import gtd.engine.cli as cli_mod


@pytest.fixture(autouse=True)
def stub_lock(monkeypatch, tmp_path):
    @contextmanager
    def fake_acquire(path, *, holder_argv0="gtd-engine", timeout_s=60.0):
        yield
    monkeypatch.setattr("gtd.engine.cli.LOCK_PATH", tmp_path / "engine.lock")
    try:
        import gtd.engine.lock as lock_mod
        monkeypatch.setattr(lock_mod, "acquire", fake_acquire)
    except ImportError:
        pass


def test_cmd_next_empty_with_ctx_shows_helpful_message(monkeypatch):
    """Empty list + --ctx @home → message names the context and points to actions."""
    with patch("gtd.engine.engage.next_actions", return_value=[]):
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli_mod.main(["next", "--ctx", "@home"])
    assert rc == 0
    msg = out.getvalue()
    assert "@home" in msg
    assert "/gtd:capture" in msg or "capture" in msg.lower()


def test_cmd_next_empty_no_ctx_shows_helpful_message(monkeypatch):
    """No --ctx and empty result → still helpful, doesn't print 'None' or empty."""
    with patch("gtd.engine.engage.next_actions", return_value=[]):
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli_mod.main(["next"])
    assert rc == 0
    msg = out.getvalue().strip()
    assert msg, "empty-state output must not be blank"
    assert "any context" in msg or "no next actions" in msg.lower()


def test_cmd_next_with_actions_calls_format(monkeypatch):
    """Sanity: when there ARE actions, the formatter is called and output is non-empty."""
    fake_actions = [{"id": "X", "title": "Do X", "ctx": "@home"}]
    with patch("gtd.engine.engage.next_actions", return_value=fake_actions), \
         patch("gtd.engine.engage.format_for_chat", return_value="formatted output"):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli_mod.main(["next", "--ctx", "@home"])
    assert rc == 0
    assert "formatted output" in out.getvalue()
