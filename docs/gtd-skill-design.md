# GTD-on-Apple-Reminders — Design Doc

**Target:** single-user (Eddy), iPhone-primary, Mac-as-daemon, all local. A Cowork `gtd:*` skill that layers on top of the existing `reminder_claude_sync` and Supernote sync.

---

## 1. System architecture

```
 ┌─────────────────────────────────────────────────────────────────────────┐
 │                              iPhone                                      │
 │  Reminders.app (Siri / Share-sheet / widget / notifications)             │
 │   ├─ Inbox, @calls, @computer, @errands, @home, @agenda/*,               │
 │   ├─ Waiting For, Someday, Projects, Tickler, Questions                  │
 │   └─ Built-in Scheduled / Today / Flagged smart views                    │
 └───────────────┬─────────────────────────────────────────────────────────┘
                 │ iCloud (CalDAV-like sync, 5–60 s latency)
 ┌───────────────▼─────────────────────────────────────────────────────────┐
 │                               Mac                                        │
 │                                                                          │
 │   ┌──────────── EventKit plane ────────────┐                             │
 │   │ bin/reminders-cli  (Swift, read/write) │ ← existing                  │
 │   │ applescripts/update.applescript (due,  │                             │
 │   │ move — the 2 ops CLI lacks)            │                             │
 │   └──────────────────┬────────────────────┘                             │
 │                      │                                                   │
 │   ┌──────────────────▼────────────────────┐                             │
 │   │ bin/lib/reminders.py — bridge          │ ← existing                  │
 │   └──────────────────┬────────────────────┘                             │
 │                      │                                                   │
 │   ┌──────────────────▼────────────────────┐   ┌──────────────────────┐  │
 │   │  gtd/engine  (NEW, Python)             │   │ Supernote sync       │  │
 │   │   ├─ capture.py  (inbox writers)       │   │ (runs every 15 min   │  │
 │   │   ├─ clarify.py  (inbox state machine) │   │  via launchd)        │  │
 │   │   ├─ qchannel.py (Q-reminder protocol) │   │ sync_state.db        │  │
 │   │   ├─ tickler.py  (date-triggered move) │   │ category_map.json    │  │
 │   │   ├─ projects.py (outcome, next-act)   │   │ → writes Reminders   │  │
 │   │   ├─ review.py   (weekly review prep)  │   │                      │  │
 │   │   ├─ engage.py   (/gtd:next selector)  │   │                      │  │
 │   │   └─ state.py    (SQLite)              │   └──────────────────────┘  │
 │   └──────────────────┬────────────────────┘                             │
 │                      │                                                   │
 │   ┌──────────────────▼────────────────────┐                             │
 │   │ .gtd/state.db (SQLite, single file)   │                              │
 │   │  items, questions, projects, ticklers,│                              │
 │   │  reviews, events                      │                              │
 │   └───────────────────────────────────────┘                             │
 │                                                                          │
 │   ┌───────────────────────────────────────┐                             │
 │   │  bin/sync.py  (existing md↔reminders) │                             │
 │   │   TASKS.md ←→ Reminders (sha1 state)   │                             │
 │   └──────────────────┬────────────────────┘                             │
 │                      ▼                                                   │
 │                TASKS.md (projected read-only view) + memory/             │
 │                                                                          │
 │   ┌───────────────────────────────────────┐                             │
 │   │  Claude Code skill (markdown+scripts) │                             │
 │   │  /gtd:capture, /gtd:clarify, …        │                             │
 │   │  reads memory/, writes via gtd/engine │                             │
 │   └───────────────────────────────────────┘                             │
 └─────────────────────────────────────────────────────────────────────────┘
```

**State ownership:**
- **Reminders (iCloud)** — authoritative truth for task identity, title, due, list, completion.
- **`.gtd/state.db`** (new SQLite at `/Users/ehsanfathi/Documents/repos/todo/.gtd/state.db`) — GTD metadata Reminders can't hold: project↔next-action links, question threads, tickler release dates, review snapshots, heuristics. Engine-only writer.
- **`.sync-state.json`** — unchanged; owned by `bin/sync.py`.
- **`sync_state.db`** — unchanged; owned by Supernote sync.
- **`TASKS.md`** — derived view (see §10).
- **`memory/`** — read by engine, never written (except `memory/reviews/`).

Three daemons, serialized by a POSIX file lock `.gtd/engine.lock`:
1. `reminder_claude_sync` — existing, every 5 min.
2. Supernote sync — existing, every 15 min.
3. `gtd-engine tick` — new, every 5 min offset by +2 min.

