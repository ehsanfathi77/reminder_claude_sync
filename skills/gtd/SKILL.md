---
name: gtd
description: GTD (Getting Things Done) engine layered over macOS Reminders. Use when the user wants to capture, clarify, organize, reflect on, or engage with their tasks. Triggers on /gtd:* commands, "GTD", "weekly review", "what's next", "clarify inbox", or task-management requests that benefit from the GTD methodology.
---

# GTD Skill — Getting Things Done on Apple Reminders

## What This Skill Does

This skill layers a complete GTD (Getting Things Done) system on top of Apple Reminders via a Python engine at `gtd/engine/cli.py`. Apple Reminders.app remains the source of truth for task identity, due dates, and iCloud/iOS/Watch sync; the engine owns GTD metadata (projects, contexts, ticklers, questions) in a local SQLite database. The skill teaches Claude how to invoke the 13 GTD commands and what each one does. A novel Q-channel (Questions list in Reminders) acts as a phone-side message bus: the engine dispatches clarification questions to the iPhone, the user replies in Reminders, and the engine consumes answers on the next tick.

**Key insight**: Reminders stays your real task database. The engine runs every 5 minutes on the Mac, processes inbox, surfaces due items, and asks clarifying questions—all without requiring the laptop to be open.

---

## Prerequisites

Before using the GTD skill, ensure:

1. **`bin/reminders-cli` built** — Run `make build` at the repo root. This compiles the Swift CLI that talks to EventKit.
2. **Reminders permission granted** — macOS will prompt Terminal/iTerm for Reminders access on first invocation. Grant it.
3. **iPhone Siri default list set to `Inbox`** — Open Settings → Reminders → Default List → tap **Inbox**. Without this, "Hey Siri, remind me to X" lands in the default `Reminders` list and bypasses clarify. This is the primary control. The engine ships an opt-in safety-net (`leak_capture`) that can drain a list into `Inbox` on every tick, but it is **disabled by default** because the `Reminders` list is yours for real time-based reminders. Enable only if you cannot change the iOS default — set `leak_capture_lists: ["Reminders"]` in `.gtd/config.json`. Doing so will move every non-tracked item in `Reminders` into `Inbox`.
4. **Legacy lists considered** — If you have old lists (`Personal`, `Books to Read`, etc.), decide whether to migrate them into GTD lists. Run `/gtd:adopt` once per list when ready (default: no-op until explicitly confirmed).
5. **State database initialized** — Run `python3 gtd/engine/cli.py tick` once. The command is idempotent; it creates `.gtd/state.db` if missing and runs one engine tick. No error if it already exists.

---

## The 13 GTD Commands

### 1. `/gtd:capture <text>`

**What it does:** Drop one or more items into the Inbox list in Reminders. Multi-line input creates one reminder per line.

**Invocation:**
```
/gtd:capture Pick up dry cleaning
/gtd:capture
  Review the IP agreement
  Call Dan about the lease
  Buy coffee
```

**Args:**
- `<text>` — free-form text, required. Can be a single line or multi-line (each line becomes one reminder).

**Behavior:**
- Each line is created as a separate reminder in the `Inbox` list.
- Metadata is set: `kind: unclarified`, `clarified: 0`.
- Items sit in Inbox until the next engine tick processes them (auto-clarify or dispatch a Q-reminder).

**What NOT to do:**
- Do not manually write to the `Inbox` list from Reminders.app and expect the engine to ignore metadata. All engine-managed lists carry metadata in notes; editing a reminder's title without updating metadata breaks GTD state tracking.

**Common failure modes:**
- Engine tick is not running (launchd not configured). Clarifications won't happen automatically; `/gtd:clarify` won't see questions. **Fix**: ensure `launchd` has the daemon plist installed or run ticks manually via `python3 gtd/engine/cli.py tick`.
- Inbox item title contains `---` characters. Parser may misbehave. Keep titles free of triple-dashes.

---

### 2. `/gtd:clarify`

**What it does:** Interactive walk through Inbox items in chat. For each item, ask "what is this, what list does it belong in, any due date?" and move the reminder once clarified.

**Invocation:**
```
/gtd:clarify
```

**Args:** None.

**Behavior:**
- Reads all `kind: unclarified` items from the Inbox list.
- For each item, Claude asks the user interactively: "Is this a next action for @home? Due date?"
- User replies; Claude moves the reminder to the target list and rewrites metadata.
- Process continues until all inbox items are clarified.

### Claude's job for /gtd:clarify

When the user invokes `/gtd:clarify`, **you** are the interactive layer. The CLI doesn't prompt — you do.

