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
    POOL_ROTATE_AFTER,
    STOP_APPROVAL_TIMEOUT_SEC,
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


def test_recovery_exhausted_enters_pending_stop_approval_not_paused(
    tmp_path: Path,
) -> None:
    """3 recovery attempts all fail → pending_stop_approval (NOT paused)."""
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

    final = o.run()
    # New behaviour: instead of unconditional pause, the loop asks the
    # user for stop approval and waits for a reply (or timeout).
    assert final.state == "pending_stop_approval"
    assert final.paused_reason and "recovery exhausted" in final.paused_reason
    assert final.recovery_attempt == MAX_RECOVERY_ATTEMPTS
    assert final.pending_since_iso is not None

    # The outbox must contain a stop_approval_requested row with the
    # actionable options string telegram users will see.
    obx = [
        json.loads(l)
        for l in (tmp_path / "data" / "outbox.jsonl").read_text().splitlines()
    ]
    requests = [r for r in obx if r.get("phase") == "stop_approval_requested"]
    assert len(requests) == 1
    req = requests[0]
    assert req["state"] == "pending_stop_approval"
    assert "approval_deadline_iso" in req
    assert "options" in req
    assert "stop" in req["options"] and "continue" in req["options"]
    assert "shift" in req["options"]


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
    assert final.paused_reason == "all holdout pools exhausted"


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


# --------------------------------------------------------------------------- #
# 15. Holdout pool integration: first iter attaches pool 0
# --------------------------------------------------------------------------- #


def _seed_real_sealed_pools(data_dir: Path, n_pools: int = 4) -> Path:
    """Write a real ``sealed_pools.json`` so :mod:`holdout` can read/seal it.

    Uses :func:`holdout.slice_oos_into_pools` directly with a small
    synthetic events DataFrame so the file structure matches production.
    """
    import pandas as pd

    from strategies.cb_redemption import holdout as holdout_mod

    data_dir.mkdir(parents=True, exist_ok=True)
    pool_path = data_dir / "sealed_pools.json"
    # Build n_pools * 4 fake events so pool sizes are non-trivial.
    events = pd.DataFrame(
        {
            "event_id": [f"11{i:04d}_2025-{(i % 12) + 1:02d}-15" for i in range(n_pools * 4)],
        }
    )
    holdout_mod.slice_oos_into_pools(
        events,
        event_id_col="event_id",
        n_pools=n_pools,
        seed=42,
        pool_file=pool_path,
        split_at="2025-01-01",
    )
    return pool_path


def test_first_iter_attaches_pool_0(tmp_path: Path) -> None:
    """Initial iter must attach pool 0 and mark it read in sealed_pools.json."""
    data_dir = tmp_path / "data"
    pool_path = _seed_real_sealed_pools(data_dir)

    o = _make_orchestrator(tmp_path, max_iterations=1)
    # Point the orchestrator at the real pool file we just wrote.
    o.holdout_path = pool_path

    final = o.run()
    assert final.iteration == 1
    assert final.current_pool_id == 0
    assert final.iters_in_current_pool == 1

    # Inspect the on-disk pool state.
    with open(pool_path, "r", encoding="utf-8") as f:
        pools = json.load(f)
    pool0 = next(p for p in pools["pools"] if p["id"] == 0)
    assert pool0["read_count"] == 1
    assert pool0["first_read_at"] is not None
    assert pool0["sealed_at"] is None  # not yet rotated past

    # Outbox should record pool_attached for pool 0 with the right event count.
    obx = [
        json.loads(l)
        for l in (data_dir / "outbox.jsonl").read_text().splitlines()
    ]
    attaches = [r for r in obx if r.get("phase") == "pool_attached"]
    assert len(attaches) == 1
    assert attaches[0]["pool_id"] == 0
    assert attaches[0]["n_events"] == len(pool0["event_ids"])


# --------------------------------------------------------------------------- #
# 16. Holdout pool integration: rotates after POOL_ROTATE_AFTER iters
# --------------------------------------------------------------------------- #


