"""
notes_metadata.py — Parse and serialize the GTD-engine metadata block
stamped at the top of macOS Reminders' notes field.

Block shape:
    --- gtd ---
    id: 01H8WZ3...
    kind: next-action
    created: 2026-04-19T14:03-04:00
    ctx: '@home'
    project: 01HABC...
    delegate: Dan
    release: 2026-05-01
    --- end ---
    <user-visible prose>

Public API:
    parse_metadata(notes: str) -> tuple[dict, str]
    serialize_metadata(meta: dict, prose: str) -> str

See module docstrings on each function for full contracts.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Keep regex compatible with the one in bin/lib/syncstate.py so that stripping
# behaviour is identical across the two modules.
_GTD_FENCE_RE = re.compile(
    r"^\s*---\s*gtd\s*---.*?---\s*end\s*---\s*\n?",
    re.DOTALL,
)

# Stable key order for serialisation.  Extras are appended alphabetically.
_KEY_ORDER = ["id", "kind", "created", "ctx", "project", "delegate", "release"]

# 512-byte hard cap on the fenced block (fence lines + YAML content + closing
# fence line), encoded as UTF-8.
_MAX_BLOCK_BYTES = 512

# Default log path, relative to the repository root (resolved at call time so
# tests can monkeypatch it via the module attribute).
_INVARIANTS_LOG: Path | None = None  # override in tests via monkeypatch


def _log_path() -> Path:
    """Return the invariants log path, creating parent dirs as needed.

    Tests may set `notes_metadata._INVARIANTS_LOG` to a tmp_path file via
    monkeypatch; that value is always respected over the default.
    """
    import gtd.engine.notes_metadata as _self
    override = _self._INVARIANTS_LOG
    if override is not None:
        override.parent.mkdir(parents=True, exist_ok=True)
        return override
    # Default: anchor at the project root (two levels up from gtd/engine/).
    here = Path(__file__).resolve()
    root = here.parent.parent.parent  # gtd/engine/ -> gtd/ -> repo root
    log = root / ".gtd" / "log" / "invariants.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    return log


# ---------------------------------------------------------------------------
# Custom tiny YAML parser (flat key: value only — no nesting, no lists)
# ---------------------------------------------------------------------------

_SIMPLE_VALUE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*?)\s*$")


def _parse_flat_yaml(block: str) -> dict:
    """Parse a flat key: value YAML-like block.

    Supports:
      - Bare values:          key: value
      - Single-quoted values: key: 'value with spaces or @'
      - Double-quoted values: key: "value"
      - Empty values:         key:  (returns '')

    Raises ValueError if any line (that isn't blank or a comment) is
    structurally malformed.
    """
    result: dict = {}
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _SIMPLE_VALUE_RE.match(stripped)
        if m is None:
            raise ValueError(f"Unparseable YAML line: {stripped!r}")
        key, raw_value = m.group(1), m.group(2)
        # Unquote single or double quotes.
        if raw_value.startswith("'"):
            # Must end with a closing single quote (and have at least '' = 2 chars).
            if not raw_value.endswith("'") or len(raw_value) < 2:
                raise ValueError(f"Malformed quoted value on key {key!r}: {raw_value!r}")
            raw_value = raw_value[1:-1]
        elif raw_value.startswith('"'):
            if not raw_value.endswith('"') or len(raw_value) < 2:
                raise ValueError(f"Malformed quoted value on key {key!r}: {raw_value!r}")
            raw_value = raw_value[1:-1]
        result[key] = raw_value
    return result


def _needs_quoting(value: str) -> bool:
    """Return True if the value must be wrapped in single quotes."""
    if not value:
        return False
    # Quote if it starts with special YAML chars or contains a colon.
    return value[0] in ("@", '"', "'", "{", "[", "|", ">", "&", "*", "!", "%") or ":" in value


def _serialize_flat_yaml(meta: dict) -> str:
    """Serialise meta dict to flat YAML lines in stable key order."""
    lines: list[str] = []
    # Emit keys in canonical order first, then extras alphabetically.
    ordered_keys = [k for k in _KEY_ORDER if k in meta]
    extra_keys = sorted(k for k in meta if k not in _KEY_ORDER)
    for key in ordered_keys + extra_keys:
        value = str(meta[key])
        if _needs_quoting(value):
            # Escape embedded single quotes by doubling them (YAML convention).
            escaped = value.replace("'", "''")
            lines.append(f"{key}: '{escaped}'")
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class MetadataTooLargeError(ValueError):
    """Raised when the serialised fenced block exceeds 512 bytes (UTF-8)."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_metadata(notes: str) -> tuple[dict, str]:
    """Parse the leading GTD-engine metadata fence from *notes*.

    Returns:
        (meta_dict, prose_remainder)

    Behaviour:
      - No fence, empty input, or completely absent fence → ({}, notes_or_empty).
      - Fence found but YAML inside is malformed → ({}, notes) and appends a
        JSONL line to .gtd/log/invariants.jsonl with
        kind='metadata_parse_error' and a sample of the bad block.
      - Fence found and YAML is valid → (parsed_dict, prose_after_fence).
    """
    if not notes:
        return {}, ""

    m = _GTD_FENCE_RE.match(notes)
    if m is None:
        return {}, notes

    full_match = m.group(0)
    prose = notes[m.end():]

    # Extract the YAML content between the two fence markers.
    inner_re = re.compile(
        r"^\s*---\s*gtd\s*---\s*\n(.*?)\n?\s*---\s*end\s*---",
        re.DOTALL,
    )
    inner_m = inner_re.match(full_match.rstrip())
    if inner_m is None:
        # Fence matched outer RE but has no inner content to extract — treat as
        # malformed.
        yaml_block = ""
    else:
        yaml_block = inner_m.group(1)

    try:
        meta = _parse_flat_yaml(yaml_block)
    except ValueError as exc:
        # Log the invariant violation and return graceful empty dict.
        _append_parse_error(notes, str(exc))
        return {}, notes

    return meta, prose


def serialize_metadata(meta: dict, prose: str) -> str:
    """Produce a fenced GTD metadata block prepended to *prose*.

    Format:
        --- gtd ---
        <stable-key-order YAML>
        --- end ---
        <prose>

    Rules:
      - Empty meta → returns *prose* unchanged (no fence written).
      - Stable key order: id, kind, created, ctx, project, delegate, release,
        then any extras alphabetically.
      - 512-byte UTF-8 hard cap on the fenced block (fence + content only, not
        trailing prose). Oversize raises MetadataTooLargeError.
    """
    if not meta:
        return prose

    yaml_content = _serialize_flat_yaml(meta)
    fenced_block = f"--- gtd ---\n{yaml_content}\n--- end ---\n"

    if len(fenced_block.encode("utf-8")) > _MAX_BLOCK_BYTES:
        raise MetadataTooLargeError(
            f"Serialised metadata block is {len(fenced_block.encode('utf-8'))} bytes "
            f"(limit {_MAX_BLOCK_BYTES}). Reduce metadata fields."
        )

    return fenced_block + prose


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _append_parse_error(notes: str, reason: str) -> None:
    """Append a JSONL invariant-violation record for a metadata parse failure."""
    log = _log_path()
    sample = notes[:200]  # cap sample to 200 chars
    record = {
        "ts": datetime.now().replace(microsecond=0).isoformat(),
        "kind": "metadata_parse_error",
        "reason": reason,
        "sample": sample,
    }
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
