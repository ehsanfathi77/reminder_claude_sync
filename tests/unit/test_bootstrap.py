"""
Tests for gtd/engine/bootstrap.py — US-017 idempotent list provisioning.

All tests use monkeypatched subprocess.run to avoid touching actual Reminders.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from gtd.engine.bootstrap import existing_lists, provision_lists  # noqa: E402
from gtd.engine.write_fence import DEFAULT_MANAGED_LISTS  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ALL_LISTS = sorted(DEFAULT_MANAGED_LISTS)
_CLI = Path("/fake/reminders-cli")


def _make_show_result(names: list[str]) -> MagicMock:
    """Return a mock CompletedProcess whose stdout is newline-joined names."""
    m = MagicMock()
    m.stdout = "\n".join(names) + ("\n" if names else "")
    m.returncode = 0
    return m


def _make_new_result() -> MagicMock:
    m = MagicMock()
    m.stdout = ""
    m.returncode = 0
    return m


def _make_error(stderr: str = "Something went wrong") -> subprocess.CalledProcessError:
    err = subprocess.CalledProcessError(1, ["reminders-cli", "new-list", "X"])
    err.stderr = stderr
    return err


# ---------------------------------------------------------------------------
# existing_lists()
# ---------------------------------------------------------------------------

class TestExistingLists:
    def test_returns_set_of_names(self):
        with patch("gtd.engine.bootstrap.subprocess.run") as mock_run:
            mock_run.return_value = _make_show_result(["Inbox", "@calls", "Someday"])
            result = existing_lists(reminders_cli=_CLI)
        assert result == {"Inbox", "@calls", "Someday"}

    def test_empty_lists(self):
        with patch("gtd.engine.bootstrap.subprocess.run") as mock_run:
            mock_run.return_value = _make_show_result([])
            result = existing_lists(reminders_cli=_CLI)
        assert result == set()

    def test_strips_whitespace(self):
        with patch("gtd.engine.bootstrap.subprocess.run") as mock_run:
            mock_run.return_value = _make_show_result(["  Inbox  ", "  @home  "])
            result = existing_lists(reminders_cli=_CLI)
        assert result == {"Inbox", "@home"}

    def test_passes_correct_command(self):
        with patch("gtd.engine.bootstrap.subprocess.run") as mock_run:
            mock_run.return_value = _make_show_result([])
            existing_lists(reminders_cli=_CLI)
        mock_run.assert_called_once_with(
            [str(_CLI), "show-lists"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )


# ---------------------------------------------------------------------------
# provision_lists() — fresh (0 existing → all (len(DEFAULT_MANAGED_LISTS)) created)
# ---------------------------------------------------------------------------

class TestProvisionFresh:
    def test_all_lists_created(self, tmp_path: Path):
        call_results = [_make_show_result([])] + [_make_new_result()] * len(DEFAULT_MANAGED_LISTS)

        with patch("gtd.engine.bootstrap.subprocess.run", side_effect=call_results):
            result = provision_lists(
                reminders_cli=_CLI,
                log_dir=tmp_path,
            )

        assert len(result) == len(DEFAULT_MANAGED_LISTS)
        assert all(v == "created" for v in result.values()), result

    def test_second_call_all_exist(self, tmp_path: Path):
        # First call: none exist → all created
        call_results_1 = [_make_show_result([])] + [_make_new_result()] * len(DEFAULT_MANAGED_LISTS)
        with patch("gtd.engine.bootstrap.subprocess.run", side_effect=call_results_1):
            provision_lists(reminders_cli=_CLI, log_dir=tmp_path)

        # Second call: all (len(DEFAULT_MANAGED_LISTS)) exist → 0 created
        all_names = list(DEFAULT_MANAGED_LISTS)
        with patch("gtd.engine.bootstrap.subprocess.run",
                   return_value=_make_show_result(all_names)):
            result2 = provision_lists(reminders_cli=_CLI, log_dir=tmp_path)

        assert all(v == "exists" for v in result2.values()), result2
        assert sum(1 for v in result2.values() if v == "exists") == len(DEFAULT_MANAGED_LISTS)


# ---------------------------------------------------------------------------
# provision_lists() — partial (8 exist → 7 created)
# ---------------------------------------------------------------------------

class TestProvisionPartial:
    def test_partial_existing(self, tmp_path: Path):
        all_names = sorted(DEFAULT_MANAGED_LISTS)
        existing = all_names[:8]
        missing = all_names[8:]

        call_results = [_make_show_result(existing)] + [_make_new_result()] * len(missing)
        with patch("gtd.engine.bootstrap.subprocess.run", side_effect=call_results):
            result = provision_lists(reminders_cli=_CLI, log_dir=tmp_path)

        created = [k for k, v in result.items() if v == "created"]
        exists = [k for k, v in result.items() if v == "exists"]
        assert len(created) == len(missing)
        assert len(exists) == len(existing)
        assert set(exists) == set(existing)
        assert set(created) == set(missing)


# ---------------------------------------------------------------------------
# provision_lists() — idempotent (run twice → second has 0 created)
# ---------------------------------------------------------------------------

class TestIdempotent:
    def test_idempotent_second_run(self, tmp_path: Path):
        all_names = list(DEFAULT_MANAGED_LISTS)

        # First run: none exist
        first_results = [_make_show_result([])] + [_make_new_result()] * len(DEFAULT_MANAGED_LISTS)
        with patch("gtd.engine.bootstrap.subprocess.run", side_effect=first_results):
            first = provision_lists(reminders_cli=_CLI, log_dir=tmp_path)

        assert sum(1 for v in first.values() if v == "created") == len(DEFAULT_MANAGED_LISTS)

        # Second run: all exist
        with patch("gtd.engine.bootstrap.subprocess.run",
                   return_value=_make_show_result(all_names)):
            second = provision_lists(reminders_cli=_CLI, log_dir=tmp_path)

        assert sum(1 for v in second.values() if v == "created") == 0
        assert sum(1 for v in second.values() if v == "exists") == len(DEFAULT_MANAGED_LISTS)


# ---------------------------------------------------------------------------
# provision_lists() — dry_run=True
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_no_new_list_calls(self, tmp_path: Path):
        """dry_run=True calls show-lists once, never calls new-list."""
        show_mock = _make_show_result([])

        with patch("gtd.engine.bootstrap.subprocess.run", return_value=show_mock) as mock_run:
            result = provision_lists(
                reminders_cli=_CLI,
                log_dir=tmp_path,
                dry_run=True,
            )

        # Only show-lists was called (once)
        assert mock_run.call_count == 1
        called_args = mock_run.call_args[0][0]
        assert "show-lists" in called_args

        # All statuses are 'skipped' (nothing exists, but dry_run)
        assert all(v == "skipped" for v in result.values()), result

    def test_dry_run_partial_existing_shows_exists_and_skipped(self, tmp_path: Path):
        """With some lists existing, dry_run returns 'exists' for them and 'skipped' for rest."""
        all_names = sorted(DEFAULT_MANAGED_LISTS)
        existing = all_names[:5]

        with patch("gtd.engine.bootstrap.subprocess.run",
                   return_value=_make_show_result(existing)):
            result = provision_lists(
                reminders_cli=_CLI,
                log_dir=tmp_path,
                dry_run=True,
            )

        assert sum(1 for v in result.values() if v == "exists") == 5
        assert sum(1 for v in result.values() if v == "skipped") == len(DEFAULT_MANAGED_LISTS) - 5
        assert sum(1 for v in result.values() if v == "created") == 0


# ---------------------------------------------------------------------------
# provision_lists() — error path
# ---------------------------------------------------------------------------

class TestErrorPath:
    def test_single_error_does_not_abort(self, tmp_path: Path):
        """If new-list raises for one name, that name gets 'error: ...', others succeed."""
        all_names = sorted(DEFAULT_MANAGED_LISTS)
        failing_name = all_names[3]  # pick an arbitrary name to fail

        def fake_run(args, **kwargs):
            if args[1] == "show-lists":
                return _make_show_result([])
            if args[2] == failing_name:
                raise _make_error(stderr="permission denied")
            return _make_new_result()

        with patch("gtd.engine.bootstrap.subprocess.run", side_effect=fake_run):
            result = provision_lists(reminders_cli=_CLI, log_dir=tmp_path)

        assert result[failing_name] == "error: permission denied"
        created = [k for k, v in result.items() if v == "created"]
        assert len(created) == len(DEFAULT_MANAGED_LISTS) - 1
        assert failing_name not in created

    def test_error_message_contains_stderr(self, tmp_path: Path):
        all_names = sorted(DEFAULT_MANAGED_LISTS)
        target = all_names[0]

        def fake_run(args, **kwargs):
            if args[1] == "show-lists":
                return _make_show_result([])
            if args[2] == target:
                raise _make_error(stderr="list already exists (unexpected)")
            return _make_new_result()

        with patch("gtd.engine.bootstrap.subprocess.run", side_effect=fake_run):
            result = provision_lists(reminders_cli=_CLI, log_dir=tmp_path)

        assert "list already exists (unexpected)" in result[target]


# ---------------------------------------------------------------------------
# Log line verification
# ---------------------------------------------------------------------------

class TestLogLine:
    def _read_engine_jsonl(self, log_dir: Path) -> list[dict]:
        path = log_dir / "engine.jsonl"
        assert path.exists(), "engine.jsonl not created"
        lines = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if raw:
                lines.append(json.loads(raw))
        return lines

    def test_log_line_written_with_correct_op(self, tmp_path: Path):
        with patch("gtd.engine.bootstrap.subprocess.run",
                   return_value=_make_show_result(list(DEFAULT_MANAGED_LISTS))):
            provision_lists(reminders_cli=_CLI, log_dir=tmp_path)

        records = self._read_engine_jsonl(tmp_path)
        assert len(records) == 1
        rec = records[0]
        assert rec["op"] == "bootstrap"

    def test_log_line_counts_correct_all_exist(self, tmp_path: Path):
        all_names = list(DEFAULT_MANAGED_LISTS)
        with patch("gtd.engine.bootstrap.subprocess.run",
                   return_value=_make_show_result(all_names)):
            provision_lists(reminders_cli=_CLI, log_dir=tmp_path)

        rec = self._read_engine_jsonl(tmp_path)[0]
        assert rec["total"] == len(DEFAULT_MANAGED_LISTS)
        assert rec["created"] == 0
        assert rec["exists"] == len(DEFAULT_MANAGED_LISTS)
        assert rec["errors"] == 0

    def test_log_line_counts_correct_all_fresh(self, tmp_path: Path):
        call_results = [_make_show_result([])] + [_make_new_result()] * len(DEFAULT_MANAGED_LISTS)
        with patch("gtd.engine.bootstrap.subprocess.run", side_effect=call_results):
            provision_lists(reminders_cli=_CLI, log_dir=tmp_path)

        rec = self._read_engine_jsonl(tmp_path)[0]
        assert rec["total"] == len(DEFAULT_MANAGED_LISTS)
        assert rec["created"] == len(DEFAULT_MANAGED_LISTS)
        assert rec["exists"] == 0
        assert rec["errors"] == 0

    def test_log_line_details_dict_present(self, tmp_path: Path):
        all_names = list(DEFAULT_MANAGED_LISTS)
        with patch("gtd.engine.bootstrap.subprocess.run",
                   return_value=_make_show_result(all_names)):
            result = provision_lists(reminders_cli=_CLI, log_dir=tmp_path)

        rec = self._read_engine_jsonl(tmp_path)[0]
        assert "details" in rec
        assert isinstance(rec["details"], dict)
        assert len(rec["details"]) == len(DEFAULT_MANAGED_LISTS)
        assert rec["details"] == result

    def test_log_line_has_ts_and_pid(self, tmp_path: Path):
        with patch("gtd.engine.bootstrap.subprocess.run",
                   return_value=_make_show_result([])) as mock_run:
            # All 15 missing, no new-list needed for this — use dry_run
            provision_lists(reminders_cli=_CLI, log_dir=tmp_path, dry_run=True)

        rec = self._read_engine_jsonl(tmp_path)[0]
        assert "ts" in rec
        assert "pid" in rec

    def test_log_line_dry_run_flag(self, tmp_path: Path):
        with patch("gtd.engine.bootstrap.subprocess.run",
                   return_value=_make_show_result([])):
            provision_lists(reminders_cli=_CLI, log_dir=tmp_path, dry_run=True)

        rec = self._read_engine_jsonl(tmp_path)[0]
        assert rec["dry_run"] is True

    def test_log_line_errors_counted(self, tmp_path: Path):
        all_names = sorted(DEFAULT_MANAGED_LISTS)
        fail1, fail2 = all_names[0], all_names[1]

        def fake_run(args, **kwargs):
            if args[1] == "show-lists":
                return _make_show_result([])
            if args[2] in (fail1, fail2):
                raise _make_error(stderr="quota exceeded")
            return _make_new_result()

        with patch("gtd.engine.bootstrap.subprocess.run", side_effect=fake_run):
            provision_lists(reminders_cli=_CLI, log_dir=tmp_path)

        rec = self._read_engine_jsonl(tmp_path)[0]
        assert rec["errors"] == 2
        assert rec["created"] == len(DEFAULT_MANAGED_LISTS) - 2

    def test_two_runs_produce_two_log_lines(self, tmp_path: Path):
        all_names = list(DEFAULT_MANAGED_LISTS)
        # First run: all exist
        with patch("gtd.engine.bootstrap.subprocess.run",
                   return_value=_make_show_result(all_names)):
            provision_lists(reminders_cli=_CLI, log_dir=tmp_path)

        # Second run: all exist again
        with patch("gtd.engine.bootstrap.subprocess.run",
                   return_value=_make_show_result(all_names)):
            provision_lists(reminders_cli=_CLI, log_dir=tmp_path)

        records = self._read_engine_jsonl(tmp_path)
        assert len(records) == 2
        assert all(r["op"] == "bootstrap" for r in records)