def test_pool_rotates_after_threshold(tmp_path: Path) -> None:
    """After POOL_ROTATE_AFTER+1 iters pool 0 is sealed and pool 1 attached."""
    data_dir = tmp_path / "data"
    pool_path = _seed_real_sealed_pools(data_dir)

    # Provide a hypothesis on every iter so the none-streak pause does
    # not trip before we reach the rotation threshold.
    @dataclass
    class StubHypo:
        item_path: str = "parameters.w_premium_ratio"
        new_value: float = -0.7
        expected_direction: str = "neutral"
        reason: str = "stub: identity nudge to keep loop running"
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

    o = _make_orchestrator(
        tmp_path,
        max_iterations=POOL_ROTATE_AFTER + 1,
        hypothesizer_fn=lambda **kw: StubHypo(),
    )
    o.holdout_path = pool_path

    final = o.run()
    assert final.iteration == POOL_ROTATE_AFTER + 1
    # We've spent 1 iter on pool 1 after rotating.
    assert final.current_pool_id == 1
    assert final.iters_in_current_pool == 1

    with open(pool_path, "r", encoding="utf-8") as f:
        pools = json.load(f)
    pool0 = next(p for p in pools["pools"] if p["id"] == 0)
    pool1 = next(p for p in pools["pools"] if p["id"] == 1)
    assert pool0["sealed_at"] is not None  # sealed at rotation
    assert pool0["read_count"] == 1
    assert pool1["read_count"] == 1
    assert pool1["sealed_at"] is None  # currently attached


# --------------------------------------------------------------------------- #
# 17. Holdout pools exhausted (pools_remaining → []) → paused with explicit reason
# --------------------------------------------------------------------------- #


