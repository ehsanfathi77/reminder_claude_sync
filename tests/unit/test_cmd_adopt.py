"""
Unit tests for cmd_adopt and _adopt_apply (AC-TEST-1..5, AC-TEST-9, AC-TEST-12, AC-TEST-14).

These tests cover the silent-success class of regressions in the adoption
flow — the kind where the CLI prints "moved 5 items" but no items actually
moved (because of mocked-too-deep tests, swallowed exceptions, or an UPDATE
that affected zero rows).

Conventions:
- Use a real on-disk SQLite (`tmp_path`) instead of mocking the DB layer.
- Use a recording stub for `bin.lib.reminders` that asserts move_to_list
  was called with the expected args (catches "no actual move").
- Stub the lock and config so tests don't need real .gtd/ infrastructure.
"""
from __future__ import annotations

import io
import sys
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import gtd.engine.cli as cli_mod
import gtd.engine.state as state_mod


# ---------------------------------------------------------------------------
# Recording stub for bin.lib.reminders
# ---------------------------------------------------------------------------

class StubReminders:
    """Records every move_to_list call. Optional injected failure for partial-failure tests."""

    def __init__(self, *, fail_on_rids: set[str] | None = None):
        self.move_calls: list[tuple[str, str]] = []
        self.fail_on_rids = fail_on_rids or set()

    def move_to_list(self, rid: str, list_name: str) -> None:
        self.move_calls.append((rid, list_name))
        if rid in self.fail_on_rids:
            raise RuntimeError(f"injected failure for {rid}")

    def list_all(self, *args, **kwargs):
        return []


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def stub_lock(monkeypatch, tmp_path):
    @contextmanager
    def fake_acquire(path, *, holder_argv0="gtd-engine", timeout_s=60.0):
        yield

    monkeypatch.setattr("gtd.engine.cli.LOCK_PATH", tmp_path / "engine.lock")
    try:
        import gtd.engine.lock as lock_mod
        monkeypatch.setattr(lock_mod, "acquire", fake_acquire)
    except ImportError:
        pass


