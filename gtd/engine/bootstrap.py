"""
bootstrap.py — provision the 15 GTD-managed Reminders lists.

Idempotent: subsequent runs detect existing lists and skip. Logs per-list
status to engine.jsonl.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from gtd.engine.observability import log
from gtd.engine.write_fence import DEFAULT_MANAGED_LISTS

REMINDERS_CLI_DEFAULT = Path.home() / "Documents/repos/todo/bin/reminders-cli"

# Canonical display order: Inbox first, then @-contexts alphabetically,
# then process lists. Any name not explicitly listed falls back to alpha.
_CANONICAL_ORDER: list[str] = [
    "Inbox",
    "@agenda",
    "@anywhere",
    "@calls",
    "@computer",
    "@errands",
    "@home",
    "@jax",
    "@nyc",
    "@odita",
    "Waiting For",
    "Someday",
    "Projects",
    "Tickler",
    "Questions",
]


def _sort_key(name: str) -> tuple[int, str]:
    """Return a sort key that places names in canonical GTD order."""
    try:
        return (_CANONICAL_ORDER.index(name), name)
    except ValueError:
        return (len(_CANONICAL_ORDER), name)


def existing_lists(reminders_cli: Path = REMINDERS_CLI_DEFAULT) -> set[str]:
    """Return current Reminders list names."""
    result = subprocess.run(
        [str(reminders_cli), "show-lists"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def provision_lists(
    *,
    reminders_cli: Path = REMINDERS_CLI_DEFAULT,
    managed: frozenset[str] | set[str] | None = None,
    log_dir: Path | None = None,
    dry_run: bool = False,
) -> dict[str, str]:
    """For each list in `managed`: if it exists, skip; else create.

    Returns {list_name: 'created' | 'exists' | 'skipped' | 'error: <msg>'}.
    Logs a single engine.jsonl line with op='bootstrap' and per-list status.
    dry_run=True: returns the would-be plan without invoking new-list.
    """
    effective = DEFAULT_MANAGED_LISTS if managed is None else managed
    current = existing_lists(reminders_cli=reminders_cli)

    ordered = sorted(effective, key=_sort_key)
    details: dict[str, str] = {}

    for name in ordered:
        if name in current:
            details[name] = "exists"
            continue

        if dry_run:
            details[name] = "skipped"
            continue

        try:
            subprocess.run(
                [str(reminders_cli), "new-list", name],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            details[name] = "created"
        except subprocess.CalledProcessError as exc:
            details[name] = f"error: {exc.stderr.strip()}"

    n_created = sum(1 for s in details.values() if s == "created")
    n_exists = sum(1 for s in details.values() if s == "exists")
    n_errors = sum(1 for s in details.values() if s.startswith("error:"))

    log(
        "engine",
        log_dir=log_dir,
        op="bootstrap",
        total=len(effective),
        created=n_created,
        exists=n_exists,
        errors=n_errors,
        dry_run=dry_run,
        details=details,
    )

    return details