def test_pools_exhausted_pauses(tmp_path: Path) -> None:
    """When pools_remaining returns [] the loop pauses with the expected reason."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    # We need a sealed_pools.json file present so _holdout_path_configured
    # returns True; contents do not matter because we override the
    # remaining_fn to always return [].
    pool_path = data_dir / "sealed_pools.json"
    pool_path.write_text(json.dumps({"pools": []}))

    o = _make_orchestrator(
        tmp_path,
        max_iterations=3,
        holdout_remaining_fn=lambda path: [],
    )
    o.holdout_path = pool_path
    final = o.run()
    assert final.state == "paused"
    assert final.paused_reason == "all holdout pools exhausted"
    # No iter should have run because attach failed up-front.
    assert final.iteration == 0
    # current_pool_id stays None — never attached.
    assert final.current_pool_id is None


# --------------------------------------------------------------------------- #
# 18. oos_event_ids is forwarded to the verifier on every iter
# --------------------------------------------------------------------------- #


def test_verifier_receives_oos_event_ids_from_pool(tmp_path: Path) -> None:
    """When a pool is attached, the verifier receives its event_ids set."""
    data_dir = tmp_path / "data"
    pool_path = _seed_real_sealed_pools(data_dir)

    seen_ids: list[set[str] | None] = []

    def capturing_verifier(weights, thresholds, rules, oos_event_ids=None):
        seen_ids.append(oos_event_ids)
        return FakeBacktestResult()

    o = _make_orchestrator(
        tmp_path,
        max_iterations=2,
        verifier_fn=capturing_verifier,
    )
    o.holdout_path = pool_path
    o.run()

    # Both iters should have received the same non-empty set (pool 0).
    assert len(seen_ids) == 2
    assert seen_ids[0] is not None and len(seen_ids[0]) > 0
    assert seen_ids[1] == seen_ids[0]


# --------------------------------------------------------------------------- #
# 19. state.json round-trip: current_pool_id and iters_in_current_pool persist
# --------------------------------------------------------------------------- #


def test_state_json_persists_pool_fields(tmp_path: Path) -> None:
    """current_pool_id + iters_in_current_pool round-trip through state.json."""
    data_dir = tmp_path / "data"
    pool_path = _seed_real_sealed_pools(data_dir)

    o1 = _make_orchestrator(tmp_path, max_iterations=2)
    o1.holdout_path = pool_path
    o1.run()

    raw = json.loads((data_dir / "state.json").read_text())
    assert raw["current_pool_id"] == 0
    assert raw["iters_in_current_pool"] == 2

    # Old-state compat: load a state.json missing the new fields.
    legacy = {
        "state": "running",
        "iteration": 7,
        "since_iso": "2026-05-07T00:00:00Z",
        "last_verdict": "healthy",
        "paused_reason": None,
        "none_streak": 0,
        "stagnant_streak": 0,
        "recovery_attempt": 0,
    }
    legacy_state = LoopState.from_dict(legacy)
    assert legacy_state.current_pool_id is None
    assert legacy_state.iters_in_current_pool == 0
    assert legacy_state.iteration == 7


# --------------------------------------------------------------------------- #
# 20. Strategy-agnostic: orchestrator works with a non-cb yaml (no factors,
#     custom parameter names, custom strategy field). The change_summary in
#     the outbox must use the REAL yaml parameter names — not cb's.
# --------------------------------------------------------------------------- #


def _write_arbitrary_strategy_yaml(path: Path) -> None:
    """Drop a non-cb yaml: 3 custom params, no factors, custom thresholds."""
    payload = {
        "version": 1,
        "strategy": "imaginary_strategy",
        "last_updated": "2026-05-07T00:00:00Z",
        "parameters": [
            {"name": "alpha", "current": 1.0, "range": [0.0, 5.0],
             "prior": "x"},
            {"name": "beta", "current": 2.0, "range": [0.0, 5.0],
             "prior": "x"},
            {"name": "gamma", "current": 3.0, "range": [0.0, 5.0],
             "prior": "x"},
        ],
        "factors": [],  # no factors at all
        "thresholds": [],
        "rules": [
            {"name": "fee", "current": 0.001, "range": [0.0, 0.01], "prior": "x"},
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=False)


def test_orchestrator_strategy_agnostic_outbox_uses_real_param_names(
    tmp_path: Path,
) -> None:
    """非 cb 的 yaml 也能跑；outbox 的 change_summary 用真实参数名,不是 cb 的 w_*."""
    space = tmp_path / "tunable_space.yaml"
    _write_arbitrary_strategy_yaml(space)
    data_dir = tmp_path / "data"

    @dataclass
    class StubHypo:
        item_path: str = "parameters.beta"
        new_value: float = 2.5
        expected_direction: str = "↑"
        reason: str = "stub"
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

    captured_factor_names: list[list[str]] = []

    def judge_fn(result, weights, factor_names):
        captured_factor_names.append(list(factor_names))
        # len(weights) must equal len(factor_names) for cb judge
        assert len(weights) == len(factor_names)
        return {
            "is_oos_gap_sharpe": 0.05,
            "is_oos_gap_winrate": 1.0,
            "weak_factors": [],
            "weakness_text": "stub",
        }

    counter = {"n": 0}

    def hypo_fn(**kw):
        counter["n"] += 1
        return StubHypo() if counter["n"] == 1 else None

    o = Orchestrator(
        data_dir=data_dir,
        space_path=space,
        cooldown_s=0.0,
        max_iterations=2,
        dry_run=True,
        hypothesizer_fn=hypo_fn,
        judge_fn=judge_fn,
        auditor_fn=lambda runs_path, holdout_path, **kw: StubAuditReport(),
        sleep_fn=lambda _s: None,
        holdout_remaining_fn=lambda path: [0, 1],
        commit_fn=lambda msg: True,
    )
    final = o.run()
    assert final.iteration == 2

    # weights had len 3 (alpha/beta/gamma) and judge got len-3 factor_names.
    assert captured_factor_names
    assert len(captured_factor_names[0]) == 3

    # outbox change_summary uses the REAL param name, not cb's w_*.
    rows = [
        json.loads(l)
        for l in (data_dir / "outbox.jsonl").read_text().splitlines()
        if "change_summary" in l
    ]
    summaries = " | ".join(r.get("change_summary", "") for r in rows)
    assert "parameters.beta" in summaries
    assert "w_redeem_progress" not in summaries
    assert "w_premium_ratio" not in summaries

    # yaml on disk: beta.current actually got updated.
    with open(space, "r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f)
    beta = next(p for p in loaded["parameters"] if p["name"] == "beta")
    assert beta["current"] == pytest.approx(2.5)


def test_orchestrator_strategy_agnostic_recovery_uses_real_param_names(
    tmp_path: Path,
) -> None:
    """recovery attempt 3 (shrink weights) 必须收缩 yaml 真实参数,不能撞 cb 名字."""
    space = tmp_path / "tunable_space.yaml"
    _write_arbitrary_strategy_yaml(space)
    data_dir = tmp_path / "data"

    # Auditor always vetos so recovery cascade runs.
    auditor_fn = lambda runs_path, holdout_path, **kw: StubAuditReport(
        verdict="data_mining", veto=True, veto_reason="forced veto"
    )

    o = Orchestrator(
        data_dir=data_dir,
        space_path=space,
        cooldown_s=0.0,
        max_iterations=3,
        dry_run=True,
        hypothesizer_fn=lambda **kw: None,
        judge_fn=lambda r, w, n: {"weak_factors": [], "weakness_text": ""},
        auditor_fn=auditor_fn,
        sleep_fn=lambda _s: None,
        holdout_remaining_fn=lambda path: [0, 1],
        commit_fn=lambda msg: True,
    )
    o.run()

    # editor_writes.jsonl should record edits to the real params.
    audit_log = data_dir / "editor_writes.jsonl"
    if audit_log.exists():
        recorded = [
            json.loads(l) for l in audit_log.read_text().splitlines()
        ]
        names = {r.get("item_path") for r in recorded}
        # No cb name should leak into a strategy that has none.
        assert not any(n and n.startswith("parameters.w_") for n in names), (
            f"recovery wrote cb-shaped names: {names}"
        )


# --------------------------------------------------------------------------- #
# 21. pending_stop_approval + control.signal=stop → paused (truly halt)
# --------------------------------------------------------------------------- #


def _force_into_pending(tmp_path: Path, *, max_iterations: int = 10) -> Orchestrator:
    """Helper: build an orchestrator where the very first iter wedges the
    loop into pending_stop_approval (auditor always vetos + editor blocked)."""
    auditor_fn = lambda runs_path, holdout_path, **kw: StubAuditReport(
        verdict="data_mining", veto=True, veto_reason="forced veto for tests"
    )

    def failing_editor_update(*, item_path, new_value, expected_direction, reason, path):
        raise RuntimeError("blocked")

    o = _make_orchestrator(tmp_path, max_iterations=max_iterations, auditor_fn=auditor_fn)
    o._editor_update_fn = failing_editor_update
    return o


def test_pending_approval_stop_signal_pauses(tmp_path: Path) -> None:
    """In pending_stop_approval, control.signal=stop → paused (not stopped)."""
    o = _force_into_pending(tmp_path, max_iterations=4)
    final = o.run()
    assert final.state == "pending_stop_approval"

    # Now simulate the user replying "stop" and resume the daemon.
    with open(o.control_path, "w") as f:
        f.write("stop")

    # Build a fresh orchestrator over the same dirs so the loop wakes up
    # and sees the control signal — this mimics the long-running daemon
    # picking up the file on its next poll iteration.
    o2 = _make_orchestrator(
        tmp_path,
        max_iterations=2,
        auditor_fn=lambda runs_path, holdout_path, **kw: StubAuditReport(
            verdict="data_mining", veto=True, veto_reason="forced"
        ),
    )
    o2.resume()
    assert o2.loop_state.state == "pending_stop_approval"
    final2 = o2.run()
    # User-approved halt: daemon flips to ``paused`` (kept alive for resume),
    # NOT ``stopped`` (which would exit the process).
    assert final2.state == "paused"
    assert final2.paused_reason == "user approved stop after veto"


def test_pending_approval_continue_signal_resumes_running(tmp_path: Path) -> None:
    """control.signal=continue clears pending state and resumes running."""
    o = _force_into_pending(tmp_path, max_iterations=4)
    o.run()
    assert o.loop_state.state == "pending_stop_approval"

    # User replies "continue" — orchestrator should resume on next wake.
    with open(o.control_path, "w") as f:
        f.write("continue")

    # Use a healthy auditor on the next run so the loop doesn't immediately
    # re-enter recovery; this isolates the transition under test.
    o2 = _make_orchestrator(
        tmp_path,
        max_iterations=1,
        auditor_fn=lambda runs_path, holdout_path, **kw: StubAuditReport(),
    )
    o2.resume()
    assert o2.loop_state.state == "pending_stop_approval"
    final = o2.run()
    assert final.state in {"running", "paused"}
    # After "continue" the recovery counter should be reset.
    assert final.recovery_attempt == 0
    # pending_since_iso must be cleared.
    assert final.pending_since_iso is None


def test_pending_approval_shift_signal_triggers_auto_shift(tmp_path: Path) -> None:
    """control.signal=shift resets every writable param to its range midpoint
    and resumes running."""
    o = _force_into_pending(tmp_path, max_iterations=4)
    o.run()
    assert o.loop_state.state == "pending_stop_approval"

    # Restore the editor (un-block writes) so the auto-shift can actually
    # land its updates on the yaml.
    o2 = _make_orchestrator(
        tmp_path,
        max_iterations=1,
        auditor_fn=lambda runs_path, holdout_path, **kw: StubAuditReport(),
    )
    o2.resume()
    assert o2.loop_state.state == "pending_stop_approval"

    with open(o2.control_path, "w") as f:
        f.write("shift")

    final = o2.run()
    assert final.state in {"running", "paused"}
    assert final.recovery_attempt == 0

    # Check the yaml: every parameter should now sit at the midpoint of
    # its range (per the helper-written fixture).
    with open(o2.space_path, "r", encoding="utf-8") as f:
        space = yaml.safe_load(f)
    for sect in ("parameters", "thresholds", "rules"):
        for it in space[sect]:
            lo, hi = it["range"]
            mid = (lo + hi) / 2
            cur = it["current"]
            if isinstance(cur, int) and not isinstance(cur, bool):
                assert cur == int(round(mid)), (
                    f"{sect}.{it['name']} expected midpoint int {int(round(mid))}, got {cur}"
                )
            else:
                assert abs(float(cur) - float(mid)) < 1e-6, (
                    f"{sect}.{it['name']} expected midpoint {mid}, got {cur}"
                )

    # Outbox must include an auto_shift row.
    obx = [
        json.loads(l)
        for l in (o2.data_dir / "outbox.jsonl").read_text().splitlines()
    ]
    shifts = [r for r in obx if r.get("phase") == "auto_shift"]
    assert shifts, "expected at least one auto_shift outbox row"
    assert "reset" in shifts[-1]["change_summary"]


def test_pending_approval_timeout_triggers_auto_shift(tmp_path: Path) -> None:
    """30-min timeout in pending_stop_approval auto-shifts and resumes."""
    o = _force_into_pending(tmp_path, max_iterations=4)
    o.run()
    assert o.loop_state.state == "pending_stop_approval"

    # Backdate pending_since_iso so the timeout check fires immediately.
    state_path = o.state_path
    raw = json.loads(state_path.read_text())
    # 31 minutes ago (well past the 30-min default).
    raw["pending_since_iso"] = "2020-01-01T00:00:00Z"
    state_path.write_text(json.dumps(raw))

    # Resume with healthy auditor so we don't bounce right back into pending.
    o2 = _make_orchestrator(
        tmp_path,
        max_iterations=2,
        auditor_fn=lambda runs_path, holdout_path, **kw: StubAuditReport(),
    )
    o2.resume()
    assert o2.loop_state.state == "pending_stop_approval"
    assert o2.loop_state.pending_since_iso == "2020-01-01T00:00:00Z"

    final = o2.run()
    # Timeout should have flipped to auto-shift then running. With a healthy
    # auditor we expect the loop to actually execute iterations.
    assert final.state in {"running", "paused"}
    assert final.pending_since_iso is None
    assert final.recovery_attempt == 0

    # An auto_shift row tagged with the timeout reason must be in outbox.
    obx = [
        json.loads(l)
        for l in (o2.data_dir / "outbox.jsonl").read_text().splitlines()
    ]
    shifts = [r for r in obx if r.get("phase") == "auto_shift"]
    assert shifts, "auto_shift row missing after timeout"


def test_holdout_exhausted_still_truly_pauses(tmp_path: Path) -> None:
    """pools_remaining()==[] is still a real ``paused``, NOT pending_stop_approval."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    pool_path = data_dir / "sealed_pools.json"
    pool_path.write_text(json.dumps({"pools": []}))

    o = _make_orchestrator(
        tmp_path,
        max_iterations=3,
        holdout_remaining_fn=lambda path: [],
    )
    o.holdout_path = pool_path
    final = o.run()
    # OOS exhaustion is a hard stop — the user MUST refresh holdout pools
    # manually; auto-shift would be meaningless.
    assert final.state == "paused"
    assert final.paused_reason == "all holdout pools exhausted"
    assert final.pending_since_iso is None


