"""
tasksmd.py — Parse + serialize the productivity-skill TASKS.md format.

Format reference (from productivity:task-management SKILL.md):

    # Tasks

    ## Active

    ## Waiting On

    ## Someday

    ## Done

Task line shape:
    - [ ] **Title** - context, for whom, due date <!-- rid:X list:Y -->
    - [x] ~~Title~~ (date) <!-- rid:X list:Y -->

We allow optional `### <List Name>` subheadings under any section to group by list.
Sub-bullets (lines starting with two spaces and "- ") are captured as notes.

Round-trip identity is held by the trailing HTML comment:
    <!-- rid:<reminder-id> list:<list name> due:<ISO?> prio:<n?> -->

When a line has no rid, it is treated as a brand-new task (to be created on push).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

SECTIONS = ["Active", "Waiting On", "Someday", "Done"]

# Section-to-default-list mapping for new tasks discovered in TASKS.md (no rid yet).
SECTION_DEFAULT_LIST = {
    "Active": "Reminders",       # macOS default list name
    "Waiting On": "Waiting On",
    "Someday": "Someday",
    "Done": "Reminders",
}

TASK_RE = re.compile(
    r"""^(?P<indent>\s*)
        -\s\[(?P<box>[ xX])\]\s+
        (?P<rest>.*)$""",
    re.VERBOSE,
)
SUBBULLET_RE = re.compile(r"^\s{2,}-\s+(?P<text>.*)$")
META_RE = re.compile(r"<!--\s*(?P<body>[^>]*?)\s*-->\s*$")
META_FIELD_RE = re.compile(r"(\w+):((?:[^\s\"]+|\"[^\"]*\"))")
TITLE_RE = re.compile(r"\*\*(?P<title>.+?)\*\*")
STRIKE_RE = re.compile(r"~~(?P<title>.+?)~~")


@dataclass
class Task:
    title: str = ""
    notes: str = ""             # multi-line; joined by \n
    completed: bool = False
    due_iso: str = ""           # YYYY-MM-DDTHH:MM:SS local; "" if none
    list_name: str = ""         # Reminders list this belongs to
    rid: str = ""               # Apple reminder id; "" for brand-new tasks
    section: str = "Active"     # Which TASKS.md section this lives in
    completion_date_iso: str = ""
    priority: int = 0
    extras: dict = field(default_factory=dict)  # other metadata kept verbatim

    @property
    def is_new(self) -> bool:
        return not self.rid


def _parse_meta(comment_body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in META_FIELD_RE.finditer(comment_body):
        key = m.group(1)
        val = m.group(2)
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
        out[key] = val
    return out


def _serialize_meta(meta: dict[str, str]) -> str:
    parts = []
    for k, v in meta.items():
        if v == "":
            continue
        if any(c in v for c in (" ", "\t")):
            parts.append(f'{k}:"{v}"')
        else:
            parts.append(f"{k}:{v}")
    if not parts:
        return ""
    return "<!-- " + " ".join(parts) + " -->"


def _format_due_human(iso: str) -> str:
    """Render an ISO date as 'Apr 25 5:00 PM' (no seconds, no year if current)."""
    if not iso:
        return ""
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(iso)
        # 'Fri Apr 25, 5:00 PM'
        if dt.hour == 0 and dt.minute == 0:
            return dt.strftime("%a %b %d")
        return dt.strftime("%a %b %d, %-I:%M %p")
    except (ValueError, TypeError):
        return iso


def parse(path: Path) -> list[Task]:
    """Parse TASKS.md into a flat list of Task objects in file order."""
    if not path.exists():
        return []
    tasks: list[Task] = []
    section = "Active"
    sub_list: str = ""        # current ### subheading list-name
    current: Task | None = None

    for raw in path.read_text().splitlines():
        line = raw.rstrip()

        # ## Section
        if line.startswith("## "):
            section_name = line[3:].strip()
            if section_name in SECTIONS:
                section = section_name
                sub_list = ""
            current = None
            continue

        # ### Sub-list grouping
        if line.startswith("### "):
            sub_list = line[4:].strip()
            current = None
            continue

        # Top-level task line
        m = TASK_RE.match(line)
        if m and len(m.group("indent")) == 0:
            rest = m.group("rest")
            box = m.group("box")
            completed = box.lower() == "x"

            # Strip trailing meta comment
            meta: dict[str, str] = {}
            mm = META_RE.search(rest)
            if mm:
                meta = _parse_meta(mm.group("body"))
                rest = rest[: mm.start()].rstrip()

            # Title: prefer **bold** or ~~strike~~; else first chunk before " - "
            title = ""
            stm = STRIKE_RE.search(rest) if completed else None
            tm = TITLE_RE.search(rest)
            if tm:
                title = tm.group("title").strip()
            elif stm:
                title = stm.group("title").strip()
            else:
                title = rest.split(" - ", 1)[0].strip()

            # Section assignment: meta wins, else current section
            sec = meta.pop("section", section)
            if sec not in SECTIONS:
                sec = section

            # List: meta wins; else sub_list grouping; else section default
            list_name = meta.pop("list", "") or sub_list or SECTION_DEFAULT_LIST.get(sec, "Reminders")

            current = Task(
                title=title,
                completed=completed,
                rid=meta.pop("rid", ""),
                due_iso=meta.pop("due", ""),
                priority=int(meta.pop("prio", "0") or 0),
                completion_date_iso=meta.pop("done_at", ""),
                list_name=list_name,
                section=sec,
                extras=meta,
            )
            tasks.append(current)
            continue

        # Sub-bullet → append to notes of current task
        sm = SUBBULLET_RE.match(line)
        if sm and current is not None:
            text = sm.group("text").strip()
            if current.notes:
                current.notes += "\n" + text
            else:
                current.notes = text
            continue

        # otherwise: skip (blank lines, prose, etc.)

    return tasks


def serialize(tasks: list[Task]) -> str:
    """Render a list of Tasks back into TASKS.md text."""
    by_section: dict[str, list[Task]] = {s: [] for s in SECTIONS}
    for t in tasks:
        sec = t.section if t.section in SECTIONS else "Active"
        by_section[sec].append(t)

    lines = ["# Tasks", ""]
    for sec in SECTIONS:
        lines.append(f"## {sec}")
        lines.append("")

        bucket = by_section[sec]
        if not bucket:
            continue

        # For Active, group by list with ### subheaders. Other sections: flat.
        if sec == "Active":
            by_list: dict[str, list[Task]] = {}
            for t in bucket:
                by_list.setdefault(t.list_name or "Reminders", []).append(t)
            for list_name in sorted(by_list.keys(), key=str.lower):
                lines.append(f"### {list_name}")
                lines.append("")
                for t in by_list[list_name]:
                    lines.extend(_render_task(t))
                lines.append("")
        else:
            for t in bucket:
                lines.extend(_render_task(t))
            lines.append("")

    # collapse multiple trailing blanks → single blank
    out = "\n".join(lines).rstrip() + "\n"
    return out


def _render_task(t: Task) -> list[str]:
    box = "x" if t.completed else " "
    title_md = f"~~{t.title}~~" if t.completed else f"**{t.title}**"

    suffix_parts = []
    if t.due_iso and not t.completed:
        suffix_parts.append(f"due {_format_due_human(t.due_iso)}")
    if t.completed and t.completion_date_iso:
        suffix_parts.append(f"({_format_due_human(t.completion_date_iso)})")

    suffix = (" - " + ", ".join(suffix_parts)) if suffix_parts else ""

    meta = {"rid": t.rid, "list": t.list_name}
    if t.due_iso:
        meta["due"] = t.due_iso
    if t.priority:
        meta["prio"] = str(t.priority)
    if t.completion_date_iso:
        meta["done_at"] = t.completion_date_iso
    for k, v in t.extras.items():
        meta.setdefault(k, v)
    meta_str = _serialize_meta(meta)

    line = f"- [{box}] {title_md}{suffix}"
    if meta_str:
        line += " " + meta_str

    out = [line]
    if t.notes:
        for n in t.notes.split("\n"):
            n = n.strip()
            if n:
                out.append(f"  - {n}")
    return out


def template() -> str:
    """Empty TASKS.md template (matches productivity-skill spec exactly)."""
    return "# Tasks\n\n## Active\n\n## Waiting On\n\n## Someday\n\n## Done\n"
