"""
Unit tests for cli.py argparse behavior (AC-TEST-8 + AC-TEST-13).

Two distinct concerns:
1. Mutually-exclusive flags fail loudly (`adopt --confirm-list X --apply`).
2. Global vs subcommand `--dry-run` placement: BOTH positions must reach
   the handler as args.dry_run == True. Pre-fix bug: argparse subparser
   default of False would shadow the global flag.
"""
from __future__ import annotations

import io
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


@pytest.fixture(autouse=True)
def stub_config(monkeypatch, tmp_path):
    cfg = {"dispatch_dryrun": True, "flip_at_iso": None,
           "managed_lists": None, "quiet_hours": [22, 8],
           "q_max_open": 3, "q_max_per_day": 8}
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg.copy())


# ---------------------------------------------------------------------------
# AC-TEST-13: --dry-run placement parametrized
# ---------------------------------------------------------------------------

# Each entry: (subcommand, extra_args)
# extra_args is the per-subcommand arg list needed to make argparse happy.
_SUBCOMMAND_FIXTURES = [
    ("capture", ["--text", "x"]),
    ("adopt", []),                              # bare adopt = discovery, no extra args
    ("tickler", ["FAKERID", "Inbox", "2026-06-01"]),
    ("project", ["FakeProject", "--outcome", "Test outcome"]),
]


@pytest.mark.parametrize("subcommand,extra", _SUBCOMMAND_FIXTURES)
def test_dryrun_flag_at_global_position_reaches_handler(subcommand, extra):
    """`gtd --dry-run <sub> ...` → args.dry_run == True at parse time."""
    parser = cli_mod._build_parser()
    args = parser.parse_args(["--dry-run", subcommand, *extra])
    assert getattr(args, "dry_run", False) is True, (
        f"{subcommand}: global --dry-run did not survive to args.dry_run"
    )


@pytest.mark.parametrize("subcommand,extra", _SUBCOMMAND_FIXTURES)
def test_dryrun_flag_at_subcommand_position_reaches_handler(subcommand, extra):
    """`gtd <sub> ... --dry-run` → args.dry_run == True at parse time.

    This is the case that would silently break under default=False on the
    subparser; SUPPRESS prevents the shadow.
    """
    parser = cli_mod._build_parser()
    args = parser.parse_args([subcommand, *extra, "--dry-run"])
    assert getattr(args, "dry_run", False) is True, (
        f"{subcommand}: subcommand-position --dry-run was lost"
    )


@pytest.mark.parametrize("subcommand,extra", _SUBCOMMAND_FIXTURES)
def test_dryrun_flag_neither_position_defaults_to_false(subcommand, extra):
    """No --dry-run anywhere → handler reads it as False via getattr default."""
    parser = cli_mod._build_parser()
    args = parser.parse_args([subcommand, *extra])
    assert getattr(args, "dry_run", False) is False


@pytest.mark.parametrize("subcommand,extra", _SUBCOMMAND_FIXTURES)
def test_dryrun_flag_both_positions_resolves_true(subcommand, extra):
    """Belt-and-braces: passing the flag twice still resolves to True."""
    parser = cli_mod._build_parser()
    args = parser.parse_args(["--dry-run", subcommand, *extra, "--dry-run"])
    assert getattr(args, "dry_run", False) is True


def test_no_dryrun_normalization_helper_present():
    """Regression-guard: ensure nobody re-introduces a post-parse normalization
    that force-sets args.dry_run = False (which would mask SUPPRESS bugs)."""
    import inspect
    src = inspect.getsource(cli_mod.main)
    forbidden = [
        "args.dry_run = False",
        "args.dry_run=False",
        'if not hasattr(args, "dry_run")',
        "if not hasattr(args, 'dry_run')",
    ]
    for snippet in forbidden:
        assert snippet not in src, (
            f"main() contains forbidden normalization: {snippet!r}. "
            "This would mask the argparse.SUPPRESS pattern. "
            "If you need a default, use getattr(args, 'dry_run', False) in the handler."
        )


# ---------------------------------------------------------------------------
# AC-TEST-8: --confirm-list and --apply are mutually exclusive
# ---------------------------------------------------------------------------

def test_adopt_confirm_list_and_apply_mutually_exclusive(monkeypatch):
    """Passing both flags should exit 2 with 'mutually exclusive' in stderr."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    err = io.StringIO()
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_mod.main(["adopt", "--confirm-list", "Personal", "--apply"])
    assert rc == 2
    assert "mutually exclusive" in err.getvalue()
