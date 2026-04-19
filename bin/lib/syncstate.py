"""
syncstate.py — Sync state, hashing, and three-way diff.

State file `.sync-state.json` shape:
{
  "version": 1,
  "last_sync": "2026-04-18T20:30:00",
  "tasks": {
    "<rid>": {
      "title": "...",
      "notes": "...",
      "due_iso": "...",
      "completed": false,
      "list": "...",
      "hash": "<sha1 of canonical form>",
      "synced_at": "ISO"
    }
  }
}

Hash is over a canonical, deterministic projection of the task fields we care about.
Same projection is used for both sides (Reminders, TASKS.md) so equal content → equal hash.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any


VERSION = 1


def _normalize_text(s: str | None) -> str:
    """Collapse CRLF/CR to LF and strip outer whitespace.
    Reminders.app stores notes with CRLF; markdown writes LF; without this
    normalization the hash flips every sync and the loop never converges."""
    if not s:
        return ""
    return s.replace("\r\n", "\n").replace("\r", "\n").strip()


def _canonical(d: dict[str, Any]) -> str:
    """Stable JSON projection used for hashing."""
    keep = {
        "title": _normalize_text(d.get("title")),
        "notes": _normalize_text(d.get("notes")),
        # Truncate due to minute precision so we don't churn on second-level drift.
        "due_iso": (d.get("due_iso") or "")[:16],
        "completed": bool(d.get("completed", False)),
        "list": _normalize_text(d.get("list")),
    }
    return json.dumps(keep, sort_keys=True, ensure_ascii=False)


def hash_record(d: dict[str, Any]) -> str:
    return hashlib.sha1(_canonical(d).encode("utf-8")).hexdigest()


def load(path: Path) -> dict:
    if not path.exists():
        return {"version": VERSION, "last_sync": "", "tasks": {}}
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or "tasks" not in data:
            return {"version": VERSION, "last_sync": "", "tasks": {}}
        data.setdefault("version", VERSION)
        data.setdefault("last_sync", "")
        data.setdefault("tasks", {})
        return data
    except (json.JSONDecodeError, OSError):
        return {"version": VERSION, "last_sync": "", "tasks": {}}


def save(path: Path, state: dict) -> None:
    state["last_sync"] = datetime.now().replace(microsecond=0).isoformat()
    state["version"] = VERSION
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True))


def reminder_to_record(r) -> dict[str, Any]:
    """Convert reminders.Reminder → record dict for hashing."""
    return {
        "title": r.name,
        "notes": r.body,
        "due_iso": r.due_date,
        "completed": r.completed,
        "list": r.list,
    }


def task_to_record(t) -> dict[str, Any]:
    """Convert tasksmd.Task → record dict for hashing."""
    return {
        "title": t.title,
        "notes": t.notes,
        "due_iso": t.due_iso,
        "completed": t.completed,
        "list": t.list_name,
    }


def append_conflict(path: Path, msg: str) -> None:
    """Append a conflict line. Escapes control bytes so `cat`-ing the log
    can't be hijacked by ANSI escapes embedded in reminder titles/notes."""
    ts = datetime.now().replace(microsecond=0).isoformat()
    safe = msg.encode("unicode_escape").decode("ascii")
    with path.open("a") as f:
        f.write(f"[{ts}] {safe}\n")
