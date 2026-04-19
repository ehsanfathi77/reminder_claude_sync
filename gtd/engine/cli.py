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
    if text_arg:
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
    """projects.add_next_action(project_id, ctx, title)."""
    import gtd.engine.projects as projects_mod
    cfg = load_config()
    dryrun = effective_dryrun(cfg) or getattr(args, "dry_run", False)

    project_id = args.project_id
    ctx = args.ctx
    title = args.title

    _vprint(args, f"[project-next] project_id={project_id} ctx={ctx} title={title!r}")

    if dryrun:
        print(f"[project-next] dryrun: would add next action {title!r} to {project_id} in {ctx}")
        return 0

    conn = _open_db()
    try:
        rid = projects_mod.add_next_action(project_id, ctx, title, conn=conn, log_dir=LOG_DIR)
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
    release_at = args.release_at
    target_list = getattr(args, "target_list", "Inbox")

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
        import gtd.engine.state as state_mod
        import gtd.engine.qchannel as qchannel_mod
        import gtd.engine.projects as projects_mod

        # Per-list counts from items table
        rows = conn.execute(
            "SELECT list, COUNT(*) as cnt FROM items GROUP BY list ORDER BY list"
        ).fetchall()

        print("=== GTD Status ===")
        print()
        print("Lists:")
        if rows:
            for row in rows:
                print(f"  {row[0]:<30} {row[1]:>4} items")
        else:
            print("  (no items in state.db)")

        # Open Qs
        try:
            open_q = qchannel_mod.open_count(conn=conn)
            print(f"\nOpen Questions:      {open_q}")
        except Exception:
            print("\nOpen Questions:      (unavailable)")

        # Stalled projects
        try:
            stalled = projects_mod.stalled_projects(conn=conn)
            print(f"Stalled projects:    {len(stalled)}")
            for p in stalled:
                print(f"  - {p.get('project_id', '?')}: {p.get('outcome', '(no outcome)')}")
        except Exception:
            print("Stalled projects:    (unavailable)")

        # Last review
        try:
            row = conn.execute(
                "SELECT completed_at FROM reviews ORDER BY completed_at DESC LIMIT 1"
            ).fetchone()
            last_review = row[0] if row else "(never)"
            print(f"Last review:         {last_review}")
        except Exception:
            print("Last review:         (unavailable)")

        # Lock holder — skip self (status holds the lock while it reads).
        if LOCK_PATH.exists():
            try:
                lock_text = LOCK_PATH.read_text().strip().splitlines()
                if len(lock_text) >= 3:
                    holder_pid = int(lock_text[0])
                    if holder_pid != os.getpid():
                        # Another daemon held the lock; report it.
                        print(f"\nLock holder:         pid={lock_text[0]} since={lock_text[1]} ({lock_text[2]})")
                    else:
                        print("\nLock holder:         (none — engine idle)")
                else:
                    print("\nLock holder:         (none — engine idle)")
            except Exception:
                print("\nLock holder:         (unreadable)")
        else:
            print("\nLock holder:         (none — engine idle)")

        # Last tick from engine.jsonl
        engine_log = LOG_DIR / "engine.jsonl"
        if engine_log.exists():
            try:
                lines = engine_log.read_text().splitlines()
                last_tick = None
                for line in reversed(lines):
                    try:
                        obj = json.loads(line)
                        if obj.get("op") == "tick":
                            last_tick = obj.get("ts")
                            break
                    except (json.JSONDecodeError, KeyError):
                        continue
                print(f"Last tick:           {last_tick or '(none recorded)'}")
            except Exception:
                print("Last tick:           (unreadable)")
        else:
            print("Last tick:           (no engine.jsonl)")

    finally:
        conn.close()
    return 0