def test_auto_shift_resets_streaks(tmp_path: Path) -> None:
    """_do_auto_shift() zeros stagnant_streak / recovery_attempt / none_streak."""
    o = _make_orchestrator(tmp_path, max_iterations=1)

    # Simulate a state where all three streaks are non-zero.
    o.loop_state.stagnant_streak = 4
    o.loop_state.recovery_attempt = 3
    o.loop_state.none_streak = 4

    ok = o._do_auto_shift()
    assert ok is True
    assert o.loop_state.stagnant_streak == 0
    assert o.loop_state.recovery_attempt == 0
    assert o.loop_state.none_streak == 0

    # Auto-shift should have written every writable param to its midpoint.
    with open(o.space_path, "r", encoding="utf-8") as f:
        space = yaml.safe_load(f)
    for sect in ("parameters", "thresholds", "rules"):
        for it in space[sect]:
            lo, hi = it["range"]
            mid = (lo + hi) / 2
            cur = it["current"]
            if isinstance(cur, int) and not isinstance(cur, bool):
                assert cur == int(round(mid))
            else:
                assert abs(float(cur) - float(mid)) < 1e-6


def test_state_json_pending_since_iso_legacy_compat(tmp_path: Path) -> None:
    """Legacy state.json files (no pending_since_iso) load to None."""
    legacy = {
        "state": "running",
        "iteration": 9,
        "since_iso": "2026-05-07T00:00:00Z",
        "last_verdict": "healthy",
        "paused_reason": None,
        "none_streak": 0,
        "stagnant_streak": 0,
        "recovery_attempt": 0,
        # NB: no pending_since_iso, no current_pool_id
    }
    legacy_state = LoopState.from_dict(legacy)
    assert legacy_state.pending_since_iso is None
    assert legacy_state.current_pool_id is None
    assert legacy_state.iteration == 9

    # Round-trip preserves None.
    re_parsed = LoopState.from_dict(legacy_state.to_dict())
    assert re_parsed.pending_since_iso is None


def test_stop_approval_timeout_default_is_60_seconds() -> None:
    """Sanity: per user request 2026-05-08, default timeout is 60s (was 1800)."""
    assert STOP_APPROVAL_TIMEOUT_SEC == 60


def test_stagnant_max_streak_enters_pending_stop_approval_not_paused(
    tmp_path: Path,
) -> None:
    """User feedback: stagnant 5 should also go through approval gate.

    Old behavior: stagnant_streak hits MAX_STAGNANT_STREAK -> _enter_paused()
    unilaterally. User pushed back: 'don't stop without asking'. New behavior:
    same approval gate as audit-veto + recovery exhausted.
    """
    from strategies.cb_redemption.orchestrator import MAX_STAGNANT_STREAK

    auditor_fn = lambda runs_path, holdout_path, **kw: StubAuditReport(
        verdict="stagnant", veto=False, veto_reason=None
    )
    o = _make_orchestrator(
        tmp_path,
        max_iterations=MAX_STAGNANT_STREAK + 1,
        auditor_fn=auditor_fn,
    )
    final = o.run()
    assert final.state == "pending_stop_approval", (
        f"expected pending_stop_approval after stagnant streak, got {final.state}"
    )
    assert "stagnant" in (final.paused_reason or "").lower()