The flow is identical to `/gtd:adopt` (see `### Claude's job for /gtd:adopt`) with one substitution: the source list is `Inbox` instead of a legacy list. The clarifier loop, escalation menu, and project-creation chain all apply the same way.

1. Read open Inbox items: `python3 gtd/engine/cli.py adopt --confirm-list Inbox` (the suggest path emits the same JSON-Lines format and works for any source list, including Inbox).
2. For each item, follow the per-item flow from the adopt runbook (steps 2–5 there): clarifier evaluate → branch on verdict → loop with cap=2 → escalation menu if needed.
3. Apply via the same Bash heredoc into `gtd adopt --apply` (no separate clarify-apply path; adopt-apply handles any source list including Inbox).

**What NOT to do:**
- Do NOT manually invoke `/gtd:clarify` while the engine is running (via launchd or background `tick` loop). The async path is: engine runs `tick` every 5 minutes, auto-clarifies what it can, and dispatches Q-reminders (on the iPhone) for ambiguous items. Clarify via the phone, then engine picks up answers. Manual `/gtd:clarify` in chat is for laptop-first workflows when you prefer sync chat interaction.

**Common failure modes:**
- Engine is running concurrently. Lock contention on `.gtd/engine.lock` causes timeouts. **Fix**: stop background engine (kill launchd daemon) before interactive `/gtd:clarify`.
- Inbox is empty. Command succeeds but takes no action. Not an error; safe.

---

### 3. `/gtd:next [--ctx X] [--time Nm] [--energy low|med|high]`

**What it does:** Return a ranked list of next actions, filtered by current context, time available, and energy level.

**Invocation:**
```
/gtd:next
/gtd:next --ctx @home
/gtd:next --ctx @home --time 30m
/gtd:next --ctx @errands --time 45m --energy low
```

**Args:**
- `--ctx X` — Filter by context list (e.g., `@home`, `@computer`, `@errands`, `@calls`, `@anywhere`). If omitted, infers from calendar + current time heuristic or asks via Q-reminder.
- `--time Nm` — Time available in minutes (e.g., `30m`, `60m`). If omitted, defaults to minutes until next calendar event. Used to drop items with longer estimated duration.
- `--energy low|med|high` — Energy level. If omitted, infers from time-of-day (early morning = low, midday = high, evening = medium). Filters items that exceed energy claim.

**Behavior:**
- Pulls all open next-actions from the specified context + `@anywhere` list.
- Drops items with estimated duration > time budget.
- Ranks by: (project has no other next action +3) + (overdue +5) + (manual priority) + (age × 0.1).
- Returns top 5 ranked items.

**What NOT to do:**
- Do not use `--ctx` with a non-existent context (e.g., `@shopping`). The engine only recognizes standard contexts: `@home`, `@computer`, `@errands`, `@calls`, `@anywhere`, `@agenda`. Misspellings silently return an empty list.

**Common failure modes:**
- No next actions exist in the chosen context. Returns an empty list. **Expected behavior**; not an error.
- Time or energy estimate is wrong. `/gtd:next` is a heuristic ranker, not a real planner. **Workaround**: adjust manually (skip an item, or refocus with tighter time/energy bounds).

---

### 4. `/gtd:project <name>`

**What it does:** Create a new project record. Prompts you for the outcome (the end-state goal), then creates a reminder in the `Projects` list with outcome stored in notes.

**Invocation:**
```
/gtd:project Complete IP agreement review
```

**Args:**
- `<name>` — Project name (required). Becomes the reminder title.

**Behavior:**
- Creates a reminder in the `Projects` list with title = `<name>`.
- Prompts: "What is the successful outcome for this project?"
- Stores outcome in notes under GTD metadata block.
- Writes a memory stub to `memory/projects/<slug>.md` for Claude context on future invocations.
- No next-actions created yet; you must use `/gtd:project-next` to add them.

### Claude's job for /gtd:project

When the user invokes `/gtd:project <name>` without an outcome, **you** elicit the outcome inline before calling the CLI.

1. Look at the chat context: did the user already say what they want this project to achieve? If yes, propose that as the outcome and ask the user to confirm.
2. If not, ask **one** question: `"What is the successful end-state for project <name>? (one line)"`.
3. Once the user provides an outcome, call: `python3 gtd/engine/cli.py project "<name>" --outcome "<outcome>"`. Always pass `--outcome` — don't rely on the CLI's interactive prompt; it's gated to TTY-only and won't fire from the slash-command shell.
4. Report the resulting `project_id` to the user.

**What NOT to do:**
- Do not create a project via `/gtd:project` and then manually add next-actions to other lists without linking them. The GTD invariant (every project has ≥1 open next action) is enforced by the engine. If you create a project but add no next-actions, the engine will dispatch a Q-reminder on next tick asking you to clarify.

