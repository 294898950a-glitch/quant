"""Tests for the infrastructure cluster detector.

User mandate (2026-05-25): when the same infra failure signature appears
in two consecutive queue tasks, auto-pause to stop burning compute.

User mandate (2026-05-26 — recovery awareness):
- detector must only count failures that happened *after* the
  recovery_armed_at cutoff (or the rolling lookback window)
- historical infra_failed corpses from a previous incident must not
  retrigger pause
- a separate state file tracks the most recent recovery point
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import framework.autonomous.infra_cluster_detector as detector


# --- helpers -----------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _minutes_ago(n: float) -> str:
    return _iso(_now() - timedelta(minutes=n))


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


# --- legacy behaviour (still must hold) --------------------------------

def test_no_failures_does_not_pause(tmp_path):
    state = {"queue": []}
    result = detector.evaluate(state, tmp_path)
    assert result["should_pause"] is False


def test_single_failure_does_not_pause(tmp_path):
    state = {"queue": [_task("t1", failed_at=_minutes_ago(5))]}
    result = detector.evaluate(state, tmp_path)
    assert result["should_pause"] is False
    assert "only 1 recent failed" in result["reason"]


def test_two_consecutive_same_module_not_found_pauses(tmp_path):
    """The path bug case: two tasks back-to-back die with
    ModuleNotFoundError 'scripts'. Must trigger pause."""
    for tid in ("t_old", "t_new"):
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
            _task("t_old", failed_at=_minutes_ago(10), run_dir="data/t_old"),
            _task("t_new", failed_at=_minutes_ago(5), run_dir="data/t_new"),
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
            _task("t_a", failed_at=_minutes_ago(10), run_dir="data/t_a"),
            _task("t_b", failed_at=_minutes_ago(5), run_dir="data/t_b"),
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
                failed_at=_minutes_ago(10),
                infra_failure_type="path_unreachable_module_not_found",
            ),
            _task(
                "t2",
                status="infra_failed",
                failed_at=_minutes_ago(5),
                infra_failure_type="path_unreachable_module_not_found",
            ),
        ]
    }
    result = detector.evaluate(state, tmp_path)
    assert result["should_pause"] is True
    assert "path_unreachable_module_not_found" in result["signature"]


def test_window_only_inspects_recent_failures(tmp_path):
    state = {
        "queue": [
            _task(
                f"t{i}",
                status="infra_failed",
                failed_at=_minutes_ago(30 - i),  # newest = highest i
                infra_failure_type="path_unreachable_module_not_found",
            )
            for i in range(8)
        ]
    }
    result = detector.evaluate(state, tmp_path, window=3)
    assert result["should_pause"] is True
    assert len(result["inspected"]) == 3


def test_already_running_task_not_considered(tmp_path):
    state = {
        "queue": [
            _task(
                "running",
                status="running",
                infra_failure_type="path_unreachable_module_not_found",
            ),
            _task("complete", status="complete", failed_at=_minutes_ago(5)),
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
        "cutoff": "",
        "cutoff_source": "none",
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
    assert "somebody_else" in flag.read_text(encoding="utf-8")


def test_maybe_touch_pause_flag_skips_when_should_not_pause(tmp_path):
    flag = tmp_path / "pause.flag"
    decision = {"should_pause": False, "reason": "ok"}
    created = detector.maybe_touch_pause_flag(decision, flag)
    assert created is False
    assert not flag.exists()


# --- recovery awareness (2026-05-26 mandate) ---------------------------

def test_historical_infra_failed_does_not_trigger_after_recovery(tmp_path):
    """The exact mis-fire that happened on 2026-05-26: two historical
    infra_failed tasks from an earlier incident must NOT trigger pause
    once recovery_armed_at has been set to a moment after them."""
    state = {
        "queue": [
            _task(
                "historical_1",
                status="infra_failed",
                failed_at=_minutes_ago(120),  # 2 hours ago, pre-recovery
                infra_failure_type="path_unreachable_module_not_found",
            ),
            _task(
                "historical_2",
                status="infra_failed",
                failed_at=_minutes_ago(115),
                infra_failure_type="path_unreachable_module_not_found",
            ),
        ]
    }
    # Recovery happened 10 minutes ago — both failures predate it.
    recovery = _minutes_ago(10)
    result = detector.evaluate(
        state, tmp_path, recovery_armed_at=recovery,
    )
    assert result["should_pause"] is False
    assert result["cutoff_source"] == "recovery_armed_at"
    assert result["consecutive"] == 0


def test_new_post_recovery_failures_do_trigger_pause(tmp_path):
    """Two NEW failures after recovery_armed_at, same signature → pause.
    Old failures with the same signature in the queue don't change that."""
    state = {
        "queue": [
            # historical (predates recovery)
            _task(
                "historical_1",
                status="infra_failed",
                failed_at=_minutes_ago(120),
                infra_failure_type="path_unreachable_module_not_found",
            ),
            # new failures (after recovery)
            _task(
                "new_1",
                status="infra_failed",
                failed_at=_minutes_ago(5),
                infra_failure_type="path_unreachable_module_not_found",
            ),
            _task(
                "new_2",
                status="infra_failed",
                failed_at=_minutes_ago(2),
                infra_failure_type="path_unreachable_module_not_found",
            ),
        ]
    }
    recovery = _minutes_ago(10)  # historical_1 predates this; new_1/new_2 do not
    result = detector.evaluate(
        state, tmp_path, recovery_armed_at=recovery,
    )
    assert result["should_pause"] is True
    assert result["consecutive"] == 2
    assert "post-recovery" in result["reason"]
    # historical_1 must NOT show up among inspected tasks
    assert "historical_1" not in result["inspected"]


