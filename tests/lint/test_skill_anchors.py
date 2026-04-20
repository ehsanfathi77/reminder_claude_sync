"""
Lint: shim → SKILL.md anchor consistency.

Each agent-mediated /gtd:<cmd> shim file in .claude/commands/gtd/ may include
a literal pointer of the form `Claude's job for /gtd:<cmd>`. For every such
pointer found, this test asserts there's a matching `### Claude's job for
/gtd:<cmd>` H3 heading in skills/gtd/SKILL.md.

Goal: prevent silent drift between the slash-command shim layer (loaded by
the slash-command parser) and the canonical runbook (loaded by the gtd skill
trigger). Without this lint, a future rename in either file leaves the other
referencing a phantom anchor.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SHIMS_DIR = REPO_ROOT / ".claude" / "commands" / "gtd"
SKILL_MD = REPO_ROOT / "skills" / "gtd" / "SKILL.md"

# Match `Claude's job for /gtd:<cmd>` (with curly or straight apostrophe)
_POINTER_RE = re.compile(r"Claude[\u2019']s job for /gtd:([a-z][a-z0-9-]*)")
# Match the H3 heading; same apostrophe tolerance.
_HEADING_RE = re.compile(
    r"^###\s+Claude[\u2019']s job for /gtd:([a-z][a-z0-9-]*)\s*$",
    re.MULTILINE,
)


def _collect_shim_pointers() -> dict[str, set[str]]:
    """Return {shim_path: {cmd_name, ...}} for every shim that references
    a Claude's-job anchor."""
    pointers: dict[str, set[str]] = {}
    if not SHIMS_DIR.exists():
        return pointers
    for shim in sorted(SHIMS_DIR.glob("*.md")):
        text = shim.read_text(encoding="utf-8")
        cmds = set(_POINTER_RE.findall(text))
        if cmds:
            pointers[str(shim.relative_to(REPO_ROOT))] = cmds
    return pointers


def _collect_skill_anchors() -> set[str]:
    """Return the set of /gtd:<cmd> names that have a `### Claude's job ...` H3."""
    if not SKILL_MD.exists():
        return set()
    return set(_HEADING_RE.findall(SKILL_MD.read_text(encoding="utf-8")))


def test_skill_md_exists():
    assert SKILL_MD.exists(), f"Expected {SKILL_MD} to exist"


def test_at_least_one_anchor_defined():
    """Sanity: SKILL.md should declare at least one Claude's-job anchor.

    If this fails, the restructure was reverted or never landed.
    """
    anchors = _collect_skill_anchors()
    assert anchors, (
        "No `### Claude's job for /gtd:<cmd>` H3 headings found in SKILL.md. "
        "The agent-mediated runbooks are missing."
    )


def test_every_shim_pointer_resolves():
    """Every `Claude's job for /gtd:<cmd>` reference in a shim file MUST
    correspond to a real `### Claude's job for /gtd:<cmd>` heading in SKILL.md.

    Catches drift like: shim renames `/gtd:adopt` → `/gtd:onboard` but
    SKILL.md still has the old anchor (or vice-versa).
    """
    pointers = _collect_shim_pointers()
    anchors = _collect_skill_anchors()

    failures: list[str] = []
    for shim_path, cmds in sorted(pointers.items()):
        for cmd in sorted(cmds):
            if cmd not in anchors:
                failures.append(f"  {shim_path} → /gtd:{cmd}  (no anchor in SKILL.md)")

    if failures:
        anchor_list = ", ".join(sorted(anchors)) or "(none)"
        pytest.fail(
            "Shim files reference Claude's-job anchors that do NOT exist in SKILL.md:\n"
            + "\n".join(failures)
            + f"\nAnchors currently defined in SKILL.md: {anchor_list}"
        )


def test_no_orphan_anchors():
    """Soft check: every anchor in SKILL.md SHOULD be referenced by at least
    one shim. An orphan anchor isn't strictly broken (the gtd skill itself
    auto-loads SKILL.md so the agent can still find it), but it suggests the
    shim was forgotten.

    Marked xfail-style: emit a warning but do not fail the build, since
    SKILL.md may legitimately document Claude's job for an agent-only flow
    that has no dedicated slash command.
    """
    pointers = _collect_shim_pointers()
    referenced = {cmd for cmds in pointers.values() for cmd in cmds}
    anchors = _collect_skill_anchors()

    orphans = sorted(anchors - referenced)
    if orphans:
        # Soft signal — print but don't fail.
        print(
            "\nINFO: SKILL.md anchors with no shim pointer (may be intentional): "
            + ", ".join(orphans)
        )