**Common failure modes:**
- Project name already exists. Silently creates a duplicate. **Fix**: check the `Projects` list in Reminders and delete stale duplicates.

---

### 5. `/gtd:project-next <project>`

**What it does:** Guided next-action creation under a project. Enforces the GTD invariant: every project must have ≥1 open next action.

**Invocation:**
```
/gtd:project-next Complete IP agreement review
```

**Args:**
- `<project>` — Project name (required). Must match an existing project in the `Projects` list.

**Behavior:**
- Looks up the project by name.
- Prompts: "What is the next physical action?" (not a project, not a goal—one concrete step).
- Prompts: "Which context? @home, @computer, @errands, @calls, @anywhere?"
- Creates a reminder in the chosen context list.
- Links reminder to project via `gtd-project:<uuid>` in metadata.
- Confirms invariant is satisfied: project now has ≥1 next-action.

**What NOT to do:**
- Do not manually add next-actions without using this command. Manually created reminders won't carry the `gtd-project:<uuid>` link, and the engine won't know they're part of the project.

**Common failure modes:**
- Project name not found. Returns an error; check spelling and confirm the project exists via `/gtd:status`.
- User refuses to add a next-action after creating a project. The engine detects the invariant violation and dispatches a Q-reminder on next tick.

---

### 6. `/gtd:weekly-review`

**What it does:** Guided interactive review covering all GTD horizons (inboxes, waiting-for, projects, someday). Ideal on Friday afternoon; Sunday 10am backup nudge fires automatically via launchd.

**Invocation:**
```
/gtd:weekly-review
```

**Args:** None.

**Behavior:**
- Walks six buckets:
  1. **Collect loose papers** — any stray items? Capture them.
  2. **Inbox to zero** — invoke `/gtd:clarify` loop; process all unclarified items.
  3. **Last 7 days' calendar** — any follow-ups to capture?
  4. **Next 7 days' calendar** — any tickler items needed?
  5. **Review waiting-for** — nudge suggestions for delegated items.
  6. **Review projects** — enforce next-action invariant; any stalled projects (>14d) to revisit or close?
  7. **Review someday** — promote candidates or archive.
- Writes agenda to `memory/reviews/2026-04-17.md` (timestamped by review date).
- Creates a Q-reminder `Weekly Review — Fri Apr 17` on the iPhone with templated agenda.

### Claude's job for /gtd:weekly-review

The CLI side (`gtd weekly-review`) is mostly a snapshot writer. **You** drive the conversational walk through the six buckets:

1. Run `gtd status` to get the snapshot. Read it back to the user.
2. Walk the six buckets in order, one at a time. For each: read the relevant data (e.g., open Inbox count, stalled projects from `gtd status`, waiting-for items via `gtd waiting`), summarize, ask the user what to act on.
3. For Bucket 2 (Inbox to zero), drive `/gtd:clarify` — see `### Claude's job for /gtd:clarify`.
4. For Bucket 6 (stalled projects), if `gtd status` shows any, present each one and ask: revive (add next action) / close / leave. For "revive", drive `/gtd:project-next`.
5. After the walk, append a short summary to `memory/reviews/<today>.md` (you write the file directly; the engine doesn't yet).

**What NOT to do:**
- Do not run `/gtd:weekly-review` while the engine's automated review prep is running (Friday 15:00 launchd job). Lock contention. **Workaround**: wait 5 minutes or run on a different day.

**Common failure modes:**
- No calendar events found. Review still runs; you skip the calendar steps. Not an error.
- Waiting-for nudge count exceeds the daily cap (default 5). Suggestions batch into one digest Q instead of per-item Qs.

---

### 7. `/gtd:waiting [--nudge] [--per-item]`

**What it does:** List all delegated items (in the `Waiting For` list) with their delegate and date. Optionally draft nudge reminders for stale items (>7 days).

**Invocation:**
```
/gtd:waiting
/gtd:waiting --nudge
/gtd:waiting --nudge --per-item
```

**Args:**
- `--nudge` — Draft Q-reminders to nudge you about stale waiting items (age >7d). One per item or coalesced into a digest, depending on `--per-item`.
- `--per-item` — Dispatch one Q-reminder per waiting item (bounded by daily cap, default 5). If omitted with `--nudge`, coalesces into a single digest Q.

**Behavior:**
- Scans the `Waiting For` list.
- Returns: item title, delegate name, date delegated, days elapsed.
- If `--nudge`: identifies items older than 7 days and either (a) creates one digest Q ("You have 3 waiting items >7d old: …") or (b) creates one Q per item.
- Q-reminders include options: "Follow up", "Cancel", "Extend deadline".

**What NOT to do:**
- Do not add items to `Waiting For` manually without the GTD metadata (`delegate: <name>` in notes). The engine won't recognize them.

**Common failure modes:**
- No waiting items exist. Command returns an empty list. Expected; not an error.
- Q-reminder dispatch cap (5/day) is exceeded. Remaining items queue in state.db; a reminder digest is sent instead.

---

### 8. `/gtd:tickler <text> <date> [@ctx]`

**What it does:** Park an item in the `Tickler` list until a future date. On or after the release date, the engine moves it to the target context list and surfaces it as a next-action.

**Invocation:**
```
/gtd:tickler "Review annual insurance policy" 2026-06-01
/gtd:tickler "Call Michael about tax strategy" 2026-05-15 @calls
```

**Args:**
- `<text>` — Item title (required).
- `<date>` — Release date in YYYY-MM-DD format (required).
- `[@ctx]` — Target context list: `@home`, `@computer`, `@errands`, `@calls`, `@anywhere` (optional; defaults to `@anywhere`).

**Behavior:**
- Creates a reminder in the `Tickler` list with due date = release date at 09:00 local.
- Stores `target_ctx` in metadata.
- Engine tick: when today ≥ release date, moves reminder to target context list and rewrites `kind: next-action`.
- iOS Reminders.app sends notification at 09:00 on release date as failsafe.

**What NOT to do:**
- Do not set a release date in the past. The engine will immediately promote it. If you want a today-item, use `/gtd:capture` instead.
- Do not manually move tickler items; let the engine do it. Manual moves break the automation.

**Common failure modes:**
- Engine tick is not running. Tickler item won't surface after release date. **Fix**: ensure launchd daemon is active or run manual ticks.
- Date format is wrong. `/gtd:tickler` expects ISO YYYY-MM-DD; other formats fail silently or are misparsed. Always use `2026-05-15`, not `May 15` or `5/15/26`.

---

### 9. `/gtd:ask <question> [--ref rid]`

**What it does:** Manually drop a Q-reminder (question) into the `Questions` list on the iPhone. User replies in Reminders; engine consumes the answer on next tick.

**Invocation:**
```
/gtd:ask "Is this a @home or @computer task?"
/gtd:ask "Should I delegate the tax review or handle it myself?" --ref 7C2F2574-9E5E-...
```

**Args:**
- `<question>` — The question text (required).
- `--ref rid` — External ID of the source reminder, if this Q is tied to a specific item (optional). Helps the engine route the answer back to the right reminder.

**Behavior:**
- Creates a reminder in the `Questions` list with title = question.
- Sets due = today at 09:00 (notification on iPhone).
- User sees the Q on iPhone, replies in Notes under "Reply:", and marks complete.
- Engine's next tick: polls completed Qs, extracts reply, and applies the answer.
- Q-reminder is archived into `state.db.questions` history.

**What NOT to do:**
- Do not manually delete a Q-reminder if you don't want to answer it. Instead, mark it complete with "Reply: cancel" in notes. This tells the engine to skip the question without permanently losing the thread.

**Common failure modes:**
- User ignores the Q for >7 days (TTL expires). Engine treats it as declined and re-dispatches a gentler nudge (if the source item is still relevant).
- Multiple `Reply:` lines in notes (user edits the reply multiple times). Engine takes the last reply. Safe, but confusing.
- iCloud latency: Q sent to iPhone, but Mac reads before push. Idempotent handlers + engine lock prevent duplication.

---

### 10. `/gtd:status`

**What it does:** Read-only dashboard: counts per GTD list, open Q-reminders, stalled projects, last review date, daemon health.

**Invocation:**
```
/gtd:status
```

**Args:** None.

**Behavior:**
- Scans all GTD lists and state.db.
- Returns:
  - Count of items in each list (Inbox, @home, @computer, @errands, @calls, @anywhere, @agenda, Waiting For, Someday, Projects, Tickler, Questions).
  - Open Q-reminders (count + titles).
  - Stalled projects: >14d since last progress (warning).
  - Last review date and time.
  - Daemon health: is the engine tick running? When was the last successful tick?
- No mutations; purely read-only.

**What NOT to do:**
- Do not use `/gtd:status` to diagnose engine lock contention. Status does a read-only query and won't block on a write lock. Use `ls -l .gtd/engine.lock` to check if the lock is held.

**Common failure modes:**
- Engine has never run. Status shows all counts = 0, last review = never. Run `/gtd:capture` to create your first inbox item, then `python3 gtd/engine/cli.py tick` to initialize state.db.

---

### 11. `/gtd:adopt`

**What it does:** Agent-in-the-loop migration of legacy lists into GTD buckets. Three modes: discover legacy lists, get a per-item classification batch from the engine, then apply a user-confirmed batch of moves. Rules-based auto-clarify is **deliberately not used** here — adoption is a one-shot human act, mediated by Claude.

**Invocations:**
```
/gtd:adopt                              # discover legacy lists with counts
/gtd:adopt --confirm-list Personal      # suggest phase: emit items as JSON
/gtd:adopt --apply --from /tmp/plan.json   # apply phase: move per the plan
```

**Behavior — three phases:**

1. **Discover** (`gtd adopt`):
   - Returns the names + open-item counts for every Reminders list NOT in the GTD-managed set (`DEFAULT_MANAGED_LISTS` in `gtd/engine/write_fence.py`).
   - Read-only.

2. **Suggest** (`gtd adopt --confirm-list X`):
   - Validates X exists and is not already managed.
   - Emits one JSON object per open item to **stdout** (header to stderr): `{"rid": "...", "name": "...", "body": "...", "source_list": "X"}`.
   - No mutation.
   - Claude's job: read these items, classify each (suggest a `target_list` from the valid set printed in the header), present the full batch to the user in chat for confirmation/editing, then write a decisions plan.

3. **Apply** (`gtd adopt --apply [--from FILE]`):
   - Reads JSON Lines `{"rid": "...", "target_list": "@home"}` from FILE or stdin.
   - Validates every `target_list` is in the adoptable managed set (rejects up-front before any move).
   - For each row: calls `bin.lib.reminders.move_to_list(rid, target_list)`, then upserts a `state.db.items` row with `kind` derived from the target (`@*` → next_action; `Waiting For` → waiting_for; `Someday` → someday; `Tickler` → tickler; `Projects` → project).
   - Bypasses the v1 7-day `dispatch_dryrun` gate — this is explicit user-confirmed input, not auto-dispatch.
   - Honors `--dry-run` (global flag, before subcommand) for true preview.
   - Logs `op="adopt_apply"` to `engine.jsonl`.

**Valid `target_list` values** (the adoptable managed set):
`@home`, `@computer`, `@calls`, `@errands`, `@anywhere`, `@agenda`, `@nyc`, `@jax`, `@odita`, `Waiting For`, `Someday`, `Tickler`, `Projects`. Note: `Inbox` and `Questions` are deliberately excluded as targets.

### Claude's job for /gtd:adopt

When the user invokes `/gtd:adopt --confirm-list X`, **you** are the layered cognitive engine between the CLI's two phases. The CLI emits items; you walk the user through them **one at a time** with a **clarifier loop** for items that need it; you accumulate decisions; you pipe the final batch back via a Bash heredoc.

**MANDATORY interactive UI**: every per-item question goes through the `AskUserQuestion` tool — never plain-text "yes / skip / done" prompts. The user clicks chips; cadence stays fast. (Saved as feedback memory after a real walk.)

The clarifier runs **only after auto_clarify returns needs_user** (the layering contract — see `gtd/engine/clarifier.py` module docstring). For the adopt path you call it manually per item.

**Per-item flow (repeat for each item, in source order):**

1. **Suggest** (once, at the start of the walk) — run `python3 gtd/engine/cli.py adopt --confirm-list X`. **Capture stdout only.** Do NOT use `2>&1` or pipe stderr — the human-readable header on stderr will corrupt the apply input if it gets mixed in.
2. **For each JSON-Lines item, evaluate** with the clarifier first:
   ```bash
   python3 gtd/engine/cli.py clarifier evaluate "<item title>" --json
   ```
   Parse the result. It has `verdict` (ACCEPT | NEEDS_QUESTION), `failed_gate`, `reason`, `proposed_question`, `recommended_disposition`.

3. **Branch on verdict:**

   **(a) ACCEPT** — the item is clarified enough. Propose a `target_list` based on:
   - Action verbs (buy, sell, pay, fix, clean, file, call, email, schedule) → context list (`@errands`, `@home`, `@computer`, `@calls`)
   - Reference / informational / books / courses / hobbies → `Someday`
   - Delegated to a known person, or "make sure X happens" → `Waiting For`
   - Use `memory/people/` and `memory/projects/` for context when names appear.

   Show the user:
   ```
   N/TOTAL — "<item title>"
      ✓ ACCEPT  → <proposed_target>  (<short rationale>)
      yes / edit / skip ?
   ```

   **(b) NEEDS_QUESTION** — a gate failed. Show the user the gate name + reason + ask the canonical question. Round 1:
   ```
   N/TOTAL — "<item title>"
      ⚠ gate=<failed_gate> failed
      reason: <reason>
      question: <proposed_question>
      (or reply 'yes' to advance past this gate, or 'clear all' to accept as-is)
   ```
   Capture the user's reply. Interpret:
   - `yes` / `obvious` → advance ONE gate only (treat just this gate as PASS, re-evaluate from the next gate)
   - `clear all` (TWO words required — intentional friction) → skip remaining gates entirely, treat as ACCEPT
   - free-text answer → **append the answer to the item title in memory only** (do NOT modify Reminders during the loop), re-run `clarifier evaluate` on the combined string
   - `skip` / `quit` → see step 5

   Round 2: same as round 1 but using the post-answer evaluation. **Cap is 2 rounds total** (initial + 1 retry). After cap, go to step 4.

4. **Escalation menu** (only if hit the round cap without ACCEPT) — show the user 3 options. Default-highlight option (a) when G1 failed; option (b) when G2 or G3 failed:
   ```
   We've gone two rounds — pick one:
     (a) Send to Someday          [recommended for non-actionable]
     (b) Make this a Project (and define the first action together)
     (c) Skip — leave in source list
   ```
   - (a) → use `Someday` as the target_list for this item.
   - (b) → execute the **project-creation chain** (see step 4b below).
   - (c) → drop this item from the batch (no `apply` write for it).

   **Step 4b — project-creation chain (when user picks (b)):**
   1. Ask: `"What does 'done' look like for this project?"` (the outcome statement).
   2. Run `python3 gtd/engine/cli.py project "<original_item_title>" --outcome "<user_answer>"`. Capture the printed `project_id`.
   3. Ask **once** (single-shot, NO re-entry into the clarifier loop): `"What's the very next physical step for this project?"`. Capture the answer verbatim as the next-action title.
   4. Ask for context if not obvious: `"Which list — @home / @computer / @calls / @errands?"`.
   5. Run `python3 gtd/engine/cli.py project-next "<project_id>" "<ctx>" "<next_action_title>"`.
   6. Continue to the next item in the walk.

5. **Quit / abort handling** (if user types `quit` / `stop` / Ctrl-C mid-loop): pause the walk. Ask: `"Apply the N items confirmed so far, or discard everything?"`. If apply: jump to step 6 with what's been collected. If discard: stop, leave source list untouched.

6. **Apply confirmed decisions** — once every item has been walked (or the user halted with apply-so-far), summarize: `"Applying 18 decisions; 4 sent to Someday; 2 made into Projects; 0 skipped."`. Pipe the accumulated decisions to `gtd adopt --apply` via a single-quoted Bash heredoc:
   ```bash
   cat <<'EOF' | python3 gtd/engine/cli.py adopt --apply
   {"rid": "ABC-123", "target_list": "@computer"}
   {"rid": "DEF-456", "target_list": "@errands"}
   EOF
   ```
   The single-quoted `<<'EOF'` disables shell variable/command interpolation; safe for any title.

7. **Report** — surface the CLI's final `=== Adopt apply === moved=N errors=M` line. If `errors > 0`, also show any per-item `error:` lines from stderr.

**What NOT to do:**
- Do not present a markdown batch table for blanket approval. The user prefers item-by-item walks even for 20+ items.
- Do not skip the clarifier evaluate step for items that look obvious — the clarifier's reasoning is part of what makes the experience feel intentional.
- Do not re-enter the full clarifier loop inside the project-creation chain (step 4b.3 is single-shot — preserves the bound on user touches).
- Do not modify Reminders during the loop — only at the final apply / project create.
- Do not pipe stderr (`2>&1`) when capturing the suggest output.
- Do not write a tempfile. Use the heredoc-stdin pattern.
- Do not run apply without first running suggest in the same session — you need the canonical rids.

**Common failure modes:**
- User edits suggested targets in chat with typos like `@homw`. Validate before writing the plan; correct or ask.
- A reminder gets deleted between suggest and apply (user touches their phone). `move_to_list` errors; the per-item failure is logged but the rest of the batch proceeds.

---

### 12. `/gtd:dryrun-report [--days N] [--json]`

**What it does:** Inspect the Q-channel activity log (7 days or `--days N`) and emit a verdict: "VERDICT: READY TO FLIP" or "DO NOT FLIP". Used to validate that `dispatch_dryrun` is safe to disable in production.

**Invocation:**
```
/gtd:dryrun-report
/gtd:dryrun-report --days 14
/gtd:dryrun-report --days 7 --json
```

**Args:**
- `--days N` — Look back N days in `qchannel.jsonl`. Default: 7.
- `--json` — Output as JSON (machine-readable) instead of markdown (human-readable).

**Behavior:**
- Reads `.gtd/log/qchannel.jsonl` (Q-channel event log).
- Analyzes: cap breach counts, invariant violations, low auto-clarify rate (<70%), engine tick errors.
- If any red flags: **"DO NOT FLIP"** (don't disable dryrun mode in settings).
- If healthy: **"VERDICT: READY TO FLIP"** (safe to move dryrun=false to production).
- Output includes a detailed breakdown (JSON or markdown).

**What NOT to do:**
- Do not disable `dispatch_dryrun` in settings.json until `/gtd:dryrun-report` confirms readiness. Disabling dryrun without validation can cause Q-spam if cap logic has bugs.

**Common failure modes:**
- No `qchannel.jsonl` exists. Engine has never run the Q-channel. Report shows "no data; cannot assess". Expected on first deployment; run the engine for 7 days before flipping dryrun off.

---

### 13. `/gtd:health`

**What it does:** Weekly digest (Sunday 18:00). Fires automatically via launchd; silent when system is green. Q-reminders the user if cap breach, invariant failure, low auto-clarify rate, or tick errors detected in the past week.

**Invocation:**
```
/gtd:health
```

**Args:** None. (Normally fires automatically; you can invoke manually for testing.)

**Behavior:**
- Scans the past 7 days of logs: `.gtd/log/{engine,qchannel,clarify,invariants}.jsonl`.
- Checks:
  - Q-cap breaches (>5 Qs/day on average)?
  - Invariant violations (projects with zero next-actions)?
  - Auto-clarify rate < 70%?
  - Tick errors or lock timeouts?
- If clean: silent (no reminder sent).
- If issues detected: creates a Q-reminder `GTD Health Warning — <issue>` with actionable guidance (e.g., "Run `/gtd:weekly-review` to fix stalled projects").
- Writes a health report to `memory/reviews/health-<date>.md`.

**What NOT to do:**
- Do not ignore health warnings for >7 days. They're early signals of workflow breakdown. Act on them promptly.

**Common failure modes:**
- Logs rotate and older data is archived. Health report only sees recent logs. Not an error; expected for long-running systems.

---

## State Files

All GTD state lives in `.gtd/` at the repo root:

| File | Format | Owned by | Read by |
|---|---|---|---|
| `.gtd/state.db` | SQLite | engine only | engine, bin/sync.py, skill |
| `.gtd/engine.lock` | POSIX lock file | lock holder | all daemons (Supernote, engine, sync) |
| `.gtd/log/engine.jsonl` | JSON Lines | engine | health, dryrun-report |
| `.gtd/log/qchannel.jsonl` | JSON Lines | engine | dryrun-report, health |
| `.gtd/log/clarify.jsonl` | JSON Lines | engine | — |
| `.gtd/log/invariants.jsonl` | JSON Lines | engine | health |
| `memory/reviews/YYYY-MM-DD-*.md` | Markdown | engine | skill, user |
| `memory/projects/<slug>.md` | Markdown | engine (optional) | skill |

**Schema snapshot** (state.db):
- `schema_version` — versioning for migrations.
- `items` — all task reminders with GTD metadata (id, kind, list, project, context, created, last_seen).
- `questions` — Q-reminder history (qid, kind, ref_rid, dispatched_at, ttl_at, status, payload).
- `projects` — project metadata (project_id, outcome, created, last_review).
- `ticklers` — parking reminders (gtd_id, release_at, target_list, created).
- `reviews` — review snapshots (review_id, kind, started_at, completed_at, snapshot_json).
- `events` — audit log (ts, stream, payload_json).

---

## Coexistence with bin/sync.py

The GTD engine and `bin/sync.py` (existing Reminders ↔ TASKS.md sync) coexist without conflict loops because the engine writes a metadata fence (`--- gtd ---`) inside reminder notes. When `sync.py` reads a reminder to compute its SHA1 hash for comparison, it **strips the fence before hashing**. Result: metadata changes don't trigger false "reminder changed on both sides" conflicts. This is implemented in `bin/lib/reminders.py` and `gtd/engine/write_fence.py`.

**Do not bypass this fence.** If you manually edit a reminder's notes without respecting the fence, sync.py and the engine can diverge.

---

## What NOT to Do

1. **Never write metadata to reminders in legacy lists** — only engine-managed GTD lists (`Inbox`, `@*`, `Waiting For`, `Someday`, `Projects`, `Tickler`, `Questions`). Writing metadata to `Personal` or `Books to Read` breaks the orphan-detection heuristics and may confuse the sync layer. Enforce via `write_fence.check_list_scope()`.

2. **Never bypass `dispatch_dryrun` in the first 7 days of operation** — dryrun mode (default `true` in settings.json) logs all Q-dispatches without actually creating reminders. Disabling it prematurely risks Q-spam if cap logic has bugs. Wait for `/gtd:dryrun-report` to give "READY TO FLIP".

3. **Never modify `.sync-state.json` or `.gtd/state.db` by hand** — both are written atomically by their respective daemons. Hand-editing can corrupt state or cause sync loops. If you suspect corruption, delete and re-initialize: `rm .gtd/state.db && python3 gtd/engine/cli.py tick`.

4. **Never leave a malformed Q-reminder unanswered** — if a Q seems wrong or spam, mark it complete with reply "cancel" to abort. Leaving it open causes re-dispatch logic to trigger, potentially creating a loop.

---

## Examples

### Example 1: Capture and Clarify a Multi-Item Batch

```
User: /gtd:capture
  Call Dan about the lease
  Review the IP agreement
  Buy milk

Claude runs the command. Three reminders created in Inbox.

Next engine tick (5 minutes):
- "Call Dan..." → auto-clarifies to @calls (keyword "Call").
- "Review the IP..." → >12 words + "review" (verb), flagged as likely project. Dispatches Q: "Is 'Review the IP agreement' a project, or a single next action?"
- "Buy milk" → auto-clarifies to @errands (keyword "Buy").

User sees Q on iPhone, replies "Single action", marks complete.

Next tick: Engine reads the reply, moves "Review the IP..." to @computer.
Result: all three items clarified, in their respective lists, ready for `/gtd:next`.
```

### Example 2: Create a Project and Its First Next Action

```
User: /gtd:project Complete IP agreement review

Claude: "What is the successful outcome?"
User: "Signed IP agreement in my records and reviewed for risks."

Claude creates reminder in Projects list. Title: "Complete IP agreement review". Outcome stored in notes.

User: /gtd:project-next Complete IP agreement review

Claude: "What is the next physical action?"
User: "Schedule a call with the legal team to walk through the agreement."

Claude: "Which context?"
User: "@calls"

Claude creates a reminder in @calls list, links it to the project via metadata. Confirms: project now has 1 next action. ✓ Invariant satisfied.

User: /gtd:next --ctx @calls

Returns: "Schedule a call with the legal team to walk through the agreement" (top of @calls, tied to "Complete IP agreement review" project).
```

### Example 3: Waiting For and Nudge

```
User: Delegates "Follow up on property tax appeal" to "Michael".

(Manual: user adds to Waiting For, notes = "delegate: Michael, delegated_date: 2026-04-19")

After 7 days:

User: /gtd:waiting --nudge

Claude: "1 waiting item >7d old. Michael: Follow up on property tax appeal. Dispatching nudge."

Engine creates Q on iPhone: "Michael: Follow up on property tax appeal" (7d old). User replies:
- "Follow up: Still waiting" → remains in Waiting For, due date extended.
- "Cancel" → moved to trash.
- "Completed: Michael called back" → moved to Someday or Inbox, and marked "done".

Engine reads reply on next tick and applies disposition.
```

### Example 4: Context-Aware Next Actions

```
User: /gtd:next --ctx @home --time 30m --energy low

Claude filters:
1. Open next-actions in @home + @anywhere.
2. Drop items with est-duration > 30m.
3. Drop items exceeding low-energy claim (e.g., "Rebuild the garage" = too hard).
4. Rank by: (project has no other action) + (overdue) + (manual priority) + (age).

Returns top 5, e.g.:
1. Glue bath soap holder (5 min, low-energy, no project)
2. Clean the car (20 min, low-energy, standalone)
3. Organize desk cables (25 min, low-energy, no project)
4. Fix the faulty AirTag (15 min, low-energy, no project)
5. Order rubber gloves (2 min, low-energy, no project)

User picks #1, does it in 5 minutes, marks complete in Reminders. Next tick, engine refreshes /gtd:next.
```

---

## Success Criteria

You've set up the GTD skill correctly when:

1. **Inbox captures work**: `/gtd:capture "Buy milk"` → reminder appears in Reminders.app Inbox within 10 seconds.
2. **Auto-clarify works**: After the engine tick (every 5 min), "Buy milk" moves to @errands automatically.
3. **Q-channel works**: Ambiguous items dispatch Q-reminders to iPhone; user replies in Reminders.app; engine consumes the answer and moves the item.
4. **Projects enforce invariant**: If you create a project with `/gtd:project` but add no next-actions, the engine dispatches a Q-reminder asking you to clarify.
5. **Next-actions rank sensibly**: `/gtd:next --ctx @home --time 30m` returns items you can plausibly do in 30 minutes at home, in priority order.
6. **Weekly review archives context**: `/gtd:weekly-review` writes a report to `memory/reviews/2026-04-19.md` summarizing inboxes, projects, waiting-for, and someday items.

---

**Last updated**: 2026-04-19. Skill targets GTD engine v1.0+ at `gtd/engine/cli.py`.
