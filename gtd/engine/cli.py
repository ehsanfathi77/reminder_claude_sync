"""
cli.py — unified GTD engine CLI.

Subcommands (16 total — 13 user-facing + 3 internal):
  capture, clarify, next, project, project-next, weekly-review, waiting,
  tickler, ask, status, adopt, dryrun-report, health     [13 user-facing]
  tick, init, bootstrap                                  [3 internal/setup]

Every subcommand:
  - Acquires .gtd/engine.lock via gtd.engine.lock.acquire(holder_argv0='gtd-engine')
  - Reads dispatch_dryrun from .gtd/config.json (default True; flips to False
    after /gtd:dryrun-report green verdict). For v1 first 7 days, ALWAYS True
    even if config says False — see config.flip_at_iso gate.
  - Honors --dry-run (overrides any write to be a no-op + log only)
  - Honors --verbose (-v) for per-step logging to stderr
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
# Ensure ROOT is on sys.path so `gtd.*` imports work when run as a script.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
STATE_DB = ROOT / ".gtd/state.db"
LOCK_PATH = ROOT / ".gtd/engine.lock"
LOG_DIR = ROOT / ".gtd/log"
CONFIG_PATH = ROOT / ".gtd/config.json"
MEMORY_DIR = ROOT / "memory"

DEFAULT_CONFIG: dict = {
    "dispatch_dryrun": True,
    "flip_at_iso": None,
    "managed_lists": None,
    "quiet_hours": [22, 8],
    "q_max_open": 3,
    "q_max_per_day": 8,
}


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if CONFIG_PATH.exists():
        return {**DEFAULT_CONFIG, **json.loads(CONFIG_PATH.read_text())}
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, sort_keys=True))


def effective_dryrun(cfg: dict) -> bool:
    """Return True if dryrun mode is active (either config or 7-day gate)."""
    if cfg.get("dispatch_dryrun", True):
        return True
    flip_at = cfg.get("flip_at_iso")
    if flip_at is None:
        return True
    try:
        flip_dt = datetime.fromisoformat(flip_at)
        if datetime.now(timezone.utc) < flip_dt + timedelta(days=7):
            return True
    except (ValueError, TypeError):
        return True
    return False


# ---------------------------------------------------------------------------
# Shared context builder
# ---------------------------------------------------------------------------

def _open_db():
    """Open or init state.db, return connection."""
    import gtd.engine.state as state_mod
    if STATE_DB.exists():
        return state_mod.connect(STATE_DB)
    STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    return state_mod.init_db(STATE_DB)


def _vprint(args, *msg):
    if getattr(args, "verbose", False):
        print(*msg, file=sys.stderr)


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def cmd_init(args) -> int:
    """Initialize state.db, config, log dir, run bootstrap.provision_lists()."""
    _vprint(args, "[init] creating directories...")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    import gtd.engine.state as state_mod
    STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = state_mod.init_db(STATE_DB)
    conn.close()
    _vprint(args, f"[init] state.db initialized at {STATE_DB}")

    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG.copy())
        _vprint(args, f"[init] config written to {CONFIG_PATH}")

    if not getattr(args, "dry_run", False):
        try:
            import gtd.engine.bootstrap as bootstrap_mod
            bootstrap_mod.provision_lists(log_dir=LOG_DIR)
            _vprint(args, "[init] lists provisioned")
        except Exception as exc:
            print(f"[init] warning: bootstrap failed: {exc}", file=sys.stderr)
    else:
        print("[init] --dry-run: skipping list provisioning", file=sys.stderr)

    print("init: done")
    return 0


def cmd_bootstrap(args) -> int:
    """Idempotent: provision the 15 GTD lists via gtd.engine.bootstrap."""
    if getattr(args, "dry_run", False):
        print("[bootstrap] --dry-run: no-op", file=sys.stderr)
        return 0
    import gtd.engine.bootstrap as bootstrap_mod
    bootstrap_mod.provision_lists(log_dir=LOG_DIR)
    print("bootstrap: done")
    return 0


def cmd_capture(args) -> int:
    """Read text from --text or stdin (multi-line). Calls capture.capture_multiline."""
    import gtd.engine.capture as capture_mod
    cfg = load_config()
    dryrun = effective_dryrun(cfg) or getattr(args, "dry_run", False)

    text_arg = getattr(args, "text", None)
    # `text_arg is not None` distinguishes empty-string (`--text ""`) from
    # the flag being absent. Empty-string is a user error — fail fast instead
    # of silently falling through to stdin and hanging.
    if text_arg is not None:
        if not text_arg.strip():
            print("capture: --text is empty; pass non-empty text or omit to read stdin.",
                  file=sys.stderr)
            return 2
        lines = [text_arg]
    else:
        _vprint(args, "[capture] reading from stdin (Ctrl-D to finish)...")
        lines = sys.stdin.read().splitlines()

    lines = [l for l in lines if l.strip()]
    if not lines:
        print("capture: nothing to capture", file=sys.stderr)
        return 1

    _vprint(args, f"[capture] {len(lines)} item(s), dryrun={dryrun}")

    if dryrun:
        for line in lines:
            print(f"[capture] dryrun: would capture: {line!r}")
        return 0

    conn = _open_db()
    try:
        ids = capture_mod.capture_multiline(lines, conn=conn, log_dir=LOG_DIR)
        for gtd_id in ids:
            print(f"captured: {gtd_id}")
    finally:
        conn.close()
    return 0


def cmd_clarify(args) -> int:
    """Interactive clarify: print next inbox item + suggestions; read user choice."""
    _vprint(args, "[clarify] manual entry point")
    cfg = load_config()
    dryrun = effective_dryrun(cfg) or getattr(args, "dry_run", False)

    if dryrun:
        print("[clarify] dryrun: would process inbox interactively")
        return 0

    try:
        import gtd.engine.clarify as clarify_mod
        conn = _open_db()
        try:
            result = clarify_mod.process_inbox(
                conn=conn,
                log_dir=LOG_DIR,
                dispatch_dryrun=dryrun,
            )
            _vprint(args, f"[clarify] result: {result}")
        finally:
            conn.close()
    except Exception as exc:
        print(f"[clarify] error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_next(args) -> int:
    """engage.next_actions(ctx, time_min, energy) -> engage.format_for_chat -> stdout."""
    import gtd.engine.engage as engage_mod

    ctx = getattr(args, "ctx", None)
    time_min = getattr(args, "time", None)
    energy = getattr(args, "energy", None)

    _vprint(args, f"[next] ctx={ctx} time={time_min} energy={energy}")

    try:
        actions = engage_mod.next_actions(
            ctx=ctx,
            time_min=time_min,
            energy=energy,
        )
        if not actions:
            label = ctx if ctx else "any context"
            print(
                f"No next actions in {label}. "
                "Try /gtd:capture to add one or /gtd:status to check list counts."
            )
            return 0
        output = engage_mod.format_for_chat(actions)
        print(output)
    except Exception as exc:
        print(f"[next] error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_project(args) -> int:
    """projects.create_project(name, outcome). Prompts for outcome if not provided."""
    import gtd.engine.projects as projects_mod
    cfg = load_config()
    dryrun = effective_dryrun(cfg) or getattr(args, "dry_run", False)

    name = args.name
    outcome = getattr(args, "outcome", None)
    if not outcome:
        # Non-interactive callers (chat shells, scripts, sync.py) MUST pass
        # --outcome. Falling through to input() would hang forever on EOF.
        if not sys.stdin.isatty():
            print(
                "project: --outcome required (non-interactive). "
                "Run: gtd project <name> --outcome '<one-line outcome>'",
                file=sys.stderr,
            )
            return 2
        try:
            outcome = input(f"Outcome for project '{name}': ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\ncancelled", file=sys.stderr)
            return 1
        if not outcome:
            print("project: outcome required", file=sys.stderr)
            return 1

    _vprint(args, f"[project] name={name!r} outcome={outcome!r} dryrun={dryrun}")

    if dryrun:
        print(f"[project] dryrun: would create project {name!r} with outcome: {outcome!r}")
        return 0

    conn = _open_db()
    try:
        project_id = projects_mod.create_project(name, outcome, conn=conn, log_dir=LOG_DIR)
        print(f"project created: {project_id}")
    finally:
        conn.close()
    return 0


def cmd_project_next(args) -> int:
    """projects.add_next_action(project_id_or_name, ctx, title).

    Accepts either a ULID or a project name as the first arg; resolves via
    projects.lookup_by_name_or_ulid before adding the next action.
    """
    import gtd.engine.projects as projects_mod
    cfg = load_config()
    dryrun = effective_dryrun(cfg) or getattr(args, "dry_run", False)

    query = args.project_id  # arg name unchanged for backwards compat; can be ULID or name
    ctx = args.ctx
    title = args.title

    _vprint(args, f"[project-next] query={query!r} ctx={ctx} title={title!r}")

    if dryrun:
        print(f"[project-next] dryrun: would add next action {title!r} to {query!r} in {ctx}")
        return 0

    conn = _open_db()
    try:
        try:
            project = projects_mod.lookup_by_name_or_ulid(query, conn=conn)
        except projects_mod.AmbiguousProjectName as exc:
            print(f"project-next: {exc}", file=sys.stderr)
            print("  Disambiguate by passing the ULID instead of the name.",
                  file=sys.stderr)
            return 2
        except projects_mod.ProjectNotFound as exc:
            print(f"project-next: {exc}", file=sys.stderr)
            return 2

        rid = projects_mod.add_next_action(
            project["project_id"], ctx, title, conn=conn, log_dir=LOG_DIR,
        )
        print(f"next-action created: {rid}")
    finally:
        conn.close()
    return 0


def cmd_weekly_review(args) -> int:
    """review.run_review() -> render snapshot to stdout, prompt for actions."""
    import gtd.engine.review as review_mod
    cfg = load_config()
    dryrun = effective_dryrun(cfg) or getattr(args, "dry_run", False)

    _vprint(args, f"[weekly-review] dryrun={dryrun}")

    if dryrun:
        print("[weekly-review] dryrun: would run interactive weekly review")
        return 0

    try:
        result = review_mod.run_review()
        if isinstance(result, dict):
            print(json.dumps(result, indent=2, default=str))
    except Exception as exc:
        print(f"[weekly-review] error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_waiting(args) -> int:
    """waiting.list_waiting() -> table OR (--nudge) waiting.nudge(per_item=...)."""
    import gtd.engine.waiting as waiting_mod
    cfg = load_config()
    dryrun = effective_dryrun(cfg) or getattr(args, "dry_run", False)
    nudge = getattr(args, "nudge", False)
    per_item = getattr(args, "per_item", False)

    _vprint(args, f"[waiting] nudge={nudge} per_item={per_item} dryrun={dryrun}")

    if nudge:
        if dryrun:
            print("[waiting] dryrun: would nudge waiting items")
            return 0
        conn = _open_db()
        try:
            result = waiting_mod.nudge(
                conn=conn,
                per_item=per_item,
                log_dir=LOG_DIR,
                dispatch_dryrun=dryrun,
            )
            print(f"nudge: {result}")
        finally:
            conn.close()
    else:
        try:
            items = waiting_mod.list_waiting()
            if not items:
                print("Waiting For: (empty)")
                return 0
            print(f"{'Title':<40} {'Delegate':<20} {'Age':>5}")
            print("-" * 70)
            for item in items:
                delegate = item.delegate or "(none)"
                print(f"{item.title[:40]:<40} {delegate[:20]:<20} {item.age_days:>4}d")
        except Exception as exc:
            print(f"[waiting] error: {exc}", file=sys.stderr)
            return 1
    return 0


def cmd_tickler(args) -> int:
    """tickler.park(rid, list, release_at, target_list)."""
    import gtd.engine.tickler as tickler_mod
    cfg = load_config()
    dryrun = effective_dryrun(cfg) or getattr(args, "dry_run", False)

    rid = args.rid
    list_name = args.list
    raw_release_at = args.release_at
    target_list = getattr(args, "target_list", "Inbox")

    # Validate at the boundary so users get a friendly error instead of a
    # cryptic exception buried deep in tickler.park.
    try:
        release_at = tickler_mod.parse_release_date(raw_release_at)
    except tickler_mod.InvalidReleaseDate as exc:
        print(f"tickler: {exc}", file=sys.stderr)
        return 2

    _vprint(args, f"[tickler] rid={rid} list={list_name} release_at={release_at} target={target_list}")

    if dryrun:
        print(f"[tickler] dryrun: would park {rid} from {list_name}, release at {release_at} to {target_list}")
        return 0

    conn = _open_db()
    try:
        tickler_mod.park(rid, list_name, release_at, conn=conn, target_list=target_list, log_dir=LOG_DIR)
        print(f"tickler: parked {rid}, releases at {release_at} to {target_list}")
    finally:
        conn.close()
    return 0


def cmd_ask(args) -> int:
    """qchannel.dispatch(kind='manual', prompt=args.prompt, ref_rid=args.ref)."""
    import gtd.engine.qchannel as qchannel_mod
    cfg = load_config()
    dryrun = effective_dryrun(cfg) or getattr(args, "dry_run", False)

    prompt = args.prompt
    ref = getattr(args, "ref", None)

    _vprint(args, f"[ask] prompt={prompt!r} ref={ref} dryrun={dryrun}")

    conn = _open_db()
    try:
        result = qchannel_mod.dispatch(
            kind="manual",
            prompt=prompt,
            ref_rid=ref,
            conn=conn,
            log_dir=LOG_DIR,
            dispatch_dryrun=dryrun,
        )
        if dryrun:
            print(f"[ask] dryrun: would dispatch Q: {prompt!r}")
        else:
            print(f"ask: dispatched qid={result.qid}")
    except Exception as exc:
        print(f"[ask] error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


def cmd_status(args) -> int:
    """Read-only dashboard: per-list counts, open Qs, stalled projects, last review."""
    _vprint(args, "[status] reading state...")

    conn = _open_db()
    try:
        import gtd.engine.qchannel as qchannel_mod
        import gtd.engine.projects as projects_mod

        rows = conn.execute(
            "SELECT list, COUNT(*) as cnt FROM items GROUP BY list ORDER BY list"
        ).fetchall()

        print("=== GTD Status ===")

        # 📦 Lists
        print("\n📦 Lists")
        if rows:
            for row in rows:
                print(f"   {row[0]:<28} {row[1]:>4} items")
        else:
            print("   (no items in state.db)")

        # ❓ Open Questions
        print("\n❓ Open Questions")
        try:
            open_q = qchannel_mod.open_count(conn=conn)
            print(f"   {open_q}")
        except Exception:
            print("   (unavailable)")

        # ⚠️  Stalled Projects
        print("\n⚠️  Stalled Projects")
        try:
            stalled = projects_mod.stalled_projects(conn=conn)
            if not stalled:
                print("   0")
            else:
                print(f"   {len(stalled)}")
                for p in stalled:
                    print(f"     - {p.get('project_id', '?')}: {p.get('outcome', '(no outcome)')}")
        except Exception:
            print("   (unavailable)")

        # 🩺 Daemon Health (last review + last tick + lock holder)
        print("\n🩺 Daemon Health")
        try:
            row = conn.execute(
                "SELECT completed_at FROM reviews ORDER BY completed_at DESC LIMIT 1"
            ).fetchone()
            last_review = row[0] if row else "(never)"
            print(f"   Last review:  {last_review}")
        except Exception:
            print("   Last review:  (unavailable)")

        engine_log = LOG_DIR / "engine.jsonl"
        last_tick = None
        if engine_log.exists():
            try:
                for line in reversed(engine_log.read_text().splitlines()):
                    try:
                        obj = json.loads(line)
                        if obj.get("op") == "tick":
                            last_tick = obj.get("ts")
                            break
                    except (json.JSONDecodeError, KeyError):
                        continue
            except Exception:
                last_tick = "(unreadable)"
        print(f"   Last tick:    {last_tick or '(none recorded)'}")

        if LOCK_PATH.exists():
            try:
                lock_text = LOCK_PATH.read_text().strip().splitlines()
                if len(lock_text) >= 3:
                    holder_pid = int(lock_text[0])
                    if holder_pid != os.getpid():
                        print(f"   Lock holder:  pid={lock_text[0]} since={lock_text[1]} ({lock_text[2]})")
                    else:
                        print("   Lock holder:  (none — engine idle)")
                else:
                    print("   Lock holder:  (none — engine idle)")
            except Exception:
                print("   Lock holder:  (unreadable)")
        else:
            print("   Lock holder:  (none — engine idle)")

    finally:
        conn.close()
    return 0


# ── adopt: target-list → state.db kind/ctx mapping ─────────────────────────
# Used in --apply phase. Targets not in this map are rejected.
_ADOPT_TARGETS_BY_KIND: dict[str, str] = {
    "@home": "next_action",
    "@computer": "next_action",
    "@calls": "next_action",
    "@errands": "next_action",
    "@anywhere": "next_action",
    "@agenda": "next_action",
    "@nyc": "next_action",
    "@jax": "next_action",
    "@odita": "next_action",
    "@health": "next_action",
    "@financials": "next_action",
    "Waiting For": "waiting_for",
    "Someday": "someday",
    "Tickler": "tickler",
    "Projects": "project",
}


def cmd_adopt(args) -> int:
    """Agent-in-the-loop legacy migration. Three modes:

      gtd adopt
        Discovery: enumerate non-GTD-managed Reminders lists with counts.

      gtd adopt --confirm-list X
        Suggest phase: emit one JSON object per open item in list X to stdout,
        one per line: {"rid": "...", "name": "...", "body": "..."}. No rules,
        no mutation. Caller (Claude) classifies each item and proposes a target
        list, presents to the user for confirmation, then re-invokes with --apply.

      gtd adopt --apply [--from FILE]
        Apply phase: read JSON Lines {"rid": "...", "target_list": "@home"}
        from FILE (or stdin if --from omitted), validate each target is in the
        managed set, and move each reminder. State.db gets a row per moved item
        with kind derived from target_list. Bypasses the v1 7-day dispatch_dryrun
        gate (this is explicit user-confirmed input, not auto-dispatch). Honors
        --dry-run to predict without writing.
    """
    from gtd.engine import bootstrap as bootstrap_mod
    from gtd.engine import state as state_mod
    from gtd.engine.observability import log as obs_log
    from gtd.engine.write_fence import DEFAULT_MANAGED_LISTS, assert_writable

    confirm_list = getattr(args, "confirm_list", None)
    apply_mode = getattr(args, "apply", False)
    apply_from = getattr(args, "apply_from", None)
    dry_run = getattr(args, "dry_run", False)

    if confirm_list and apply_mode:
        print("adopt: --confirm-list and --apply are mutually exclusive.", file=sys.stderr)
        return 2

    # ── Apply phase ───────────────────────────────────────────────────────
    if apply_mode:
        return _adopt_apply(args, apply_from, dry_run, state_mod, obs_log,
                            DEFAULT_MANAGED_LISTS, assert_writable)

    # Both discovery and suggest need the current list set.
    try:
        all_lists = bootstrap_mod.existing_lists()
    except Exception as exc:
        print(f"adopt: failed to read Reminders lists: {exc}", file=sys.stderr)
        return 2

    legacy_lists = sorted(all_lists - DEFAULT_MANAGED_LISTS)

    # ── Discovery mode ────────────────────────────────────────────────────
    if not confirm_list:
        print("=== Legacy lists (not GTD-managed) ===")
        if not legacy_lists:
            print("  (none — all Reminders lists are already managed)")
            return 0

        try:
            import bin.lib.reminders as R
            all_rems = R.list_all(days_done_window=0)
            counts: dict[str, int] = {name: 0 for name in legacy_lists}
            for rem in all_rems:
                rl = getattr(rem, "list", None)
                if rl in counts:
                    counts[rl] += 1
        except Exception as exc:
            _vprint(args, f"[adopt] count probe failed: {exc}")
            counts = {name: -1 for name in legacy_lists}

        width = max(len(n) for n in legacy_lists)
        for name in legacy_lists:
            n = counts.get(name, -1)
            label = "?" if n < 0 else str(n)
            print(f"  {name:<{width}}  {label} item(s)")
        print()
        print("To migrate one:  gtd adopt --confirm-list <name>")
        print("                 (Claude classifies → you confirm → gtd adopt --apply)")
        return 0

    # ── Suggest phase (--confirm-list NAME): emit items as JSON Lines ─────
    if confirm_list in DEFAULT_MANAGED_LISTS:
        print(
            f"adopt: refusing to adopt {confirm_list!r} — it's already in the GTD-managed set.",
            file=sys.stderr,
        )
        return 2

    if confirm_list not in all_lists:
        print(f"adopt: {confirm_list!r} not found in Reminders.", file=sys.stderr)
        if legacy_lists:
            preview = ", ".join(legacy_lists[:10])
            more = "" if len(legacy_lists) <= 10 else f" (+{len(legacy_lists)-10} more)"
            print(f"       Available legacy lists: {preview}{more}", file=sys.stderr)
        else:
            print("       No legacy lists found. Run `gtd adopt` to discover.",
                  file=sys.stderr)
        return 2

    try:
        import bin.lib.reminders as R
        all_rems = R.list_all(days_done_window=0)
    except Exception as exc:
        print(f"adopt: failed to enumerate reminders: {exc}", file=sys.stderr)
        return 2

    items = [
        r for r in all_rems
        if getattr(r, "list", None) == confirm_list
        and not getattr(r, "completed", False)
    ]

    # Header to stderr so stdout stays a pure JSON-Lines stream.
    print(
        f"adopt: emitting {len(items)} item(s) from {confirm_list!r} as JSON Lines on stdout.",
        file=sys.stderr,
    )
    print(
        "       Caller should classify each, present to user, then pipe decisions to "
        "`gtd adopt --apply` (or pass via --from FILE).",
        file=sys.stderr,
    )

    valid_targets = sorted(_ADOPT_TARGETS_BY_KIND.keys())
    print(
        f"       Valid target_list values: {', '.join(valid_targets)}",
        file=sys.stderr,
    )

    if not items:
        return 0

    for rem in items:
        out = {
            "rid": getattr(rem, "id", "") or "",
            "name": getattr(rem, "name", "") or "",
            "body": getattr(rem, "body", "") or "",
            "source_list": confirm_list,
        }
        print(json.dumps(out, ensure_ascii=False))

    return 0


def _adopt_apply(
    args,
    apply_from,
    dry_run: bool,
    state_mod,
    obs_log,
    managed_lists,
    assert_writable,
) -> int:
    """Read JSON Lines decisions and apply moves. See cmd_adopt docstring."""
    if apply_from:
        try:
            text = Path(apply_from).read_text()
        except OSError as exc:
            print(f"adopt --apply: cannot read {apply_from!r}: {exc}", file=sys.stderr)
            return 2
    else:
        if sys.stdin.isatty():
            print(
                "adopt --apply: no --from FILE and stdin is a TTY (nothing to read).",
                file=sys.stderr,
            )
            return 2
        text = sys.stdin.read()

    decisions: list[dict] = []
    for line_no, raw in enumerate(text.splitlines(), 1):
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"adopt --apply: line {line_no}: invalid JSON: {exc}", file=sys.stderr)
            return 2
        if not isinstance(obj, dict) or "rid" not in obj or "target_list" not in obj:
            print(
                f"adopt --apply: line {line_no}: expected object with 'rid' and 'target_list'",
                file=sys.stderr,
            )
            return 2
        decisions.append(obj)

    if not decisions:
        print("adopt --apply: no decisions to apply.", file=sys.stderr)
        return 0

    # Validate targets up-front so a bad batch fails loudly before any moves.
    for i, d in enumerate(decisions, 1):
        tgt = d["target_list"]
        if tgt not in _ADOPT_TARGETS_BY_KIND:
            print(
                f"adopt --apply: decision {i}: target_list {tgt!r} is not an "
                f"adoptable managed list. Valid: {sorted(_ADOPT_TARGETS_BY_KIND.keys())}",
                file=sys.stderr,
            )
            return 2
        if tgt not in managed_lists:
            print(
                f"adopt --apply: decision {i}: target_list {tgt!r} unexpectedly "
                "outside DEFAULT_MANAGED_LISTS. Refusing.",
                file=sys.stderr,
            )
            return 2

    counters = {"moved": 0, "errors": 0}

    if dry_run:
        for d in decisions:
            print(f"[adopt --apply] dryrun: would move {d['rid']} → {d['target_list']}")
        counters["moved"] = len(decisions)
        obs_log("engine", log_dir=LOG_DIR, op="adopt_apply",
                dry_run=True, **counters)
        print(f"=== Adopt apply (dry-run) === moved={counters['moved']} errors=0")
        return 0

    import bin.lib.reminders as R
    conn = _open_db()
    try:
        for d in decisions:
            rid = d["rid"]
            target = d["target_list"]
            try:
                assert_writable(rid, target)
                R.move_to_list(rid, target)

                kind = _ADOPT_TARGETS_BY_KIND[target]
                ctx = target if target.startswith("@") else None
                existing = state_mod.get_item_by_rid(conn, rid)
                if existing is None:
                    state_mod.insert_item(
                        conn, rid=rid, kind=kind, list=target, ctx=ctx,
                    )
                else:
                    cursor = conn.execute(
                        "UPDATE items SET kind = ?, list = ?, ctx = ? WHERE rid = ?",
                        (kind, target, ctx, rid),
                    )
                    conn.commit()
                    # Silent-success guard: SQLite UPDATE with no matching rows
                    # is not an error; without this check, schema drift or a
                    # rid mismatch would print "moved" while leaving state.db
                    # un-tracked. AC-TEST-14.
                    if cursor.rowcount == 0:
                        counters["errors"] += 1
                        print(
                            f"warning: state.db has no row for rid {rid}; "
                            "reminder moved but state un-tracked.",
                            file=sys.stderr,
                        )
                        continue

                counters["moved"] += 1
                print(f"moved: {rid} → {target}")
            except Exception as exc:
                counters["errors"] += 1
                print(f"error: {rid} → {target}: {exc}", file=sys.stderr)
    finally:
        conn.close()

    obs_log("engine", log_dir=LOG_DIR, op="adopt_apply",
            dry_run=False, **counters)
    print(f"=== Adopt apply === moved={counters['moved']} errors={counters['errors']}")
    return 0 if counters["errors"] == 0 else 1


def cmd_clarifier(args) -> int:
    """Decision-tree clarifier: walk Allen's gates on a single item.

    Subcommands:
      evaluate <text> [--json]  → run gates, print verdict + question.

    NOTE: this CLI surface intentionally bypasses the auto_clarify layering
    contract (per AC-CLAR-6) for debug/inspection use. Production callers
    inside /gtd:adopt + /gtd:clarify only invoke `evaluate` AFTER auto_clarify
    returned needs_user.
    """
    import gtd.engine.clarifier as clarifier_mod

    sub = getattr(args, "clarifier_sub", None)
    if sub != "evaluate":
        print("clarifier: missing subcommand. Try: gtd clarifier evaluate '<text>'",
              file=sys.stderr)
        return 2

    text = getattr(args, "text", None) or ""
    if not text.strip():
        print("clarifier evaluate: text is empty.", file=sys.stderr)
        return 2

    eval_result = clarifier_mod.evaluate(text)
    as_json = getattr(args, "json", False)

    if as_json:
        print(json.dumps(eval_result.to_dict(), indent=2))
        return 0

    # Human-readable
    print(f"verdict={eval_result.verdict.value}")
    if eval_result.failed_gate:
        print(f"failed_gate={eval_result.failed_gate}")
        print(f"reason={eval_result.reason}")
        print(f"proposed_question: {eval_result.proposed_question}")
        if eval_result.recommended_disposition:
            print(f"recommended_disposition={eval_result.recommended_disposition}")
    else:
        print(f"reason={eval_result.reason}")
    return 0


def cmd_dryrun_report(args) -> int:
    """US-020: Read qchannel.jsonl, compute metrics, verdict gates, output report."""
    cfg = load_config()
    days = getattr(args, "days", 7)
    as_json = getattr(args, "json", False)
    log_path = getattr(args, "log_path", None) or (LOG_DIR / "qchannel.jsonl")
    log_path = Path(log_path)

    q_max_open = cfg.get("q_max_open", 3)
    q_max_per_day = cfg.get("q_max_per_day", 8)

    # --- Load events ---
    events: list[dict] = []
    if log_path.exists():
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        for line in log_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts_raw = obj.get("ts", "")
            try:
                # parse ISO with timezone
                if ts_raw.endswith("Z"):
                    ts_raw = ts_raw[:-1] + "+00:00"
                ts = datetime.fromisoformat(ts_raw)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
            if ts >= cutoff:
                events.append(obj)

    # --- Compute metrics ---
    from collections import defaultdict

    per_day: dict[str, int] = defaultdict(int)
    per_kind: dict[str, int] = defaultdict(int)
    open_watermark = 0
    cap_breaches: list[str] = []

    for ev in events:
        ts_raw = ev.get("ts", "")
        try:
            if ts_raw.endswith("Z"):
                ts_raw = ts_raw[:-1] + "+00:00"
            ts = datetime.fromisoformat(ts_raw)
        except (ValueError, TypeError):
            continue

        day_key = ts.strftime("%Y-%m-%d")
        per_day[day_key] += 1
        kind = ev.get("kind", "unknown")
        per_kind[kind] += 1

        open_count = ev.get("open_count", 0)
        if isinstance(open_count, (int, float)):
            open_watermark = max(open_watermark, int(open_count))

    # Check cap breaches
    daily_max = max(per_day.values()) if per_day else 0

    check_daily = daily_max <= q_max_per_day
    if not check_daily:
        cap_breaches.append(f"per-day cap: max {daily_max} > {q_max_per_day}")

    check_open = open_watermark <= 4
    if not check_open:
        cap_breaches.append(f"open-count watermark: {open_watermark} > 4")

    # Per-command distribution check: no single kind > 60% of total
    total_events = len(events)
    check_dist = True
    dist_fail_reason = None
    if total_events > 0:
        for k, cnt in per_kind.items():
            if cnt / total_events > 0.80:
                check_dist = False
                dist_fail_reason = f"kind '{k}' = {cnt}/{total_events} ({cnt*100//total_events}% > 80%)"
                break

    check_cap_breaches = len(cap_breaches) == 0

    all_green = check_daily and check_open and check_dist and check_cap_breaches
    verdict = "READY TO FLIP" if all_green else "DO NOT FLIP"

    failing = []
    if not check_daily:
        failing.append(f"daily-max: {daily_max} dispatches on one day exceeds q_max_per_day={q_max_per_day}")
    if not check_open:
        failing.append(f"open-count: watermark {open_watermark} exceeds 4")
    if not check_dist:
        failing.append(f"distribution: {dist_fail_reason}")
    if not check_cap_breaches:
        for b in cap_breaches:
            if b not in " ".join(failing):
                failing.append(b)

    if as_json:
        out = {
            "verdict": verdict,
            "all_green": all_green,
            "days": days,
            "total_events": total_events,
            "daily_max": daily_max,
            "open_watermark": open_watermark,
            "per_day": dict(per_day),
            "per_kind": dict(per_kind),
            "checks": {
                "daily_cap": {"pass": check_daily, "detail": f"max {daily_max} <= {q_max_per_day}"},
                "open_watermark": {"pass": check_open, "detail": f"watermark {open_watermark} <= 4"},
                "distribution": {"pass": check_dist, "detail": dist_fail_reason or "ok"},
                "cap_breaches": {"pass": check_cap_breaches, "detail": f"{len(cap_breaches)} breaches"},
            },
            "failing": failing,
        }
        print(json.dumps(out, indent=2))
    else:
        # Pretty output
        status_sym = lambda ok: "PASS" if ok else "FAIL"
        print(f"\n{'='*60}")
        print(f"  GTD dryrun-report  —  last {days} days  ({total_events} events)")
        print(f"{'='*60}")
        print(f"\n  VERDICT: {verdict}\n")
        print(f"  {'Check':<35} {'Result':>6}  Detail")
        print(f"  {'-'*60}")
        print(f"  {'daily-cap (<= q_max_per_day)':<35} {status_sym(check_daily):>6}  max={daily_max} limit={q_max_per_day}")
        print(f"  {'open-watermark (<= 4)':<35} {status_sym(check_open):>6}  watermark={open_watermark}")
        print(f"  {'kind-distribution (<= 80%)':<35} {status_sym(check_dist):>6}  {dist_fail_reason or 'ok'}")
        print(f"  {'zero-cap-breaches':<35} {status_sym(check_cap_breaches):>6}  {len(cap_breaches)} breach(es)")
        print()
        if failing:
            print("  Failing checks:")
            for f in failing:
                print(f"    - {f}")
            print()

        # Per-day histogram
        if per_day:
            print("  Per-day histogram:")
            for day in sorted(per_day):
                bar = "#" * per_day[day]
                print(f"    {day}  {bar:<10} {per_day[day]}")
            print()

        # Per-kind distribution
        if per_kind:
            print("  Per-kind distribution:")
            for k in sorted(per_kind, key=lambda x: -per_kind[x]):
                print(f"    {k:<30} {per_kind[k]}")
            print()

        print(f"{'='*60}\n")

    # Flip config if verdict is green
    if all_green and not getattr(args, "dry_run", False):
        cfg_now = load_config()
        if cfg_now.get("dispatch_dryrun", True):
            cfg_now["dispatch_dryrun"] = False
            if cfg_now.get("flip_at_iso") is None:
                cfg_now["flip_at_iso"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            save_config(cfg_now)
            _vprint(args, "[dryrun-report] config flipped: dispatch_dryrun=False")

    return 0 if all_green else 1


def cmd_health(args) -> int:
    """Sunday 18:00 weekly digest. Read all 4 JSONL streams, dispatch alert if needed."""
    _vprint(args, "[health] reading last 7 days of JSONL streams...")
    cfg = load_config()
    dryrun = effective_dryrun(cfg) or getattr(args, "dry_run", False)

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    failing_checks: list[str] = []

    def read_stream(name: str) -> list[dict]:
        path = LOG_DIR / f"{name}.jsonl"
        out = []
        if not path.exists():
            return out
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                ts_raw = obj.get("ts", "")
                if ts_raw.endswith("Z"):
                    ts_raw = ts_raw[:-1] + "+00:00"
                ts = datetime.fromisoformat(ts_raw)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts >= cutoff:
                    out.append(obj)
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
        return out

    q_events = read_stream("qchannel")
    inv_events = read_stream("invariants")
    clarify_events = read_stream("clarify")
    engine_events = read_stream("engine")

    # (a) cap breaches in qchannel
    cap_b = [e for e in q_events if e.get("kind") == "cap_breach"]
    if cap_b:
        failing_checks.append(f"qchannel: {len(cap_b)} cap_breach event(s)")

    # (b) write scope violations
    scope_v = [e for e in inv_events if e.get("kind") == "write_scope_violation"]
    if scope_v:
        failing_checks.append(f"invariants: {len(scope_v)} write_scope_violation(s)")

    # (c) auto-clarify rate < 50% over >= 10 events
    if len(clarify_events) >= 10:
        auto = sum(1 for e in clarify_events if e.get("result") == "auto_clarified")
        rate = auto / len(clarify_events)
        if rate < 0.5:
            failing_checks.append(f"clarify: auto-rate {rate:.0%} < 50% over {len(clarify_events)} events")

    # (d) tick errors or p95 > 3x baseline
    tick_errors = [e for e in engine_events if e.get("op") == "tick" and e.get("error")]
    if tick_errors:
        failing_checks.append(f"engine: {len(tick_errors)} tick error(s)")

    tick_durations = [
        e.get("duration_ms") for e in engine_events
        if e.get("op") == "tick" and isinstance(e.get("duration_ms"), (int, float))
    ]
    if len(tick_durations) >= 10:
        tick_durations.sort()
        p95_idx = int(len(tick_durations) * 0.95)
        p95 = tick_durations[min(p95_idx, len(tick_durations) - 1)]
        baseline = tick_durations[len(tick_durations) // 2]  # median
        if baseline > 0 and p95 > 3 * baseline:
            failing_checks.append(f"engine: p95 tick {p95}ms > 3x baseline {baseline}ms")

    if not failing_checks:
        _vprint(args, "[health] all green — no alert dispatched")
        return 0

    payload = {"failing_checks": failing_checks, "event_counts": {
        "qchannel": len(q_events),
        "invariants": len(inv_events),
        "clarify": len(clarify_events),
        "engine": len(engine_events),
    }}

    _vprint(args, f"[health] {len(failing_checks)} failing check(s), dispatching digest Q")

    if dryrun:
        print(f"[health] dryrun: would dispatch health_alert Q with payload: {json.dumps(payload)}")
        return 0

    try:
        import gtd.engine.qchannel as qchannel_mod
        conn = _open_db()
        try:
            qchannel_mod.dispatch(
                kind="health_alert",
                prompt="Weekly health digest — see payload for failing checks",
                conn=conn,
                log_dir=LOG_DIR,
                dispatch_dryrun=False,
                bypass_open_cap=True,
                payload=payload,
            )
        finally:
            conn.close()
    except Exception as exc:
        print(f"[health] error dispatching alert: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_tick(args) -> int:
    """Internal: launchd-driven 5-min tick."""
    _vprint(args, "[tick] starting...")
    cfg = load_config()
    dryrun = effective_dryrun(cfg) or getattr(args, "dry_run", False)

    conn = _open_db()
    try:
        # 1. Capture: scan Inbox
        _vprint(args, "[tick] step 1: capture scan")
        try:
            import gtd.engine.capture as capture_mod
        except Exception as exc:
            print(f"[tick] capture import error: {exc}", file=sys.stderr)

        # 2. Clarify: process inbox
        _vprint(args, "[tick] step 2: clarify.process_inbox")
        try:
            import gtd.engine.clarify as clarify_mod
            clarify_mod.process_inbox(conn=conn, log_dir=LOG_DIR, dispatch_dryrun=dryrun)
        except Exception as exc:
            print(f"[tick] clarify error: {exc}", file=sys.stderr)

        # 3. qchannel.poll
        _vprint(args, "[tick] step 3: qchannel.poll")
        try:
            import gtd.engine.qchannel as qchannel_mod
            qchannel_mod.poll(conn=conn, log_dir=LOG_DIR)
        except Exception as exc:
            print(f"[tick] qchannel.poll error: {exc}", file=sys.stderr)

        # 4. tickler.release
        _vprint(args, "[tick] step 4: tickler.release")
        try:
            import gtd.engine.tickler as tickler_mod
            tickler_mod.release(conn=conn, log_dir=LOG_DIR, dispatch_dryrun=dryrun)
        except Exception as exc:
            print(f"[tick] tickler.release error: {exc}", file=sys.stderr)

        # 5. projects.check_invariants
        _vprint(args, "[tick] step 5: projects.check_invariants")
        try:
            import gtd.engine.projects as projects_mod
            projects_mod.check_invariants(conn=conn, log_dir=LOG_DIR, dispatch_dryrun=dryrun)
        except Exception as exc:
            print(f"[tick] projects.check_invariants error: {exc}", file=sys.stderr)

    finally:
        conn.close()

    _vprint(args, "[tick] done")
    return 0


# ---------------------------------------------------------------------------
# Argument parser construction
# ---------------------------------------------------------------------------

def _add_dry_run(subparser: argparse.ArgumentParser) -> None:
    """Register --dry-run on a subparser using SUPPRESS so the subparser
    default does NOT overwrite the global flag in the merged Namespace.

    Pairs with the removal of the post-parse normalization in main(). With
    SUPPRESS, args.dry_run is only set if the user passes the flag (at
    either position). Handlers must read via getattr(args, 'dry_run', False).
    """
    subparser.add_argument(
        "--dry-run",
        action="store_true",
        default=argparse.SUPPRESS,
        help="No-op mode; log only (also accepted as a global flag before subcommand)",
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gtd",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS,
                   help="No-op mode; log only")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose per-step logging to stderr")

    sp = p.add_subparsers(dest="command", required=True, metavar="SUBCOMMAND")

    # init
    init_p = sp.add_parser("init", help="Initialize state.db, config, log dir, provision lists")
    _add_dry_run(init_p)

    # bootstrap
    boot_p = sp.add_parser("bootstrap", help="Idempotent: provision the 15 GTD lists")
    _add_dry_run(boot_p)

    # capture
    cap = sp.add_parser("capture", help="Capture one or more items into Inbox")
    cap.add_argument("--text", "-t", help="Text to capture (omit to read from stdin)")
    _add_dry_run(cap)

    # clarify
    clr_p = sp.add_parser("clarify", help="Interactive: process next inbox item")
    _add_dry_run(clr_p)

    # next
    nxt = sp.add_parser("next", help="Show ranked next actions")
    nxt.add_argument("--ctx", help="Context filter, e.g. @home")
    nxt.add_argument("--time", type=int, metavar="MINUTES", help="Max minutes available")
    nxt.add_argument("--energy", choices=["low", "med", "high"], help="Energy level filter")
    _add_dry_run(nxt)

    # project
    proj = sp.add_parser("project", help="Create a new project")
    proj.add_argument("name", help="Project name")
    proj.add_argument("--outcome", "-o", help="One-line outcome statement (required in non-interactive mode)")
    _add_dry_run(proj)

    # project-next
    pnxt = sp.add_parser("project-next", help="Add a next action to a project")
    pnxt.add_argument("project_id", help="Project name or ULID (name lookup falls back to ULID)")
    pnxt.add_argument("ctx", help="Context list, e.g. @home")
    pnxt.add_argument("title", help="Next action title")
    _add_dry_run(pnxt)

    # weekly-review
    wr_p = sp.add_parser("weekly-review", help="Run interactive weekly GTD review")
    _add_dry_run(wr_p)

    # waiting
    wait = sp.add_parser("waiting", help="Show or nudge waiting-for items")
    wait.add_argument("--nudge", action="store_true", help="Dispatch nudge Q(s)")
    wait.add_argument("--per-item", action="store_true", dest="per_item",
                      help="One Q per stale item (default: digest Q)")
    _add_dry_run(wait)

    # tickler
    tick_p = sp.add_parser("tickler", help="Park a reminder in the tickler file")
    tick_p.add_argument("rid", help="Reminder ID to park")
    tick_p.add_argument("list", help="Current list of the reminder")
    tick_p.add_argument("release_at",
                        help="Release date: YYYY-MM-DD (defaults to 09:00 local) or YYYY-MM-DDTHH:MM:SS")
    tick_p.add_argument("--target-list", default="Inbox", dest="target_list",
                        help="List to move to on release (default: Inbox)")
    _add_dry_run(tick_p)

    # ask
    ask_p = sp.add_parser("ask", help="Dispatch a manual Q to the Questions list")
    ask_p.add_argument("prompt", help="Question text")
    ask_p.add_argument("--ref", help="Optional reference reminder ID")
    _add_dry_run(ask_p)

    # status
    stat_p = sp.add_parser("status", help="Read-only dashboard: counts, Qs, projects, last review")
    _add_dry_run(stat_p)

    # clarifier
    clr_p = sp.add_parser(
        "clarifier",
        help="GTD clarifier: walk Allen's actionable→outcome→next-action gates on an item",
    )
    clr_sub = clr_p.add_subparsers(dest="clarifier_sub", metavar="SUBCOMMAND")
    clr_eval = clr_sub.add_parser("evaluate", help="Evaluate a single item's clarifier gates")
    clr_eval.add_argument("text", help="The item title to evaluate (quote it)")
    clr_eval.add_argument("--json", action="store_true",
                          help="Emit ClarifyEvaluation as JSON")
    _add_dry_run(clr_p)

    # adopt
    adopt_p = sp.add_parser(
        "adopt",
        help="Agent-in-the-loop legacy migration: discover | suggest (--confirm-list) | apply (--apply)",
    )
    adopt_p.add_argument("--confirm-list", dest="confirm_list", metavar="LIST",
                         help="Suggest phase: emit JSON Lines of items in LIST for the caller to classify")
    adopt_p.add_argument("--apply", action="store_true",
                         help="Apply phase: read JSON Lines decisions and move reminders")
    adopt_p.add_argument("--from", dest="apply_from", metavar="FILE",
                         help="With --apply: read decisions from FILE instead of stdin")
    _add_dry_run(adopt_p)

    # dryrun-report
    dr = sp.add_parser("dryrun-report", help="US-020: compute verdicts on qchannel dispatch history")
    dr.add_argument("--days", type=int, default=7, help="Look-back window in days (default: 7)")
    dr.add_argument("--json", action="store_true", help="Output as JSON")
    dr.add_argument("--log-path", dest="log_path", help="Override path to qchannel.jsonl")
    _add_dry_run(dr)

    # health
    h_p = sp.add_parser("health", help="Sunday digest: check JSONL streams, dispatch alert if needed")
    _add_dry_run(h_p)

    # tick (internal)
    tk_p = sp.add_parser("tick", help="[internal] 5-minute launchd tick")
    _add_dry_run(tk_p)

    return p


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

HANDLERS = {
    "init": cmd_init,
    "bootstrap": cmd_bootstrap,
    "capture": cmd_capture,
    "clarify": cmd_clarify,
    "next": cmd_next,
    "project": cmd_project,
    "project-next": cmd_project_next,
    "weekly-review": cmd_weekly_review,
    "waiting": cmd_waiting,
    "tickler": cmd_tickler,
    "ask": cmd_ask,
    "status": cmd_status,
    "clarifier": cmd_clarifier,
    "adopt": cmd_adopt,
    "dryrun-report": cmd_dryrun_report,
    "health": cmd_health,
    "tick": cmd_tick,
}


def main(argv: list[str] | None = None) -> int:
    p = _build_parser()
    args = p.parse_args(argv)

    # NOTE: --dry-run is registered with default=argparse.SUPPRESS on both the
    # global parser and every subparser. That means args.dry_run is set ONLY
    # when the user passes the flag (at either position). All handlers must
    # read it via getattr(args, "dry_run", False). Do NOT add a normalization
    # helper here — it would mask SUPPRESS-pattern bugs (AC-TEST-13).

    handler = HANDLERS.get(args.command)
    if handler is None:
        print(f"gtd: unknown command {args.command!r}", file=sys.stderr)
        return 2

    try:
        from gtd.engine.lock import acquire as lock_acquire
        with lock_acquire(LOCK_PATH, holder_argv0="gtd-engine"):
            return handler(args)
    except ImportError:
        # lock module not available in test environments — run without lock
        return handler(args)
    except Exception as exc:
        print(f"gtd: {args.command}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
