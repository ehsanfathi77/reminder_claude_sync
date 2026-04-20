"""
Unit tests for state.connect schema-mismatch behavior (AC-TEST-7).

`state.connect()` opens an EXISTING DB and asserts its schema_version matches
the engine. A drift (e.g., loading an old DB after schema bump) MUST raise
RuntimeError, not silently return a connection that lets the engine read
NULLs from missing columns.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import gtd.engine.state as state_mod


def test_connect_to_nonexistent_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        state_mod.connect(tmp_path / "missing.db")


def test_connect_to_db_without_schema_table_raises(tmp_path):
    """A SQLite file that lacks `schema_version` table is treated as schema 0;
    must raise RuntimeError, not surface a confusing query error later."""
    db_path = tmp_path / "broken.db"
    # Create a file that's valid SQLite but has no schema_version table
    raw = sqlite3.connect(str(db_path))
    raw.execute("CREATE TABLE not_ours (x INTEGER)")
    raw.commit()
    raw.close()

    with pytest.raises(RuntimeError, match="schema version"):
        state_mod.connect(db_path)


def test_connect_to_db_with_old_schema_version_raises(tmp_path):
    """Simulate schema drift: insert a row with version 0; engine expects ≥1."""
    db_path = tmp_path / "old.db"
    raw = sqlite3.connect(str(db_path))
    raw.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT)")
    raw.execute("INSERT INTO schema_version (version, applied_at) VALUES (0, '2026-01-01')")
    raw.commit()
    raw.close()

    with pytest.raises(RuntimeError, match=r"schema version 0 < current \d"):
        state_mod.connect(db_path)


def test_init_db_then_connect_succeeds(tmp_path):
    """Sanity: the round-trip works — init_db produces a DB that connect accepts."""
    db_path = tmp_path / "good.db"
    conn = state_mod.init_db(db_path)
    conn.close()
    conn2 = state_mod.connect(db_path)
    try:
        # Smoke: schema_version row exists and matches
        version = conn2.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert version == state_mod.CURRENT_SCHEMA_VERSION
    finally:
        conn2.close()
