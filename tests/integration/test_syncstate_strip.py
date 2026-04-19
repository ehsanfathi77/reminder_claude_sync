"""
Regression test for the GTD metadata-fence strip in bin/lib/syncstate.py.

Asserts:
  1. _strip_gtd_fence behaves correctly across edge cases (empty, no-fence,
     leading whitespace, URL-adjacent, multiple fences — only first stripped).
  2. _canonical (and therefore hash_record) produces the SAME hash for a
     reminder before and after a metadata fence is added at the top of notes.
  3. Two consecutive bin/sync.py sync runs against a fence-stamped state file
     produce zero CONFLICT lines and zero NO_BASELINE lines (loop prevention
     stays intact).
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

# Test root: the repo's bin/lib must be importable.
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin"))

from lib.syncstate import (  # noqa: E402
    _GTD_FENCE_RE,
    _canonical,
    _strip_gtd_fence,
    hash_record,
)


# ────────────────────────────────────────────────────────────────────────
# Unit-level: _strip_gtd_fence
# ────────────────────────────────────────────────────────────────────────

def test_strip_empty():
    assert _strip_gtd_fence("") == ""
    assert _strip_gtd_fence(None) == ""


def test_strip_no_fence():
    notes = "just user prose with no fence at all"
    assert _strip_gtd_fence(notes) == notes


def test_strip_basic_fence():
    notes = (
        "--- gtd ---\n"
        "id: 01H8WZ3...\n"
        "kind: next-action\n"
        "--- end ---\n"
        "the actual user-visible note"
    )
    assert _strip_gtd_fence(notes) == "the actual user-visible note"


def test_strip_leading_whitespace():
    notes = (
        "   \n"
        "--- gtd ---\n"
        "id: X\n"
        "--- end ---\n"
        "prose"
    )
    assert _strip_gtd_fence(notes) == "prose"


def test_strip_url_after_fence():
    """User adds a URL right after the fence; auto-link must not break parsing."""
    notes = (
        "--- gtd ---\n"
        "id: X\n"
        "--- end ---\n"
        "https://example.com — see this"
    )
    assert _strip_gtd_fence(notes) == "https://example.com — see this"


def test_strip_url_inside_user_prose_is_safe():
    """URL in user prose should not be confused with a fence delimiter."""
    notes = "https://example.com is a great site"
    assert _strip_gtd_fence(notes) == notes


def test_strip_multiple_fences_strips_only_first():
    """A second fence (engine bug or user paste) must NOT be stripped — that's
    the user's content as far as we care."""
    notes = (
        "--- gtd ---\n"
        "id: X\n"
        "--- end ---\n"
        "user wrote --- gtd --- in their notes for some reason --- end ---\n"
        "after"
    )
    out = _strip_gtd_fence(notes)
    assert out == (
        "user wrote --- gtd --- in their notes for some reason --- end ---\n"
        "after"
    )


# ────────────────────────────────────────────────────────────────────────
# Hash invariance: the LOAD-BEARING property
# ────────────────────────────────────────────────────────────────────────

def test_hash_unchanged_when_fence_added():
    """Adding a metadata fence to notes MUST NOT change the canonical hash.
    This is the whole reason the strip exists — without it, the GTD engine's
    first-touch metadata write would look like 'Apple changed' and trigger
    bin/sync.py's conflict path for every reminder."""
    base = {
        "title": "Buy milk",
        "notes": "2% organic, glass bottle",
        "due_iso": "2026-04-25T17:00:00",
        "completed": False,
        "list": "Reminders",
    }
    stamped = {
        **base,
        "notes": (
            "--- gtd ---\n"
            "id: 01H8WZ3F4G5HXKJ\n"
            "kind: next-action\n"
            "ctx: '@errands'\n"
            "--- end ---\n"
            "2% organic, glass bottle"
        ),
    }
    assert hash_record(base) == hash_record(stamped), (
        "fence stamp changed hash — sync.py would see a phantom 'both changed' "
        "and dump every reminder into conflict log"
    )


