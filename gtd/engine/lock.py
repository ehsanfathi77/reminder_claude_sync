"""
lock.py — POSIX flock with stale-lock kill-and-reacquire.

Usage:
    with acquire(Path("~/Documents/repos/todo/.gtd/engine.lock"), holder_argv0="gtd-engine"):
        ... do work ...

Acquire flow:
  1. Try LOCK_EX | LOCK_NB. If success: write '<pid>\\n<iso-ts>\\n<argv0>\\n' to file, return.
  2. If EWOULDBLOCK: read holder info, retry every 2s until timeout (default 60s).
  3. If still blocked AND lock age > STALE_AFTER_S (300) AND holder argv0 ∈
     KNOWN_DAEMONS: send SIGTERM, sleep 3s, send SIGKILL if still alive,
     unlink the lock file, then retry from step 1 once.
  4. If unknown holder OR not stale: raise TimeoutError.

Constants:
  STALE_AFTER_S = 300
  KNOWN_DAEMONS = frozenset({"gtd-engine", "sync.py", "supernote-sync"})
  RETRY_EVERY_S = 2.0
  DEFAULT_TIMEOUT_S = 60.0
"""
from __future__ import annotations

import errno
import fcntl
import os
import signal
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

STALE_AFTER_S: float = 300.0
KNOWN_DAEMONS: frozenset[str] = frozenset({"gtd-engine", "sync.py", "supernote-sync"})
RETRY_EVERY_S: float = 2.0
DEFAULT_TIMEOUT_S: float = 60.0


class LockHolderInfo(NamedTuple):
    pid: int
    timestamp: str  # ISO local
    argv0: str
    age_seconds: float


def read_holder(lock_path: Path) -> LockHolderInfo | None:
    """Returns None if file empty/malformed/missing."""
    try:
        text = lock_path.read_text()
    except FileNotFoundError:
        return None

    lines = text.strip().splitlines()
    if len(lines) < 3:
        return None

    try:
        pid = int(lines[0].strip())
    except ValueError:
        return None

    timestamp = lines[1].strip()
    argv0 = lines[2].strip()

    try:
        held_at = datetime.fromisoformat(timestamp)
        age = (datetime.now().astimezone() - held_at).total_seconds()
    except ValueError:
        return None

    return LockHolderInfo(pid=pid, timestamp=timestamp, argv0=argv0, age_seconds=age)


def _write_metadata(fd: int, lock_path: Path, argv0: str) -> None:
    """Overwrite lock file with pid/ts/argv0 after successful flock."""
    ts = datetime.now().astimezone().isoformat(timespec="seconds")
    content = f"{os.getpid()}\n{ts}\n{argv0}\n"
    # Truncate then write — we hold LOCK_EX so this is safe.
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, content.encode())


def _try_flock(fd: int) -> bool:
    """Attempt non-blocking exclusive flock. Returns True on success."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            return False
        raise


def _kill_holder(pid: int) -> None:
    """Send SIGTERM, wait 3s, then SIGKILL if still alive."""
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return  # already gone

    time.sleep(3)

    try:
        os.kill(pid, 0)  # probe — raises OSError(ESRCH) if gone
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return  # terminated cleanly after SIGTERM
        # EPERM or other — process still exists under different uid; escalate
    else:
        # Process is still alive — send SIGKILL
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


@contextmanager
def acquire(
    lock_path: Path,
    *,
    holder_argv0: str,
    timeout: float = DEFAULT_TIMEOUT_S,
    stale_after: float = STALE_AFTER_S,
    known_daemons: frozenset[str] | None = None,
):
    """Context manager. Yields the open file handle. Releases on exit."""
    effective_daemons = KNOWN_DAEMONS if known_daemons is None else known_daemons

    lock_path = lock_path.expanduser().resolve()
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    deadline = time.monotonic() + timeout
    killed_and_retrying = False

    while True:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if _try_flock(fd):
                _write_metadata(fd, lock_path, holder_argv0)
                break  # success — exit the retry loop, keep fd open

            # Lock is held by someone else.
            os.close(fd)
            fd = -1

            if time.monotonic() >= deadline:
                # Timed out — decide whether to kill-and-reacquire or raise.
                if killed_and_retrying:
                    raise TimeoutError(
                        f"Could not acquire {lock_path} after kill-and-reacquire"
                    )

                holder = read_holder(lock_path)
                if (
                    holder is not None
                    and holder.age_seconds > stale_after
                    and holder.argv0 in effective_daemons
                ):
                    _kill_holder(holder.pid)
                    try:
                        lock_path.unlink()
                    except FileNotFoundError:
                        pass
                    killed_and_retrying = True
                    # Reset deadline for the single post-kill retry.
                    deadline = time.monotonic() + max(timeout, RETRY_EVERY_S * 2)
                    continue
                else:
                    argv0_info = holder.argv0 if holder else "<unknown>"
                    raise TimeoutError(
                        f"Could not acquire {lock_path}: held by {argv0_info!r} "
                        f"(not stale or not a known daemon)"
                    )

            time.sleep(min(RETRY_EVERY_S, max(0.0, deadline - time.monotonic())))

        except BaseException:
            if fd != -1:
                os.close(fd)
            raise

    # Yield the raw fd wrapped as a file object for callers that want it.
    fh = os.fdopen(fd, "r+b", closefd=True)
    try:
        yield fh
    finally:
        # Truncate the metadata content on release so future readers don't see
        # stale pid/argv0 from a process that's already exited. The lock file
        # itself stays in place (so flock semantics are preserved across
        # processes) — only its content is cleared.
        try:
            fh.seek(0)
            fh.truncate(0)
        except (OSError, ValueError):
            pass
        # flock is released automatically when the fd closes.
        fh.close()
