"""
Unit tests for gtd/engine/lock.py.

Covers:
- Basic acquire+release: metadata written, next acquire works
- Concurrent: thread blocks until first context exits
- Stale-lock kill-and-reacquire: child with fake old ts gets killed, main acquires
- Unknown holder → TimeoutError: child with unknown argv0 stays alive
- read_holder on missing file → None
- read_holder on malformed (1 line) → None
- Timeout < RETRY_EVERY_S: returns within ~1s
- Lock file directory auto-created if missing

macOS multiprocessing note:
  macOS defaults to 'spawn' for multiprocessing (not 'fork'), so child target
  functions must be importable at module level — we define them as top-level
  functions below. Each test uses a generous join timeout (15s) to prevent
  hangs; if a child doesn't exit by then the test kills it and fails.
"""
from __future__ import annotations

import fcntl
import multiprocessing
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from gtd.engine.lock import (  # noqa: E402
    DEFAULT_TIMEOUT_S,
    KNOWN_DAEMONS,
    RETRY_EVERY_S,
    STALE_AFTER_S,
    LockHolderInfo,
    acquire,
    read_holder,
)

# ── multiprocessing start method ────────────────────────────────────────────
# Force 'fork' on macOS so child inherits the interpreter state.  'spawn'
# (the macOS default) would require all target functions to be importable,
# which they are, but 'fork' is simpler for lock-file tests.
_ctx = multiprocessing.get_context("fork")


# ── child process target functions (must be top-level for spawn compat) ─────

def _child_hold_lock(lock_path_str: str, ready_event_path: str, hold_seconds: float):
    """Open and flock a file, signal readiness, then sleep."""
    import fcntl, time, os
    fd = os.open(lock_path_str, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    # Signal ready by creating a sentinel file.
    Path(ready_event_path).touch()
    time.sleep(hold_seconds)
    os.close(fd)


def _child_hold_lock_with_metadata(
    lock_path_str: str,
    ready_event_path: str,
    hold_seconds: float,
    argv0: str,
    fake_age_seconds: float,
):
    """Hold lock and write metadata with a backdated timestamp."""
    import fcntl, time, os
    from datetime import datetime, timedelta

    fd = os.open(lock_path_str, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)

    ts = (datetime.now().astimezone() - timedelta(seconds=fake_age_seconds)).isoformat(
        timespec="seconds"
    )
    content = f"{os.getpid()}\n{ts}\n{argv0}\n"
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, content.encode())

    Path(ready_event_path).touch()
    time.sleep(hold_seconds)
    os.close(fd)


