"""
test_sync_coexistence.py — Integration test for sync + metadata-fence coexistence.

The CRITICAL property being tested:
  Once the GTD engine stamps a `--- gtd --- ... --- end ---` metadata fence
  into a reminder's notes, subsequent sync runs must NOT log NO_BASELINE or
  CONFLICT lines.  This is guaranteed by syncstate._strip_gtd_fence(), which
  removes the fence before hashing so the engine-owned bytes don't look like
  a content change.

Test sequence:
  1. Add a plain reminder to test_list_name via reminders-cli (no fence).
  2. Run bin/sync.py sync --root tmp_path → establishes baseline hash in state.
  3. Stamp a GTD metadata fence into the reminder's notes via reminders-cli.
  4. Run bin/sync.py sync --root tmp_path again → MUST produce zero
     NO_BASELINE / CONFLICT lines.
  5. Run sync a THIRD time → same clean result.

Isolation:
  - Uses --root tmp_path so all state files (.sync-state.json, .sync-conflicts.log,
    TASKS.md) are written to tmp_path, never touching the real repo files.
  - test_list_name fixture guarantees cleanup even on failure.

Flakiness note:
  Reminders.app has ~1-3 s iCloud propagation delay after writes.
  time.sleep(3) guards after each write; increase to 5 if still flaky on
  slow or high-load machines.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
REM_CLI = ROOT / "bin" / "reminders-cli"
SYNC_PY = ROOT / "bin" / "sync.py"


def _run_sync(root: Path, *, verbose: bool = False) -> subprocess.CompletedProcess:
    """Run bin/sync.py sync --root <root> and return the CompletedProcess."""
    cmd = [
        sys.executable,
        str(SYNC_PY),
        "sync",
        "--root",
        str(root),
        "--done-window",
        "0",   # don't pull completed reminders → faster + cleaner
    ]
    if verbose:
        cmd.append("--verbose")
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(ROOT),
    )


def _conflict_lines(root: Path) -> list[str]:
    """Return all lines from .sync-conflicts.log that contain NO_BASELINE or CONFLICT."""
    log = root / ".sync-conflicts.log"
    if not log.exists():
        return []
    lines = log.read_text().splitlines()
    return [l for l in lines if "NO_BASELINE" in l or "CONFLICT" in l]


def _get_rid(list_name: str, title_fragment: str) -> str | None:
    """Fetch the externalId of the first reminder in list_name whose title
    contains title_fragment.  Returns None if not found."""
    result = subprocess.run(
        [str(REM_CLI), "show", list_name, "--format=json"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        items = json.loads(result.stdout)
        for item in items:
            if title_fragment.lower() in (item.get("title") or "").lower():
                return item.get("externalId")
    except (json.JSONDecodeError, TypeError):
        pass
    return None


@pytest.mark.integration
def test_no_conflict_after_metadata_stamp(test_list_name: str, tmp_path: Path):
    """Sync coexistence: metadata fence stamp must not cause conflicts on re-sync."""

    # ── Step 1: Create a plain reminder (no fence) ───────────────────────────
    title = "GTD sync coexistence test reminder"
    subprocess.run(
        [str(REM_CLI), "add", test_list_name, title],
        check=True,
        capture_output=True,
        timeout=15,
    )
    time.sleep(3)  # iCloud propagation

    rid = _get_rid(test_list_name, "sync coexistence")
    assert rid is not None, (
        f"Could not find just-created reminder in '{test_list_name}'. "
        "Check Reminders.app permissions."
    )

    # ── Step 2: First sync — establishes baseline hash ───────────────────────
    result1 = _run_sync(tmp_path)
    assert result1.returncode == 0, (
        f"First sync failed:\nstdout={result1.stdout}\nstderr={result1.stderr}"
    )

    state_path = tmp_path / ".sync-state.json"
    assert state_path.exists(), "sync must write .sync-state.json"
    state = json.loads(state_path.read_text())
    assert rid in state["tasks"], (
        f"rid {rid!r} not in sync state after first sync. "
        f"Known rids: {list(state['tasks'].keys())[:5]}"
    )
    baseline_hash = state["tasks"][rid]["hash"]
    assert baseline_hash, "baseline hash must be non-empty"

    # No conflicts after first sync.
    bad_lines_1 = _conflict_lines(tmp_path)
    assert bad_lines_1 == [], f"Unexpected conflict/baseline lines after first sync: {bad_lines_1}"

    # ── Step 3: Stamp a GTD metadata fence into the reminder's notes ─────────
    fence_notes = (
        "--- gtd ---\n"
        "id: 01HTEST000000000000000000\n"
        "kind: next_action\n"
        "created: 2026-04-18T12:00:00\n"
        "--- end ---\n"
        "original user prose"
    )
    subprocess.run(
        [str(REM_CLI), "edit", test_list_name, rid, "--notes", fence_notes],
        check=True,
        capture_output=True,
        timeout=15,
    )
    time.sleep(3)  # iCloud propagation

    # ── Step 4: Second sync — must produce zero NO_BASELINE / CONFLICT lines ─
    result2 = _run_sync(tmp_path)
    assert result2.returncode == 0, (
        f"Second sync (post-fence) failed:\nstdout={result2.stdout}\nstderr={result2.stderr}"
    )

    bad_lines_2 = _conflict_lines(tmp_path)
    assert bad_lines_2 == [], (
        f"NO_BASELINE/CONFLICT detected after metadata fence stamp:\n"
        + "\n".join(bad_lines_2)
        + f"\n\nFirst sync stderr: {result1.stderr}"
        + f"\nSecond sync stderr: {result2.stderr}"
    )

    # Verify the hash in state is still the same (fence stripped → same canonical).
    state2 = json.loads(state_path.read_text())
    if rid in state2["tasks"]:
        assert state2["tasks"][rid]["hash"] == baseline_hash, (
            f"Hash changed after fence stamp: before={baseline_hash!r}, "
            f"after={state2['tasks'][rid]['hash']!r}. "
            "syncstate._strip_gtd_fence may not be working correctly."
        )

    # ── Step 5: Third sync — still clean ─────────────────────────────────────
    result3 = _run_sync(tmp_path)
    assert result3.returncode == 0, (
        f"Third sync failed:\nstdout={result3.stdout}\nstderr={result3.stderr}"
    )

    bad_lines_3 = _conflict_lines(tmp_path)
    assert bad_lines_3 == [], (
        f"NO_BASELINE/CONFLICT detected on third sync:\n"
        + "\n".join(bad_lines_3)
    )

    # Sanity: the reminder is still tracked in state.
    state3 = json.loads(state_path.read_text())
    assert rid in state3["tasks"], (
        f"rid {rid!r} disappeared from sync state after third sync."
    )
