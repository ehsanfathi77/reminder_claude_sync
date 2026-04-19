"""
Integration tests for gtd dryrun-report subcommand (US-020).

Tests:
  1. Clean fixture (dryrun_47events.jsonl) → exit 0, stdout contains 'VERDICT: READY TO FLIP'
  2. Breach fixture (dryrun_breach.jsonl)  → exit 1, stdout contains 'VERDICT: DO NOT FLIP' + failing check name
  3. --json output is valid JSON with 'verdict' field
  4. --json with breach fixture → verdict = 'DO NOT FLIP', failing[] non-empty
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch
from contextlib import contextmanager

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "tests" / "fixtures"
CLEAN_FIXTURE = FIXTURES / "dryrun_47events.jsonl"
BREACH_FIXTURE = FIXTURES / "dryrun_breach.jsonl"

import gtd.engine.cli as cli_mod


# ---------------------------------------------------------------------------
# Shared stubs
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def stub_lock(monkeypatch, tmp_path):
    """Stub lock so tests don't need .gtd/."""
    @contextmanager
    def fake_acquire(path, *, holder_argv0="gtd-engine", timeout_s=60.0):
        yield

    monkeypatch.setattr(cli_mod, "LOCK_PATH", tmp_path / "engine.lock")
    try:
        import gtd.engine.lock as lock_mod
        monkeypatch.setattr(lock_mod, "acquire", fake_acquire)
    except ImportError:
        pass