def _wait_for_sentinel(sentinel: str, timeout: float = 10.0) -> bool:
    """Poll until sentinel file exists. Returns True if found in time."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if Path(sentinel).exists():
            return True
        time.sleep(0.05)
    return False


# ── helpers ─────────────────────────────────────────────────────────────────

def _spawn(target, args) -> _ctx.Process:  # type: ignore[name-defined]
    p = _ctx.Process(target=target, args=args, daemon=True)
    p.start()
    return p


# ── tests ────────────────────────────────────────────────────────────────────

class TestReadHolder:
    def test_missing_file_returns_none(self, tmp_path: Path):
        assert read_holder(tmp_path / "nonexistent.lock") is None

    def test_malformed_one_line_returns_none(self, tmp_path: Path):
        lock = tmp_path / "lock"
        lock.write_text("12345\n")
        assert read_holder(lock) is None

    def test_malformed_two_lines_returns_none(self, tmp_path: Path):
        lock = tmp_path / "lock"
        lock.write_text("12345\n2025-01-01T00:00:00+00:00\n")
        assert read_holder(lock) is None

    def test_empty_file_returns_none(self, tmp_path: Path):
        lock = tmp_path / "lock"
        lock.write_text("")
        assert read_holder(lock) is None

    def test_valid_metadata_parsed(self, tmp_path: Path):
        lock = tmp_path / "lock"
        ts = datetime.now().astimezone().isoformat(timespec="seconds")
        lock.write_text(f"9999\n{ts}\ngtd-engine\n")
        info = read_holder(lock)
        assert info is not None
        assert info.pid == 9999
        assert info.argv0 == "gtd-engine"
        assert info.timestamp == ts
        assert info.age_seconds >= 0


class TestBasicAcquire:
    def test_acquire_writes_metadata(self, tmp_path: Path):
        lock = tmp_path / "engine.lock"
        with acquire(lock, holder_argv0="gtd-engine", timeout=5.0):
            info = read_holder(lock)
            assert info is not None
            assert info.pid == os.getpid()
            assert info.argv0 == "gtd-engine"
            assert info.age_seconds < 5.0

    def test_acquire_release_then_reacquire(self, tmp_path: Path):
        lock = tmp_path / "engine.lock"
        with acquire(lock, holder_argv0="gtd-engine", timeout=5.0):
            pass
        # Should acquire again without error.
        with acquire(lock, holder_argv0="gtd-engine", timeout=5.0):
            assert read_holder(lock) is not None

    def test_lock_dir_auto_created(self, tmp_path: Path):
        lock = tmp_path / "deep" / "nested" / "engine.lock"
        assert not lock.parent.exists()
        with acquire(lock, holder_argv0="gtd-engine", timeout=5.0):
            assert lock.exists()


class TestConcurrentAcquire:
    def test_second_thread_blocks_until_first_releases(self, tmp_path: Path):
        lock = tmp_path / "engine.lock"
        results: list[str] = []
        barrier = threading.Barrier(2)

        def first():
            with acquire(lock, holder_argv0="gtd-engine", timeout=10.0):
                barrier.wait()  # signal second thread to start trying
                results.append("first-in")
                time.sleep(0.3)
                results.append("first-out")

        def second():
            barrier.wait()  # wait for first to be inside
            time.sleep(0.05)  # tiny delay so first is definitely ahead
            with acquire(lock, holder_argv0="gtd-engine", timeout=10.0):
                results.append("second-in")

        t1 = threading.Thread(target=first)
        t2 = threading.Thread(target=second)
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        assert results == ["first-in", "first-out", "second-in"], results


class TestStaleLockKillAndReacquire:
    def test_stale_known_daemon_killed_and_lock_acquired(self, tmp_path: Path):
        lock = tmp_path / "engine.lock"
        sentinel = str(tmp_path / "ready")

        p = _spawn(
            _child_hold_lock_with_metadata,
            (str(lock), sentinel, 30.0, "gtd-engine", 600.0),
        )
        assert _wait_for_sentinel(sentinel), "child never signalled ready"

        child_pid = p.pid
        # Confirm child alive before we attempt acquire.
        assert child_pid is not None
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            pytest.fail("child died before test could run")

        # acquire() with stale_after=300 should kill the child and succeed.
        with acquire(
            lock,
            holder_argv0="gtd-engine",
            timeout=30.0,
            stale_after=300.0,
            known_daemons=frozenset({"gtd-engine"}),
        ):
            info = read_holder(lock)
            assert info is not None
            assert info.pid == os.getpid()

        # Child should be dead.
        p.join(timeout=5)
        assert not p.is_alive(), "child process should have been killed"


class TestUnknownHolderTimeout:
    def test_unknown_argv0_raises_timeout_not_killed(self, tmp_path: Path):
        lock = tmp_path / "engine.lock"
        sentinel = str(tmp_path / "ready")

        p = _spawn(
            _child_hold_lock_with_metadata,
            (str(lock), sentinel, 15.0, "random-shell", 600.0),
        )
        assert _wait_for_sentinel(sentinel), "child never signalled ready"

        child_pid = p.pid

        with pytest.raises(TimeoutError):
            acquire(
                lock,
                holder_argv0="gtd-engine",
                timeout=2.0,
                stale_after=300.0,
                known_daemons=frozenset({"gtd-engine"}),
            ).__enter__()

        # Child must still be alive — we must not kill unknown holders.
        assert child_pid is not None
        try:
            os.kill(child_pid, 0)
            child_alive = True
        except ProcessLookupError:
            child_alive = False

        # Clean up before asserting so the process doesn't linger.
        p.terminate()
        p.join(timeout=5)

        assert child_alive, "unknown-argv0 child should NOT have been killed"


class TestTimeoutBehavior:
    def test_timeout_less_than_retry_returns_quickly(self, tmp_path: Path):
        """timeout=0.5 should return in well under 2s (one partial sleep max)."""
        lock = tmp_path / "engine.lock"
        sentinel = str(tmp_path / "ready")

        p = _spawn(
            _child_hold_lock,
            (str(lock), sentinel, 10.0),
        )
        assert _wait_for_sentinel(sentinel), "child never signalled ready"

        start = time.monotonic()
        with pytest.raises(TimeoutError):
            acquire(
                lock,
                holder_argv0="gtd-engine",
                timeout=0.5,
                stale_after=STALE_AFTER_S,
            ).__enter__()
        elapsed = time.monotonic() - start

        p.terminate()
        p.join(timeout=5)

        # Should return in well under RETRY_EVERY_S (2.0) seconds.
        assert elapsed < RETRY_EVERY_S, (
            f"acquire(timeout=0.5) took {elapsed:.2f}s — should be < {RETRY_EVERY_S}s"
        )
