"""
Unit tests for `gtd clarifier evaluate` CLI surface (AC-CLI-1..3, AC-TEST-CL-4).
"""
from __future__ import annotations

import io
import json
import sys
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

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


def _run(*argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_mod.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


def test_evaluate_accept_human_format():
    rc, out, _err = _run("clarifier", "evaluate", "Pay the dental bill")
    assert rc == 0
    assert "verdict=ACCEPT" in out
    assert "all gates pass" in out


def test_evaluate_needs_question_g1_human_format():
    rc, out, _err = _run("clarifier", "evaluate", "Vanguard")
    assert rc == 0
    assert "verdict=NEEDS_QUESTION" in out
    assert "failed_gate=actionable" in out
    assert "proposed_question:" in out
    assert "recommended_disposition=Someday" in out


def test_evaluate_needs_question_g3_human_format():
    rc, out, _err = _run("clarifier", "evaluate", "Start CompassionAI")
    assert rc == 0
    assert "verdict=NEEDS_QUESTION" in out
    assert "failed_gate=next_action_concrete" in out
    assert "recommended_disposition=Projects" in out


def test_evaluate_json_mode_parseable():
    rc, out, _err = _run("clarifier", "evaluate", "Vanguard", "--json")
    assert rc == 0
    parsed = json.loads(out)
    assert set(parsed.keys()) >= {
        "verdict", "failed_gate", "reason",
        "proposed_question", "recommended_disposition",
    }
    assert parsed["verdict"] == "NEEDS_QUESTION"
    assert parsed["failed_gate"] == "actionable"
    assert parsed["recommended_disposition"] == "Someday"


def test_evaluate_json_mode_accept_serializes_nones():
    rc, out, _err = _run("clarifier", "evaluate", "Pay the dental bill", "--json")
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["verdict"] == "ACCEPT"
    assert parsed["failed_gate"] is None
    assert parsed["proposed_question"] is None
    assert parsed["recommended_disposition"] is None


def test_evaluate_empty_text_exits_2():
    rc, _out, err = _run("clarifier", "evaluate", "")
    assert rc == 2
    assert "empty" in err.lower()


def test_evaluate_missing_subcommand_exits_2():
    """`gtd clarifier` (bare) → exit 2, helpful message."""
    rc, _out, err = _run("clarifier")
    assert rc == 2
    assert "evaluate" in err


def test_evaluate_dry_run_flag_accepted():
    """`--dry-run` is registered (no-op for clarifier, but the flag must
    be accepted so the SUPPRESS pattern is consistent across subcommands)."""
    rc, out, _err = _run("--dry-run", "clarifier", "evaluate", "Pay the dental bill")
    assert rc == 0
    assert "verdict=ACCEPT" in out
