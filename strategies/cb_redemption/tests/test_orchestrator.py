"""Tests for the layer-7 orchestrator daemon.

All tests run in ``tmp_path`` and use dependency-injected stubs for
the verifier / judge / hypothesizer / auditor / memory / editor / git.
None of them touch real ``data/cb_redemption/`` or hit DeepSeek.
"""

from __future__ import annotations

import json
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from strategies.cb_redemption import orchestrator as orch_mod
from strategies.cb_redemption.orchestrator import (
    FACTOR_NAMES,
    FakeBacktestResult,
    LoopState,
    MAX_NONE_STREAK,
    MAX_RECOVERY_ATTEMPTS,
    Orchestrator,
)


# --------------------------------------------------------------------------- #
# Helpers / fixtures
# --------------------------------------------------------------------------- #


def _write_space_yaml(path: Path) -> None:
    """Drop a minimal but valid tunable_space.yaml at ``path``."""
    payload = {
        "version": 1,
        "strategy": "cb_redemption",
        "last_updated": "2026-05-07T00:00:00Z",
        "parameters": [
            {"name": "w_redeem_progress", "current": 2.0, "range": [0.5, 5.0],
             "prior": "x"},
            {"name": "w_premium_ratio", "current": -0.7, "range": [-5.0, -0.5],
             "prior": "x"},
            {"name": "w_remaining_size", "current": -3.0, "range": [-4.0, -0.5],
             "prior": "x"},
            {"name": "w_stock_momentum", "current": 1.5, "range": [-1.0, 2.0],
             "prior": "x"},
            {"name": "w_market_sentiment", "current": -0.3, "range": [-1.0, 2.0],
             "prior": "x"},
        ],
        "factors": [
            {"name": n, "formula": "f", "prior": "x", "status": "active"}
            for n in FACTOR_NAMES
        ],
        "thresholds": [
            {"name": "action", "current": 0.65, "range": [0.4, 0.9], "prior": "x"},
            {"name": "alert", "current": 0.45, "range": [0.2, 0.7], "prior": "x"},
            {"name": "watch", "current": 0.25, "range": [0.05, 0.5], "prior": "x"},
        ],
        "rules": [
            {"name": "hold_max_days", "current": 15, "range": [5, 30], "prior": "x"},
            {"name": "target_exit_pct", "current": 10.0, "range": [4.0, 25.0], "prior": "x"},
            {"name": "stop_loss_pct", "current": -8.0, "range": [-15.0, -3.0], "prior": "x"},
            {"name": "max_positions", "current": 5, "range": [1, 15], "prior": "x"},
            {"name": "top_k", "current": 5, "range": [1, 20], "prior": "x"},
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=False)


@dataclass
class StubAuditReport:
    """Mirror :class:`auditor.AuditReport` enough for the orchestrator."""

    verdict: str = "healthy"
    iteration: int = 0
    window: int = 1
    evidence: dict = field(default_factory=dict)
    veto: bool = False
    veto_reason: str | None = None
    text: str = ""

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "iteration": self.iteration,
            "window": self.window,
            "evidence": self.evidence,
            "veto": self.veto,
            "veto_reason": self.veto_reason,
            "text": self.text,
        }


def _make_orchestrator(
    tmp_path: Path,
    *,
    max_iterations: int | None = 5,
    cooldown_s: float = 0.0,
    verifier_fn: Any = None,
    judge_fn: Any = None,
    hypothesizer_fn: Any = None,
    auditor_fn: Any = None,
    sleep_fn: Any = None,
    holdout_remaining_fn: Any = None,
    seed: int = 42,
) -> Orchestrator:
    """Build an Orchestrator wired entirely under tmp_path."""
    space = tmp_path / "tunable_space.yaml"
    _write_space_yaml(space)
    data_dir = tmp_path / "data"

    # default healthy auditor
    if auditor_fn is None:
        auditor_fn = lambda runs_path, holdout_path, **kw: StubAuditReport()

    # default hypothesizer returns None (so weights are not edited; this
    # makes plain dry-run tests stable).
    if hypothesizer_fn is None:
        hypothesizer_fn = lambda **kw: None

    # default judge returns a stub diagnosis dict for orchestration.
    if judge_fn is None:
        judge_fn = lambda result, weights, names: {
            "is_oos_gap_sharpe": 0.05,
            "is_oos_gap_winrate": 1.0,
            "weak_factors": [],
            "weakness_text": "stub",
        }

    # default sleep_fn = noop (avoid wall-clock waits)
    if sleep_fn is None:
        sleep_fn = lambda _s: None

    if holdout_remaining_fn is None:
        # Default: holdout file does not exist, so check is skipped.
        holdout_remaining_fn = lambda path: [0, 1, 2, 3]

    return Orchestrator(
        data_dir=data_dir,
        space_path=space,
        cooldown_s=cooldown_s,
        max_iterations=max_iterations,
        dry_run=True,
        verifier_fn=verifier_fn,
        judge_fn=judge_fn,
        hypothesizer_fn=hypothesizer_fn,
        auditor_fn=auditor_fn,
        sleep_fn=sleep_fn,
        holdout_remaining_fn=holdout_remaining_fn,
        commit_fn=lambda msg: True,  # never shell out to git in tests
        seed=seed,
    )


