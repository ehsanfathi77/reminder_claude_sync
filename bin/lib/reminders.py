"""
reminders.py — Bridge to Reminders.app.

Backend: keith/reminders-cli (Swift/EventKit) bundled at bin/reminders-cli.
That CLI hits the EventKit data layer directly, so it returns hundreds of
reminders in <1s and does NOT freeze the Reminders.app UI process the way
osascript-driven Apple Events do.

Two operations are not exposed by the CLI and require osascript:
  * setting/clearing the due date on an existing reminder
  * moving a reminder to a different list

Those calls are per-reminder (rare in steady state) so the slowness is bounded.

Field translation (CLI JSON → Reminder dataclass):
  externalId       → id          (UUID, no scheme prefix)
  title            → name
  notes            → body
  dueDate          → due_date    (ISO with Z)
  completionDate   → completion_date
  isCompleted      → completed
  list             → list
  priority         → priority
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BIN_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CLI = BIN_DIR / "reminders-cli"
APPLESCRIPT_DIR = BIN_DIR / "applescripts"


def _resolve_cli() -> str:
    """Find the reminders binary. Bundled copy preferred; PATH fallback."""
    if DEFAULT_CLI.exists() and os.access(DEFAULT_CLI, os.X_OK):
        return str(DEFAULT_CLI)
    on_path = shutil.which("reminders")
    if on_path:
        return on_path
    raise RemindersError(
        f"reminders-cli not found. Expected at {DEFAULT_CLI} or on PATH. "
        f"Build with: cd research/reminders-cli && swift build -c release"
    )


@dataclass
class Reminder:
    id: str
    list: str
    name: str
    completed: bool = False
    due_date: str = ""           # ISO local: YYYY-MM-DDTHH:MM:SS, "" if none
    completion_date: str = ""    # ISO local
    body: str = ""
    priority: int = 0
    last_modified: str = ""      # ISO with Z, raw from CLI

    @classmethod
    def from_cli_json(cls, d: dict[str, Any]) -> "Reminder":
        return cls(
            id=d.get("externalId", "") or "",
            list=d.get("list", "") or "",
            name=d.get("title", "") or "",
            completed=bool(d.get("isCompleted", False)),
            due_date=_utc_iso_to_local(d.get("dueDate", "") or ""),
            completion_date=_utc_iso_to_local(d.get("completionDate", "") or ""),
            body=d.get("notes", "") or "",
            priority=int(d.get("priority", 0) or 0),
            last_modified=d.get("lastModified", "") or "",
        )


class RemindersError(RuntimeError):
    pass


def _utc_iso_to_local(s: str) -> str:
    """Convert '2026-04-25T17:00:00Z' (UTC) → '2026-04-25T13:00:00' (local naive).
    Empty stays empty.

    Why: the Swift CLI emits dueDate/completionDate in UTC. Our AppleScript
    fallback (update.applescript) interprets ISO components as LOCAL time.
    Without this conversion, a due date set in NY (UTC-4) would shift by 4
    hours every push round-trip until it wandered out of the day.
    """
    if not s:
        return ""
    if not s.endswith("Z"):
        # Already naive or has explicit offset; pass through.
        return s
    from datetime import datetime, timezone
    try:
        dt_utc = datetime.fromisoformat(s[:-1]).replace(tzinfo=timezone.utc)
        dt_local = dt_utc.astimezone()  # local TZ
        return dt_local.replace(tzinfo=None, microsecond=0).isoformat()
    except (ValueError, TypeError):
        return s[:-1]


def _local_iso_to_utc_z(s: str) -> str:
    """Inverse of _utc_iso_to_local: take a naive local ISO and emit UTC with Z.
    Used when sending due dates back to the CLI's --due-date flag (which expects
    a parseable date; UTC ISO is unambiguous)."""
    if not s:
        return ""
    if s.endswith("Z"):
        return s
    from datetime import datetime, timezone
    try:
        dt_local = datetime.fromisoformat(s)
        if dt_local.tzinfo is None:
            dt_local = dt_local.astimezone()  # attach local tz
        return dt_local.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError):
        return s


def _run_cli(*args: str, timeout: int = 60) -> str:
    cli = _resolve_cli()
    try:
        result = subprocess.run(
            [cli, *args], capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise RemindersError(f"reminders-cli timed out after {timeout}s: {' '.join(args)}") from e
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "").strip()
        raise RemindersError(f"reminders-cli failed ({' '.join(args)}): {msg}")
    return result.stdout


def _run_osascript(script: Path, *args: str, timeout: int = 30) -> str:
    """Used only for the few operations the CLI doesn't expose."""
    cmd = ["osascript", str(script), *args]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        raise RemindersError(f"osascript timed out after {timeout}s: {script.name}") from e
    if result.returncode != 0:
        raise RemindersError(
            f"osascript failed ({script.name}): {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


# ────────────────────────────────────────────────────────────────────────────
# Reads
# ────────────────────────────────────────────────────────────────────────────

def list_all(days_done_window: int = 7) -> list[Reminder]:
    """Every open reminder + completed within window. Fast (~0.5s for 300 items)."""
    args = ["show-all", "--format=json"]
    if days_done_window > 0:
        args.append("--include-completed")
    out = _run_cli(*args, timeout=60)
    data = json.loads(out) if out.strip() else []
    rems = [Reminder.from_cli_json(d) for d in data]

    # CLI doesn't expose a window; filter in Python.
    if days_done_window > 0:
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_done_window)
        kept = []
        for r in rems:
            if not r.completed:
                kept.append(r)
                continue
            try:
                cd = r.completion_date or ""
                if not cd:
                    continue
                # parse "YYYY-MM-DDTHH:MM:SS" (CLI emits UTC; we already stripped Z)
                cd_dt = datetime.fromisoformat(cd).replace(tzinfo=timezone.utc)
                if cd_dt >= cutoff:
                    kept.append(r)
            except (ValueError, TypeError):
                pass
        rems = kept
    else:
        rems = [r for r in rems if not r.completed]

    return rems