---

## 2. Apple Reminders list layout

| List | Purpose | Written by |
|---|---|---|
| `Inbox` | Capture. Siri/share-sheet default. | user, Supernote, engine |
| `@calls` | Next actions requiring a phone | engine |
| `@computer` | Next actions at a computer | engine |
| `@errands` | Next actions out in the world | engine |
| `@home` | Next actions at the apartment | engine |
| `@anywhere` | No context constraint | engine |
| `@agenda` | Things to raise with people (person in title prefix: `Dan: `, `Michael: `) | engine |
| `Waiting For` | Delegated, date-stamped | engine |
| `Someday` | Incubator | engine |
| `Projects` | One reminder per project. Outcome in notes. | engine |
| `Tickler` | Future-dated items parked until their day | engine |
| `Questions` | The Q-channel from skill → user (§5) | engine |
| `Reference` | Non-actionable keepers (rare) | engine |

Legacy lists (`Reminders`, `Books to Read`, `Personal`, etc.) stay untouched until `/gtd:adopt` is run explicitly.

### Encoding projects and context

- **Contexts as lists** (not tags — iOS has no user-surfaced tag UX in Siri quick-add).
- **Projects represented twice**: one reminder in `Projects` (title = project name, notes = outcome + `gtd-project-id:<uuid>`); next-actions live in `@context` lists carrying `gtd-project:<uuid>` in notes. State.db maps `project_id → [action_rid, …]` for O(1) lookup.
- **Outcome** lives in the project reminder's notes; mirrored to `memory/projects/<slug>.md` for Claude context.
- **Next-action invariant**: every row in `projects` must have ≥1 open child in a `@context` list. Weekly review dispatches a Q if violated.

### Notes schema (all lists)

All engine-managed reminders carry this metadata block at the top of `notes`:

```
--- gtd ---
id: 01H8WZ3...   (ULID; stable across renames)
kind: next-action | project | waiting | tickler | question | someday
project: 01H8...     (optional)
created: 2026-04-19T14:03-04:00
ctx: @home          (optional)
delegate: Dan       (only on waiting)
release: 2026-05-01 (only on tickler)
--- end ---
<user-visible notes>
```

---

## 3. Capture flow

All capture funnels to `Inbox`. Never skip it.

| Source | Mechanism |
|---|---|
| iPhone Siri "Remind me to X" | Default list → `Inbox` in iOS Settings → Reminders |
| iPhone share-sheet / widget | Same default-list trick |
| Supernote | `category_map.json`: `{"apple": "Inbox", "supernote": "Inbox"}` |
| Claude chat: `/gtd:capture <text>` | Skill invokes `gtd/engine capture "<text>"` |
| Email triage | v3+: `mail2inbox.py` reads a Gmail label via MCP |
| Scratch | `/gtd:capture` with no args opens a multiline prompt; each line = one inbox item |

Everything lands with `kind: unclarified` + `clarified=0` in state.db.

---

## 4. Clarify flow — asynchronous state machine

Phone-first + skill-not-always-live requires async clarify.

```
NEW → CLAUDE_ASSESSING → AUTO_CLARIFIED | NEEDS_USER
NEEDS_USER → Q_DISPATCHED → Q_ANSWERED → CLAUDE_APPLYING → DONE
                          ↘ Q_EXPIRED   → NEEDS_USER (re-dispatch with backoff)
```

**Each engine tick:**

1. **Scan inbox.** Items with `kind: unclarified` → create `NEW` rows.
2. **Auto-clarify pass** (local rules, no LLM):
   - "Call ", "Text ", "Email ", "Ping " → `@calls` / `@computer`.
   - Verb-whitelist (buy, pick up, return, mail, drop off) → `@errands`.
   - Home-keywords (fix, glue, wash, organize) → `@home`.
   - Known person from `memory/people/` → `@agenda` with that person.
   - Date tokens ("by Friday") → parse → due date.
   - >12 words OR contains "and"/"then"/"also" → suspected project → NEEDS_USER.
   - None of the above → NEEDS_USER.
3. **AUTO_CLARIFIED** → move reminder to target list, rewrite metadata. DONE.
4. **NEEDS_USER** → dispatch Q-reminder (§5).
5. **Consume answers.** Scan `Questions` list for completed reminders, parse reply, apply dispatcher.

---

## 5. Reminders-as-Q&A protocol

**This is the novel piece.**

### Reminder shape