# --------------------------------------------------------------------------- #
# 1. Dry-run runs N iterations and exits cleanly
# --------------------------------------------------------------------------- #


def test_dry_run_completes_5_iterations(tmp_path: Path) -> None:
    o = _make_orchestrator(tmp_path, max_iterations=5)
    final = o.run()
    assert final.iteration == 5
    # No paused/stopped reason if we just hit max_iterations.
    assert final.state in {"running", "paused"}  # may pause if hypothesizer returns None 5x

    runs = (tmp_path / "data" / "runs.jsonl").read_text().strip().splitlines()
    assert len(runs) == 5
    # outbox has an entry for every iteration (plus possibly paused entry).
    obx = (tmp_path / "data" / "outbox.jsonl").read_text().strip().splitlines()
    assert len(obx) >= 5


# --------------------------------------------------------------------------- #
# 2. Healthy → propose → editor.update_value applied
# --------------------------------------------------------------------------- #


def test_healthy_triggers_propose_and_edit(tmp_path: Path) -> None:
    # Force a deterministic hypothesis: bump w_premium_ratio toward zero.
    @dataclass
    class StubHypo:
        item_path: str = "parameters.w_premium_ratio"
        new_value: float = -0.6
        expected_direction: str = "oos_sharpe up by reducing weight"
        reason: str = "stub: shrink large weight 10% toward zero for stability"
        confidence: str = "low"
        source: str = "rules"

        def to_dict(self) -> dict:
            return {
                "item_path": self.item_path,
                "new_value": self.new_value,
                "expected_direction": self.expected_direction,
                "reason": self.reason,
                "confidence": self.confidence,
                "source": self.source,
            }

    counter = {"n": 0}

    def hypo_fn(**kw):
        counter["n"] += 1
        # Only emit one hypothesis (so we don't keep editing same path).
        if counter["n"] == 1:
            return StubHypo()
        return None

    o = _make_orchestrator(tmp_path, max_iterations=2, hypothesizer_fn=hypo_fn)
    final = o.run()
    assert final.iteration == 2

    # Check yaml was actually edited.
    with open(o.space_path, "r", encoding="utf-8") as f:
        space = yaml.safe_load(f)
    w_prem = next(p for p in space["parameters"] if p["name"] == "w_premium_ratio")
    assert w_prem["current"] == pytest.approx(-0.6)


# --------------------------------------------------------------------------- #
# 3. Veto → recovery attempt 1 (revert) succeeds
# --------------------------------------------------------------------------- #


def test_veto_triggers_recovery_attempt1_revert(tmp_path: Path) -> None:
    """Audit vetos on iter 2; iter 3 should run recovery and survive."""
    state = {"iter": 0}

    def auditor_fn(runs_path, holdout_path, **kw):
        state["iter"] += 1
        # iter 1 healthy, iter 2 veto, iter 3 healthy again.
        if state["iter"] == 2:
            return StubAuditReport(verdict="diverging", veto=True, veto_reason="test")
        return StubAuditReport(verdict="healthy")

    o = _make_orchestrator(tmp_path, max_iterations=3, auditor_fn=auditor_fn)

    # First pretend iter 1 is "healthy". After iter 1 runs, runs.jsonl has
    # one healthy entry — recovery attempt 1 (revert to last healthy) needs
    # this row.
    final = o.run()
    assert final.state in {"running", "paused"}

    # Recovery attempt should have been triggered exactly once.
    obx = [
        json.loads(l)
        for l in (tmp_path / "data" / "outbox.jsonl").read_text().splitlines()
    ]
    recovering = [r for r in obx if r.get("phase") == "recovering"]
    assert len(recovering) == 1
    assert "recovery attempt 1" in recovering[0]["change_summary"]


