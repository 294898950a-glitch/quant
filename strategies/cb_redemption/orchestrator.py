"""Orchestrator (Layer 7) — long-running daemon stitching layers 1-9.

Wires together:

- :mod:`data` / :mod:`backtest` (verifier)
- :mod:`editor` (writes ``tunable_space.yaml``)
- :mod:`judge` (per-run diagnosis)
- :mod:`memory` (runs.jsonl + tried_directions index)
- :mod:`auditor` (cross-run veto)
- :mod:`hypothesizer` (proposes the next edit)
- :mod:`holdout` (OOS pool guard)

Designed to run unattended for days. Humans CAN intervene (control.signal,
SIGTERM, SIGUSR1) but the loop never needs them: every adverse audit
verdict triggers an automated recovery cascade rather than a stop. The
only states that actually pause the process and wait for a human are:

1. ``holdout.pools_remaining()`` returns ``[]`` — OOS supply exhausted.
2. ``hypothesizer.propose`` returns ``None`` for ``MAX_NONE_STREAK`` rows
   in a row (loop has nothing left to try inside the registered space).
3. SIGTERM / SIGINT — graceful exit.
4. ``control.signal == "stop"`` — same as above.

State transitions
-----------------

::

    states  : running | recovering | pending_stop_approval | paused | stopped
    running                 -> recovering   audit veto
    recovering              -> running      attempt passed
    recovering              -> pending_stop_approval   3 attempts exhausted
    pending_stop_approval   -> paused       control.signal=stop
    pending_stop_approval   -> running      control.signal=continue (clear veto)
    pending_stop_approval   -> running      control.signal=shift (auto-shift then continue)
    pending_stop_approval   -> running      60-second timeout (default auto-shift)
    running                 -> paused       holdout dry (pools_remaining()==[])
    paused                  -> running      control.signal=resume
    *                       -> paused       control.signal=stop (pause-style)
    *                       -> stopped      SIGTERM / control.signal=stop (graceful exit)

The orchestrator is **self-healing by default**: every adverse audit
verdict triggers an automated recovery cascade, and even after the
cascade fails it asks the user for permission to stop rather than
unconditionally pausing. If the user does not reply within
:data:`STOP_APPROVAL_TIMEOUT_SEC` (60 seconds by default) the loop
auto-shifts every writable parameter to the midpoint of its registered
range and resumes — exploring a fresh region instead of freezing.

Files written under ``data/cb_redemption/``:

- ``state.json``     — current FSM state (atomic write)
- ``heartbeat``      — ISO timestamp + iteration + state
- ``outbox.jsonl``   — one JSON line per iteration
- ``control.signal`` — read once per iteration then atomically cleared

This module deliberately stays import-light at module level so tests
can stub everything via dependency injection on :class:`Orchestrator`.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

# Module-level imports of internal layers — kept lazy in __init__ defaults so
# tests can construct Orchestrator instances without dragging the whole
# backtest engine in.
from strategies.cb_redemption import auditor as auditor_mod
from strategies.cb_redemption import editor as editor_mod
from strategies.cb_redemption import holdout as holdout_mod
from strategies.cb_redemption import hypothesizer as hypothesizer_mod
from strategies.cb_redemption import judge as judge_mod
from strategies.cb_redemption import memory as memory_mod


# --------------------------------------------------------------------------- #
# Constants / defaults
# --------------------------------------------------------------------------- #

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent

DEFAULT_DATA_DIR = _REPO_ROOT / "data" / "cb_redemption"
DEFAULT_COOLDOWN_S = 5.0
DEFAULT_AUDIT_WINDOW = 10
HEARTBEAT_INTERVAL_S = 30.0

#: Hypothesizer returning None this many rows in a row triggers paused.
MAX_NONE_STREAK = 5

#: Auditor verdict ``stagnant`` for this many rows triggers exploration
#: (which we model as also a paused state until external review).
MAX_STAGNANT_STREAK = 5

#: Number of recovery attempts before giving up and pausing.
MAX_RECOVERY_ATTEMPTS = 3

#: After all recovery attempts fail, the orchestrator enters
#: ``pending_stop_approval`` and pushes a telegram asking the user to
#: confirm a halt. If no reply arrives within this many seconds, the
#: loop auto-shifts to range midpoints and resumes on its own. 60 seconds
#: per user request 2026-05-08: 'don't wait 30 min, if I don't reply within
#: 1 min just keep going'. Short enough that the loop won't sit idle, long
#: enough for a quick reply.
STOP_APPROVAL_TIMEOUT_SEC = 60

#: Number of iterations to spend on a single holdout pool before
#: sealing it and rotating to the next one. Each rotation consumes a
#: pool permanently — when ``holdout.pools_remaining()`` returns ``[]``
#: the loop pauses with ``"all holdout pools exhausted"``.
POOL_ROTATE_AFTER = 30

# Default factor names for cb_redemption — kept for backward compat with
# tests that import :data:`FACTOR_NAMES` directly. The orchestrator itself
# does NOT hardcode these any more: at every iteration it pulls the live
# yaml via ``editor.read_space`` and rebuilds ``param_paths`` /
# ``factor_names`` from the actual ``parameters`` / ``factors`` lists. Any
# strategy whose yaml conforms to the editor schema can plug in.
FACTOR_NAMES = (
    "redeem_progress",
    "premium_ratio",
    "remaining_size",
    "stock_momentum",
    "market_sentiment",
)

# Default param paths matching FACTOR_NAMES (same order). Same caveat:
# kept for backward compat; live code paths use the dynamic version.
PARAM_PATHS = tuple(f"parameters.w_{n}" for n in FACTOR_NAMES)


# --------------------------------------------------------------------------- #
# Module-level signal flags (set from signal handlers, polled in run loop).
# --------------------------------------------------------------------------- #

_should_stop = False
_force_iter = False


def _on_sigterm(signum: int, frame: Any) -> None:  # pragma: no cover - global
    global _should_stop
    _should_stop = True


def _on_sigusr1(signum: int, frame: Any) -> None:  # pragma: no cover - global
    global _force_iter
    _force_iter = True


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write(path: Path, text: str) -> None:
    """tmp + rename atomic write. Creates parent dirs on demand."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
    tmp.replace(path)


def _append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        f.flush()


# --------------------------------------------------------------------------- #
# Fake backtest result (dry-run + recovery dummy use)
# --------------------------------------------------------------------------- #


@dataclass
class FakeBacktestResult:
    """Minimal stand-in for :class:`backtest.BacktestResult`.

    Has the surface attributes :mod:`judge` and :mod:`memory` need
    (``is_metrics`` / ``oos_metrics`` / ``all_metrics`` / ``trades`` /
    ``date_range``). Used by ``--dry-run`` and as the default verifier
    fallback when the real backtest is not wired up.
    """

    is_metrics: dict = field(default_factory=lambda: {"sharpe": 0.4, "win_rate": 0.55, "total_trades": 30})
    oos_metrics: dict = field(default_factory=lambda: {"sharpe": 0.3, "win_rate": 0.50, "total_trades": 15})
    all_metrics: dict = field(default_factory=lambda: {"sharpe": 0.35, "win_rate": 0.52, "total_trades": 45})
    trades: list = field(default_factory=list)
    date_range: tuple = ("20230101", "20251231")

    def to_dict(self) -> dict:
        return {
            "is_metrics": dict(self.is_metrics),
            "oos_metrics": dict(self.oos_metrics),
            "all_metrics": dict(self.all_metrics),
            "trades": [],
            "date_range": list(self.date_range),
        }


