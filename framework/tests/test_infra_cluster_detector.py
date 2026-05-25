"""Tests for the infrastructure cluster detector.

User mandate (2026-05-25): when the same infra failure signature appears
in two consecutive queue tasks, auto-pause to stop burning compute.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import framework.autonomous.infra_cluster_detector as detector


def _write_log(run_dir: Path, traceback_tail: str) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "auto_pipeline.log").write_text(
        "[noise]\nrunning evaluator\n" + traceback_tail + "\n",
        encoding="utf-8",
    )


def _task(
    task_id: str,
    *,
    status: str = "failed",
    failed_at: str = "",
    failure_reason: str = "",
    run_dir: str = "",
    infra_failure_type: str = "",
) -> dict:
    out: dict = {"id": task_id, "status": status}
    if failed_at:
        out["failed_at"] = failed_at
    if failure_reason:
        out["failure_reason"] = failure_reason
    if run_dir:
        out["run_dir"] = run_dir
    if infra_failure_type:
        out["infra_failure_type"] = infra_failure_type
    return out


def test_no_failures_does_not_pause(tmp_path):
    state = {"queue": []}
    result = detector.evaluate(state, tmp_path)
    assert result["should_pause"] is False


def test_single_failure_does_not_pause(tmp_path):
    state = {"queue": [_task("t1", failed_at="2026-05-25T10:00:00")]}
    result = detector.evaluate(state, tmp_path)
    assert result["should_pause"] is False
    assert "only 1 recent failed" in result["reason"]


def test_two_consecutive_same_module_not_found_pauses(tmp_path):
    """The path bug case: two tasks back-to-back die with
    ModuleNotFoundError 'scripts'. Must trigger pause."""
    for tid, t in [("t_old", "2026-05-25T09:50:00"), ("t_new", "2026-05-25T10:00:00")]:
        run = tmp_path / f"data/{tid}"
        _write_log(
            run,
            "Traceback (most recent call last):\n"
            "  File '/x/y.py', line 1, in <module>\n"
            "    from scripts.gatekeeper import GateKeeper\n"
            "ModuleNotFoundError: No module named 'scripts'",
        )
    state = {
        "queue": [
            _task("t_old", failed_at="2026-05-25T09:50:00", run_dir="data/t_old"),
            _task("t_new", failed_at="2026-05-25T10:00:00", run_dir="data/t_new"),
        ]
    }
    result = detector.evaluate(state, tmp_path)
    assert result["should_pause"] is True
    assert result["signature"] == "traceback:ModuleNotFoundError:scripts"
    assert result["consecutive"] == 2


def test_two_consecutive_different_signatures_does_not_pause(tmp_path):
    run_a = tmp_path / "data/t_a"
    _write_log(
        run_a,
        "Traceback ...\n"
        "  File '/x.py', line 1\n"
        "NameError: name 'ft_test_trades' is not defined",
    )
    run_b = tmp_path / "data/t_b"
    _write_log(
        run_b,
        "Traceback ...\n"
        "  File '/y.py', line 9\n"
        "ModuleNotFoundError: No module named 'scripts'",
    )
    state = {
        "queue": [
            _task("t_a", failed_at="2026-05-25T09:50:00", run_dir="data/t_a"),
            _task("t_b", failed_at="2026-05-25T10:00:00", run_dir="data/t_b"),
        ]
    }
    result = detector.evaluate(state, tmp_path)
    assert result["should_pause"] is False
    assert result["consecutive"] == 1


def test_infra_failure_type_overrides_traceback(tmp_path):
    """If the task has been reclassified with infra_failure_type, use
    that as the signature (no need to read the log)."""
    state = {
        "queue": [
            _task(
                "t1",
                status="infra_failed",
                failed_at="2026-05-25T09:50:00",
                infra_failure_type="path_unreachable_module_not_found",
            ),
            _task(
                "t2",
                status="infra_failed",
                failed_at="2026-05-25T10:00:00",
                infra_failure_type="path_unreachable_module_not_found",
            ),
        ]
    }
    result = detector.evaluate(state, tmp_path)
    assert result["should_pause"] is True
    assert "path_unreachable_module_not_found" in result["signature"]


def test_window_only_inspects_recent_failures(tmp_path):
    """Older failures with different signatures don't reset the
    consecutive count, but they don't matter once we look only at the
    window."""
    state = {
        "queue": [
            _task(
                f"t{i}",
                status="infra_failed",
                failed_at=f"2026-05-25T09:0{i}:00",
                infra_failure_type="path_unreachable_module_not_found",
            )
            for i in range(8)
        ]
    }
    result = detector.evaluate(state, tmp_path, window=3)
    assert result["should_pause"] is True
    # window only inspects 3 most recent
    assert len(result["inspected"]) == 3


def test_already_running_task_not_considered(tmp_path):
    state = {
        "queue": [
            _task(
                "running",
                status="running",
                infra_failure_type="path_unreachable_module_not_found",
            ),
            _task("complete", status="complete", failed_at="2026-05-25T09:00:00"),
        ]
    }
    result = detector.evaluate(state, tmp_path)
    assert result["should_pause"] is False


def test_maybe_touch_pause_flag_writes_when_should_pause(tmp_path):
    flag = tmp_path / "pause.flag"
    decision = {
        "should_pause": True,
        "reason": "test reason",
        "signature": "test:sig",
        "consecutive": 2,
        "inspected": ["a", "b"],
    }
    created = detector.maybe_touch_pause_flag(decision, flag)
    assert created is True
    assert flag.exists()
    body = flag.read_text(encoding="utf-8")
    assert "paused_by: infra_cluster_detector" in body
    assert "signature: test:sig" in body
    assert "consecutive: 2" in body
    assert "- a" in body
    assert "- b" in body


def test_maybe_touch_pause_flag_skips_when_already_exists(tmp_path):
    flag = tmp_path / "pause.flag"
    flag.write_text("paused_by: somebody_else\n", encoding="utf-8")
    decision = {
        "should_pause": True,
        "reason": "test",
        "signature": "x",
        "consecutive": 2,
        "inspected": [],
    }
    created = detector.maybe_touch_pause_flag(decision, flag)
    assert created is False
    # Pre-existing content preserved
    assert "somebody_else" in flag.read_text(encoding="utf-8")


def test_maybe_touch_pause_flag_skips_when_should_not_pause(tmp_path):
    flag = tmp_path / "pause.flag"
    decision = {"should_pause": False, "reason": "ok"}
    created = detector.maybe_touch_pause_flag(decision, flag)
    assert created is False
    assert not flag.exists()
