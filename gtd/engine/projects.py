"""
projects.py — projects + GTD next-action invariant.

Each project = one reminder in the 'Projects' list with notes containing:
  --- gtd ---
  id: <project-ulid>
  kind: project
  outcome: <one-line outcome statement>
  --- end ---
  <user prose>

Next-actions live in @context lists with a 'project: <project-ulid>' field
in their gtd-fence metadata block.

Public API:

def create_project(
    name: str,
    outcome: str,
    *,
    conn,
    rem_module=R,
    log_dir: Path | None = None,
    now: datetime | None = None,
) -> str:
    '''Create a Projects-list reminder + state row. Returns project_id.'''

def add_next_action(
    project_id: str,
    ctx: str,                        # '@home', '@calls', etc.
    title: str,
    *,
    conn,
    rem_module=R,
    log_dir: Path | None = None,
    now: datetime | None = None,
) -> str:
    '''Create a next-action reminder in the @ctx list with project link.
    Returns rid. Calls write_fence.assert_writable.'''

def project_children(project_id: str, *, conn, rem_module=R) -> list[dict]:
    '''Return all open reminders linked to this project. Reads via rem_module
    (since the link lives in notes-metadata, not state.db; the engine just
    indexes for performance).'''

def stalled_projects(*, conn, rem_module=R) -> list[dict]:
    '''Returns project dicts with NO open next-action child. Cross-checks
    state.projects_without_open_next_action (DB index) AGAINST a fresh
    rem_module.list_all() pass (in case state is stale).'''

def check_invariants(
    *,
    conn,
    rem_module=R,
    qchannel_module=Q,
    log_dir: Path | None = None,
    dispatch_dryrun: bool = True,
    now: datetime | None = None,
) -> dict:
    '''Find stalled projects. If any, dispatch a SINGLE digest Q via qchannel
    (digest=True) listing all stalled projects and their outcomes. Returns
    {'stalled_count': N, 'q_dispatched': bool}.'''
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

# Import reminders module as the default rem_module.
# Tests inject a stub via the rem_module parameter.
try:
    import bin.lib.reminders as R  # type: ignore
except ImportError:
    R = None  # type: ignore

# Import qchannel module as the default qchannel_module.
# Tests inject a stub via the qchannel_module parameter.
try:
    import gtd.engine.qchannel as Q  # type: ignore
except ImportError:
    Q = None  # type: ignore

import re

from gtd.engine.notes_metadata import parse_metadata, serialize_metadata
from gtd.engine.observability import log as obs_log
from gtd.engine.state import _ulid, insert_item, insert_project, projects_without_open_next_action
from gtd.engine.write_fence import assert_writable

_PROJECTS_LIST = "Projects"

# Crockford-base32 ULID — 26 chars, [0-9A-HJKMNP-TV-Z], case-insensitive.
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Za-hjkmnp-tv-z]{26}$")


class ProjectNotFound(LookupError):
    """Raised when neither name nor ULID lookup matches a Projects-list entry."""

    def __init__(self, query: str):
        self.query = query
        super().__init__(f"project {query!r} not found")


class AmbiguousProjectName(LookupError):
    """Raised when a name lookup matches multiple projects."""

    def __init__(self, query: str, matches: list[dict]):
        self.query = query
        self.matches = matches
        ids = ", ".join(m.get("project_id", "?") for m in matches)
        super().__init__(
            f"project name {query!r} matches {len(matches)} projects: {ids}"
        )


def lookup_by_name_or_ulid(query: str, *, conn, rem_module=None) -> dict:
    """Resolve a CLI-supplied project identifier to a project dict.

    Tries ULID first (regex match against state.db); falls back to a
    case-insensitive name match against the Projects-list reminder titles.

    Returns: {'project_id': str, 'outcome': str, 'name': str, 'rid': str}

    Raises:
      ProjectNotFound          — neither ULID nor name resolves
      AmbiguousProjectName     — name resolves to >1 project
    """
    if rem_module is None:
        rem_module = R
    if not isinstance(query, str) or not query.strip():
        raise ProjectNotFound(query if isinstance(query, str) else repr(query))
    q = query.strip()

    # ULID path: cheap, definitive
    if _ULID_RE.match(q):
        row = conn.execute(
            "SELECT project_id, outcome FROM projects WHERE project_id = ?", (q,)
        ).fetchone()
        if row is not None:
            r = dict(row)
            return {
                "project_id": r["project_id"],
                "outcome": r["outcome"],
                "name": "",
                "rid": "",
            }
        raise ProjectNotFound(q)

    # Name path: walk Projects-list reminders, match title case-insensitively
    if rem_module is None:
        raise ProjectNotFound(q)
    matches: list[dict] = []
    q_lower = q.lower()
    for rem in rem_module.list_all():
        if getattr(rem, "list", None) != _PROJECTS_LIST:
            continue
        if getattr(rem, "completed", False):
            continue
        name = getattr(rem, "name", "") or ""
        if name.lower() != q_lower:
            continue
        meta, _ = parse_metadata(getattr(rem, "body", "") or "")
        pid = meta.get("id") or ""
        outcome = meta.get("outcome", "") or ""
        matches.append({
            "project_id": pid,
            "outcome": outcome,
            "name": name,
            "rid": getattr(rem, "id", "") or "",
        })

    if not matches:
        raise ProjectNotFound(q)
    if len(matches) > 1:
        raise AmbiguousProjectName(q, matches)
    return matches[0]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_project(
    name: str,
    outcome: str,
    *,
    conn,
    rem_module=R,
    log_dir: Path | None = None,
    now: datetime | None = None,
) -> str:
    """Create a Projects-list reminder + state row. Returns project_id."""
    if now is None:
        now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")

    project_id = _ulid()

    # Assert write scope (Projects list is in managed set)
    assert_writable("<new-project>", _PROJECTS_LIST)

    # Build fenced metadata notes: id, kind, outcome embedded
    notes = serialize_metadata(
        {"id": project_id, "kind": "project", "outcome": outcome, "created": now_iso},
        "",
    )

    # Create the reminder in Reminders.app (Projects list)
    rem_module.create(_PROJECTS_LIST, name, notes=notes)

    # Persist to state.db projects table
    insert_project(conn, project_id, outcome)

    obs_log("engine", log_dir=log_dir, op="create_project", project_id=project_id, name=name)

    return project_id


def add_next_action(
    project_id: str,
    ctx: str,
    title: str,
    *,
    conn,
    rem_module=R,
    log_dir: Path | None = None,
    now: datetime | None = None,
) -> str:
    """Create a next-action reminder in the @ctx list with project link.
    Returns rid. Calls write_fence.assert_writable."""
    if now is None:
        now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")

    # Enforce write scope on the context list — raises WriteScopeError for unmanaged ctx
    assert_writable("<new-next-action>", ctx)

    gtd_id = _ulid()

    # Build fenced metadata notes with project link
    notes = serialize_metadata(
        {
            "id": gtd_id,
            "kind": "next_action",
            "created": now_iso,
            "ctx": ctx,
            "project": project_id,
        },
        "",
    )

    # Create the reminder in the @ctx list
    rid = rem_module.create(ctx, title, notes=notes)

    # Persist to state.db items table (links project for DB index)
    insert_item(
        conn,
        gtd_id=gtd_id,
        rid=rid,
        kind="next_action",
        list=ctx,
        project=project_id,
        ctx=ctx,
        created=now_iso,
    )

    obs_log(
        "engine",
        log_dir=log_dir,
        op="add_next_action",
        project_id=project_id,
        ctx=ctx,
        rid=rid,
    )

    return rid


def project_children(project_id: str, *, conn, rem_module=R) -> list[dict]:
    """Return all open reminders linked to this project.

    Reads via rem_module (since the link lives in notes-metadata, not state.db;
    the engine just indexes for performance).
    """
    all_rems = rem_module.list_all()
    children: list[dict] = []
    for rem in all_rems:
        if rem.completed:
            continue
        meta, _ = parse_metadata(rem.body)
        if meta.get("project") == project_id and meta.get("kind") == "next_action":
            children.append({
                "rid": rem.id,
                "name": rem.name,
                "list": rem.list,
                "body": rem.body,
                "meta": meta,
            })
    return children


def stalled_projects(*, conn, rem_module=R) -> list[dict]:
    """Returns project dicts with NO open next-action child.

    Cross-checks state.projects_without_open_next_action (DB index) AGAINST
    a fresh rem_module.list_all() pass (in case state is stale).
    """
    # DB index candidates (projects with no items row in state)
    db_stalled = projects_without_open_next_action(conn)
    db_stalled_ids = {p["project_id"] for p in db_stalled}

    # Cross-check against live reminders data
    all_rems = rem_module.list_all()

    # Build set of project_ids that have at least one open next-action in live data
    live_active_project_ids: set[str] = set()
    for rem in all_rems:
        if rem.completed:
            continue
        meta, _ = parse_metadata(rem.body)
        if meta.get("kind") == "next_action" and meta.get("project"):
            live_active_project_ids.add(meta["project"])

    # A project is truly stalled only if DB says stalled AND live data confirms no children
    result: list[dict] = []
    for p in db_stalled:
        pid = p["project_id"]
        if pid not in live_active_project_ids:
            result.append(p)

    return result


def check_invariants(
    *,
    conn,
    rem_module=R,
    qchannel_module=Q,
    log_dir: Path | None = None,
    dispatch_dryrun: bool = True,
    now: datetime | None = None,
) -> dict:
    """Find stalled projects. If any, dispatch a SINGLE digest Q via qchannel
    (digest=True) listing all stalled projects and their outcomes. Returns
    {'stalled_count': N, 'q_dispatched': bool}."""
    if now is None:
        now = datetime.now(timezone.utc)

    stalled = stalled_projects(conn=conn, rem_module=rem_module)
    stalled_count = len(stalled)

    if stalled_count == 0:
        obs_log(
            "engine",
            log_dir=log_dir,
            op="check_invariants",
            stalled_count=0,
            q_dispatched=False,
        )
        return {"stalled_count": 0, "q_dispatched": False}

    # Build a digest prompt listing all stalled projects
    project_summaries = [
        f"{p['project_id']}: {p['outcome']}" for p in stalled
    ]
    prompt = f"Stalled projects ({stalled_count}): " + "; ".join(
        p["outcome"] for p in stalled
    )
    # Truncate to 80 chars for reminder title; full payload carries the detail
    prompt = prompt[:80]

    payload = {
        "stalled_projects": [
            {"project_id": p["project_id"], "outcome": p["outcome"]}
            for p in stalled
        ],
    }

    result = qchannel_module.dispatch(
        conn=conn,
        rem_module=rem_module,
        kind="invariant",
        prompt=prompt,
        payload=payload,
        digest=True,
        dispatch_dryrun=dispatch_dryrun,
        now=now,
        log_dir=log_dir,
    )

    q_dispatched = result.status in ("dispatched", "dryrun", "queued_quiet")

    obs_log(
        "engine",
        log_dir=log_dir,
        op="check_invariants",
        stalled_count=stalled_count,
        q_dispatched=q_dispatched,
        q_status=result.status,
    )

    return {"stalled_count": stalled_count, "q_dispatched": q_dispatched}