def _default_dry_verifier(
    weights: list[float],
    thresholds: dict,
    rules: dict,
    oos_event_ids: set[str] | None = None,
) -> FakeBacktestResult:
    """Deterministic-ish fake backtest: small drift on weight changes.

    Not random — same input → same output, so dry runs are reproducible.
    ``oos_event_ids`` is accepted (and ignored numerically) so the dry
    verifier shares the live signature; tests inspecting the call args
    can still see whether the orchestrator passed a pool through.
    """
    s = sum(abs(w) for w in weights) or 1.0
    base = 0.3 + min(0.3, 0.05 * s)
    return FakeBacktestResult(
        is_metrics={"sharpe": base + 0.05, "win_rate": 0.55, "total_trades": 30},
        oos_metrics={"sharpe": base, "win_rate": 0.50, "total_trades": 15},
        all_metrics={"sharpe": base + 0.025, "win_rate": 0.52, "total_trades": 45},
    )


# --------------------------------------------------------------------------- #
# Real backtest adaptor (only imported if used)
# --------------------------------------------------------------------------- #


def _default_live_verifier(
    weights: list[float],
    thresholds: dict,
    rules: dict,
    oos_event_ids: set[str] | None = None,
) -> Any:
    """Adapter: pull snapshots, build BacktestConfig, run pure core."""
    from strategies.cb_redemption import backtest as backtest_mod
    from strategies.cb_redemption import data as data_mod

    snap = data_mod.load_historical_snapshots()
    cfg = backtest_mod.BacktestConfig(
        hold_max_days=int(rules.get("hold_max_days", 15)),
        target_exit_pct=float(rules.get("target_exit_pct", 10.0)),
        stop_loss_pct=float(rules.get("stop_loss_pct", -8.0)),
        max_positions=int(rules.get("max_positions", 5)),
        top_k=int(rules.get("top_k", 5)),
        alert_threshold=float(thresholds.get("alert", 0.45)),
    )
    return backtest_mod.run_backtest_core(
        snap, weights, thresholds, cfg, oos_event_ids=oos_event_ids
    )


# --------------------------------------------------------------------------- #
# State machine
# --------------------------------------------------------------------------- #


@dataclass
class LoopState:
    #: One of: ``running | recovering | pending_stop_approval | paused | stopped``.
    state: str = "running"
    iteration: int = 0
    since_iso: str = ""
    last_verdict: str | None = None
    paused_reason: str | None = None
    none_streak: int = 0
    stagnant_streak: int = 0
    recovery_attempt: int = 0  # 0 = not in recovery; 1..3 = which attempt
    # ---- holdout pool attachment ----
    #: id of the pool whose event_ids are currently feeding OOS metrics.
    #: ``None`` until the very first iter attaches pool 0.
    current_pool_id: int | None = None
    #: number of iterations already spent against ``current_pool_id``.
    #: When this reaches :data:`POOL_ROTATE_AFTER` the orchestrator
    #: seals the pool and rotates to the next remaining one.
    iters_in_current_pool: int = 0
    #: ISO timestamp the loop entered ``pending_stop_approval`` (``None``
    #: in any other state). Used to detect the 60-second timeout that
    #: triggers an auto-shift. Missing in legacy state.json files —
    #: ``from_dict`` defaults to ``None``.
    pending_since_iso: str | None = None

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "iteration": self.iteration,
            "since_iso": self.since_iso,
            "last_verdict": self.last_verdict,
            "paused_reason": self.paused_reason,
            "none_streak": self.none_streak,
            "stagnant_streak": self.stagnant_streak,
            "recovery_attempt": self.recovery_attempt,
            "current_pool_id": self.current_pool_id,
            "iters_in_current_pool": self.iters_in_current_pool,
            "pending_since_iso": self.pending_since_iso,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LoopState":
        # current_pool_id may be missing in older state.json; treat as None.
        cpi_raw = d.get("current_pool_id", None)
        cpi: int | None = None
        if cpi_raw is not None:
            try:
                cpi = int(cpi_raw)
            except (TypeError, ValueError):
                cpi = None
        # pending_since_iso may be missing in older state.json; treat as None.
        psi_raw = d.get("pending_since_iso", None)
        psi: str | None = psi_raw if isinstance(psi_raw, str) and psi_raw else None
        return cls(
            state=d.get("state", "running"),
            iteration=int(d.get("iteration", 0)),
            since_iso=d.get("since_iso", ""),
            last_verdict=d.get("last_verdict"),
            paused_reason=d.get("paused_reason"),
            none_streak=int(d.get("none_streak", 0)),
            stagnant_streak=int(d.get("stagnant_streak", 0)),
            recovery_attempt=int(d.get("recovery_attempt", 0)),
            current_pool_id=cpi,
            iters_in_current_pool=int(d.get("iters_in_current_pool", 0)),
            pending_since_iso=psi,
        )


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