@pytest.fixture(autouse=True)
def stub_config(monkeypatch, tmp_path):
    cfg = {
        "dispatch_dryrun": True,
        "flip_at_iso": None,
        "managed_lists": None,
        "quiet_hours": [22, 8],
        "q_max_open": 3,
        "q_max_per_day": 8,
    }
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg.copy())
    monkeypatch.setattr(cli_mod, "save_config", lambda c: None)
    monkeypatch.setattr(cli_mod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(cli_mod, "LOG_DIR", tmp_path / "log")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def run_report(fixture_path: Path, extra_args: list[str] | None = None, days: int = 7) -> tuple[int, str]:
    """Run dryrun-report against a fixture, capture stdout, return (rc, stdout)."""
    argv = ["dryrun-report", "--days", str(days), "--log-path", str(fixture_path)]
    if extra_args:
        argv.extend(extra_args)

    captured_lines: list[str] = []
    original_print = print

    def capture_print(*args, **kwargs):
        if kwargs.get("file") is sys.stderr:
            original_print(*args, **kwargs)
            return
        line = " ".join(str(a) for a in args)
        captured_lines.append(line)
        original_print(*args, **kwargs)

    import builtins
    with patch.object(builtins, "print", side_effect=capture_print):
        rc = cli_mod.main(argv)

    return rc, "\n".join(captured_lines)


# ---------------------------------------------------------------------------
# Test 1: Clean fixture → READY TO FLIP
# ---------------------------------------------------------------------------

class TestCleanFixture:
    def test_exit_code_0(self):
        rc, _ = run_report(CLEAN_FIXTURE, days=8)  # days=8 to cover all 7 days of fixture
        assert rc == 0, "Clean fixture should exit 0"

    def test_verdict_ready_to_flip(self, capsys):
        rc = cli_mod.main([
            "dryrun-report",
            "--days", "8",
            "--log-path", str(CLEAN_FIXTURE),
        ])
        out = capsys.readouterr().out
        assert "READY TO FLIP" in out, f"Expected 'READY TO FLIP' in output, got:\n{out}"
        assert rc == 0

    def test_all_checks_pass(self, capsys):
        cli_mod.main([
            "dryrun-report",
            "--days", "8",
            "--log-path", str(CLEAN_FIXTURE),
        ])
        out = capsys.readouterr().out
        # Pretty format shows PASS for each check
        assert "PASS" in out

    def test_event_count_reported(self, capsys):
        cli_mod.main([
            "dryrun-report",
            "--days", "8",
            "--log-path", str(CLEAN_FIXTURE),
        ])
        out = capsys.readouterr().out
        # 47 events should be mentioned
        assert "47" in out, f"Expected 47 events in output:\n{out}"


# ---------------------------------------------------------------------------
# Test 2: Breach fixture → DO NOT FLIP
# ---------------------------------------------------------------------------

class TestBreachFixture:
    def test_exit_code_1(self):
        rc, _ = run_report(BREACH_FIXTURE, days=8)
        assert rc == 1, "Breach fixture should exit 1"

    def test_verdict_do_not_flip(self, capsys):
        rc = cli_mod.main([
            "dryrun-report",
            "--days", "8",
            "--log-path", str(BREACH_FIXTURE),
        ])
        out = capsys.readouterr().out
        assert "DO NOT FLIP" in out, f"Expected 'DO NOT FLIP' in output, got:\n{out}"
        assert rc == 1

    def test_failing_check_named(self, capsys):
        """Output must name which check failed."""
        cli_mod.main([
            "dryrun-report",
            "--days", "8",
            "--log-path", str(BREACH_FIXTURE),
        ])
        out = capsys.readouterr().out
        # The breach fixture has 9 events in one day (> q_max_per_day=8)
        # and open_count=5 (> 4)
        # At least one failing check should be named
        has_daily_fail = "daily" in out.lower() or "per_day" in out.lower() or "cap" in out.lower()
        has_open_fail = "open" in out.lower() or "watermark" in out.lower()
        assert has_daily_fail or has_open_fail, (
            f"Expected failing check name in output, got:\n{out}"
        )

    def test_fail_shows_at_least_one_fail_row(self, capsys):
        cli_mod.main([
            "dryrun-report",
            "--days", "8",
            "--log-path", str(BREACH_FIXTURE),
        ])
        out = capsys.readouterr().out
        assert "FAIL" in out, f"Expected 'FAIL' row in output:\n{out}"


# ---------------------------------------------------------------------------
# Test 3: --json output is valid JSON with verdict field
# ---------------------------------------------------------------------------

class TestJsonOutput:
    def test_clean_fixture_json_valid(self, capsys):
        rc = cli_mod.main([
            "dryrun-report",
            "--days", "8",
            "--log-path", str(CLEAN_FIXTURE),
            "--json",
        ])
        raw = capsys.readouterr().out
        data = json.loads(raw)  # raises if invalid
        assert "verdict" in data, f"JSON missing 'verdict' field: {data.keys()}"

    def test_clean_fixture_json_verdict_ready(self, capsys):
        cli_mod.main([
            "dryrun-report",
            "--days", "8",
            "--log-path", str(CLEAN_FIXTURE),
            "--json",
        ])
        raw = capsys.readouterr().out
        data = json.loads(raw)
        assert data["verdict"] == "READY TO FLIP", f"Expected READY TO FLIP, got {data['verdict']}"
        assert data["all_green"] is True

    def test_clean_fixture_json_has_checks(self, capsys):
        cli_mod.main([
            "dryrun-report",
            "--days", "8",
            "--log-path", str(CLEAN_FIXTURE),
            "--json",
        ])
        raw = capsys.readouterr().out
        data = json.loads(raw)
        assert "checks" in data
        assert "daily_cap" in data["checks"]
        assert "open_watermark" in data["checks"]
        assert "distribution" in data["checks"]
        assert "cap_breaches" in data["checks"]

    def test_breach_fixture_json_verdict_do_not_flip(self, capsys):
        rc = cli_mod.main([
            "dryrun-report",
            "--days", "8",
            "--log-path", str(BREACH_FIXTURE),
            "--json",
        ])
        raw = capsys.readouterr().out
        data = json.loads(raw)
        assert data["verdict"] == "DO NOT FLIP", f"Expected DO NOT FLIP, got {data['verdict']}"
        assert data["all_green"] is False
        assert rc == 1

    def test_breach_fixture_json_failing_non_empty(self, capsys):
        cli_mod.main([
            "dryrun-report",
            "--days", "8",
            "--log-path", str(BREACH_FIXTURE),
            "--json",
        ])
        raw = capsys.readouterr().out
        data = json.loads(raw)
        assert len(data.get("failing", [])) > 0, "Expected non-empty failing list for breach fixture"

    def test_breach_fixture_json_checks_have_fail(self, capsys):
        cli_mod.main([
            "dryrun-report",
            "--days", "8",
            "--log-path", str(BREACH_FIXTURE),
            "--json",
        ])
        raw = capsys.readouterr().out
        data = json.loads(raw)
        checks = data["checks"]
        # At least one check must have pass=False
        failing = [k for k, v in checks.items() if not v["pass"]]
        assert failing, f"Expected at least one failing check, got all passing: {checks}"

    def test_json_per_day_histogram_present(self, capsys):
        cli_mod.main([
            "dryrun-report",
            "--days", "8",
            "--log-path", str(CLEAN_FIXTURE),
            "--json",
        ])
        raw = capsys.readouterr().out
        data = json.loads(raw)
        assert "per_day" in data
        assert len(data["per_day"]) > 0

    def test_json_per_kind_present(self, capsys):
        cli_mod.main([
            "dryrun-report",
            "--days", "8",
            "--log-path", str(CLEAN_FIXTURE),
            "--json",
        ])
        raw = capsys.readouterr().out
        data = json.loads(raw)
        assert "per_kind" in data
        # Should see at least clarify and manual
        assert "clarify" in data["per_kind"]
        assert "manual" in data["per_kind"]


# ---------------------------------------------------------------------------
# Test 4: Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_missing_log_file_exits_0(self, tmp_path, capsys):
        """No log file = zero events = all gates pass = exit 0."""
        missing = tmp_path / "nonexistent_qchannel.jsonl"
        rc = cli_mod.main([
            "dryrun-report",
            "--days", "7",
            "--log-path", str(missing),
        ])
        assert rc == 0

    def test_empty_log_file_exits_0(self, tmp_path):
        empty = tmp_path / "empty.jsonl"
        empty.write_text("")
        rc = cli_mod.main([
            "dryrun-report",
            "--days", "7",
            "--log-path", str(empty),
        ])
        assert rc == 0

    def test_days_1_filters_correctly(self, tmp_path, capsys):
        """With --days 1, only today's events should be counted."""
        from datetime import datetime, timezone
        import json as json_mod

        log_file = tmp_path / "q.jsonl"
        now = datetime.now(timezone.utc)
        # One event today
        today_event = {
            "ts": now.isoformat(timespec="seconds"),
            "kind": "clarify", "status": "dispatched",
            "open_count": 1, "per_day_count": 1,
            "stream": "qchannel", "dryrun": True,
            "qid": "QTEST001", "pid": 999,
        }
        # One event 5 days ago (should be excluded)
        old_event = {
            "ts": (now.replace(day=max(1, now.day - 5))).isoformat(timespec="seconds"),
            "kind": "manual", "status": "dispatched",
            "open_count": 1, "per_day_count": 1,
            "stream": "qchannel", "dryrun": True,
            "qid": "QTEST002", "pid": 998,
        }
        log_file.write_text(
            json_mod.dumps(today_event) + "\n" +
            json_mod.dumps(old_event) + "\n"
        )

        rc = cli_mod.main([
            "dryrun-report", "--days", "1",
            "--log-path", str(log_file),
            "--json",
        ])
        out = capsys.readouterr().out
        data = json_mod.loads(out)
        # Only 1 event within last 1 day
        assert data["total_events"] == 1
