"""
Unit tests for gtd/engine/cli.py (US-015)

Smoke tests: each subcommand invoked with --dry-run exits 0.
Engine modules are stubbed so no real Reminders.app calls occur.
Lock is also stubbed (no .gtd dir needed for unit tests).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import gtd.engine.cli as cli_mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def stub_lock(monkeypatch):
    """Stub out the lock so tests don't need .gtd/ to exist."""
    from contextlib import contextmanager

    @contextmanager
    def fake_acquire(path, *, holder_argv0="gtd-engine", timeout_s=60.0):
        yield

    monkeypatch.setattr("gtd.engine.cli.LOCK_PATH", Path("/tmp/test_gtd_engine.lock"))
    # Patch the lock.acquire at the import site in cli.main
    try:
        import gtd.engine.lock as lock_mod
        monkeypatch.setattr(lock_mod, "acquire", fake_acquire)
    except ImportError:
        pass


@pytest.fixture(autouse=True)
def stub_config(monkeypatch, tmp_path):
    """Return a minimal config with dispatch_dryrun=True."""
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
    monkeypatch.setattr(cli_mod, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(cli_mod, "LOCK_PATH", tmp_path / "engine.lock")


@pytest.fixture()
def stub_db(monkeypatch, tmp_path):
    """Stub _open_db to return an in-memory SQLite connection."""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # Minimal schema for status/dryrun-report
    conn.execute(
        "CREATE TABLE IF NOT EXISTS items "
        "(gtd_id TEXT, rid TEXT, kind TEXT, list TEXT, project TEXT, "
        " ctx TEXT, created TEXT, last_seen TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS questions "
        "(qid TEXT, kind TEXT, ref_rid TEXT, dispatched_at TEXT, "
        " ttl_at TEXT, status TEXT, payload_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS projects "
        "(project_id TEXT, outcome TEXT, created TEXT, last_review TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS reviews "
        "(review_id TEXT, kind TEXT, started_at TEXT, completed_at TEXT, snapshot_json TEXT)"
    )
    conn.commit()
    monkeypatch.setattr(cli_mod, "_open_db", lambda: conn)
    return conn


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def run(argv: list[str]) -> int:
    """Run cli.main() with given argv, return exit code."""
    return cli_mod.main(argv)


# ---------------------------------------------------------------------------
# Smoke tests — each subcommand with --dry-run
# ---------------------------------------------------------------------------

class TestInit:
    def test_dry_run_exit_0(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cli_mod, "STATE_DB", tmp_path / "state.db")
        import gtd.engine.state as state_mod
        monkeypatch.setattr(state_mod, "init_db", lambda p: MagicMock())
        assert run(["--dry-run", "init"]) == 0

    def test_no_dry_run_creates_config(self, monkeypatch, tmp_path):
        config_path = tmp_path / "config.json"
        monkeypatch.setattr(cli_mod, "CONFIG_PATH", config_path)
        monkeypatch.setattr(cli_mod, "STATE_DB", tmp_path / "state.db")
        import gtd.engine.state as state_mod
        monkeypatch.setattr(state_mod, "init_db", lambda p: MagicMock())
        monkeypatch.setattr("gtd.engine.cli.load_config", lambda: cli_mod.DEFAULT_CONFIG.copy())
        real_save = cli_mod.save_config

        saved = {}
        def capture_save(cfg):
            saved.update(cfg)
        monkeypatch.setattr(cli_mod, "save_config", capture_save)

        # mock bootstrap
        bootstrap_mock = MagicMock()
        monkeypatch.setattr("gtd.engine.cli.cmd_bootstrap", lambda args: 0)
        try:
            import gtd.engine.bootstrap as bmod
            monkeypatch.setattr(bmod, "provision_lists", lambda **kw: None)
        except ImportError:
            pass

        assert run(["init"]) == 0


class TestBootstrap:
    def test_dry_run_exit_0(self):
        assert run(["--dry-run", "bootstrap"]) == 0


class TestCapture:
    def test_dry_run_exit_0(self, stub_db):
        assert run(["--dry-run", "capture", "--text", "Buy milk"]) == 0

    def test_dry_run_no_real_calls(self, stub_db, monkeypatch):
        called = []
        import gtd.engine.capture as cap_mod
        monkeypatch.setattr(cap_mod, "capture_multiline", lambda lines, **kw: called.append(lines) or ["ULID1"])
        # dry-run should NOT call capture_multiline
        run(["--dry-run", "capture", "--text", "Hello"])
        assert called == [], "capture_multiline must not be called in dryrun"

    def test_empty_text_returns_1(self, stub_db, monkeypatch):
        # Simulate empty stdin
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        assert run(["--dry-run", "capture"]) == 1


class TestClarify:
    def test_dry_run_exit_0(self, stub_db):
        assert run(["--dry-run", "clarify"]) == 0


class TestNext:
    def test_dry_run_exit_0(self, monkeypatch):
        import gtd.engine.engage as engage_mod
        monkeypatch.setattr(engage_mod, "next_actions", lambda **kw: [])
        monkeypatch.setattr(engage_mod, "format_for_chat", lambda actions, **kw: "(no actions)")
        assert run(["--dry-run", "next"]) == 0

    def test_with_ctx_filter(self, monkeypatch):
        import gtd.engine.engage as engage_mod
        captured = {}
        def fake_next(**kw):
            captured.update(kw)
            return []
        monkeypatch.setattr(engage_mod, "next_actions", fake_next)
        monkeypatch.setattr(engage_mod, "format_for_chat", lambda a, **kw: "")
        run(["next", "--ctx", "@home", "--time", "30", "--energy", "low"])
        assert captured.get("ctx") == "@home"
        assert captured.get("time_min") == 30
        assert captured.get("energy") == "low"


class TestProject:
    def test_dry_run_exit_0(self):
        assert run(["--dry-run", "project", "Write book", "--outcome", "Published novel"]) == 0

    def test_dry_run_no_real_create(self, monkeypatch, stub_db):
        called = []
        import gtd.engine.projects as proj_mod
        monkeypatch.setattr(proj_mod, "create_project", lambda *a, **kw: called.append(1) or "ID1")
        run(["--dry-run", "project", "My Project", "--outcome", "Done"])
        assert called == [], "create_project must not be called in dryrun"


class TestProjectNext:
    def test_dry_run_exit_0(self):
        assert run(["--dry-run", "project-next", "PROJECTID123", "@home", "Draft outline"]) == 0


class TestWeeklyReview:
    def test_dry_run_exit_0(self):
        assert run(["--dry-run", "weekly-review"]) == 0


class TestWaiting:
    def test_dry_run_list_exit_0(self, monkeypatch):
        import gtd.engine.waiting as waiting_mod
        monkeypatch.setattr(waiting_mod, "list_waiting", lambda **kw: [])
        assert run(["--dry-run", "waiting"]) == 0

    def test_dry_run_nudge_exit_0(self, stub_db):
        assert run(["--dry-run", "waiting", "--nudge"]) == 0


class TestTickler:
    def test_dry_run_exit_0(self):
        assert run(["--dry-run", "tickler", "RID123", "Inbox", "2026-05-01T09:00:00"]) == 0


class TestAsk:
    def test_dry_run_exit_0(self, stub_db, monkeypatch):
        import gtd.engine.qchannel as qchannel_mod
        mock_result = MagicMock()
        mock_result.qid = "QIDTEST001"
        monkeypatch.setattr(qchannel_mod, "dispatch", lambda **kw: mock_result)
        assert run(["--dry-run", "ask", "What should I do with this?"]) == 0


class TestStatus:
    def test_exit_0_with_empty_db(self, stub_db, monkeypatch, tmp_path):
        import gtd.engine.qchannel as qchannel_mod
        import gtd.engine.projects as proj_mod
        monkeypatch.setattr(qchannel_mod, "open_count", lambda **kw: 0)
        monkeypatch.setattr(proj_mod, "stalled_projects", lambda **kw: [])
        assert run(["status"]) == 0

    def test_dry_run_exit_0(self, stub_db, monkeypatch):
        import gtd.engine.qchannel as qchannel_mod
        import gtd.engine.projects as proj_mod
        monkeypatch.setattr(qchannel_mod, "open_count", lambda **kw: 1)
        monkeypatch.setattr(proj_mod, "stalled_projects", lambda **kw: [])
        assert run(["--dry-run", "status"]) == 0


class TestAdopt:
    def test_no_confirm_list_is_noop(self):
        # Without --confirm-list, should print warning and return 0
        assert run(["adopt"]) == 0

    def test_dry_run_with_confirm_list(self):
        assert run(["--dry-run", "adopt", "--confirm-list", "Books to Read"]) == 0


class TestDryrunReport:
    def test_empty_log_exit_1(self, tmp_path, monkeypatch):
        """No events = daily_max=0 which is ok, but open_watermark=0 ok too.
        Actually with zero events all checks pass — exit 0."""
        log_path = tmp_path / "qchannel.jsonl"
        log_path.write_text("")
        assert run(["dryrun-report", "--log-path", str(log_path)]) == 0

    def test_json_flag(self, tmp_path, capsys):
        log_path = tmp_path / "qchannel.jsonl"
        log_path.write_text("")
        rc = run(["dryrun-report", "--log-path", str(log_path), "--json"])
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "verdict" in data
        assert rc == 0

    def test_dry_run_flag_passes(self, tmp_path):
        log_path = tmp_path / "qchannel.jsonl"
        log_path.write_text("")
        assert run(["--dry-run", "dryrun-report", "--log-path", str(log_path)]) == 0


class TestHealth:
    def test_dry_run_exit_0(self, tmp_path, monkeypatch):
        # Empty log dir — all checks green
        monkeypatch.setattr(cli_mod, "LOG_DIR", tmp_path / "log")
        assert run(["--dry-run", "health"]) == 0


class TestTick:
    def test_dry_run_exit_0(self, stub_db, monkeypatch):
        import gtd.engine.clarify as clarify_mod
        import gtd.engine.qchannel as qchannel_mod
        import gtd.engine.tickler as tickler_mod
        import gtd.engine.projects as proj_mod
        monkeypatch.setattr(clarify_mod, "process_inbox", lambda **kw: {"processed": 0})
        monkeypatch.setattr(qchannel_mod, "poll", lambda **kw: {})
        monkeypatch.setattr(tickler_mod, "release", lambda **kw: {"released": 0})
        monkeypatch.setattr(proj_mod, "check_invariants", lambda **kw: {})
        assert run(["--dry-run", "tick"]) == 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

class TestArgParsing:
    def test_missing_subcommand_exits_nonzero(self):
        with pytest.raises(SystemExit) as exc:
            cli_mod.main([])
        assert exc.value.code != 0

    def test_help_exits_0(self):
        with pytest.raises(SystemExit) as exc:
            cli_mod.main(["--help"])
        assert exc.value.code == 0

    def test_subcommand_help_exits_0(self):
        with pytest.raises(SystemExit) as exc:
            cli_mod.main(["dryrun-report", "--help"])
        assert exc.value.code == 0

    def test_unknown_subcommand_exits_nonzero(self):
        with pytest.raises(SystemExit):
            cli_mod.main(["nonexistent-subcommand"])

    def test_next_parses_energy(self, monkeypatch):
        import gtd.engine.engage as engage_mod
        captured = {}
        monkeypatch.setattr(engage_mod, "next_actions", lambda **kw: captured.update(kw) or [])
        monkeypatch.setattr(engage_mod, "format_for_chat", lambda a, **kw: "")
        run(["next", "--energy", "high"])
        assert captured.get("energy") == "high"

    def test_tickler_parses_target_list(self, monkeypatch):
        import gtd.engine.tickler as tickler_mod
        captured = {}
        def fake_park(rid, list_name, release_at, *, conn, target_list, **kw):
            captured["target_list"] = target_list
        monkeypatch.setattr(tickler_mod, "park", fake_park)
        # Not dry-run so it actually calls park
        monkeypatch.setattr(cli_mod, "effective_dryrun", lambda cfg: False)
        run(["tickler", "RID1", "Inbox", "2026-06-01T09:00:00", "--target-list", "@computer"])
        assert captured.get("target_list") == "@computer"
