"""
observability.py — append-only JSONL logging across 4 named streams.

Streams: engine, qchannel, clarify, invariants.
Files: <log_dir>/<stream>.jsonl (default log_dir = ~/Documents/repos/todo/.gtd/log).

Every line auto-includes 'ts' (ISO local with TZ) and 'pid'. Concurrent writes
from multiple processes (sync.py, supernote-sync, gtd-engine) won't interleave
because we use O_APPEND with line-buffered writes — POSIX guarantees atomic
appends ≤ PIPE_BUF (typically 4KB) per write.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

STREAMS: tuple[str, ...] = ("engine", "qchannel", "clarify", "invariants", "clarifier")
DEFAULT_LOG_DIR = Path.home() / "Documents/repos/todo/.gtd/log"


def log(
    stream: str,
    *,
    log_dir: Path | None = None,
    **fields: Any,
) -> None:
    """Append a single JSONL line to <log_dir>/<stream>.jsonl.

    Auto-adds 'ts' (ISO local YYYY-MM-DDTHH:MM:SS+ZZ:ZZ) and 'pid' to fields.
    Raises ValueError if stream not in STREAMS.
    Creates log_dir if missing.
    """
    if stream not in STREAMS:
        raise ValueError(f"Unknown stream {stream!r}. Must be one of: {STREAMS}")

    resolved = log_dir if log_dir is not None else DEFAULT_LOG_DIR
    resolved.mkdir(parents=True, exist_ok=True)

    record = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "pid": os.getpid(),
        **fields,
    }

    line = json.dumps(record, ensure_ascii=False) + "\n"
    encoded = line.encode("utf-8")

    path = resolved / f"{stream}.jsonl"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, encoded)
    finally:
        os.close(fd)


def tail(stream: str, n: int = 50, log_dir: Path | None = None) -> list[dict]:
    """Read the last n lines of a stream as parsed dicts.

    Used by dryrun-report and health commands.
    Skips any malformed line and continues.
    """
    if stream not in STREAMS:
        raise ValueError(f"Unknown stream {stream!r}. Must be one of: {STREAMS}")

    resolved = log_dir if log_dir is not None else DEFAULT_LOG_DIR
    path = resolved / f"{stream}.jsonl"

    if not path.exists():
        return []

    with open(path, "r", encoding="utf-8") as f:
        raw_lines = f.readlines()

    last_n = raw_lines[-n:] if n < len(raw_lines) else raw_lines

    result: list[dict] = []
    for raw in last_n:
        raw = raw.strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
            result.append(parsed)
        except json.JSONDecodeError:
            continue

    return result