def list_names() -> list[str]:
    """Return the names of every Reminders list."""
    out = _run_cli("show-lists", timeout=10)
    return [n.strip() for n in out.splitlines() if n.strip()]


# ────────────────────────────────────────────────────────────────────────────
# Writes
# ────────────────────────────────────────────────────────────────────────────

def create(list_name: str, name: str, notes: str = "", due_iso: str = "") -> str:
    """Create a reminder; return its externalId."""
    args = ["add", list_name, name, "--format=json"]
    if notes:
        args += ["--notes", notes]
    if due_iso:
        # Convert local-naive ISO → UTC-with-Z so the CLI parses unambiguously.
        args += ["--due-date", _local_iso_to_utc_z(due_iso)]
    out = _run_cli(*args, timeout=30).strip()
    if not out:
        raise RemindersError("reminders add returned no output")
    try:
        data = json.loads(out)
        return data.get("externalId", "") or ""
    except json.JSONDecodeError:
        raise RemindersError(f"reminders add: unparseable output {out!r}")


def update_title(rid: str, list_name: str, new_title: str) -> None:
    _run_cli("edit", list_name, rid, new_title, timeout=15)


def update_notes(rid: str, list_name: str, notes: str) -> None:
    # `edit` requires positional reminder text; pass current title is risky
    # but reminders-cli treats trailing args as the new title. To set notes
    # without changing title, omit positional args — verified by CLI help.
    _run_cli("edit", list_name, rid, "--notes", notes, timeout=15)


def update_due(rid: str, due_iso: str) -> None:
    """Set or clear the due date via osascript (CLI doesn't expose this)."""
    _run_osascript(APPLESCRIPT_DIR / "update.applescript", rid, "due", due_iso, timeout=20)


def set_complete(rid: str, list_name: str, completed: bool) -> None:
    sub = "complete" if completed else "uncomplete"
    _run_cli(sub, list_name, rid, timeout=15)


def move_to_list(rid: str, list_name: str) -> None:
    """Move via osascript (CLI doesn't expose this)."""
    _run_osascript(APPLESCRIPT_DIR / "update.applescript", rid, "move", list_name, timeout=20)


def delete(rid: str, list_name: str) -> None:
    _run_cli("delete", list_name, rid, timeout=15)