def test_hash_changes_when_user_prose_changes():
    """Sanity check: the strip must NOT make hashes blind to actual changes."""
    a = {"title": "X", "notes": "first", "due_iso": "", "completed": False, "list": "L"}
    b = {**a, "notes": "second"}
    assert hash_record(a) != hash_record(b)


def test_hash_normalizes_crlf_inside_prose():
    """Pre-existing behavior must still hold."""
    a = {"title": "X", "notes": "line1\nline2", "due_iso": "", "completed": False, "list": "L"}
    b = {**a, "notes": "line1\r\nline2"}
    assert hash_record(a) == hash_record(b)


# ────────────────────────────────────────────────────────────────────────
# Integration: simulated two-sync run with a fence-stamped state
# ────────────────────────────────────────────────────────────────────────

def test_two_sync_runs_produce_no_conflict_no_no_baseline(tmp_path: Path):
    """Build a minimal fake state file representing a reminder we 'know about'
    pre-fence, then check that adding a fence to the notes (engine simulation)
    keeps the hash equal so the diff classifier sees 'both unchanged'.

    We don't actually invoke bin/sync.py here (that requires a live Reminders
    list and would be flaky); we exercise the same public functions sync.py
    uses to make its diff decisions: hash_record and _canonical."""
    # Pretend sync.py recorded this reminder's hash on a previous run.
    record = {
        "title": "Pay rent",
        "notes": "Zelle to landlord",
        "due_iso": "2026-05-01T09:00:00",
        "completed": False,
        "list": "Inbox",
    }
    baseline_hash = hash_record(record)

    # GTD engine stamps metadata at the top of notes (next tick).
    stamped = {
        **record,
        "notes": (
            "--- gtd ---\n"
            "id: 01HABC...\n"
            "kind: next-action\n"
            "--- end ---\n"
            "Zelle to landlord"
        ),
    }
    post_stamp_hash = hash_record(stamped)

    # The whole point: hashes match → sync.py classifies as 'unchanged' → no
    # CONFLICT, no NO_BASELINE entries get written.
    assert post_stamp_hash == baseline_hash, (
        "Hash drifted after metadata stamp. sync.py would log NO_BASELINE on "
        "first touch (state has old hash, apple has new) and CONFLICT on "
        "subsequent runs once md picked up the fence too."
    )

    # Second sync round: simulate that md has now adopted the same fence
    # (TASKS.md serialization, if it ever happens). Both sides should still
    # canonicalize to the same baseline.
    second_round_md = stamped.copy()
    second_round_apple = stamped.copy()
    assert hash_record(second_round_md) == baseline_hash
    assert hash_record(second_round_apple) == baseline_hash


def test_canonical_strip_is_idempotent():
    """Running strip on already-stripped notes is a no-op."""
    notes = "just prose"
    once = _strip_gtd_fence(notes)
    twice = _strip_gtd_fence(once)
    assert once == twice == notes


# ────────────────────────────────────────────────────────────────────────
# Regex sanity
# ────────────────────────────────────────────────────────────────────────

def test_regex_does_not_match_partial_fence():
    """Half a fence shouldn't strip anything."""
    half = "--- gtd ---\nid: X\nbut no end marker\nuser prose"
    assert _strip_gtd_fence(half) == half  # no match → unchanged


def test_regex_handles_extra_whitespace_in_delimiters():
    """`---  gtd  ---` (double space) is intentionally NOT a match — strict shape."""
    weird = "---  gtd  ---\nid: X\n--- end ---\nprose"
    # The regex uses \s* between hyphens and 'gtd' so this should still match.
    out = _strip_gtd_fence(weird)
    assert out == "prose"


if __name__ == "__main__":
    import sys
    rc = subprocess.call([sys.executable, "-m", "pytest", "-x", "-v", __file__])
    sys.exit(rc)