# --------------------------------------------------------------------------- #
# 4. 3 recovery attempts all fail → paused
# --------------------------------------------------------------------------- #


def test_three_recovery_failures_paused(tmp_path: Path) -> None:
    # Auditor always vetos.
    auditor_fn = lambda runs_path, holdout_path, **kw: StubAuditReport(
        verdict="data_mining", veto=True, veto_reason="forced veto"
    )

    # Block the editor: every update_value raises so all 3 attempts produce
    # zero edits.
    def failing_editor_update(*, item_path, new_value, expected_direction, reason, path):
        raise RuntimeError("blocked")

    o = _make_orchestrator(tmp_path, max_iterations=10, auditor_fn=auditor_fn)
    o._editor_update_fn = failing_editor_update

    # No prior healthy run → attempt 1 returns None, attempt 2 also fails
    # (editor blocked), attempt 3 also fails. After three attempts: paused.
    final = o.run()
    assert final.state == "paused"
    assert final.paused_reason and "recovery exhausted" in final.paused_reason


# --------------------------------------------------------------------------- #
# 5. control.signal=pause → paused
# --------------------------------------------------------------------------- #


def test_control_signal_pause_pauses(tmp_path: Path) -> None:
    o = _make_orchestrator(tmp_path, max_iterations=3)
    # Pre-write the control signal so the very first read picks it up.
    o.data_dir.mkdir(parents=True, exist_ok=True)
    with open(o.control_path, "w") as f:
        f.write("pause")

    final = o.run()
    assert final.state == "paused"
    assert final.paused_reason == "control.signal=pause"


# --------------------------------------------------------------------------- #
# 6. control.signal=stop → stopped exit
# --------------------------------------------------------------------------- #


def test_control_signal_stop_exits(tmp_path: Path) -> None:
    o = _make_orchestrator(tmp_path, max_iterations=10)
    o.data_dir.mkdir(parents=True, exist_ok=True)
    with open(o.control_path, "w") as f:
        f.write("stop")

    final = o.run()
    assert final.state == "stopped"
    assert final.iteration == 0  # we stopped before any iter ran


# --------------------------------------------------------------------------- #
# 7. control.signal=force-iter skips cooldown
# --------------------------------------------------------------------------- #


def test_control_signal_force_iter_skips_cooldown(tmp_path: Path) -> None:
    sleeps: list[float] = []
    sleep_fn = lambda s: sleeps.append(s)

    o = _make_orchestrator(
        tmp_path,
        max_iterations=2,
        cooldown_s=10.0,
        sleep_fn=sleep_fn,
    )
    # Write force-iter for the first iter.
    o.data_dir.mkdir(parents=True, exist_ok=True)
    with open(o.control_path, "w") as f:
        f.write("force-iter")

    final = o.run()
    assert final.iteration == 2
    # On force-iter we skip the cooldown sleep for that iter; the second
    # iteration should sleep normally (10s).
    assert 10.0 in sleeps
    # And there must be one fewer "10.0" call than iterations (force-iter
    # iter skipped it).
    long_sleeps = [s for s in sleeps if s == 10.0]
    assert len(long_sleeps) == 1


# --------------------------------------------------------------------------- #
# 8. heartbeat file updated each iteration
# --------------------------------------------------------------------------- #


def test_heartbeat_updates(tmp_path: Path) -> None:
    o = _make_orchestrator(tmp_path, max_iterations=3)
    o.run()
    hb_text = o.heartbeat_path.read_text()
    payload = json.loads(hb_text)
    assert payload["iteration"] == 3
    assert payload["state"] in {"running", "paused"}
    assert "ts_iso" in payload


# --------------------------------------------------------------------------- #
# 9. outbox.jsonl gets one row per iteration
# --------------------------------------------------------------------------- #


