"""
conftest.py — Top-level pytest configuration.

Defines:
  - 'integration' marker (tests that hit real macOS Reminders.app)
  - test_list_name fixture: creates a uniquely-named GTD-TEST-<hex> list in
    Reminders, yields the name, then tears down all reminders inside it and
    deletes the list itself via osascript.

Safety rules enforced here:
  1. All test lists are prefixed with 'GTD-TEST-' so they never collide with
     the user's real lists.
  2. Teardown runs inside a try/finally so cleanup happens even on test failure.
  3. Tests decorated with @pytest.mark.integration are excluded from plain
     'pytest tests/unit/' runs via the ini marker filter in pytest.ini /
     pyproject.toml — or by callers passing '-m not integration'.
"""
from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest

REM_CLI = Path("/Users/ehsanfathi/Documents/repos/todo/bin/reminders-cli")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: tests that hit real macOS Reminders.app — excluded from unit-test runs",
    )


@pytest.fixture
def test_list_name():
    """Yield a unique 'GTD-TEST-<hex>' list name.

    Lifecycle:
      setup   — create the list in Reminders via reminders-cli new-list
      yield   — test body runs
      teardown (always) —
        1. enumerate all reminders in the list (including completed)
        2. delete each by externalId
        3. delete the list itself via osascript
    """
    name = f"GTD-TEST-{uuid.uuid4().hex[:8]}"

    # ── Setup ────────────────────────────────────────────────────────────────
    subprocess.run(
        [str(REM_CLI), "new-list", name],
        check=True,
        capture_output=True,
        timeout=15,
    )

    yield name

    # ── Teardown (always runs) ───────────────────────────────────────────────
    try:
        # Fetch all reminders (open + completed) in the test list.
        result = subprocess.run(
            [str(REM_CLI), "show", name, "--format=json", "--include-completed"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            try:
                items = json.loads(result.stdout)
                for item in items:
                    rid = item.get("externalId")
                    if rid:
                        subprocess.run(
                            [str(REM_CLI), "delete", name, rid],
                            capture_output=True,
                            timeout=10,
                        )
            except (json.JSONDecodeError, TypeError):
                pass
    except Exception:
        pass

    # Delete the list itself — reminders-cli has no delete-list subcommand,
    # so we use osascript.
    try:
        subprocess.run(
            ["osascript", "-e", f'tell application "Reminders" to delete list "{name}"'],
            capture_output=True,
            timeout=15,
        )
    except Exception:
        import warnings
        warnings.warn(f"Could not delete test Reminders list '{name}' — remove it manually.")