```
Title:  What's the next action for "IP agreement review"?
List:   Questions
Due:    2026-04-20 09:00 (notification)
Priority: medium
Notes:
--- gtd ---
id: 01HXQ...
kind: question
qkind: next-action       (clarify | next-action | project-outcome | waiting-nudge | context-check)
ref: 7C2F2574-9E5E-...   (source reminder external ID)
dispatched_at: 2026-04-19T14:03-04:00
expires_at: 2026-04-22T14:03-04:00
options: skip | trash | someday | 2min | free-text
--- end ---
Reply below, then mark this reminder complete.

Reply:
```

User types under `Reply:` in Notes, taps the circle. Next tick:

1. `qchannel.poll()` filters `list='Questions' && completed && id in state.questions{Q_DISPATCHED}`.
2. Extract reply (after last `Reply:` line). Empty is valid for some qkinds (`waiting-nudge` = still waiting).
3. Dispatch to `handle_<qkind>(answer, ref_rid, meta)`. Pure function, returns action list, engine applies atomically.
4. Archive: delete Q reminder, persist thread to `state.db.questions`.

### Failure modes

| Failure | Detection | Behaviour |
|---|---|---|
| Empty reply | Blank after `Reply:` | Per qkind; clarify→re-dispatch coarser; else reopen Q |
| Edited, not completed | `lastModified > dispatched_at` + not completed >24h | Gentler nudge Q |
| User deletes Q | `id` missing | Mark `Q_CANCELLED`; 7-day cooldown on same ref |
| Multi-answer thread | Multiple `Reply:` lines | Always take the last |
| Race with sync | Mid-write | `.gtd/engine.lock` blocks; 60s wait |
| iCloud latency | Mac reads before push | Idempotent handlers; duplicate tick = no-op |

### Drowning prevention

- **Cap**: ≤3 open Q-reminders at a time; overflow queues in state.db.
- **Coalesce**: if open Q references same source, append instead of new.
- **Quiet hours**: no Qs 22:00–08:00.
- **Priority**: waiting-nudge < clarify < project-outcome < next-action-missing.
- **Weekly budget**: ≤20 Qs/week; above → batch digest reminder.

---

## 6. Tickler & scheduled surfacing

**Chosen: dedicated `Tickler` list + engine-managed release date.**

Why not petioptrv's `#deferred` tag in title? Pollutes title, relies on Siri Shortcuts. Engine on Mac is the right place.

Tickler reminder:
- List: `Tickler`
- Due date: release date at 09:00 local (iOS notifies anyway if engine fails)
- Notes metadata: `release:`, `target_ctx:`, `original_project:`

Engine tick (`tickler.py`):
1. Query `Tickler` list.
2. For each item with `release ≤ today`: move to `target_ctx` list, rewrite metadata (`kind: next-action`), clear release-only due date.
3. Log promotion to `state.db.events`.

43-folders simulation: helper commands `/gtd:tickler add "<text>" 2026-06-01 @errands`, `/gtd:tickler this-weekend "<text>"`.

---

## 7. Weekly Review

**Trigger:** Friday 15:00 launchd `gtd-engine review --prepare`. Also `/gtd:weekly-review` on-demand.

### Passive "prep" flow

1. Snapshot all lists into `state.db.reviews` under `review_id = YYYY-MM-DD`.
2. Compute: stalled projects (>14d), stale waiting-for (>7d), overdue scheduled, someday candidates, inbox count.
3. Write one Q-reminder `Weekly Review — Friday Apr 17` with templated agenda.
4. Write agenda to `memory/reviews/2026-04-17.md` (only file engine writes in memory/).

### Guided flow (Claude chatting)