def test_outbox_one_row_per_iteration(tmp_path: Path) -> None:
    o = _make_orchestrator(tmp_path, max_iterations=4)
    o.run()
    rows = [
        json.loads(l)
        for l in (tmp_path / "data" / "outbox.jsonl").read_text().splitlines()
    ]
    iter_rows = [r for r in rows if r.get("iteration") in {1, 2, 3, 4} and "verdict" in r]
    assert len(iter_rows) == 4
    for i, r in enumerate(iter_rows, start=1):
        assert r["iteration"] == i
        assert r["verdict"] == "healthy"


# --------------------------------------------------------------------------- #
# 10. SIGTERM handler stops the loop gracefully
# --------------------------------------------------------------------------- #


def test_sigterm_handler_stops_loop(tmp_path: Path) -> None:
    """Set the global stop flag mid-run; loop must exit cleanly."""
    o = _make_orchestrator(tmp_path, max_iterations=100, cooldown_s=0.0)

    iters_done = {"n": 0}

    def counting_sleep(s: float) -> None:
        iters_done["n"] += 1
        if iters_done["n"] == 2:
            # Trigger SIGTERM equivalent.
            orch_mod._should_stop = True

    o._sleep_fn = counting_sleep
    final = o.run()
    assert final.state == "stopped"
    assert final.iteration <= 3


# --------------------------------------------------------------------------- #
# 11. Hypothesizer returning None for MAX_NONE_STREAK consecutive iters → paused
# --------------------------------------------------------------------------- #


def test_hypothesizer_none_streak_pauses(tmp_path: Path) -> None:
    # default hypothesizer_fn in helper already returns None.
    o = _make_orchestrator(tmp_path, max_iterations=MAX_NONE_STREAK + 2)
    final = o.run()
    assert final.state == "paused"
    assert final.paused_reason and "hypothesizer returned None" in final.paused_reason
    # Should have stopped issuing new iterations once paused.
    assert final.iteration == MAX_NONE_STREAK


# --------------------------------------------------------------------------- #
# 12. Holdout pools exhausted → paused
# --------------------------------------------------------------------------- #


def test_holdout_exhausted_pauses(tmp_path: Path) -> None:
    # Materialise a fake sealed_pools.json so the orchestrator considers
    # holdout to be configured.
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    pool_path = data_dir / "sealed_pools.json"
    pool_path.write_text(json.dumps({"pools": []}))

    holdout_remaining_fn = lambda path: []  # no pools left

    o = _make_orchestrator(
        tmp_path,
        max_iterations=3,
        holdout_remaining_fn=holdout_remaining_fn,
    )
    # ensure orchestrator looks at the path we just wrote
    o.holdout_path = pool_path
    final = o.run()
    assert final.state == "paused"
    assert final.paused_reason == "holdout pools exhausted"


# --------------------------------------------------------------------------- #
# 13. resume() restores prior iteration counter
# --------------------------------------------------------------------------- #


def test_resume_restores_iteration(tmp_path: Path) -> None:
    o1 = _make_orchestrator(tmp_path, max_iterations=2)
    o1.run()
    iter_after = o1.loop_state.iteration
    assert iter_after == 2

    # Build a new orchestrator over the same dirs and resume.
    o2 = _make_orchestrator(tmp_path, max_iterations=1)
    o2.resume()
    assert o2.loop_state.iteration == 2  # picked up previous state

    o2.run()
    # Should run exactly 1 more iteration.
    assert o2.loop_state.iteration == 3


# --------------------------------------------------------------------------- #
# 14. CLI --dry-run defaults to a tmp data-dir and never writes to the real
#     data/cb_redemption/ or the real tunable_space.yaml
# --------------------------------------------------------------------------- #


def test_editor_audit_log_lives_inside_data_dir(tmp_path: Path) -> None:
    """When the orchestrator triggers an edit, ``editor_writes.jsonl`` must
    land inside ``data_dir`` — NOT in the repo root ``logs/`` directory.

    This protects dry-runs (and any sandboxed run) from polluting the
    repository's ``logs/`` directory.
    """
    @dataclass
    class StubHypo:
        item_path: str = "parameters.w_premium_ratio"
        new_value: float = -0.6
        expected_direction: str = "oos_sharpe up by reducing weight"
        reason: str = "stub: shrink large weight 10% toward zero"
        confidence: str = "low"
        source: str = "rules"

        def to_dict(self) -> dict:
            return {
                "item_path": self.item_path,
                "new_value": self.new_value,
                "expected_direction": self.expected_direction,
                "reason": self.reason,
                "confidence": self.confidence,
                "source": self.source,
            }

    counter = {"n": 0}

    def hypo_fn(**kw):
        counter["n"] += 1
        if counter["n"] == 1:
            return StubHypo()
        return None

    o = _make_orchestrator(tmp_path, max_iterations=2, hypothesizer_fn=hypo_fn)
    o.run()

    # The editor audit log MUST be inside data_dir.
    expected = tmp_path / "data" / "editor_writes.jsonl"
    assert expected.exists(), (
        f"editor_writes.jsonl should live under data_dir; expected at {expected}"
    )

    rec = json.loads(expected.read_text().strip().splitlines()[0])
    assert rec["item_path"] == "parameters.w_premium_ratio"
    assert rec["new_value"] == -0.6