def test_different_post_recovery_signatures_do_not_merge(tmp_path):
    """Two new post-recovery failures with DIFFERENT signatures must
    not trigger pause — the consecutive counter resets at the
    signature boundary."""
    state = {
        "queue": [
            _task(
                "new_1",
                status="infra_failed",
                failed_at=_minutes_ago(5),
                infra_failure_type="path_unreachable_module_not_found",
            ),
            _task(
                "new_2",
                status="infra_failed",
                failed_at=_minutes_ago(2),
                infra_failure_type="some_other_error_kind",
            ),
        ]
    }
    recovery = _minutes_ago(10)
    result = detector.evaluate(
        state, tmp_path, recovery_armed_at=recovery,
    )
    assert result["should_pause"] is False
    assert result["consecutive"] == 1


def test_lookback_window_excludes_old_failures_without_recovery(tmp_path):
    """If recovery_armed_at is not set, the rolling lookback window
    still excludes ancient failures so the detector cannot wake up
    on long-dead corpses after a fresh deploy."""
    state = {
        "queue": [
            _task(
                "ancient_1",
                status="infra_failed",
                failed_at=_minutes_ago(180),
                infra_failure_type="path_unreachable_module_not_found",
            ),
            _task(
                "ancient_2",
                status="infra_failed",
                failed_at=_minutes_ago(175),
                infra_failure_type="path_unreachable_module_not_found",
            ),
        ]
    }
    # No recovery_armed_at; default lookback is 60 min, both >180 min ago
    result = detector.evaluate(state, tmp_path, recovery_armed_at=None)
    assert result["should_pause"] is False
    assert result["cutoff_source"].startswith("lookback_")


def test_pause_flag_records_recovery_context(tmp_path):
    """When pause is triggered post-recovery, the written flag must
    surface that this is a fresh-failure trip, not historical."""
    state = {
        "queue": [
            _task(
                "new_1",
                status="infra_failed",
                failed_at=_minutes_ago(5),
                infra_failure_type="path_unreachable_module_not_found",
            ),
            _task(
                "new_2",
                status="infra_failed",
                failed_at=_minutes_ago(2),
                infra_failure_type="path_unreachable_module_not_found",
            ),
        ]
    }
    recovery = _minutes_ago(10)
    decision = detector.evaluate(
        state, tmp_path, recovery_armed_at=recovery,
    )
    assert decision["should_pause"] is True
    flag_path = tmp_path / "pause.flag"
    detector.maybe_touch_pause_flag(decision, flag_path)
    body = flag_path.read_text(encoding="utf-8")
    assert "cutoff_source: recovery_armed_at" in body
    assert "post-recovery" in body


# --- state file round-trip --------------------------------------------

def test_mark_recovery_armed_writes_state(tmp_path):
    state_path = tmp_path / "cluster_detector_state.json"
    stamp = detector.mark_recovery_armed(
        state_path, armed_by="test_unpause", notes="manual test"
    )
    assert stamp
    assert state_path.exists()
    data = json.loads(state_path.read_text())
    assert data["recovery_armed_at"] == stamp
    assert data["armed_by"] == "test_unpause"
    assert data["notes"] == "manual test"


def test_load_recovery_armed_at_handles_missing_file(tmp_path):
    state_path = tmp_path / "does_not_exist.json"
    assert detector.load_recovery_armed_at(state_path) == ""


def test_load_recovery_armed_at_handles_malformed_file(tmp_path):
    state_path = tmp_path / "garbage.json"
    state_path.write_text("this is not json\n")
    assert detector.load_recovery_armed_at(state_path) == ""