@pytest.fixture(autouse=True)
def stub_config(monkeypatch, tmp_path):
    cfg = {
        "dispatch_dryrun": False,
        "flip_at_iso": None,
        "managed_lists": None,
        "quiet_hours": [22, 8],
        "q_max_open": 3,
        "q_max_per_day": 8,
    }
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg.copy())
    monkeypatch.setattr(cli_mod, "save_config", lambda c: None)
    monkeypatch.setattr(cli_mod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(cli_mod, "LOG_DIR", tmp_path / "log")
    # Force effective_dryrun to return False so apply phase actually executes.
    # This bypasses the v1 7-day gate for these specific tests.
    monkeypatch.setattr(cli_mod, "effective_dryrun", lambda cfg: False)


@pytest.fixture
def real_db(monkeypatch, tmp_path):
    """Provide a real SQLite DB at a tmp path."""
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(cli_mod, "STATE_DB", db_path)
    conn = state_mod.init_db(db_path)
    conn.close()
    return db_path


def _install_stub_reminders(monkeypatch, stub) -> None:
    """Place `stub` at sys.modules['bin.lib.reminders'] AND as the
    `reminders` attribute on the `bin.lib` package module.

    Setting only sys.modules is not enough once another test in the same
    session has imported the real `bin.lib.reminders`: subsequent
    `import bin.lib.reminders as R` statements resolve via the package's
    attribute table, not sys.modules. Patching both surfaces is required
    for cross-file test isolation.
    """
    import types
    if "bin" not in sys.modules:
        bin_mod = types.ModuleType("bin")
        bin_mod.__path__ = []
        monkeypatch.setitem(sys.modules, "bin", bin_mod)
    if "bin.lib" not in sys.modules:
        lib_mod = types.ModuleType("bin.lib")
        lib_mod.__path__ = []
        monkeypatch.setitem(sys.modules, "bin.lib", lib_mod)
    monkeypatch.setitem(sys.modules, "bin.lib.reminders", stub)
    # Also patch the parent package's attribute, since `import bin.lib.reminders`
    # binds via attribute lookup, not sys.modules, when the parent is loaded.
    monkeypatch.setattr(sys.modules["bin.lib"], "reminders", stub, raising=False)


@pytest.fixture
def stub_reminders(monkeypatch):
    """Install a StubReminders instance at every cli.py import site."""
    stub = StubReminders()
    _install_stub_reminders(monkeypatch, stub)
    return stub


def _run_apply(monkeypatch, jsonl_input: str, *, dry_run: bool = False) -> tuple[int, str, str]:
    """Invoke main(['adopt', '--apply']) with stdin = jsonl_input. Returns
    (exit_code, stdout, stderr)."""
    monkeypatch.setattr("sys.stdin", io.StringIO(jsonl_input))
    # Pretend stdin is a pipe (not a TTY) so apply reads it
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    out = io.StringIO()
    err = io.StringIO()
    argv = ["adopt", "--apply"]
    if dry_run:
        argv = ["--dry-run"] + argv
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_mod.main(argv)
    return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# AC-TEST-1: success path moves AND persists state
# ---------------------------------------------------------------------------

def test_adopt_apply_success_calls_move_and_persists_state(
    monkeypatch, real_db, stub_reminders
):
    """A clean batch of two decisions: each rid moves, each gets a state row."""
    payload = (
        '{"rid": "RID-A", "target_list": "@home"}\n'
        '{"rid": "RID-B", "target_list": "@errands"}\n'
    )
    rc, out, err = _run_apply(monkeypatch, payload)

    assert rc == 0, f"unexpected exit; stderr={err!r}"
    assert ("RID-A", "@home") in stub_reminders.move_calls
    assert ("RID-B", "@errands") in stub_reminders.move_calls
    assert len(stub_reminders.move_calls) == 2

    conn = state_mod.connect(real_db)
    try:
        a = state_mod.get_item_by_rid(conn, "RID-A")
        b = state_mod.get_item_by_rid(conn, "RID-B")
    finally:
        conn.close()

    assert a is not None and a["kind"] == "next_action" and a["list"] == "@home" and a["ctx"] == "@home"
    assert b is not None and b["kind"] == "next_action" and b["list"] == "@errands" and b["ctx"] == "@errands"
    assert "moved=2 errors=0" in out


# ---------------------------------------------------------------------------
# AC-TEST-2: partial failure (Reminders move raises mid-batch)
# ---------------------------------------------------------------------------

def test_adopt_apply_partial_failure_continues_and_counts_errors(
    monkeypatch, real_db
):
    """First move succeeds, second raises, third still attempted."""
    stub = StubReminders(fail_on_rids={"RID-FAIL"})
    _install_stub_reminders(monkeypatch, stub)

    payload = (
        '{"rid": "RID-OK1", "target_list": "@home"}\n'
        '{"rid": "RID-FAIL", "target_list": "@errands"}\n'
        '{"rid": "RID-OK2", "target_list": "@calls"}\n'
    )
    rc, out, err = _run_apply(monkeypatch, payload)

    # Returns 1 because errors > 0
    assert rc == 1
    # All three were attempted
    attempted_rids = [c[0] for c in stub.move_calls]
    assert "RID-OK1" in attempted_rids
    assert "RID-FAIL" in attempted_rids
    assert "RID-OK2" in attempted_rids
    assert "moved=2 errors=1" in out
    assert "RID-FAIL" in err


# ---------------------------------------------------------------------------
# AC-TEST-3 & AC-TEST-9: invalid target rejects ENTIRE batch before any move
# ---------------------------------------------------------------------------

def test_adopt_apply_invalid_target_rejects_entire_batch(
    monkeypatch, real_db, stub_reminders
):
    """Inbox is intentionally NOT in _ADOPT_TARGETS_BY_KIND."""
    payload = (
        '{"rid": "RID-A", "target_list": "@home"}\n'
        '{"rid": "RID-B", "target_list": "Inbox"}\n'
    )
    rc, out, err = _run_apply(monkeypatch, payload)

    assert rc == 2
    assert "not an adoptable managed list" in err or "outside DEFAULT_MANAGED_LISTS" in err
    # Crucially: NO moves happened — validation runs before the first call
    assert stub_reminders.move_calls == []


def test_adopt_apply_unknown_target_list_rejected(
    monkeypatch, real_db, stub_reminders
):
    """Decision targets a list that doesn't exist anywhere in the GTD set."""
    payload = '{"rid": "X", "target_list": "@nonexistent"}\n'
    rc, _out, err = _run_apply(monkeypatch, payload)
    assert rc == 2
    assert stub_reminders.move_calls == []


# ---------------------------------------------------------------------------
# AC-TEST-4: dry-run never opens DB or calls Reminders
# ---------------------------------------------------------------------------

def test_adopt_apply_dry_run_no_db_no_reminders_writes(
    monkeypatch, real_db, stub_reminders
):
    payload = (
        '{"rid": "RID-A", "target_list": "@home"}\n'
        '{"rid": "RID-B", "target_list": "@errands"}\n'
    )
    rc, out, err = _run_apply(monkeypatch, payload, dry_run=True)

    assert rc == 0
    assert stub_reminders.move_calls == [], "dry-run must NOT call move_to_list"
    assert "dryrun: would move RID-A → @home" in out
    assert "dryrun: would move RID-B → @errands" in out
    # State.db not opened — inserts/updates not present
    conn = state_mod.connect(real_db)
    try:
        assert state_mod.get_item_by_rid(conn, "RID-A") is None
        assert state_mod.get_item_by_rid(conn, "RID-B") is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# AC-TEST-5: malformed JSON aborts at parse, before any move
# ---------------------------------------------------------------------------

def test_adopt_apply_malformed_json_aborts_at_parse(
    monkeypatch, real_db, stub_reminders
):
    payload = (
        '{"rid": "RID-A", "target_list": "@home"}\n'
        'this is not json\n'
        '{"rid": "RID-B", "target_list": "@errands"}\n'
    )
    rc, _out, err = _run_apply(monkeypatch, payload)
    assert rc == 2
    assert "invalid JSON" in err
    assert stub_reminders.move_calls == [], "parse failure must short-circuit before any move"


def test_adopt_apply_missing_required_keys_rejected(
    monkeypatch, real_db, stub_reminders
):
    """JSON line missing 'target_list' is rejected up-front."""
    payload = '{"rid": "RID-A"}\n'
    rc, _out, err = _run_apply(monkeypatch, payload)
    assert rc == 2
    assert "expected object with 'rid' and 'target_list'" in err
    assert stub_reminders.move_calls == []


# ---------------------------------------------------------------------------
# AC-TEST-12: UPDATE branch (existing rid) actually updates the row
# ---------------------------------------------------------------------------

def test_adopt_apply_existing_rid_update_branch_actually_updates_row(
    monkeypatch, real_db, stub_reminders
):
    """When state.db already has a row for the rid, the UPDATE branch fires.
    Verify cursor.rowcount == 1 succeeds (row found) and the post-state matches."""
    # Pre-seed the DB
    conn = state_mod.connect(real_db)
    try:
        state_mod.insert_item(conn, rid="RID-X", kind="unclarified", list="Personal")
    finally:
        conn.close()

    payload = '{"rid": "RID-X", "target_list": "@computer"}\n'
    rc, out, err = _run_apply(monkeypatch, payload)

    assert rc == 0
    assert ("RID-X", "@computer") in stub_reminders.move_calls

    conn = state_mod.connect(real_db)
    try:
        row = state_mod.get_item_by_rid(conn, "RID-X")
    finally:
        conn.close()
    assert row["kind"] == "next_action"
    assert row["list"] == "@computer"
    assert row["ctx"] == "@computer"


# ---------------------------------------------------------------------------
# AC-TEST-14: rowcount == 0 (rid doesn't match any state row) → error, NOT silent
# ---------------------------------------------------------------------------

def test_adopt_apply_unmatched_rid_increments_errors_not_silent_success(
    monkeypatch, real_db, stub_reminders
):
    """The UPDATE path runs when state.get_item_by_rid returns truthy, but
    the actual UPDATE matches 0 rows (e.g., schema drift, concurrent delete).
    Must increment errors and warn, not silently report success.

    We simulate this by stubbing get_item_by_rid to return a fake row while
    the real UPDATE finds nothing (rid not in table).
    """
    payload = '{"rid": "RID-PHANTOM", "target_list": "@home"}\n'

    real_get = state_mod.get_item_by_rid
    def fake_get(conn, rid):
        # Pretend the row exists so the UPDATE branch is taken
        return {"gtd_id": "fake", "rid": rid, "kind": "unclarified", "list": "Personal"}
    monkeypatch.setattr(state_mod, "get_item_by_rid", fake_get)

    rc, out, err = _run_apply(monkeypatch, payload)

    # rowcount == 0 → counted as error, exit 1
    assert rc == 1, f"expected exit 1 but got {rc}; stderr={err!r}"
    assert "no row for rid" in err.lower() or "no row" in err.lower()
    assert "moved=0 errors=1" in out
    # The Reminders move DID happen (move_to_list was called before the UPDATE)
    assert ("RID-PHANTOM", "@home") in stub_reminders.move_calls


# ---------------------------------------------------------------------------
# Suggest phase + discovery
# ---------------------------------------------------------------------------

def test_adopt_suggest_emits_jsonl_with_required_keys(monkeypatch, real_db):
    """Wire stubs so the suggest phase emits one JSON line per item.
    Each line MUST contain rid/name/body/source_list keys."""
    import json as _json

    class StubR:
        def __init__(self):
            self.items = [
                SimpleNamespace(id="RID-1", name="Pay bill", body="", list="MyList", completed=False),
                SimpleNamespace(id="RID-2", name="Call mom", body="due Tue", list="MyList", completed=False),
                SimpleNamespace(id="RID-3", name="Done item", body="", list="MyList", completed=True),
            ]

        def list_all(self, *a, **kw):
            return list(self.items)

    _install_stub_reminders(monkeypatch, StubR())

    # Stub bootstrap.existing_lists to include MyList
    import gtd.engine.bootstrap as boot_mod
    monkeypatch.setattr(boot_mod, "existing_lists", lambda **kw: {"MyList", "Inbox", "@home"})

    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_mod.main(["adopt", "--confirm-list", "MyList"])

    assert rc == 0
    lines = [l for l in out.getvalue().splitlines() if l.strip()]
    assert len(lines) == 2, f"expected 2 open items, got: {lines}"
    parsed = [_json.loads(l) for l in lines]
    for obj in parsed:
        assert set(obj.keys()) >= {"rid", "name", "body", "source_list"}
        assert obj["source_list"] == "MyList"
    rids = {p["rid"] for p in parsed}
    assert rids == {"RID-1", "RID-2"}
    # Header to stderr — confirms stdout/stderr separation (AC-UX-11)
    assert "Valid target_list values" in err.getvalue()


def test_adopt_confirm_list_unknown_exits_2_with_legacy_list_hint(monkeypatch):
    """Unknown list name → exit 2 + helpful list of legacy candidates."""
    import gtd.engine.bootstrap as boot_mod
    monkeypatch.setattr(boot_mod, "existing_lists", lambda **kw: {
        "Inbox", "@home", "Personal", "Books to Read",
    })

    err_buf = io.StringIO()
    out_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = cli_mod.main(["adopt", "--confirm-list", "Typo"])

    err = err_buf.getvalue()
    assert rc == 2
    assert "Typo" in err
    assert "not found" in err
    # Hint must list legacy candidates so user can correct
    assert "Personal" in err and "Books to Read" in err