def test_dry_run_isolated_under_tmp_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Run orchestrator.main() in --dry-run with no --data-dir given.

    Verify:
      - It writes to a fresh tempfile.mkdtemp dir (created by the CLI).
      - It does NOT touch the configured DEFAULT_DATA_DIR.
      - It does NOT modify the real tunable_space.yaml on disk.
      - stderr advertises the tmp data-dir path.
    """
    # Redirect tempfile.mkdtemp into tmp_path so we can inspect it.
    captured: dict[str, Path] = {}

    real_mkdtemp = orch_mod.tempfile.mkdtemp

    def fake_mkdtemp(prefix: str = "tmp"):
        d = tmp_path / f"{prefix}captured"
        d.mkdir(parents=True, exist_ok=True)
        captured["dir"] = d
        return str(d)

    monkeypatch.setattr(orch_mod.tempfile, "mkdtemp", fake_mkdtemp)

    # Point DEFAULT_DATA_DIR at a tmp_path location and assert it is NOT
    # touched (live mode would have written here; dry-run must not).
    fake_default_data = tmp_path / "fake_default_data_cb_redemption"
    monkeypatch.setattr(orch_mod, "DEFAULT_DATA_DIR", fake_default_data)

    # Provide a valid yaml at the editor's DEFAULT_SPACE_FILE so the
    # dry-run shutil.copy2 has a source to copy.
    fake_default_yaml = tmp_path / "default_tunable_space.yaml"
    _write_space_yaml(fake_default_yaml)
    monkeypatch.setattr(orch_mod.editor_mod, "DEFAULT_SPACE_FILE", fake_default_yaml)

    yaml_mtime_before = fake_default_yaml.stat().st_mtime_ns

    # Force the run to use only the dry-fake verifier and a no-op
    # hypothesizer so it does not need real warehouse data.
    monkeypatch.setattr(
        orch_mod.hypothesizer_mod,
        "propose",
        lambda **kw: None,
    )
    # Auditor: pretend healthy regardless of holdout (so loop runs cleanly).
    from dataclasses import dataclass as _dc, field as _f

    @_dc
    class _Stub:
        verdict: str = "healthy"
        iteration: int = 0
        window: int = 1
        evidence: dict = _f(default_factory=dict)
        veto: bool = False
        veto_reason: str | None = None
        text: str = ""

        def to_dict(self):
            return {
                "verdict": self.verdict, "iteration": self.iteration,
                "window": self.window, "evidence": self.evidence,
                "veto": self.veto, "veto_reason": self.veto_reason,
                "text": self.text,
            }

    monkeypatch.setattr(
        orch_mod.auditor_mod,
        "audit",
        lambda runs_path, holdout_path, **kw: _Stub(),
    )

    # Run the CLI.
    rc = orch_mod.main(["--dry-run", "--max-iterations", "2", "--cooldown", "0"])
    assert rc == 0

    # Tmp dir was created and used.
    assert "dir" in captured
    tmp_data_dir = captured["dir"]
    assert (tmp_data_dir / "runs.jsonl").exists()
    assert (tmp_data_dir / "tunable_space.yaml").exists()

    # The fake "real" default data dir was never touched.
    assert not fake_default_data.exists() or not any(fake_default_data.iterdir())

    # The real default yaml was not modified (mtime unchanged — copy2
    # may write to dst_yaml but we forbid editing src_yaml in place).
    assert fake_default_yaml.stat().st_mtime_ns == yaml_mtime_before

    # stderr mentions the tmp path so users can find their artifacts.
    err = capsys.readouterr().err
    assert str(tmp_data_dir) in err
    assert "dry-run" in err