class Orchestrator:
    """Long-running daemon. ``run()`` is the entrypoint.

    All external interactions go through the dependency-injected callables
    declared in ``__init__`` so tests can replace them with deterministic
    stubs. The defaults wire in the real layer modules.
    """

    def __init__(
        self,
        *,
        data_dir: Path = DEFAULT_DATA_DIR,
        space_path: Path | None = None,
        cooldown_s: float = DEFAULT_COOLDOWN_S,
        max_iterations: int | None = None,
        dry_run: bool = True,
        # injectable hooks (defaults wire into real modules)
        verifier_fn: Callable | None = None,
        judge_fn: Callable | None = None,
        hypothesizer_fn: Callable | None = None,
        editor_update_fn: Callable | None = None,
        editor_read_fn: Callable | None = None,
        editor_list_fn: Callable | None = None,
        memory_append_fn: Callable | None = None,
        memory_read_fn: Callable | None = None,
        memory_record_attempt_fn: Callable | None = None,
        memory_search_fn: Callable | None = None,
        auditor_fn: Callable | None = None,
        holdout_remaining_fn: Callable | None = None,
        holdout_read_fn: Callable | None = None,
        holdout_seal_fn: Callable | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        # optional state seeds
        runs_path: Path | None = None,
        attempts_path: Path | None = None,
        holdout_path: Path | None = None,
        # control
        seed: int = 42,
        commit_fn: Callable[[str], bool] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.cooldown_s = float(cooldown_s)
        self.max_iterations = max_iterations
        self.dry_run = bool(dry_run)
        self._rng = random.Random(seed)

        self.space_path = (
            Path(space_path) if space_path else editor_mod.DEFAULT_SPACE_FILE
        )
        self.runs_path = Path(runs_path) if runs_path else self.data_dir / "runs.jsonl"
        self.attempts_path = (
            Path(attempts_path) if attempts_path else self.data_dir / "tried_directions.jsonl"
        )
        self.holdout_path = (
            Path(holdout_path) if holdout_path else self.data_dir / "sealed_pools.json"
        )

        self.state_path = self.data_dir / "state.json"
        self.heartbeat_path = self.data_dir / "heartbeat"
        self.outbox_path = self.data_dir / "outbox.jsonl"
        self.control_path = self.data_dir / "control.signal"

        # 数据新鲜度 marker — 由 ``scripts/refresh_warehouse.py`` 写。在 dry-run 下，
        # tmp data_dir 通常不存在该文件，auditor 会因 last_refresh_path 不存在而
        # 跳过 freshness 检查（冷启动容忍）。
        self.last_refresh_path = self.data_dir / "last_refresh.json"

        # editor 写每次 update_value 的审计记录的目标路径。把它放进 data_dir 而不是
        # repo 根的 logs/，以便 dry-run 不污染仓库。
        self.editor_audit_path = self.data_dir / "editor_writes.jsonl"

        # Injection — verifier
        if verifier_fn is None:
            verifier_fn = _default_dry_verifier if self.dry_run else _default_live_verifier
        self._verifier_fn = verifier_fn

        # Injection — judge
        self._judge_fn = judge_fn or judge_mod.diagnose

        # Injection — hypothesizer
        self._hypothesizer_fn = hypothesizer_fn or hypothesizer_mod.propose

        # Injection — editor
        self._editor_update_fn = editor_update_fn or editor_mod.update_value
        self._editor_read_fn = editor_read_fn or editor_mod.read_space
        self._editor_list_fn = editor_list_fn or editor_mod.list_writable

        # Injection — memory
        self._memory_append_fn = memory_append_fn or memory_mod.append_run
        self._memory_read_fn = memory_read_fn or memory_mod.read_runs
        self._memory_record_attempt_fn = memory_record_attempt_fn or memory_mod.record_attempt
        self._memory_search_fn = memory_search_fn or memory_mod.search_history

        # Injection — auditor
        self._auditor_fn = auditor_fn or auditor_mod.audit

        # Injection — holdout
        self._holdout_remaining_fn = holdout_remaining_fn or holdout_mod.pools_remaining
        self._holdout_read_fn = holdout_read_fn or holdout_mod.read_pool
        self._holdout_seal_fn = holdout_seal_fn or holdout_mod.seal_pool

        # in-memory cache of the event_id set for the currently-attached
        # pool (recovered from holdout file on attach; not persisted in
        # state.json — we re-derive on resume by re-reading the pool file
        # if needed).
        self._current_pool_event_ids: set[str] | None = None

        # Injection — sleep
        self._sleep_fn = sleep_fn or time.sleep

        # Injection — git commit
        self._commit_fn = commit_fn or self._default_commit

        # FSM
        self.loop_state = LoopState(
            state="running",
            iteration=0,
            since_iso=_utcnow_iso(),
        )

        self._last_heartbeat_ts = 0.0

    # ------------------------------------------------------------------ #
    # State persistence
    # ------------------------------------------------------------------ #

    def _save_state(self) -> None:
        _atomic_write(self.state_path, json.dumps(self.loop_state.to_dict(), ensure_ascii=False, indent=2))

    def _load_state(self) -> bool:
        if not self.state_path.exists():
            return False
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                self.loop_state = LoopState.from_dict(json.load(f))
            return True
        except (OSError, json.JSONDecodeError):
            return False

    def resume(self) -> None:
        """Load FSM from disk if present (used by --resume)."""
        self._load_state()
        # If we were stopped, treat resume as a fresh start.
        if self.loop_state.state == "stopped":
            self.loop_state.state = "running"
            self.loop_state.since_iso = _utcnow_iso()

    # ------------------------------------------------------------------ #
    # Outbox / heartbeat
    # ------------------------------------------------------------------ #

    def _write_heartbeat(self, *, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self._last_heartbeat_ts) < HEARTBEAT_INTERVAL_S:
            return
        text = json.dumps(
            {
                "ts_iso": _utcnow_iso(),
                "iteration": self.loop_state.iteration,
                "state": self.loop_state.state,
            }
        )
        _atomic_write(self.heartbeat_path, text)
        self._last_heartbeat_ts = now

    def _outbox(self, **kwargs: Any) -> None:
        row = {"ts_iso": _utcnow_iso(), **kwargs}
        _append_jsonl(self.outbox_path, row)

    # ------------------------------------------------------------------ #
    # Control signal
    # ------------------------------------------------------------------ #

    def _read_control(self) -> str | None:
        """Read control.signal once and atomically clear it.

        Returns one of ``pause | resume | stop | force-iter | continue |
        shift`` or ``None``. ``continue`` and ``shift`` are only meaningful
        while the FSM sits in ``pending_stop_approval``.
        """
        if not self.control_path.exists():
            return None
        try:
            with open(self.control_path, "r", encoding="utf-8") as f:
                cmd = f.read().strip()
        except OSError:
            return None
        # Clear (atomic empty)
        try:
            _atomic_write(self.control_path, "")
        except OSError:
            pass
        if not cmd:
            return None
        cmd = cmd.lower()
        if cmd in {"pause", "resume", "stop", "force-iter", "continue", "shift"}:
            return cmd
        return None

    # ------------------------------------------------------------------ #
    # Git commit
    # ------------------------------------------------------------------ #

    def _default_commit(self, msg: str) -> bool:
        """Run git commit --allow-empty. Failures are swallowed.

        Returns True on success, False otherwise.
        """
        if self.dry_run:
            return True
        try:
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=loop",
                    "-c",
                    "user.email=loop@local",
                    "commit",
                    "--allow-empty",
                    "-q",
                    "-m",
                    msg,
                ],
                cwd=str(_REPO_ROOT),
                check=True,
                capture_output=True,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            self._outbox(
                phase="git_commit_error",
                iteration=self.loop_state.iteration,
                error=str(exc),
            )
            return False

    # ------------------------------------------------------------------ #
    # Core helpers
    # ------------------------------------------------------------------ #

    def _read_space_dynamic(self) -> tuple[list[str], list[str], list[float], dict, dict]:
        """Pull (param_paths, factor_names, weights, thresholds, rules) out of yaml.

        Strategy-agnostic. ``param_paths[i]`` matches ``weights[i]`` 1:1 by
        the order of ``parameters`` in the yaml. ``factor_names`` reflects
        the live ``factors`` section (may be empty). All reads go through
        the injected ``editor_read_fn`` so tests can stub.

        Falls back to the cb-style legacy defaults (5 weights named after
        :data:`FACTOR_NAMES`) only when the yaml cannot be read at all —
        this keeps very early dry-run tests against missing files alive.
        """
        try:
            data = self._editor_read_fn(self.space_path)
        except Exception:
            # Fallback: cb-shaped defaults. Only relevant when the space
            # file is missing entirely.
            return (
                list(PARAM_PATHS),
                list(FACTOR_NAMES),
                [0.0] * len(FACTOR_NAMES),
                {},
                {},
            )

        params = list(data.get("parameters", []) or [])
        param_paths = [f"parameters.{p['name']}" for p in params]
        weights = [float(p.get("current", 0.0)) for p in params]

        factors = list(data.get("factors", []) or [])
        factor_names = [f["name"] for f in factors if "name" in f]

        thresholds = {
            t["name"]: t.get("current") for t in (data.get("thresholds", []) or [])
        }
        rules = {r["name"]: r.get("current") for r in (data.get("rules", []) or [])}
        return param_paths, factor_names, weights, thresholds, rules

    def _read_current_params(self) -> tuple[list[float], dict, dict]:
        """Backward-compat shim around :meth:`_read_space_dynamic`.

        Returns ``(weights, thresholds, rules)`` — callers that also need
        the live ``param_paths`` / ``factor_names`` should use
        :meth:`_read_space_dynamic` directly. Kept because external tests
        / scripts might call it.
        """
        _, _, weights, thresholds, rules = self._read_space_dynamic()
        return weights, thresholds, rules

    def _list_writable(self) -> list[dict]:
        try:
            return list(self._editor_list_fn(self.space_path))
        except Exception:
            return []

    def _build_run_record(
        self,
        iteration: int,
        weights: list[float],
        thresholds: dict,
        rules: dict,
        result: Any,
        diagnosis: Any,
        hypothesis_attempt: dict | None,
        audit_report: Any,
        git_commit: str | None,
    ) -> "memory_mod.RunRecord":
        bt_dict = result.to_dict() if hasattr(result, "to_dict") else {
            "is_metrics": getattr(result, "is_metrics", {}),
            "oos_metrics": getattr(result, "oos_metrics", {}),
            "all_metrics": getattr(result, "all_metrics", {}),
            "date_range": list(getattr(result, "date_range", ("", ""))),
        }
        # Trades may be huge — drop to count for now.
        if "trades" in bt_dict:
            bt_dict["trades"] = []

        return memory_mod.RunRecord(
            run_id=f"run-{iteration:06d}",
            iteration=iteration,
            timestamp_iso=_utcnow_iso(),
            phase="inner",
            params={
                "weights": list(weights),
                "thresholds": dict(thresholds),
                "rules": dict(rules),
            },
            backtest=bt_dict,
            diagnosis=(
                diagnosis.to_dict() if hasattr(diagnosis, "to_dict") else diagnosis
            ),
            hypothesis_attempt=hypothesis_attempt,
            audit=(
                audit_report.to_dict()
                if hasattr(audit_report, "to_dict")
                else audit_report
            ),
            git_commit=git_commit,
        )

    # ------------------------------------------------------------------ #
    # Recovery
    # ------------------------------------------------------------------ #

    def _attempt_revert_to_last_healthy(self) -> str | None:
        """Recovery attempt 1 — revert to most recent healthy params.

        Searches runs.jsonl backwards for ``audit.verdict == "healthy"``;
        returns a one-line description on success, ``None`` if nothing to
        revert to. Strategy-agnostic — derives the live ``param_paths``
        from the yaml and only accepts a healthy run whose recorded
        ``weights`` length matches.
        """
        try:
            runs = self._memory_read_fn(self.runs_path)
        except Exception:
            return None
        param_paths, _, _, _, _ = self._read_space_dynamic()
        for rec in reversed(runs):
            audit = rec.audit if hasattr(rec, "audit") else (rec.get("audit") if isinstance(rec, dict) else None)
            if not audit:
                continue
            if (audit.get("verdict") if isinstance(audit, dict) else None) != "healthy":
                continue
            params = rec.params if hasattr(rec, "params") else rec.get("params", {})
            weights = params.get("weights", [])
            if not weights or len(weights) != len(param_paths):
                continue
            return self._apply_weights(
                weights,
                param_paths,
                "revert to last healthy iteration",
                rule_no=None,
            )
        return None

    def _attempt_random_untouched(self) -> str | None:
        """Recovery attempt 2 — pick a writable item not edited recently
        and nudge it 20% toward the range mid-point.
        """
        items = self._list_writable()
        if not items:
            return None

        # Items recently edited (last N attempts in tried_directions). Skip them.
        try:
            recent_attempts = self._memory_search_fn(path=self.attempts_path)[-20:]
        except Exception:
            recent_attempts = []
        recent_paths = {r.get("item_path") for r in recent_attempts}

        candidates = [
            it
            for it in items
            if it["item_path"] not in recent_paths and isinstance(it.get("current"), (int, float))
        ]
        if not candidates:
            candidates = [
                it for it in items if isinstance(it.get("current"), (int, float))
            ]
        if not candidates:
            return None

        # Deterministic pick (rng seeded at __init__).
        item = self._rng.choice(candidates)
        cur = float(item["current"])
        lo, hi = item["range"]
        mid = (lo + hi) / 2.0
        new_val = round(cur + (mid - cur) * 0.2, 4)
        # Clamp to range.
        new_val = max(lo, min(hi, new_val))
        if new_val == cur:
            # nudge by 1% if still equal (very narrow range edge case)
            new_val = round(cur + (hi - lo) * 0.01, 4)
            new_val = max(lo, min(hi, new_val))
        return self._apply_single_edit(
            item_path=item["item_path"],
            new_value=new_val,
            expected_direction="oos_sharpe up after recovery nudge",
            reason="recovery attempt 2: random untouched item nudged 20% toward range mid",
        )

    def _attempt_shrink_weights(self) -> str | None:
        """Recovery attempt 3 — shrink every parameter weight 20% toward 0."""
        items = self._list_writable()
        if not items:
            return None
        edited = 0
        details: list[str] = []
        for it in items:
            path = it["item_path"]
            if not path.startswith("parameters."):
                continue
            cur = it.get("current")
            if not isinstance(cur, (int, float)):
                continue
            new_val = round(float(cur) * 0.8, 4)
            lo, hi = it["range"]
            new_val = max(lo, min(hi, new_val))
            if new_val == cur:
                continue
            ok = self._apply_single_edit(
                item_path=path,
                new_value=new_val,
                expected_direction="oos_sharpe up by reducing complexity",
                reason="recovery attempt 3: shrink weight 20% toward zero (complexity reduction)",
            )
            if ok:
                edited += 1
                details.append(f"{path}={cur}->{new_val}")
        if edited == 0:
            return None
        return f"shrink {edited} weights 20% toward 0: " + "; ".join(details[:3])

    def _apply_weights(
        self,
        weights: list[float],
        param_paths: list[str],
        reason: str,
        rule_no: int | None,
    ) -> str | None:
        """Apply a full weight-vector update via per-item editor calls.

        ``param_paths`` is derived from the live yaml so the editor calls
        target the actual parameter names of whichever strategy is loaded.
        """
        applied = 0
        for path, val in zip(param_paths, weights):
            ok = self._apply_single_edit(
                item_path=path,
                new_value=float(val),
                expected_direction="oos_sharpe up by reverting params",
                reason=reason + " (recovery)",
            )
            if ok:
                applied += 1
        if applied == 0:
            return None
        return f"apply {applied} weights from prior healthy run"

    def _apply_single_edit(
        self,
        *,
        item_path: str,
        new_value: Any,
        expected_direction: str,
        reason: str,
    ) -> bool:
        """Wrap editor.update_value, swallow errors, return success bool."""
        try:
            try:
                self._editor_update_fn(
                    item_path=item_path,
                    new_value=new_value,
                    expected_direction=expected_direction,
                    reason=reason,
                    path=self.space_path,
                    audit_log_path=self.editor_audit_path,
                )
            except TypeError:
                # Injected editor stubs may not accept audit_log_path; degrade.
                self._editor_update_fn(
                    item_path=item_path,
                    new_value=new_value,
                    expected_direction=expected_direction,
                    reason=reason,
                    path=self.space_path,
                )
            return True
        except Exception as exc:
            self._outbox(
                phase="editor_error",
                iteration=self.loop_state.iteration,
                item_path=item_path,
                error=str(exc),
            )
            return False

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #

    def run(self) -> LoopState:
        """Block running the daemon. Returns the final :class:`LoopState`.

        Iteration limits + ``--max-iterations`` make this returnable in
        tests; in production it runs forever (until SIGTERM / control=stop).
        """
        global _should_stop, _force_iter
        _should_stop = False
        _force_iter = False

        # Install signal handlers — only do this from the main thread.
        try:
            signal.signal(signal.SIGTERM, _on_sigterm)
            signal.signal(signal.SIGINT, _on_sigterm)
            signal.signal(signal.SIGUSR1, _on_sigusr1)
        except (ValueError, AttributeError):  # pragma: no cover - non-main thread
            pass

        if self.loop_state.iteration == 0:
            self.loop_state.since_iso = _utcnow_iso()

        self._save_state()
        self._write_heartbeat(force=True)

        iters_done = 0
        while True:
            # Early exits
            if _should_stop:
                self._enter_stopped("SIGTERM/SIGINT received")
                break
            if (
                self.max_iterations is not None
                and iters_done >= self.max_iterations
            ):
                break

            # Control signal handling (always honoured at top of loop).
            cmd = self._read_control()
            if cmd == "stop":
                # Pending state? then "stop" is the user approving the
                # halt that the loop just requested → real paused (not
                # stopped) so the daemon stays alive for resume.
                if self.loop_state.state == "pending_stop_approval":
                    self._enter_paused("user approved stop after veto")
                else:
                    self._enter_stopped("control.signal=stop")
                    break
            elif cmd == "pause":
                self._enter_paused("control.signal=pause")
                # don't break; sit in paused waiting for next control
            elif cmd == "resume" and self.loop_state.state == "paused":
                self.loop_state.state = "running"
                self.loop_state.paused_reason = None
                self.loop_state.since_iso = _utcnow_iso()
                self._save_state()
            elif cmd == "continue" and self.loop_state.state == "pending_stop_approval":
                # User overrides the veto: clear pending, keep current
                # params, resume running (next audit will re-decide).
                self._resume_from_pending("control.signal=continue")
            elif cmd == "shift" and self.loop_state.state == "pending_stop_approval":
                # User asks for an immediate auto-shift, then resume.
                self._do_auto_shift()
                self._resume_from_pending("control.signal=shift")

            # Honour SIGUSR1 / control=force-iter
            force_now = bool(_force_iter or cmd == "force-iter")
            if _force_iter:
                _force_iter = False

            # ----- Pending-stop-approval handling -----
            # Before any iteration runs, check whether the 60-second deadline
            # has elapsed; if so the loop auto-shifts and resumes itself
            # without waiting for the user.
            if self.loop_state.state == "pending_stop_approval":
                if self._pending_timed_out():
                    self._do_auto_shift()
                    self._resume_from_pending("auto-shift after 60s timeout")
                else:
                    # Still waiting on the user — heartbeat + sleep + poll.
                    self._write_heartbeat()
                    if self.max_iterations is not None:
                        break
                    self._sleep_fn(min(self.cooldown_s, 1.0))
                    continue

            # If paused, just heartbeat + sleep + loop (do NOT run iteration).
            if self.loop_state.state in {"paused", "stopped"}:
                self._write_heartbeat()
                if self.loop_state.state == "stopped":
                    break
                # In tests we pass max_iterations; exit promptly so we don't
                # spin forever in paused.
                if self.max_iterations is not None:
                    break
                # Paused: respect cooldown but allow control to wake us
                # quickly. Use a small slice of the cooldown so resume is
                # responsive.
                self._sleep_fn(min(self.cooldown_s, 1.0))
                # don't increment iters_done — paused doesn't "spend" an iter
                continue

            # ----- Pre-iteration: attach / rotate holdout pool -----
            pause_reason = self._maybe_rotate_pool()
            if pause_reason is not None:
                self._enter_paused(pause_reason)
                self._write_heartbeat()
                self._sleep_fn(min(self.cooldown_s, 1.0))
                continue

            # ============== ONE ITERATION ==============
            self.loop_state.iteration += 1
            iters_done += 1
            self.loop_state.iters_in_current_pool += 1
            outcome = self._run_iteration()
            self._write_heartbeat(force=True)
            self._save_state()

            # Loop exit triggers
            if self.loop_state.state == "stopped":
                break

            # Cooldown — skip if force_now requested.
            if not force_now:
                self._sleep_fn(self.cooldown_s)

        # Final state save.
        self._write_heartbeat(force=True)
        self._save_state()
        return self.loop_state

    def _holdout_path_configured(self) -> bool:
        """We only enforce the holdout-exhausted check if the file exists."""
        return self.holdout_path.exists()

    # ------------------------------------------------------------------ #
    # Holdout pool attach / rotate
    # ------------------------------------------------------------------ #

    def _maybe_rotate_pool(self) -> str | None:
        """Attach pool 0 on first run; rotate after POOL_ROTATE_AFTER iters.

        Returns:
            ``None`` if attachment proceeded normally (or holdout file
            isn't configured at all). Returns a string reason if the loop
            must pause because all pools are exhausted.
        """
        if not self._holdout_path_configured():
            return None  # no holdout file → skip (test envs)

        need_attach = (
            self.loop_state.current_pool_id is None
            or self.loop_state.iters_in_current_pool >= POOL_ROTATE_AFTER
            or self._current_pool_event_ids is None  # cold-resume
        )
        if not need_attach:
            return None

        # If we already have a pool attached, seal it before rotating.
        if (
            self.loop_state.current_pool_id is not None
            and self.loop_state.iters_in_current_pool >= POOL_ROTATE_AFTER
        ):
            old_id = self.loop_state.current_pool_id
            try:
                self._holdout_seal_fn(old_id, pool_file=self.holdout_path)
                self._outbox(
                    phase="pool_sealed",
                    iteration=self.loop_state.iteration,
                    pool_id=old_id,
                    iters_spent=self.loop_state.iters_in_current_pool,
                )
            except Exception as exc:
                self._outbox(
                    phase="holdout_seal_error",
                    iteration=self.loop_state.iteration,
                    pool_id=old_id,
                    error=str(exc),
                )

        # Find next available pool.
        try:
            remaining = list(self._holdout_remaining_fn(self.holdout_path))
        except Exception as exc:
            self._outbox(
                phase="holdout_remaining_error",
                iteration=self.loop_state.iteration,
                error=str(exc),
            )
            remaining = []

        # If we're rotating (not initial attach) and the only "remaining"
        # pool is the one we just sealed, treat as exhausted.
        if self.loop_state.current_pool_id is not None:
            remaining = [
                p for p in remaining if p != self.loop_state.current_pool_id
            ]

        if not remaining:
            return "all holdout pools exhausted"

        new_id = remaining[0]
        try:
            event_ids = list(
                self._holdout_read_fn(new_id, pool_file=self.holdout_path)
            )
        except Exception as exc:
            self._outbox(
                phase="holdout_read_error",
                iteration=self.loop_state.iteration,
                pool_id=new_id,
                error=str(exc),
            )
            # Treat read failure as exhaustion to fail safe.
            return f"holdout read failed for pool {new_id}: {exc}"

        self.loop_state.current_pool_id = int(new_id)
        self.loop_state.iters_in_current_pool = 0
        self._current_pool_event_ids = set(event_ids)
        self._outbox(
            phase="pool_attached",
            iteration=self.loop_state.iteration,
            pool_id=int(new_id),
            n_events=len(event_ids),
        )
        return None

    # ------------------------------------------------------------------ #
    # One iteration
    # ------------------------------------------------------------------ #

    def _run_iteration(self) -> dict:
        """Execute one full loop iteration. Always commits + outboxes + saves."""
        it = self.loop_state.iteration

        # 1. Read current params (strategy-agnostic — names come from yaml).
        (
            param_paths,
            factor_names,
            weights,
            thresholds,
            rules,
        ) = self._read_space_dynamic()

        # 2. Run verifier — pass oos_event_ids if a holdout pool is attached.
        oos_ids = self._current_pool_event_ids
        try:
            try:
                result = self._verifier_fn(
                    weights, thresholds, rules, oos_event_ids=oos_ids
                )
            except TypeError:
                # Older injected verifier stubs don't accept the kwarg —
                # degrade gracefully so existing tests keep passing.
                result = self._verifier_fn(weights, thresholds, rules)
        except Exception as exc:
            # Fall back to a "no metrics" fake — keeps loop alive.
            self._outbox(phase="verifier_error", iteration=it, error=str(exc))
            result = FakeBacktestResult(
                is_metrics={"sharpe": 0.0, "win_rate": 0.0, "total_trades": 0},
                oos_metrics={"sharpe": 0.0, "win_rate": 0.0, "total_trades": 0},
                all_metrics={"sharpe": 0.0, "win_rate": 0.0, "total_trades": 0},
            )

        # 3. Diagnose. ``factor_names`` is pulled from yaml so each
        #    strategy's judge sees the right names; if the strategy has
        #    no factors (e.g. pure-grid), pad with synthetic ``f_i``
        #    placeholders so cb judge's len(weights)==len(factor_names)
        #    invariant holds regardless of yaml shape.
        diagnosis = None
        if len(factor_names) == len(weights):
            judge_factor_names = list(factor_names)
        else:
            judge_factor_names = [
                factor_names[i] if i < len(factor_names) else f"f_{i}"
                for i in range(len(weights))
            ]
        try:
            diagnosis = self._judge_fn(result, list(weights), judge_factor_names)
        except Exception as exc:
            self._outbox(phase="judge_error", iteration=it, error=str(exc))

        # 4. Append a preliminary record (without audit yet) so that audit
        #    sees this iteration in runs.jsonl. We rewrite later? No — runs
        #    is append-only. Strategy: write run AFTER audit completes.

        # 5. Audit (uses runs.jsonl + previously-written records).
        audit_report = None
        try:
            audit_report = self._auditor_fn(
                self.runs_path,
                self.holdout_path,
                last_refresh_path=self.last_refresh_path,
            )
        except TypeError:
            # Allow injected stubs that don't accept the new kwarg.
            try:
                audit_report = self._auditor_fn(
                    self.runs_path, self.holdout_path
                )
            except Exception as exc:
                self._outbox(phase="auditor_error", iteration=it, error=str(exc))
        except Exception as exc:
            self._outbox(phase="auditor_error", iteration=it, error=str(exc))

        # 6. Decide branch.
        verdict = (
            (audit_report.verdict if hasattr(audit_report, "verdict") else None)
            if audit_report
            else "healthy"
        )
        veto = bool(getattr(audit_report, "veto", False)) if audit_report else False

        self.loop_state.last_verdict = verdict
        change_summary = "no-change"
        hypothesis_attempt: dict | None = None
        phase = "healthy"

        if veto:
            phase = "recovering"
            change_summary, recovered = self._do_recovery()
            if not recovered:
                # Stay in ``recovering`` so the next iteration retries with
                # a higher ``recovery_attempt``. Only after MAX_RECOVERY_ATTEMPTS
                # cumulative failures do we ask the user to halt.
                if self.loop_state.recovery_attempt >= MAX_RECOVERY_ATTEMPTS:
                    self._enter_pending_stop_approval(
                        f"recovery exhausted after veto: {audit_report.veto_reason}"
                    )
                else:
                    self.loop_state.state = "recovering"
        elif verdict == "stagnant":
            self.loop_state.stagnant_streak += 1
            phase = "stagnant"
            if self.loop_state.stagnant_streak >= MAX_STAGNANT_STREAK:
                # Don't unilaterally pause — go through the same approval gate
                # as audit-veto so the user can override. If user doesn't
                # reply within STOP_APPROVAL_TIMEOUT_SEC, the loop auto-shifts
                # all writable params to range midpoints and resumes.
                self._enter_pending_stop_approval(
                    f"stagnant for {MAX_STAGNANT_STREAK} consecutive iterations — "
                    f"loop has nothing fresh to try in current direction"
                )
            else:
                # Try to propose anyway (treat stagnant as a soft signal).
                hypothesis_attempt, change_summary = self._do_propose(
                    diagnosis, weights, thresholds, rules
                )
        else:
            self.loop_state.stagnant_streak = 0
            self.loop_state.recovery_attempt = 0
            hypothesis_attempt, change_summary = self._do_propose(
                diagnosis, weights, thresholds, rules
            )

        # 7. Commit.
        commit_msg = (
            f"loop iter={it} verdict={verdict}: {change_summary}"
        )
        committed = self._commit_fn(commit_msg)

        # 8. Persist run record.
        try:
            run_rec = self._build_run_record(
                iteration=it,
                weights=weights,
                thresholds=thresholds,
                rules=rules,
                result=result,
                diagnosis=diagnosis,
                hypothesis_attempt=hypothesis_attempt,
                audit_report=audit_report,
                git_commit=("ok" if committed else None),
            )
            self._memory_append_fn(run_rec, path=self.runs_path)
        except Exception as exc:
            self._outbox(phase="memory_error", iteration=it, error=str(exc))

        # 9. Outbox — enrich with reason / audit text / OOS trade context so
        #    each telegram push tells the user *why*, not just iter+verdict.
        oos_sharpe = None
        oos_trades = None
        oos_return = None
        try:
            oos_m = getattr(result, "oos_metrics", {}) or {}
            oos_sharpe = float(oos_m.get("sharpe", 0.0))
            oos_trades = int(oos_m.get("total_trades", oos_m.get("trades", 0)) or 0)
            ret = oos_m.get("total_return", oos_m.get("avg_return", None))
            oos_return = float(ret) if ret is not None else None
        except Exception:
            pass

        hypo_reason = None
        hypo_confidence = None
        hypo_item_path = None
        hypo_new_value = None
        hypo_old_value = None
        hypo_source = None
        if hypothesis_attempt:
            hypo_reason = hypothesis_attempt.get("reason")
            hypo_confidence = hypothesis_attempt.get("confidence")
            hypo_item_path = hypothesis_attempt.get("item_path")
            hypo_new_value = hypothesis_attempt.get("new_value")
            hypo_source = hypothesis_attempt.get("source")
            # old_value isn't in hypothesis_attempt directly — pull from
            # change_summary "from X to Y" pattern when present.
            try:
                import re as _re
                m = _re.search(r"from (\S+) to ", change_summary or "")
                if m:
                    hypo_old_value = m.group(1)
            except Exception:
                pass

        audit_text = None
        if audit_report is not None:
            try:
                audit_text = getattr(audit_report, "text", None)
                if audit_text and len(audit_text) > 240:
                    audit_text = audit_text[:237] + "..."
            except Exception:
                audit_text = None

        self._outbox(
            iteration=it,
            phase=phase,
            verdict=verdict,
            oos_sharpe=oos_sharpe,
            oos_trades=oos_trades,
            oos_return=oos_return,
            change_summary=change_summary,
            change_item_path=hypo_item_path,
            change_new_value=hypo_new_value,
            change_old_value=hypo_old_value,
            change_source=hypo_source,
            hypothesis_reason=hypo_reason,
            hypothesis_confidence=hypo_confidence,
            audit_text=audit_text,
            paused_reason=self.loop_state.paused_reason,
            state=self.loop_state.state,
        )

        return {
            "iteration": it,
            "phase": phase,
            "verdict": verdict,
            "change_summary": change_summary,
        }

    # ------------------------------------------------------------------ #
    # Sub-routines: propose & recover
    # ------------------------------------------------------------------ #

    def _do_propose(
        self,
        diagnosis: Any,
        weights: list[float],
        thresholds: dict,
        rules: dict,
    ) -> tuple[dict | None, str]:
        """Ask hypothesizer for next edit, apply it via editor.

        Returns ``(hypothesis_attempt_dict_or_none, change_summary)``.
        """
        writable = self._list_writable()
        try:
            recent_runs = self._memory_read_fn(self.runs_path, last_n=5)
        except Exception:
            recent_runs = []
        try:
            tried_dirs = self._memory_search_fn(path=self.attempts_path)
        except Exception:
            tried_dirs = []

        diag_dict = diagnosis.to_dict() if hasattr(diagnosis, "to_dict") else (diagnosis or {})

        try:
            hypo = self._hypothesizer_fn(
                writable_items=writable,
                recent_runs=recent_runs,
                diagnosis=diag_dict,
                tried_directions=tried_dirs,
                tried_path=self.attempts_path,
            )
        except TypeError:
            # Allow simpler injected signatures (no kwargs).
            try:
                hypo = self._hypothesizer_fn(writable, recent_runs, diag_dict, tried_dirs)
            except Exception as exc:
                self._outbox(phase="hypothesizer_error", iteration=self.loop_state.iteration, error=str(exc))
                hypo = None
        except Exception as exc:
            self._outbox(phase="hypothesizer_error", iteration=self.loop_state.iteration, error=str(exc))
            hypo = None

        if hypo is None:
            self.loop_state.none_streak += 1
            if self.loop_state.none_streak >= MAX_NONE_STREAK:
                self._enter_paused(
                    f"hypothesizer returned None for {MAX_NONE_STREAK} consecutive iterations"
                )
            return None, "no-change"

        self.loop_state.none_streak = 0

        # Apply. On apply failure, treat as no-change.
        hypo_dict = hypo.to_dict() if hasattr(hypo, "to_dict") else dict(hypo)
        old_value = None
        for it_w in writable:
            if it_w["item_path"] == hypo_dict["item_path"]:
                old_value = it_w.get("current")
                break

        ok = self._apply_single_edit(
            item_path=hypo_dict["item_path"],
            new_value=hypo_dict["new_value"],
            expected_direction=hypo_dict["expected_direction"],
            reason=hypo_dict["reason"],
        )

        # Record attempt.
        try:
            direction = self._direction_label(old_value, hypo_dict["new_value"])
            key = memory_mod.AttemptKey.from_value(
                item_path=hypo_dict["item_path"],
                direction=direction,
                new_value=hypo_dict["new_value"],
            )
            self._memory_record_attempt_fn(
                key,
                run_id=f"run-{self.loop_state.iteration:06d}",
                outcome="accepted" if ok else "no_change",
                path=self.attempts_path,
                timestamp_iso=_utcnow_iso(),
            )
        except Exception as exc:
            self._outbox(
                phase="memory_attempt_error",
                iteration=self.loop_state.iteration,
                error=str(exc),
            )

        change_summary = (
            f"changed {hypo_dict['item_path']} from {old_value} to "
            f"{hypo_dict['new_value']} ({hypo_dict.get('source', 'unknown')})"
            if ok
            else f"propose failed for {hypo_dict['item_path']}"
        )
        return hypo_dict, change_summary

    @staticmethod
    def _direction_label(old_value: Any, new_value: Any) -> str:
        try:
            if float(new_value) > float(old_value):
                return "increase"
            if float(new_value) < float(old_value):
                return "decrease"
            return "set"
        except (TypeError, ValueError):
            return "set"

    def _do_recovery(self) -> tuple[str, bool]:
        """Run the next recovery attempt. Returns (description, success)."""
        self.loop_state.state = "recovering"
        self.loop_state.recovery_attempt = max(1, self.loop_state.recovery_attempt + 1)

        attempt_no = self.loop_state.recovery_attempt
        if attempt_no == 1:
            desc = self._attempt_revert_to_last_healthy()
        elif attempt_no == 2:
            desc = self._attempt_random_untouched()
        elif attempt_no == 3:
            desc = self._attempt_shrink_weights()
        else:
            desc = None

        if desc is None and attempt_no < MAX_RECOVERY_ATTEMPTS:
            # Skip ahead to the next attempt that might work.
            return f"recovery attempt {attempt_no} no-op", False
        if desc is None:
            return f"recovery attempt {attempt_no} produced no edit", False

        # Successful application — reset to running so the NEXT audit decides
        # whether the recovery actually helped.
        self.loop_state.state = "running"
        return f"recovery attempt {attempt_no}: {desc}", True

    # ------------------------------------------------------------------ #
    # State transitions
    # ------------------------------------------------------------------ #

    def _enter_paused(self, reason: str) -> None:
        if self.loop_state.state == "stopped":
            return
        self.loop_state.state = "paused"
        self.loop_state.paused_reason = reason
        self.loop_state.since_iso = _utcnow_iso()
        # Leaving pending_stop_approval — clear the timer.
        self.loop_state.pending_since_iso = None
        self._save_state()
        self._outbox(
            phase="paused",
            iteration=self.loop_state.iteration,
            verdict=self.loop_state.last_verdict,
            paused_reason=reason,
            state="paused",
        )

    def _enter_pending_stop_approval(self, reason: str) -> None:
        """Transition into ``pending_stop_approval``: ask the user, keep timer.

        Sets ``pending_since_iso`` to *now* so the run loop can detect the
        :data:`STOP_APPROVAL_TIMEOUT_SEC` deadline. Pushes a structured
        outbox row that the telegram pusher renders as an actionable
        prompt (``options`` field enumerates the recognised replies).
        """
        if self.loop_state.state == "stopped":
            return
        self.loop_state.state = "pending_stop_approval"
        self.loop_state.paused_reason = reason
        now_iso = _utcnow_iso()
        self.loop_state.since_iso = now_iso
        self.loop_state.pending_since_iso = now_iso
        self._save_state()

        # Compute deadline = now + STOP_APPROVAL_TIMEOUT_SEC for the user.
        deadline_dt = datetime.now(timezone.utc) + timedelta(
            seconds=STOP_APPROVAL_TIMEOUT_SEC
        )
        deadline_iso = deadline_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        self._outbox(
            phase="stop_approval_requested",
            iteration=self.loop_state.iteration,
            verdict=self.loop_state.last_verdict,
            paused_reason=reason,
            state="pending_stop_approval",
            approval_deadline_iso=deadline_iso,
            options=(
                "reply 'stop' to halt / 'continue' to override veto / "
                "'shift' to reset params and continue / "
                "no reply = auto-shift after 60s"
            ),
        )

    def _pending_timed_out(self) -> bool:
        """True iff we've sat in pending_stop_approval ≥ timeout seconds."""
        if self.loop_state.state != "pending_stop_approval":
            return False
        if not self.loop_state.pending_since_iso:
            return False
        try:
            since_dt = datetime.strptime(
                self.loop_state.pending_since_iso, "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            # Corrupt timestamp → treat as not timed out (fail-safe: keep
            # waiting rather than auto-shift on garbage data).
            return False
        elapsed = (datetime.now(timezone.utc) - since_dt).total_seconds()
        return elapsed >= STOP_APPROVAL_TIMEOUT_SEC

    def _do_auto_shift(self) -> bool:
        """Reset every writable item to the midpoint of its registered range.

        Used as the default action after :data:`STOP_APPROVAL_TIMEOUT_SEC`
        elapses, or on demand when the user replies ``shift``. Each item
        is updated via :func:`editor.update_value` so the audit log
        captures who/why. Per-item failures are logged but do not stop
        the cascade — one out-of-range or unknown item must not pin the
        whole loop.

        Side effects on :class:`LoopState`:

        - ``stagnant_streak`` / ``recovery_attempt`` / ``none_streak`` → 0
        - leaves ``pending_since_iso`` cleared by the caller's transition

        Returns ``True`` on success (at least one item written), ``False``
        if there were no writable items at all.
        """
        items = self._list_writable()
        if not items:
            self._outbox(
                phase="auto_shift",
                iteration=self.loop_state.iteration,
                change_summary="auto-shift: no writable items found",
                state="running",
            )
            return False

        edited = 0
        failed = 0
        for it in items:
            cur = it.get("current")
            rng = it.get("range") or []
            if len(rng) != 2:
                failed += 1
                continue
            lo, hi = rng[0], rng[1]
            try:
                mid = (float(lo) + float(hi)) / 2.0
            except (TypeError, ValueError):
                failed += 1
                continue
            # Round int-typed params to the nearest int.
            if isinstance(cur, int) and not isinstance(cur, bool):
                new_val: Any = int(round(mid))
            elif isinstance(cur, float):
                new_val = round(mid, 4)
            else:
                # Treat other numeric currents (or None) as float.
                try:
                    new_val = round(float(mid), 4)
                except (TypeError, ValueError):
                    failed += 1
                    continue

            ok = self._apply_single_edit(
                item_path=it["item_path"],
                new_value=new_val,
                expected_direction="auto-shift reset to midpoint",
                reason=(
                    "exhausted current direction; resetting to range "
                    "midpoint to explore fresh region"
                ),
            )
            if ok:
                edited += 1
            else:
                failed += 1

        # Reset the streaks regardless of partial failures — we're
        # deliberately abandoning the prior trajectory.
        self.loop_state.stagnant_streak = 0
        self.loop_state.recovery_attempt = 0
        self.loop_state.none_streak = 0

        change_summary = (
            f"auto-shift: reset {edited} params to range midpoints"
            + (f" ({failed} failed)" if failed else "")
        )
        self._outbox(
            phase="auto_shift",
            iteration=self.loop_state.iteration,
            change_summary=change_summary,
            state="running",
        )
        return edited > 0

    def _resume_from_pending(self, reason: str) -> None:
        """Leave ``pending_stop_approval`` and go back to ``running``.

        Clears the pending timer + paused_reason so the next iteration
        starts on a clean trajectory.
        """
        self.loop_state.state = "running"
        self.loop_state.paused_reason = None
        self.loop_state.pending_since_iso = None
        self.loop_state.since_iso = _utcnow_iso()
        self.loop_state.recovery_attempt = 0
        self._save_state()

    def _enter_stopped(self, reason: str) -> None:
        self.loop_state.state = "stopped"
        self.loop_state.paused_reason = reason
        self.loop_state.since_iso = _utcnow_iso()
        self._save_state()
        self._outbox(
            phase="stopped",
            iteration=self.loop_state.iteration,
            paused_reason=reason,
            state="stopped",
        )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _build_argparser() -> Any:
    import argparse

    p = argparse.ArgumentParser(
        prog="orchestrator",
        description="cb_redemption self-loop daemon (layer 7)",
    )
    p.add_argument("--max-iterations", type=int, default=None)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True)
    mode.add_argument("--live", action="store_false", dest="dry_run")
    p.add_argument("--cooldown", type=float, default=DEFAULT_COOLDOWN_S)
    p.add_argument("--resume", action="store_true", default=False)
    p.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help=(
            "Directory for runtime artifacts (state.json, runs.jsonl, "
            "outbox.jsonl, heartbeat, control.signal, sealed_pools.json, "
            "tried_directions.jsonl). Defaults to data/cb_redemption/ in "
            "live mode; in --dry-run mode defaults to a fresh tempfile.mkdtemp "
            "directory so real data is never touched."
        ),
    )
    p.add_argument(
        "--yaml-path",
        type=Path,
        default=None,
        help=(
            "Path to tunable_space.yaml. Defaults to "
            "strategies/cb_redemption/tunable_space.yaml in live mode; in "
            "--dry-run mode the file is copied into the tmp data-dir so the "
            "real yaml is never edited."
        ),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)

    data_dir = args.data_dir
    yaml_path = args.yaml_path

    if args.dry_run:
        # Default: redirect everything into a fresh tmp dir so the real
        # data/cb_redemption/ and tunable_space.yaml are untouched.
        if data_dir is None:
            data_dir = Path(tempfile.mkdtemp(prefix="cb_dryrun_"))
        else:
            data_dir = Path(data_dir)
            data_dir.mkdir(parents=True, exist_ok=True)
        if yaml_path is None:
            src_yaml = editor_mod.DEFAULT_SPACE_FILE
            dst_yaml = data_dir / "tunable_space.yaml"
            if src_yaml.exists() and not dst_yaml.exists():
                shutil.copy2(src_yaml, dst_yaml)
            yaml_path = dst_yaml
        else:
            yaml_path = Path(yaml_path)
        print(
            f"[orchestrator] dry-run: writing to {data_dir}, yaml at {yaml_path}",
            file=sys.stderr,
        )
        print(
            f"[orchestrator] dry-run artifacts at: {data_dir}",
            file=sys.stderr,
        )
    else:
        if data_dir is None:
            data_dir = DEFAULT_DATA_DIR
        else:
            data_dir = Path(data_dir)
        if yaml_path is None:
            yaml_path = editor_mod.DEFAULT_SPACE_FILE
        else:
            yaml_path = Path(yaml_path)
        print(
            f"[orchestrator] live: writing to {data_dir}, yaml at {yaml_path}",
            file=sys.stderr,
        )

    orch = Orchestrator(
        data_dir=data_dir,
        space_path=yaml_path,
        cooldown_s=args.cooldown,
        max_iterations=args.max_iterations,
        dry_run=args.dry_run,
    )
    if args.resume:
        orch.resume()
    final = orch.run()
    return 0 if final.state in {"stopped", "running", "paused"} else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "Orchestrator",
    "LoopState",
    "FakeBacktestResult",
    "FACTOR_NAMES",
    "PARAM_PATHS",
    "MAX_NONE_STREAK",
    "MAX_STAGNANT_STREAK",
    "MAX_RECOVERY_ATTEMPTS",
    "STOP_APPROVAL_TIMEOUT_SEC",
    "main",
]
