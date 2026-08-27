"""Contract tests for the single durable JSONL ledger append path."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from framework.autonomous import jsonl_ledger
from framework.autonomous.jsonl_ledger import (
    LedgerLockTimeout,
    append_jsonl,
    encode_line,
    ledger_lock,
    ledger_lock_path,
)


REPO_ROOT = Path(__file__).resolve().parents[2]

# Long enough that O_APPEND alone stops being atomic (PIPE_BUF is 4096 on
# Linux), which is exactly the case framework_change_log.jsonl and
# orchestrator_log.jsonl already hit on disk.
TEARING_PAYLOAD_BYTES = 9000
TEARING_WORKERS = 6
TEARING_ROWS_PER_WORKER = 12

_CONCURRENT_WRITER = """
import sys
sys.path.insert(0, {repo!r})
from framework.autonomous.jsonl_ledger import append_jsonl

path, worker, rows, payload_bytes = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
for index in range(rows):
    append_jsonl(path, {{
        "worker": worker,
        "index": index,
        "payload": "x" * payload_bytes,
    }})
"""


def _legacy_line(row: dict) -> str:
    """The exact expression every call site inlined before this module existed."""
    return json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"


class TestEncoding(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_encode_line_matches_legacy_inline_json_dumps(self) -> None:
        rows = [
            {"b": 1, "a": 2},
            {"summary": "中文 unicode", "impact": None, "nested": {"z": [1, 2], "a": True}},
            {"payload": {}, "ts": "2026-08-27T00:00:00+00:00", "float": 1.5},
            {"quote": 'he said "hi"', "backslash": "a\\b", "newline": "a\nb"},
        ]
        for row in rows:
            with self.subTest(row=row):
                self.assertEqual(encode_line(row), _legacy_line(row))

    def test_appended_bytes_match_legacy_writer_byte_for_byte(self) -> None:
        rows = [
            {"schema_version": 1, "actor": "auto", "evidence": {"status_rows": [{"path": "a", "status": "M"}]}},
            {"event": "issued", "payload": {}, "pid": 4242, "ts": "2026-08-27T10:00:00"},
            {"non_ascii": "回测", "list": ["b", "a"]},
        ]

        new_path = self.root / "new.jsonl"
        for row in rows:
            append_jsonl(new_path, row)

        legacy_path = self.root / "legacy.jsonl"
        with legacy_path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

        self.assertEqual(new_path.read_bytes(), legacy_path.read_bytes())

    def test_append_returns_the_line_it_wrote(self) -> None:
        path = self.root / "ledger.jsonl"
        line = append_jsonl(path, {"a": 1})
        self.assertEqual(line, '{"a": 1}\n')
        self.assertEqual(path.read_text(encoding="utf-8"), line)

    def test_non_serialisable_row_raises_and_writes_nothing(self) -> None:
        path = self.root / "ledger.jsonl"
        with self.assertRaises(TypeError):
            append_jsonl(path, {"bad": object()})
        self.assertFalse(path.exists())

    def test_parent_directories_are_created(self) -> None:
        path = self.root / "deep" / "nested" / "ledger.jsonl"
        append_jsonl(path, {"a": 1})
        self.assertTrue(path.exists())


class TestDurability(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _record_fsync(self):
        calls: list[int] = []
        real_fsync = os.fsync

        def spy(fd: int) -> None:
            calls.append(fd)
            real_fsync(fd)

        return calls, patch.object(jsonl_ledger.os, "fsync", spy)

    def test_append_fsyncs_the_ledger_file(self) -> None:
        path = self.root / "ledger.jsonl"
        path.write_text("", encoding="utf-8")  # exists already: no directory fsync expected
        calls, patcher = self._record_fsync()
        with patcher:
            append_jsonl(path, {"a": 1})
        self.assertEqual(len(calls), 1, "expected exactly one fsync of the ledger fd")

    def test_creating_the_ledger_also_fsyncs_the_directory(self) -> None:
        path = self.root / "ledger.jsonl"
        calls, patcher = self._record_fsync()
        with patcher:
            append_jsonl(path, {"a": 1})
        # File fd + parent directory fd: the fsync on a new file does not make
        # its directory entry durable on its own.
        self.assertEqual(len(calls), 2)

    def test_fsync_dir_flag_forces_directory_fsync_on_existing_ledger(self) -> None:
        path = self.root / "ledger.jsonl"
        append_jsonl(path, {"a": 1})
        calls, patcher = self._record_fsync()
        with patcher:
            append_jsonl(path, {"a": 2}, fsync_dir=True)
        self.assertEqual(len(calls), 2)


class TestLocking(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_lock_path_matches_the_pre_existing_sidecar_convention(self) -> None:
        # run_recorder / research_question used path.with_suffix(suffix + ".lock")
        # inline; the shared writer must target the identical file so a mixed
        # old/new pair of processes still excludes each other.
        path = self.root / "research_question_events.jsonl"
        self.assertEqual(
            ledger_lock_path(path),
            path.with_suffix(path.suffix + ".lock"),
        )

    def test_outer_lock_holder_can_append_without_deadlock(self) -> None:
        path = self.root / "ledger.jsonl"
        with ledger_lock(path):
            append_jsonl(path, {"a": 1}, lock_held=True)
            append_jsonl(path, {"a": 2}, lock_held=True)
        self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 2)

    def test_outer_lock_holder_forgetting_the_flag_times_out_instead_of_hanging(self) -> None:
        # flock is per open file description, so a second acquire inside the
        # same process would block forever. The bounded wait turns that
        # programming error into a visible failure.
        path = self.root / "ledger.jsonl"
        with ledger_lock(path):
            with self.assertRaises(LedgerLockTimeout):
                append_jsonl(path, {"a": 1}, lock_timeout=0.2)

    def test_lock_is_released_when_the_body_raises(self) -> None:
        path = self.root / "ledger.jsonl"
        with self.assertRaises(RuntimeError):
            with ledger_lock(path):
                raise RuntimeError("boom")
        append_jsonl(path, {"a": 1}, lock_timeout=1.0)
        self.assertEqual(path.read_text(encoding="utf-8"), '{"a": 1}\n')

    def test_concurrent_processes_writing_long_rows_never_tear_a_line(self) -> None:
        path = self.root / "ledger.jsonl"
        script = self.root / "writer.py"
        script.write_text(_CONCURRENT_WRITER.format(repo=str(REPO_ROOT)), encoding="utf-8")

        procs = [
            subprocess.Popen(
                [
                    sys.executable,
                    str(script),
                    str(path),
                    f"w{worker}",
                    str(TEARING_ROWS_PER_WORKER),
                    str(TEARING_PAYLOAD_BYTES),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for worker in range(TEARING_WORKERS)
        ]
        for proc in procs:
            _, err = proc.communicate(timeout=180)
            self.assertEqual(proc.returncode, 0, err.decode("utf-8", "replace"))

        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), TEARING_WORKERS * TEARING_ROWS_PER_WORKER)
        seen = set()
        for line in lines:
            self.assertGreater(len(line.encode("utf-8")), 4096, "row must exceed PIPE_BUF to be a real test")
            row = json.loads(line)  # a torn line raises here
            self.assertEqual(len(row["payload"]), TEARING_PAYLOAD_BYTES)
            seen.add((row["worker"], row["index"]))
        self.assertEqual(len(seen), TEARING_WORKERS * TEARING_ROWS_PER_WORKER)


class TestCallSiteWiring(unittest.TestCase):
    """The ten ledger append sites must not re-grow their own open("a") writers."""

    CALL_SITES = (
        "framework/autonomous/framework_change_recorder.py",
        "framework/autonomous/run_recorder.py",
        "framework/autonomous/execution_result_ledger.py",
        "framework/autonomous/controller_owner.py",
        "framework/autonomous/verification_tool.py",
        "framework/evaluation/research_question.py",
        "framework/evaluation/pending_research_input.py",
        "scripts/quant_access_guard.py",
        "scripts/validate_truth_sync.py",
        "scripts/research_queue_runner.py",
    )

    def test_every_ledger_call_site_uses_the_shared_writer(self) -> None:
        for rel_path in self.CALL_SITES:
            with self.subTest(path=rel_path):
                source = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
                self.assertIn("append_jsonl", source)

    def test_no_call_site_still_inlines_the_legacy_jsonl_dumps_append(self) -> None:
        for rel_path in self.CALL_SITES:
            with self.subTest(path=rel_path):
                source = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
                self.assertNotIn(
                    'json.dumps(row, ensure_ascii=False, sort_keys=True)',
                    source,
                    "ledger rows must be encoded by jsonl_ledger.encode_line only",
                )


if __name__ == "__main__":
    unittest.main()
