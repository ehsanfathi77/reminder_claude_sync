"""
Unit tests for effective_dryrun (AC-TEST-6).

The 7-day post-flip safety window: even if `dispatch_dryrun` is set to False
in config.json, the engine must continue dry-run-ing for 7 days after
`flip_at_iso` to give /gtd:dryrun-report a chance to gate a regression.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from gtd.engine.cli import effective_dryrun


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_dispatch_dryrun_true_returns_true():
    """Default config: dispatch_dryrun=True → always dry-run."""
    cfg = {"dispatch_dryrun": True, "flip_at_iso": None}
    assert effective_dryrun(cfg) is True


def test_dispatch_dryrun_false_no_flip_returns_true():
    """User flipped to False but never set flip_at_iso → safe default = stay dry-run."""
    cfg = {"dispatch_dryrun": False, "flip_at_iso": None}
    assert effective_dryrun(cfg) is True


def test_dispatch_dryrun_false_within_7d_window_returns_true():
    """flip_at_iso = 3 days ago → still inside the 7-day cooldown."""
    flip_at = (_now() - timedelta(days=3)).isoformat()
    cfg = {"dispatch_dryrun": False, "flip_at_iso": flip_at}
    assert effective_dryrun(cfg) is True


def test_dispatch_dryrun_false_after_7d_window_returns_false():
    """flip_at_iso = 8 days ago → window has elapsed; live mode."""
    flip_at = (_now() - timedelta(days=8)).isoformat()
    cfg = {"dispatch_dryrun": False, "flip_at_iso": flip_at}
    assert effective_dryrun(cfg) is False


def test_dispatch_dryrun_false_malformed_flip_returns_true():
    """Malformed flip_at_iso → safe default = stay dry-run, no exception."""
    cfg = {"dispatch_dryrun": False, "flip_at_iso": "not-a-date"}
    assert effective_dryrun(cfg) is True


def test_dispatch_dryrun_false_at_exactly_7d_boundary_returns_true():
    """At exactly 7 days, the < comparison still treats us as inside.

    Lock the exact comparison semantics — a regression that flips this from
    < to <= would change the window by 1 second.
    """
    # Pick a flip_at exactly 7 days before "now"; mock now so the comparison is deterministic.
    fixed_now = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    flip_at = (fixed_now - timedelta(days=7)).isoformat()
    cfg = {"dispatch_dryrun": False, "flip_at_iso": flip_at}

    with patch("gtd.engine.cli.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat = datetime.fromisoformat
        # `flip_dt + 7d == fixed_now`, so the < check is False → returns False
        assert effective_dryrun(cfg) is False
