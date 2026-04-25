# reminder_claude_sync

> Bidirectional sync between **macOS Reminders.app** and a markdown `TASKS.md` — fast enough to use from Claude Code without freezing Reminders.

[![macOS](https://img.shields.io/badge/macOS-13%2B-000000?logo=apple)](#requirements)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776ab?logo=python&logoColor=white)](#requirements)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

```
┌──────────────────────┐                    ┌────────────────────────┐
│  Apple Reminders     │   bin/todo-pull    │  TASKS.md              │
│  (iCloud / iOS /     │ ─────────────────► │  productivity-skill    │
│   Watch / Siri)      │                    │  format, editable      │
│                      │ ◄───────────────── │                        │
└──────────────────────┘   bin/todo-push    └────────────────────────┘
                                                       ▲
                                                       │ Claude Code reads
                                                       │ and edits this
                                                       │ as a normal file
```

Reminders stays the source of truth — every iCloud, iOS, Watch, and Siri integration keeps working. `TASKS.md` is a mirror that you (and Claude) can read and edit as plain markdown.

---

## Why this exists

Working with Apple Reminders from a coding agent is awkward. Two real options:

1. **Raw AppleScript** — works, but a query against ~150 reminders takes 1–2 minutes and **freezes Reminders.app's UI** the whole time.
2. **Replace Reminders with a markdown file** — fast, but you lose iCloud sync, watchOS complications, Siri, time/location alerts.

This repo is option 3: keep Reminders as the database, mirror it to `TASKS.md` for the agent to operate on, push edits back. Reads are sub-second because they go through [`keith/reminders-cli`](https://github.com/keith/reminders-cli) (Swift + EventKit) instead of AppleScript.

| Approach | 133 reminders, full read | Freezes Reminders.app? |
|---|---:|:---:|
| `osascript` per-property | ~120 s | ✅ yes |
| `osascript` bulk-fetch | ~16 s | ✅ yes |
| **`reminders-cli` (this repo)** | **~0.3 s** | ❌ no |

---

## Requirements

- macOS 13+ (uses EventKit)
- Python 3.10+ (stdlib only — no pip install needed)
- Swift toolchain (Xcode Command Line Tools is enough — `xcode-select --install`)
- Permission for Terminal/iTerm/your shell to control Reminders (macOS will prompt on first run)

---

## Install

```bash
git clone https://github.com/ehsanfathi77/reminder_claude_sync.git
cd reminder_claude_sync
make build         # clones keith/reminders-cli, builds it, drops binary at bin/reminders-cli
```

Optional — symlink the wrappers so `todo-pull`, `todo-push`, etc. are on your `$PATH`:

```bash
make install       # symlinks bin/todo-* into ~/.local/bin
```

### On your iPhone

If you also use the GTD skill in this repo (`skills/gtd`), set the Siri default Reminders list to `Inbox`:

> **Settings → Reminders → Default List → Inbox**

Without this, "Hey Siri, remind me to X" lands in the default `Reminders` list, which the GTD engine doesn't scan. The engine ships an opt-in safety-net (`leak_capture`) that can drain a list into `Inbox` on every tick, but it is **disabled by default** because the `Reminders` list is yours for real time-based reminders. Fixing the iOS-side default is the primary control; only enable `leak_capture` (set `leak_capture_lists: ["Reminders"]` in `.gtd/config.json`) if you cannot change the iOS default.

---

## Use

```bash
make pull          # Reminders → TASKS.md (safe: never writes back)
make push          # TASKS.md → Reminders
make sync          # bidirectional
make status        # read-only summary; no writes
```

Or call the CLI directly for more options:

```bash
python3 bin/sync.py sync --verbose --done-window 14
python3 bin/sync.py --dry-run sync
python3 bin/sync.py lists
```

| Flag | Default | What it does |
|---|---|---|
| `--done-window N` | 7 | Mirror reminders completed in the last *N* days (0 = skip all completed) |
| `--dry-run` | off | Plan only; touches nothing |
| `--verbose` / `-v` | off | Per-operation log |
| `--root PATH` | repo root | Run against a different `TASKS.md` directory |

---

## TASKS.md format

Matches the Anthropic productivity skill's [`task-management`](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills) spec:

```markdown
# Tasks

## Active

### Reminders                                              ← grouping by Reminders list
- [ ] **Buy milk** - due Fri Apr 25, 5:00 PM <!-- rid:ABC-123 list:Reminders due:2026-04-25T17:00:00 -->
  - 2% organic                                              ← sub-bullets become reminder notes

### Books to Read
- [ ] **Project Hail Mary** <!-- rid:DEF-456 list:"Books to Read" -->

## Waiting On
## Someday
## Done
- [x] ~~Old task~~ (Fri Apr 18) <!-- rid:GHI-789 list:Reminders done_at:2026-04-18T15:00:00 -->
```

The trailing HTML comment is the round-trip identity (`rid` = the reminder's stable EventKit external id). **Don't strip it manually** — the sync needs it to match a markdown line back to its Reminder.

### Section ↔ list mapping

| Section in TASKS.md | What lives there |
|---|---|
| `## Active` | Open reminders from any list, grouped by `### <List Name>` |
| `## Waiting On` | Open reminders in a list literally named `Waiting On` or `Waiting` |
| `## Someday` | Open reminders in a list literally named `Someday` |
| `## Done` | Reminders completed within the last 7 days (configurable) |

### Adding a brand-new task

Append a line under any section, no `rid` needed. On next push, the sync creates the Reminder and writes the rid back into TASKS.md:

```markdown
## Active
### Reminders
- [ ] **Brand new task**           ← no comment, will be created
```

After push:

```markdown
- [ ] **Brand new task** <!-- rid:NEW-UUID list:Reminders -->
```

---

## How it works

```
                  ┌──────────────────────────────────┐
                  │  bin/sync.py                     │  pull/push/sync/status
                  └──────────────┬───────────────────┘
                                 │
                ┌────────────────┼────────────────┐
                │                │                │
        ┌───────▼──────┐ ┌──────▼─────┐ ┌────────▼────────┐
        │ reminders.py │ │ tasksmd.py │ │  syncstate.py   │
        │  CLI bridge  │ │  parse +   │ │  hash-based     │
        │  (+ small    │ │  serialize │ │  loop prevention│
        │  AppleScript │ │  TASKS.md  │ │                 │
        │  for due/    │ └────────────┘ └─────────────────┘
        │  move only)  │
        └──────┬───────┘
               │
        ┌──────▼──────────────────┐
        │  bin/reminders-cli      │   Swift + EventKit
        │  (keith/reminders-cli)  │   ~0.5s for hundreds of items
        └─────────────────────────┘
```

**Loop prevention.** State is kept in `.sync-state.json` as a SHA1 of the canonical projection of each task `{title, notes, due_iso[:16], completed, list}`. On each sync, the engine compares Apple-side hash and md-side hash against the stored baseline:

- both unchanged → no-op
- only one side changed → propagate that side
- both changed since baseline → log conflict, **Apple wins** (iCloud is authoritative)
- no baseline yet (fresh state) → if hashes are equal, adopt as baseline; if they differ, log conflict and do nothing — neither side wins automatically

This algorithm is a stripped-down version of the pattern in [`liketheduck/supernote-apple-reminders-sync`](https://github.com/liketheduck/supernote-apple-reminders-sync); credit there.

---

## Using this with Claude Code

Drop a `CLAUDE.md` next to your `TASKS.md` so the agent knows the sync exists. See [`CLAUDE.md.example`](CLAUDE.md.example) for a starting template.

---

## Layout

```
bin/
  sync.py                             main entry (subcommands)
  todo-{pull,push,sync,status}        thin shell wrappers
  reminders-cli                       built by `make build` (gitignored)
  lib/
    reminders.py                      reminders-cli + osascript bridge
    tasksmd.py                        TASKS.md parse/serialize
    syncstate.py                      state load/save, hashing
  applescripts/
    update.applescript                only used for due-date / list-move
Makefile
LICENSE
README.md
CLAUDE.md.example
```

Per-machine state files (gitignored): `TASKS.md`, `.sync-state.json`, `.sync-conflicts.log`.

---

## Limitations

- **Recurring reminders**: RRULE isn't exposed cleanly. Recurrence is preserved in Reminders.app; the next-instance title/due/notes mirror to TASKS.md but the recurrence rule itself doesn't round-trip.
- **Subtasks**: Apple's subtask model isn't exposed by `reminders-cli`. Use sub-bullets in notes instead.
- **Location/geofence alerts**: preserved on the Reminders side, not represented in TASKS.md.
- **Real-time sync**: this runs on demand. For periodic pulls, add a `launchd` plist that runs `make pull` every N minutes.
- **Done-window flip**: if you pull with `--done-window 60` then later run with the default `7`, the older completed rows stay in md until you remove them by hand.

---

## Troubleshooting

**`reminders-cli not found`** — run `make build`. macOS will prompt once for Reminders access on first run; approve it.

**Title shows weird characters in TASKS.md** — non-ASCII is passed through unchanged; check your editor is reading TASKS.md as UTF-8.

**Conflicts** — open `.sync-conflicts.log` to see the apple-side and md-side records that disagreed.

---

## Credits

- [`keith/reminders-cli`](https://github.com/keith/reminders-cli) — the Swift binary that makes this fast. Without it, Reminders.app would freeze every time you sync.
- [`liketheduck/supernote-apple-reminders-sync`](https://github.com/liketheduck/supernote-apple-reminders-sync) — content-hash + sync-state pattern for loop prevention.
- [`AungMyoKyaw/apple-reminders-cli`](https://github.com/AungMyoKyaw/apple-reminders-cli) and [`serhiip/org2any`](https://github.com/serhiip/org2any) — studied during research.

## License

[MIT](LICENSE).