def cmd_adopt(args) -> int:
    """One-time legacy migration. Walks legacy lists and prompts user for GTD bucket."""
    confirm_list = getattr(args, "confirm_list", None)
    dry_run = getattr(args, "dry_run", False)

    if not confirm_list and not dry_run:
        print(
            "adopt: NO-OP — pass --confirm-list <ListName> to migrate a specific list.\n"
            "       This prevents accidental bulk mutations of legacy data.",
            file=sys.stderr,
        )
        return 0

    _vprint(args, f"[adopt] confirm_list={confirm_list} dry_run={dry_run}")
    print(f"[adopt] {'dryrun: would migrate' if dry_run else 'migrating'} list: {confirm_list or '(none)'}")
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

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gtd",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--dry-run", action="store_true", help="No-op mode; log only")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose per-step logging to stderr")

    sp = p.add_subparsers(dest="command", required=True, metavar="SUBCOMMAND")

    # init
    sp.add_parser("init", help="Initialize state.db, config, log dir, provision lists")

    # bootstrap
    sp.add_parser("bootstrap", help="Idempotent: provision the 15 GTD lists")

    # capture
    cap = sp.add_parser("capture", help="Capture one or more items into Inbox")
    cap.add_argument("--text", "-t", help="Text to capture (omit to read from stdin)")

    # clarify
    sp.add_parser("clarify", help="Interactive: process next inbox item")

    # next
    nxt = sp.add_parser("next", help="Show ranked next actions")
    nxt.add_argument("--ctx", help="Context filter, e.g. @home")
    nxt.add_argument("--time", type=int, metavar="MINUTES", help="Max minutes available")
    nxt.add_argument("--energy", choices=["low", "med", "high"], help="Energy level filter")

    # project
    proj = sp.add_parser("project", help="Create a new project")
    proj.add_argument("name", help="Project name")
    proj.add_argument("--outcome", "-o", help="One-line outcome statement")

    # project-next
    pnxt = sp.add_parser("project-next", help="Add a next action to a project")
    pnxt.add_argument("project_id", help="Project ULID")
    pnxt.add_argument("ctx", help="Context list, e.g. @home")
    pnxt.add_argument("title", help="Next action title")

    # weekly-review
    sp.add_parser("weekly-review", help="Run interactive weekly GTD review")

    # waiting
    wait = sp.add_parser("waiting", help="Show or nudge waiting-for items")
    wait.add_argument("--nudge", action="store_true", help="Dispatch nudge Q(s)")
    wait.add_argument("--per-item", action="store_true", dest="per_item",
                      help="One Q per stale item (default: digest Q)")

    # tickler
    tick_p = sp.add_parser("tickler", help="Park a reminder in the tickler file")
    tick_p.add_argument("rid", help="Reminder ID to park")
    tick_p.add_argument("list", help="Current list of the reminder")
    tick_p.add_argument("release_at", help="ISO datetime to release (e.g. 2026-05-01T09:00:00)")
    tick_p.add_argument("--target-list", default="Inbox", dest="target_list",
                        help="List to move to on release (default: Inbox)")

    # ask
    ask_p = sp.add_parser("ask", help="Dispatch a manual Q to the Questions list")
    ask_p.add_argument("prompt", help="Question text")
    ask_p.add_argument("--ref", help="Optional reference reminder ID")

    # status
    sp.add_parser("status", help="Read-only dashboard: counts, Qs, projects, last review")

    # adopt
    adopt_p = sp.add_parser("adopt", help="One-time legacy migration (NO-OP unless --confirm-list)")
    adopt_p.add_argument("--confirm-list", dest="confirm_list", metavar="LIST",
                         help="Name of legacy list to migrate")

    # dryrun-report
    dr = sp.add_parser("dryrun-report", help="US-020: compute verdicts on qchannel dispatch history")
    dr.add_argument("--days", type=int, default=7, help="Look-back window in days (default: 7)")
    dr.add_argument("--json", action="store_true", help="Output as JSON")
    dr.add_argument("--log-path", dest="log_path", help="Override path to qchannel.jsonl")

    # health
    sp.add_parser("health", help="Sunday digest: check JSONL streams, dispatch alert if needed")

    # tick (internal)
    sp.add_parser("tick", help="[internal] 5-minute launchd tick")

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
    "adopt": cmd_adopt,
    "dryrun-report": cmd_dryrun_report,
    "health": cmd_health,
    "tick": cmd_tick,
}


def main(argv: list[str] | None = None) -> int:
    p = _build_parser()
    args = p.parse_args(argv)

    # Normalize --dry-run to args.dry_run
    if not hasattr(args, "dry_run"):
        args.dry_run = False

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
