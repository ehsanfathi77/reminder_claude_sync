"""
Tests for gtd/engine/observability.py — US-006 structured JSONL logging.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from gtd.engine.observability import STREAMS, log, tail


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_lines(path: Path) -> list[dict]:
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if raw:
            lines.append(json.loads(raw))
    return lines


# ---------------------------------------------------------------------------
# log() — basic correctness
# ---------------------------------------------------------------------------

class TestLogBasic:
    def test_log_to_each_stream(self, tmp_path: Path) -> None:
        """Log one line to each stream; verify file exists, parses, has ts and pid."""
        for stream in STREAMS:
            log(stream, log_dir=tmp_path, op="test", stream_name=stream)
            file = tmp_path / f"{stream}.jsonl"
            assert file.exists(), f"{stream}.jsonl not created"
            records = read_lines(file)
            assert len(records) == 1, f"Expected 1 line in {stream}.jsonl"
            rec = records[0]
            assert "ts" in rec, "Missing 'ts' field"
            assert "pid" in rec, "Missing 'pid' field"
            assert rec["op"] == "test"
            assert rec["stream_name"] == stream

    def test_invalid_stream_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Unknown stream"):
            log("bogus", log_dir=tmp_path)

    def test_log_dir_auto_created(self, tmp_path: Path) -> None:
        new_dir = tmp_path / "deeply" / "nested" / "dir"
        assert not new_dir.exists()
        log("engine", log_dir=new_dir, op="autocreate")
        assert new_dir.exists()
        assert (new_dir / "engine.jsonl").exists()

    def test_custom_log_dir_honored(self, tmp_path: Path) -> None:
        custom = tmp_path / "custom_logs"
        log("clarify", log_dir=custom, decision="auto")
        file = custom / "clarify.jsonl"
        assert file.exists()
        records = read_lines(file)
        assert records[0]["decision"] == "auto"


# ---------------------------------------------------------------------------
# Concurrent writes — atomicity
# ---------------------------------------------------------------------------

class TestConcurrentWrites:
    def test_100_concurrent_writes_no_interleave(self, tmp_path: Path) -> None:
        """100 writes from 5 threads → all 100 lines parse cleanly."""
        n_threads = 5
        writes_per_thread = 20
        total = n_threads * writes_per_thread

        def writer(thread_id: int) -> None:
            for i in range(writes_per_thread):
                log("engine", log_dir=tmp_path, thread=thread_id, seq=i)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        file = tmp_path / "engine.jsonl"
        raw_lines = file.read_text(encoding="utf-8").splitlines()
        assert len(raw_lines) == total, f"Expected {total} lines, got {len(raw_lines)}"

        for i, raw in enumerate(raw_lines):
            try:
                json.loads(raw)
            except json.JSONDecodeError as e:
                pytest.fail(f"Line {i} failed to parse: {e!r}\nContent: {raw!r}")


# ---------------------------------------------------------------------------
# tail()
# ---------------------------------------------------------------------------

class TestTail:
    def _write_n_lines(self, stream: str, n: int, log_dir: Path) -> None:
        for i in range(n):
            log(stream, log_dir=log_dir, seq=i)

    def test_tail_returns_last_n(self, tmp_path: Path) -> None:
        self._write_n_lines("qchannel", 25, tmp_path)
        result = tail("qchannel", n=10, log_dir=tmp_path)
        assert len(result) == 10
        # Last 10 lines should have seq 15..24
        seqs = [r["seq"] for r in result]
        assert seqs == list(range(15, 25))

    def test_tail_n_bigger_than_file(self, tmp_path: Path) -> None:
        self._write_n_lines("clarify", 7, tmp_path)
        result = tail("clarify", n=50, log_dir=tmp_path)
        assert len(result) == 7

    def test_tail_skips_malformed_line(self, tmp_path: Path) -> None:
        file = tmp_path / "invariants.jsonl"
        # Write 3 good lines, 1 bad, 2 more good
        lines = []
        for i in range(3):
            lines.append(json.dumps({"ts": "2026-01-01T00:00:00+00:00", "pid": 1, "seq": i}))
        lines.append("{NOT VALID JSON:::}")
        for i in range(3, 5):
            lines.append(json.dumps({"ts": "2026-01-01T00:00:00+00:00", "pid": 1, "seq": i}))
        file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = tail("invariants", n=10, log_dir=tmp_path)
        assert len(result) == 5  # 6 lines minus 1 malformed
        seqs = [r["seq"] for r in result]
        assert seqs == [0, 1, 2, 3, 4]

    def test_tail_nonexistent_stream_returns_empty(self, tmp_path: Path) -> None:
        result = tail("engine", n=10, log_dir=tmp_path)
        assert result == []

    def test_tail_invalid_stream_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Unknown stream"):
            tail("no_such_stream", log_dir=tmp_path)

    def test_tail_custom_log_dir(self, tmp_path: Path) -> None:
        custom = tmp_path / "alt"
        self._write_n_lines("engine", 5, custom)
        result = tail("engine", n=3, log_dir=custom)
        assert len(result) == 3
        seqs = [r["seq"] for r in result]
        assert seqs == [2, 3, 4]