`/gtd:weekly-review` walks six buckets (Hamberg's six horizons compressed):
1. Collect loose papers/inboxes.
2. Inbox to zero (invokes `/gtd:clarify` loop).
3. Review last 7 days' calendar → follow-ups to inbox.
4. Review next 7 days' calendar → tickler items if needed.
5. Review waiting-for → nudge suggestions.
6. Review projects → enforce next-action invariant.
7. Review someday → promote or trash.

Output archived to `memory/reviews/<date>.md`.

---

## 8. Engage — `/gtd:next`

Four filters: context → time → energy → priority.

**Knowing current context:**
- **Primary**: user-maintained reminder in `.gtd-state` list titled `current_context` with body `@home / @computer / @errands / @calls / @anywhere`. Siri-editable.
- **Fallback**: weekday 09:00–18:00 + WFH calendar = `@computer` + `@home`; errand block = `@errands`; else ask via Q-reminder.

**Time & energy:**
- User: `/gtd:next --time 20m --energy low`.
- Default: time = minutes until next calendar event; energy = time-of-day heuristic.

**Selection algorithm:**
1. Pull open `@<current-context>` + `@anywhere`.
2. Drop items with est-duration > time budget.
3. Drop items exceeding energy claim.
4. Rank: (project has no other next action +3) + (overdue +5) + (manual priority) + (age × 0.1).
5. Return top 5.

---

## 9. Integration with Supernote sync

**Verdict: peer daemon, not subordinate.** It's working; don't touch its invariants.

Changes:

1. **`category_map.json`** — add GTD lists. Existing Supernote categories keep their mapping. New entries for `Inbox`, `@calls`, `@computer`, `@errands`, `@home`, `@agenda`, `Waiting For`, `Someday`, `Projects`, `Tickler`. **Exclude `Questions` and `.gtd-state`** via `"exclude_lists": ["Questions", ".gtd-state"]` in settings.json.
2. **Collision avoidance**: both daemons honor `.gtd/engine.lock`. Staggered schedules (Supernote :00/:15/:30/:45, GTD :05/:10/… offset).
3. **Metadata preservation**: 2KB notes budget per reminder.
4. **Content-hash harmony**: Supernote's `sync_engine.py` gets a 2-line predicate: "if only `--- gtd ---` block differs, don't bounce back unless Supernote side also modified".

---

## 10. TASKS.md and memory integration

**Canonical source of truth: Reminders.** TASKS.md becomes a generated view.

GTD engine emits two new rendered artifacts in `.gtd/views/`:
- `active.md` — grouped by GTD list, project tags rendered as `[Proj: IP agreement]`.
- `projects.md` — one block per project: outcome, open next actions, waiting items.

Skill consults these for "what am I working on" — they encode GTD shape.

**TASKS.md is NOT deprecated**: stays as friendly editable surface for legacy sync. `/gtd:*` writes through engine, not TASKS.md.

**`memory/` read paths** (engine-read-only):
- `memory/people/*.md` → `@agenda` routing, delegate matching.
- `memory/projects/*.md` → project outcome enrichment, orphan detection.
- `memory/glossary.md` → disambiguate shorthand ("JB" → Justin Bailey).
- `CLAUDE.md` → overall context.

Engine writes only `memory/reviews/YYYY-MM-DD.md` and (behind `--mirror-projects`) project stubs.

---

## 11. Skill surface

| Command | Invocation | Behaviour |
|---|---|---|
| `/gtd:capture <text>` | user | Creates reminder(s) in Inbox. One per line. |
| `/gtd:clarify` | user | Interactive inbox walk in chat (sync path, not async Q). |
| `/gtd:next [--ctx X --time Nm --energy low]` | user | §8 engage. |
| `/gtd:project <name>` | user | Creates project record + outcome prompt + memory stub. |
| `/gtd:project-next <project>` | user | Guided next-action with invariant enforcement. |
| `/gtd:weekly-review` | user | §7 guided flow. |
| `/gtd:review-inbox` | user | Read-only inbox + proposed dispositions. |
| `/gtd:waiting [--nudge]` | user | Lists waiting-for; drafts nudges via memory. |
| `/gtd:tickler <text> <date> [@ctx]` | user | §6. |
| `/gtd:ask <question> [--ref rid]` | user | Manually drop a Q-reminder. |
| `/gtd:status` | user | Dashboard: counts, open Qs, stalled projects, last review. |
| `/gtd:adopt` | user (one-time) | Migrates legacy lists via Q per item. |
| `gtd-engine tick` | launchd every 5 min | Clarify + Q-poll + tickler. |
| `gtd-engine review --prepare` | Fri 15:00 | Weekly prep. |
| `gtd-engine nudge` | daily 09:00 | Waiting-for nudge Qs. |

---

## 12. Phased implementation

### v0 — Q-channel spike (2–3 evenings)
- Build: minimal `qchannel.py` (dispatch, poll, archive), state.db `questions` schema, one hardcoded `clarify` qkind. Test script drops a reminder, user answers on iPhone, script prints parsed reply.
- Validate: round-trip < 2 min. Parsing works for empty/single/multi-line.

### v1 — Capture + Clarify + Next (1 week)
- Build: `capture.py`, `clarify.py` state machine + auto-clarify heuristics, all GTD lists created, `engage.py`. File lock with Supernote. Commands `/gtd:capture /gtd:clarify /gtd:next`.
- Weekly review manual (checklist render only).
- Validate: one week capturing from iPhone, engine clearing inbox each tick, `/gtd:next` returns sensible list. Zero duplicates.

### v2 — Tickler + Projects-with-invariant + nudges (1 week)
- Build: `tickler.py`, `projects.py` invariant enforcement, `waiting.py` nudge generation, quiet hours, drowning caps.
- Commands: `/gtd:project /gtd:project-next /gtd:tickler /gtd:waiting /gtd:ask`.
- Validate: create a project with 3 actions, complete the last, verify invariant-Q dispatches next tick. Tickler parked +3 days actually surfaces.

### v3 — Proactive daemon + smart clarify + calendar (1–2 weeks)
- Build: automated weekly review prep, calendar MCP hooks, trigger lists in `memory/triggers/`, memory-aware auto-clarify, optional email→inbox Gmail MCP.
- Validate: full week without manual `/gtd:clarify` — engine handles >70% auto, rest via phone Qs that actually get answered.

Each phase smoke test: "from iPhone in airplane mode, capture, land, sync, observe disposition."

---

## 13. Risks & open questions

- **EventKit notes limit**: ~KB before UI lags. Metadata budget <500B. Enforced in qchannel.
- **iCloud latency**: 5–60s typical, worst ~10 min. Acceptable for async GTD; documented.
- **Offline iPhone**: captures batch on reconnect; engine handles burst.
- **Permissions**: Terminal/Python/launchd each need Reminders + FDA. Same as existing syncs.
- **Recurring reminders**: `reminders-cli` no RRULE; GTD rarely needs it. Hard-punt: engine never moves recurring items.
- **Subtasks** (petioptrv's project-decomposition): `reminders-cli` can't read. Skip; use list-of-reminders + notes pointer.
- **Location/geofence**: pass through; engine ignores.
- **Privacy**: nothing leaves Mac except iCloud (where data already lives).
- **Open Q: two-context actions** (e.g., @home AND @computer)? Pick the harder one (usually @home). Don't split.
- **Open Q: Supernote bulk import Q-spam**? Heuristic: >10 new Inbox items in 60s → skip auto-clarify, batch "I see 14 new items — run /gtd:clarify?" single Q.

---

## 14. Build vs fork vs hybrid

**Build fresh; steal patterns from my-gtd-buddy.**

Rationale:
- my-gtd-buddy is AppleScript-heavy with its own orchestrator. Forking = ripping out AppleScript to plug into our `reminders-cli` bridge + our sync state = ≥60% rewrite.
- You have two working sync loops (Supernote + md↔reminders) with SHA1-hash loop-prevention. GTD engine must integrate with these, not reintroduce a competing sync.
- Q-channel is novel; no analogue in my-gtd-buddy. Has to be built regardless.
- Patterns to steal: list layout, mode-based orchestrator (single "router" command dispatching to modes — cleaner than N commands), AppleScript snippets as references, system-health checks as `/gtd:status` template.

Fresh `gtd/` module sibling to `bin/` composes cleanly with both syncs, reads existing `memory/`.

---

## Decisions I'd recommend (pick-one summary)

- **List layout**: dedicated `@context` lists + `Inbox`, `Waiting For`, `Someday`, `Projects`, `Tickler`, `Questions`. Legacy untouched until `/gtd:adopt`.
- **Projects**: one `Projects` reminder per project (outcome + ULID), children in context lists reference via `gtd-project:<ulid>` in metadata.
- **Q-channel list**: `Questions`. Excluded from Supernote sync.
- **GTD metadata store**: new SQLite at `.gtd/state.db`.
- **Daemons**: three peer, file-locked, staggered. GTD every 5 min at :02/:07/…
- **Tickler**: dedicated list + engine promotion. No #deferred tag.
- **TASKS.md**: stays as flat view; engine emits `.gtd/views/*.md` additional renders. Reminders authoritative.
- **Memory**: engine reads `people/`, `projects/`, `glossary.md`; writes only `memory/reviews/*.md`.
- **Weekly review**: Friday 15:00 automated prep → Q-reminder agenda; guided `/gtd:weekly-review` on laptop.
- **Engage context source**: user-maintained `current_context` reminder, fallback inference.
- **Build strategy**: fresh `gtd/` module; pattern-lift from my-gtd-buddy, don't fork.
- **v0 deliverable**: end-to-end Q-channel round-trip with one hardcoded question kind. Everything else waits.
