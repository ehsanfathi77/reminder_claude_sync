"""
Unit tests for tickler.parse_release_date + cmd_tickler date validation
(AC-UX-3, AC-TEST-10).

The plan locks the EXACT serialized format with a fixed-timezone fixture,
so a future regression that drops the offset (or computes it wrong) fails.
"""
from __future__ import annotations

import io
import os
import sys
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import gtd.engine.cli as cli_mod
import gtd.engine.tickler as tickler_mod
from gtd.engine.tickler import InvalidReleaseDate, parse_release_date


# ---------------------------------------------------------------------------
# Fixed-TZ fixture so the assertion is deterministic across CI machines
# ---------------------------------------------------------------------------

@pytest.fixture
def fixed_eastern_tz(monkeypatch):
    """Pin the local timezone to America/New_York for the test.

    On systems supporting TZ env var + tzset, this changes datetime.now()'s
    local offset to EDT (-04:00) or EST (-05:00) depending on date.
    """
    monkeypatch.setenv("TZ", "America/New_York")
    import time
    if hasattr(time, "tzset"):
        time.tzset()
    yield
    if hasattr(time, "tzset"):
        time.tzset()


# ---------------------------------------------------------------------------
# parse_release_date — engine API
# ---------------------------------------------------------------------------

def test_parse_release_date_date_only_normalizes_to_local_9am(fixed_eastern_tz):
    """YYYY-MM-DD → 09:00 in user's local TZ, serialized with offset."""
    result = parse_release_date("2026-06-01")
    # June 1, 2026 in NYC is EDT (DST active) → -04:00
    assert result == "2026-06-01T09:00:00-04:00"


def test_parse_release_date_date_only_winter_offset(fixed_eastern_tz):
    """January date → EST (-05:00) — verifies offset is dynamic, not constant."""
    result = parse_release_date("2026-01-15")
    assert result == "2026-01-15T09:00:00-05:00"


def test_parse_release_date_offset_naive_datetime_assumed_local(fixed_eastern_tz):
    """YYYY-MM-DDTHH:MM:SS without offset → interpreted as local."""
    result = parse_release_date("2026-06-01T14:30:00")
    assert result == "2026-06-01T14:30:00-04:00"


def test_parse_release_date_offset_aware_datetime_preserved():
    """An explicit offset is preserved as-given."""
    result = parse_release_date("2026-06-01T14:30:00-07:00")
    assert result == "2026-06-01T14:30:00-07:00"


def test_parse_release_date_zulu_normalized_to_plus_zero():
    """`Z` suffix → `+00:00`."""
    result = parse_release_date("2026-06-01T14:30:00Z")
    assert result == "2026-06-01T14:30:00+00:00"


def test_parse_release_date_garbage_raises_with_canonical_hint():
    with pytest.raises(InvalidReleaseDate) as exc_info:
        parse_release_date("banana")
    assert "banana" in str(exc_info.value)
    assert "YYYY-MM-DD" in str(exc_info.value)


def test_parse_release_date_empty_raises():
    with pytest.raises(InvalidReleaseDate):
        parse_release_date("")
    with pytest.raises(InvalidReleaseDate):
        parse_release_date("   ")


def test_parse_release_date_partial_iso_rejected():
    """Reject things that look almost-right but aren't."""
    for bad in ["2026-06", "2026/06/01", "06-01-2026", "2026-13-01T14:30:00"]:
        with pytest.raises(InvalidReleaseDate):
            parse_release_date(bad)


def test_parse_release_date_invalid_calendar_date_rejected():
    """Calendar-invalid dates (Feb 30, etc.) raise."""
    with pytest.raises(InvalidReleaseDate):
        parse_release_date("2026-02-30")


# ---------------------------------------------------------------------------
# CLI translation — cmd_tickler
# ---------------------------------------------------------------------------

@pytest.fixture
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


@pytest.fixture
def stub_config(monkeypatch, tmp_path):
    cfg = {"dispatch_dryrun": True, "flip_at_iso": None,
           "managed_lists": None, "quiet_hours": [22, 8],
           "q_max_open": 3, "q_max_per_day": 8}
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg.copy())
    monkeypatch.setattr(cli_mod, "LOG_DIR", tmp_path / "log")


def test_cmd_tickler_invalid_date_exits_2_with_friendly_message(
    stub_lock, stub_config
):
    err = io.StringIO()
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_mod.main(["tickler", "FAKERID", "Inbox", "banana"])
    assert rc == 2
    assert "tickler:" in err.getvalue()
    assert "banana" in err.getvalue()
    assert "YYYY-MM-DD" in err.getvalue()


def test_cmd_tickler_date_only_passes_validation_in_dryrun(
    stub_lock, stub_config, fixed_eastern_tz
):
    """Date-only succeeds; in dry-run we never touch state.db."""
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_mod.main(["--dry-run", "tickler", "FAKERID", "Inbox", "2026-06-01"])
    assert rc == 0
    # The normalized form (with local offset) must appear in the dry-run output
    assert "2026-06-01T09:00:00-04:00" in out.getvalue()
