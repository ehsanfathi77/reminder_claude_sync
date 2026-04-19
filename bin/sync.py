#!/usr/bin/env python3
"""
sync.py — Bidirectional sync between macOS Reminders and TASKS.md.

Subcommands:
  pull              Reminders → TASKS.md only (no writes to Reminders)
  push              TASKS.md → Reminders only (no rewrite of TASKS.md
                    except to backfill rids on newly-created reminders)
  sync              Bidirectional (default)
  status            Show counts + drift, no writes
  lists             List Reminders lists with open counts

Flags:
  --dry-run         Plan only; do nothing
  --done-window N   Days of completed reminders to mirror (default 7)
  --root PATH       Override the todo root (default: directory containing this script's parent)
  --verbose         Print each operation
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

# Make ./lib importable regardless of cwd.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from lib import reminders as R          # noqa: E402
from lib import tasksmd as M            # noqa: E402
from lib import syncstate as S          # noqa: E402


# ────────────────────────────────────────────────────────────────────────────
# Section / list mapping helpers
# ────────────────────────────────────────────────────────────────────────────

def section_for_reminder(rem: R.Reminder, done_in_window: bool) -> str:
    """Decide which TASKS.md section a Reminder lives in."""
    if rem.completed:
        return "Done" if done_in_window else "Active"  # caller filters out-of-window
    name_lower = rem.list.strip().lower()
    if name_lower in ("waiting", "waiting on"):
        return "Waiting On"
    if name_lower == "someday":
        return "Someday"
    return "Active"


def reminder_to_task(rem: R.Reminder) -> M.Task:
    return M.Task(
        title=rem.name,
        notes=rem.body,
        completed=rem.completed,
        due_iso=rem.due_date,
        completion_date_iso=rem.completion_date,
        list_name=rem.list,
        rid=rem.id,
        section=section_for_reminder(rem, done_in_window=True),
        priority=rem.priority,
    )


# ────────────────────────────────────────────────────────────────────────────
# Pull (Reminders → TASKS.md)
# ────────────────────────────────────────────────────────────────────────────

def cmd_pull(root: Path, *, done_window: int, dry_run: bool, verbose: bool) -> int:
    tasks_path = root / "TASKS.md"
    state_path = root / ".sync-state.json"

    rems = R.list_all(days_done_window=done_window)
    if verbose:
        print(f"[pull] read {len(rems)} reminders (open + done within {done_window}d)", file=sys.stderr)

    # Convert to Tasks.
    tasks = [reminder_to_task(r) for r in rems]

    # Sort: section order, then by list name (case-insensitive), then by title.
    sec_order = {s: i for i, s in enumerate(M.SECTIONS)}
    tasks.sort(key=lambda t: (sec_order.get(t.section, 99), (t.list_name or "").lower(), t.title.lower()))

    out = M.serialize(tasks)

    if dry_run:
        print("[pull] DRY-RUN — would write TASKS.md:")
        print(out[:1500] + ("\n... (truncated)" if len(out) > 1500 else ""))
        return 0

    tasks_path.write_text(out)
    if verbose:
        print(f"[pull] wrote {tasks_path} ({len(tasks)} tasks)", file=sys.stderr)

    # Snapshot state so a subsequent push doesn't re-trigger anything.
    state = S.load(state_path)
    state["tasks"] = {}
    for r in rems:
        rec = S.reminder_to_record(r)
        rec["hash"] = S.hash_record(rec)
        rec["synced_at"] = datetime.now().replace(microsecond=0).isoformat()
        state["tasks"][r.id] = rec
    S.save(state_path, state)
    if verbose:
        print(f"[pull] wrote state {state_path}", file=sys.stderr)
    return 0


# ────────────────────────────────────────────────────────────────────────────
# Push (TASKS.md → Reminders)
# ────────────────────────────────────────────────────────────────────────────

def cmd_push(root: Path, *, done_window: int, dry_run: bool, verbose: bool) -> int:
    tasks_path = root / "TASKS.md"
    state_path = root / ".sync-state.json"
    if not tasks_path.exists():
        print(f"[push] no TASKS.md at {tasks_path}; nothing to push", file=sys.stderr)
        return 0

    md_tasks = M.parse(tasks_path)
    state = S.load(state_path)
    rems = {r.id: r for r in R.list_all(days_done_window=done_window)}

    md_by_rid: dict[str, M.Task] = {t.rid: t for t in md_tasks if t.rid}
    new_md_tasks = [t for t in md_tasks if not t.rid]

    creates = 0
    updates = 0
    completes = 0
    skipped_unchanged = 0

    # 1. Existing tasks (rid present): apply local edits to Reminders.
    for rid, t in md_by_rid.items():
        rec = S.task_to_record(t)
        new_hash = S.hash_record(rec)
        prev = state["tasks"].get(rid, {})
        prev_hash = prev.get("hash")
        if prev_hash == new_hash:
            skipped_unchanged += 1
            continue

        rem = rems.get(rid)
        if rem is None:
            # Reminder was deleted on Apple side. Drop the markdown row too,
            # else it sits forever as an orphan and a future re-creation with
            # the same title will produce a duplicate.
            if verbose:
                print(f"[push] dropping orphan rid={rid[:24]}… (deleted on Apple side)", file=sys.stderr)
            md_tasks = [x for x in md_tasks if x.rid != rid]
            state["tasks"].pop(rid, None)
            continue

        if verbose:
            print(f"[push] update rid={rid[:24]}… title={t.title!r}", file=sys.stderr)

        success = True
        if not dry_run:
            try:
                # Apply moves FIRST so subsequent edits target the new list.
                effective_list = rem.list
                if (t.list_name or "") and t.list_name != rem.list:
                    R.move_to_list(rid, t.list_name)
                    effective_list = t.list_name
                if t.title != rem.name:
                    R.update_title(rid, effective_list, t.title)
                if t.notes != rem.body:
                    R.update_notes(rid, effective_list, t.notes)
                if (t.due_iso or "") != (rem.due_date or ""):
                    R.update_due(rid, t.due_iso or "")
                if t.completed != rem.completed:
                    R.set_complete(rid, effective_list, t.completed)
                    if t.completed:
                        completes += 1
            except R.RemindersError as e:
                print(f"[push] WARNING update rid={rid}: {e}", file=sys.stderr)
                success = False

        if success:
            updates += 1
            # Refresh state ONLY on full success. Partial failures leave the
            # previous baseline so the next sync retries from a known state.
            rec["hash"] = new_hash
            rec["synced_at"] = datetime.now().replace(microsecond=0).isoformat()
            state["tasks"][rid] = rec

    # 2. New tasks (no rid): create in Reminders, backfill rid.
    for t in new_md_tasks:
        list_name = t.list_name or M.SECTION_DEFAULT_LIST.get(t.section, "Reminders")
        if verbose:
            print(f"[push] create list={list_name} title={t.title!r}", file=sys.stderr)
        if dry_run:
            creates += 1
            continue
        try:
            new_rid = R.create(list_name, t.title, t.notes, t.due_iso)
        except R.RemindersError as e:
            print(f"[push] WARNING create {t.title!r}: {e}", file=sys.stderr)
            continue
        t.rid = new_rid
        t.list_name = list_name
        creates += 1
        rec = S.task_to_record(t)
        rec["hash"] = S.hash_record(rec)
        rec["synced_at"] = datetime.now().replace(microsecond=0).isoformat()
        state["tasks"][new_rid] = rec
        # If user marked it done at creation time, push that too.
        if t.completed:
            try:
                R.set_complete(new_rid, list_name, True)
                completes += 1
            except R.RemindersError as e:
                print(f"[push] WARNING complete {new_rid}: {e}", file=sys.stderr)

    # 3. Removed from md (rid in state, not in md, still in Apple): mark complete.
    md_rids = set(md_by_rid.keys())
    state_rids = set(state["tasks"].keys())
    for rid in state_rids - md_rids:
        if rid in rems and not rems[rid].completed:
            if verbose:
                print(f"[push] removed locally → complete rid={rid[:24]}…", file=sys.stderr)
            if not dry_run:
                try:
                    R.set_complete(rid, rems[rid].list, True)
                    completes += 1
                except R.RemindersError as e:
                    print(f"[push] WARNING complete-removed rid={rid}: {e}", file=sys.stderr)
        state["tasks"].pop(rid, None)

    if not dry_run:
        # Always rewrite TASKS.md — we may have created new rids OR dropped
        # orphans for reminders deleted on Apple side.
        tasks_path.write_text(M.serialize(md_tasks))
        S.save(state_path, state)

    print(f"[push] {'DRY-RUN ' if dry_run else ''}"
          f"updated={updates} created={creates} completed={completes} unchanged={skipped_unchanged}",
          file=sys.stderr)
    return 0


# ────────────────────────────────────────────────────────────────────────────
# Sync (bidirectional)
# ────────────────────────────────────────────────────────────────────────────

def cmd_sync(root: Path, *, done_window: int, dry_run: bool, verbose: bool) -> int:
    tasks_path = root / "TASKS.md"
    state_path = root / ".sync-state.json"
    conflict_path = root / ".sync-conflicts.log"

    rems_list = R.list_all(days_done_window=done_window)
    rems = {r.id: r for r in rems_list}
    if not tasks_path.exists():
        # First-time bootstrap → equivalent to pull.
        return cmd_pull(root, done_window=done_window, dry_run=dry_run, verbose=verbose)

    md_tasks = M.parse(tasks_path)
    md_by_rid: dict[str, M.Task] = {t.rid: t for t in md_tasks if t.rid}
    state = S.load(state_path)

    creates = updates_a = updates_m = completes = conflicts = unchanged = 0

    # Walk every rid we know about (apple ∪ md ∪ state).
    all_rids = set(rems.keys()) | set(md_by_rid.keys()) | set(state["tasks"].keys())
    for rid in all_rids:
        rem = rems.get(rid)
        mt = md_by_rid.get(rid)
        prev = state["tasks"].get(rid, {})
        prev_hash = prev.get("hash")

        a_rec = S.reminder_to_record(rem) if rem else None
        m_rec = S.task_to_record(mt) if mt else None
        a_hash = S.hash_record(a_rec) if a_rec else None
        m_hash = S.hash_record(m_rec) if m_rec else None

        a_changed = (a_rec is not None and a_hash != prev_hash)
        m_changed = (m_rec is not None and m_hash != prev_hash)

        # ── Missing-baseline carve-out ────────────────────────────────────
        # When prev_hash is None (fresh state file or new rid both sides
        # already know), the "X changed since baseline" question has no
        # answer. Treat hash-equality as the truth signal: equal → adopt as
        # baseline, no-op. Unequal → log conflict, do NOT auto-resolve.
        if prev_hash is None and rem is not None and mt is not None:
            if a_hash == m_hash:
                rec = a_rec
                rec["hash"] = a_hash
                rec["synced_at"] = datetime.now().replace(microsecond=0).isoformat()
                state["tasks"][rid] = rec
                unchanged += 1
            else:
                S.append_conflict(
                    conflict_path,
                    f"NO_BASELINE rid={rid} apple={a_rec} md={m_rec} (not auto-resolved)",
                )
                conflicts += 1
            continue

        # Case: missing on both → drop from state
        if rem is None and mt is None:
            state["tasks"].pop(rid, None)
            continue

        # Case: present in apple, absent in md
        if rem is not None and mt is None:
            if rid in state["tasks"]:
                # Was synced before, now gone from md → user "removed" → complete in apple.
                if not rem.completed:
                    if verbose:
                        print(f"[sync] removed-locally → complete rid={rid[:16]}…", file=sys.stderr)
                    if not dry_run:
                        try:
                            R.set_complete(rid, rem.list, True)
                        except R.RemindersError as e:
                            print(f"[sync] WARNING complete {rid}: {e}", file=sys.stderr)
                    completes += 1
                state["tasks"].pop(rid, None)
            else:
                # Brand new on apple → add to md
                new_t = reminder_to_task(rem)
                md_tasks.append(new_t)
                md_by_rid[rid] = new_t
                a_rec["hash"] = a_hash
                a_rec["synced_at"] = datetime.now().replace(microsecond=0).isoformat()
                state["tasks"][rid] = a_rec
                updates_m += 1
            continue

        # Case: present in md, absent in apple
        if rem is None and mt is not None:
            if rid in state["tasks"]:
                # Was synced; apple deleted it → drop from md
                md_tasks = [t for t in md_tasks if t.rid != rid]
                state["tasks"].pop(rid, None)
                updates_m += 1
            else:
                # No state, no apple, but rid present in md → orphan; ignore (could be a stale paste).
                pass
            continue

        # Case: present on both
        if not a_changed and not m_changed:
            unchanged += 1
            continue

        if a_changed and not m_changed:
            # Apple changed → update md task in place
            for fld in ("name", "body", "due_date", "completed", "list", "completion_date"):
                pass
            mt.title = rem.name
            mt.notes = rem.body
            mt.due_iso = rem.due_date
            mt.completed = rem.completed
            mt.list_name = rem.list
            mt.completion_date_iso = rem.completion_date
            mt.section = section_for_reminder(rem, done_in_window=True)
            updates_m += 1
            a_rec["hash"] = a_hash
            a_rec["synced_at"] = datetime.now().replace(microsecond=0).isoformat()
            state["tasks"][rid] = a_rec
            continue

        if m_changed and not a_changed:
            # MD changed → push to apple
            if verbose:
                print(f"[sync] md→apple rid={rid[:16]}… title={mt.title!r}", file=sys.stderr)
            if not dry_run:
                try:
                    effective_list = rem.list
                    if (mt.list_name or "") and mt.list_name != rem.list:
                        R.move_to_list(rid, mt.list_name)
                        effective_list = mt.list_name
                    if mt.title != rem.name:
                        R.update_title(rid, effective_list, mt.title)
                    if mt.notes != rem.body:
                        R.update_notes(rid, effective_list, mt.notes)
                    if (mt.due_iso or "") != (rem.due_date or ""):
                        R.update_due(rid, mt.due_iso or "")
                    if mt.completed != rem.completed:
                        R.set_complete(rid, effective_list, mt.completed)
                        if mt.completed:
                            completes += 1
                except R.RemindersError as e:
                    print(f"[sync] WARNING update {rid}: {e}", file=sys.stderr)
            updates_a += 1
            m_rec["hash"] = m_hash
            m_rec["synced_at"] = datetime.now().replace(microsecond=0).isoformat()
            state["tasks"][rid] = m_rec
            continue

        # Both changed → conflict. Apple wins by default; log it.
        S.append_conflict(
            conflict_path,
            f"CONFLICT rid={rid} apple={a_rec} md={m_rec}",
        )
        conflicts += 1
        # Apple wins
        mt.title = rem.name
        mt.notes = rem.body
        mt.due_iso = rem.due_date
        mt.completed = rem.completed
        mt.list_name = rem.list
        mt.completion_date_iso = rem.completion_date
        mt.section = section_for_reminder(rem, done_in_window=True)
        a_rec["hash"] = a_hash
        a_rec["synced_at"] = datetime.now().replace(microsecond=0).isoformat()
        state["tasks"][rid] = a_rec
        updates_m += 1

    # Brand-new md tasks (no rid yet) → create in apple
    new_md = [t for t in md_tasks if not t.rid]
    for t in new_md:
        list_name = t.list_name or M.SECTION_DEFAULT_LIST.get(t.section, "Reminders")
        if verbose:
            print(f"[sync] create list={list_name} title={t.title!r}", file=sys.stderr)
        if dry_run:
            creates += 1
            continue
        try:
            new_rid = R.create(list_name, t.title, t.notes, t.due_iso)
        except R.RemindersError as e:
            print(f"[sync] WARNING create {t.title!r}: {e}", file=sys.stderr)
            continue
        t.rid = new_rid
        t.list_name = list_name
        if t.completed:
            try:
                R.set_complete(new_rid, list_name, True)
                completes += 1
            except R.RemindersError as e:
                print(f"[sync] WARNING complete-new {new_rid}: {e}", file=sys.stderr)
        rec = S.task_to_record(t)
        rec["hash"] = S.hash_record(rec)
        rec["synced_at"] = datetime.now().replace(microsecond=0).isoformat()
        state["tasks"][new_rid] = rec
        creates += 1

    if not dry_run:
        # Re-sort & rewrite TASKS.md.
        sec_order = {s: i for i, s in enumerate(M.SECTIONS)}
        md_tasks.sort(key=lambda t: (sec_order.get(t.section, 99), (t.list_name or "").lower(), t.title.lower()))
        tasks_path.write_text(M.serialize(md_tasks))
        S.save(state_path, state)

    print(
        f"[sync] {'DRY-RUN ' if dry_run else ''}"
        f"created={creates} apple_updates={updates_a} md_updates={updates_m} "
        f"completed={completes} conflicts={conflicts} unchanged={unchanged}",
        file=sys.stderr,
    )
    return 0


# ────────────────────────────────────────────────────────────────────────────
# Status / lists (read-only diagnostics)
# ────────────────────────────────────────────────────────────────────────────

def cmd_status(root: Path, *, done_window: int, **_) -> int:
    rems = R.list_all(days_done_window=done_window)
    state = S.load(root / ".sync-state.json")
    md_tasks = M.parse(root / "TASKS.md") if (root / "TASKS.md").exists() else []
    by_list: dict[str, int] = defaultdict(int)
    open_count = 0
    for r in rems:
        if not r.completed:
            by_list[r.list] += 1
            open_count += 1
    print(f"Reminders: {open_count} open across {len(by_list)} lists")
    for name, n in sorted(by_list.items(), key=lambda kv: (-kv[1], kv[0].lower())):
        print(f"  {name}: {n}")
    print(f"TASKS.md: {len(md_tasks)} tasks parsed")
    print(f"State: {len(state.get('tasks', {}))} known rids; last_sync={state.get('last_sync') or 'never'}")
    return 0


def cmd_lists(root: Path, **_) -> int:
    for n in R.list_names():
        print(n)
    return 0


# ────────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["pull", "push", "sync", "status", "lists"], nargs="?", default="sync")
    p.add_argument("--root", type=Path, default=SCRIPT_DIR.parent,
                   help="Todo folder root (default: parent of bin/)")
    p.add_argument("--done-window", type=int, default=7,
                   help="Days of completed reminders to mirror (default 7)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    root: Path = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)

    cmds = {
        "pull": cmd_pull,
        "push": cmd_push,
        "sync": cmd_sync,
        "status": cmd_status,
        "lists": cmd_lists,
    }
    return cmds[args.command](
        root,
        done_window=args.done_window,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    raise SystemExit(main())
