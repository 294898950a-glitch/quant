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

    states  : running | recovering | paused | stopped
    running     -> recovering   audit veto
    recovering  -> running      attempt passed
    recovering  -> paused       3 attempts exhausted
    running     -> paused       stagnant or hypothesizer dry or holdout dry
    paused      -> running      control.signal=resume
    *           -> stopped      SIGTERM / control.signal=stop

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
import signal
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

# 5-factor names — must match :func:`backtest.signal_rank` ordering.
FACTOR_NAMES = (
    "redeem_progress",
    "premium_ratio",
    "remaining_size",
    "stock_momentum",
    "market_sentiment",
)

# tunable_space parameter names matching FACTOR_NAMES (same order).
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


def _default_dry_verifier(weights: list[float], thresholds: dict, rules: dict) -> FakeBacktestResult:
    """Deterministic-ish fake backtest: small drift on weight changes.

    Not random — same input → same output, so dry runs are reproducible.
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


def _default_live_verifier(weights: list[float], thresholds: dict, rules: dict) -> Any:
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
    return backtest_mod.run_backtest_core(snap, weights, thresholds, cfg)


# --------------------------------------------------------------------------- #
# State machine
# --------------------------------------------------------------------------- #


@dataclass
class LoopState:
    state: str = "running"  # running | recovering | paused | stopped
    iteration: int = 0
    since_iso: str = ""
    last_verdict: str | None = None
    paused_reason: str | None = None
    none_streak: int = 0
    stagnant_streak: int = 0
    recovery_attempt: int = 0  # 0 = not in recovery; 1..3 = which attempt

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
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LoopState":
        return cls(
            state=d.get("state", "running"),
            iteration=int(d.get("iteration", 0)),
            since_iso=d.get("since_iso", ""),
            last_verdict=d.get("last_verdict"),
            paused_reason=d.get("paused_reason"),
            none_streak=int(d.get("none_streak", 0)),
            stagnant_streak=int(d.get("stagnant_streak", 0)),
            recovery_attempt=int(d.get("recovery_attempt", 0)),
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

        Returns one of ``pause | resume | stop | force-iter`` or ``None``.
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
        if cmd in {"pause", "resume", "stop", "force-iter"}:
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

    def _read_current_params(self) -> tuple[list[float], dict, dict]:
        """Pull (weights, thresholds, rules) out of tunable_space.yaml.

        Always reads via the injected editor.read_space so tests can stub.
        """
        try:
            data = self._editor_read_fn(self.space_path)
        except Exception:
            # Fallback: zero weights + empty dicts (only relevant when space
            # file is missing, e.g. very early dry-run tests).
            return [0.0] * len(FACTOR_NAMES), {}, {}

        params_by_name = {p["name"]: p for p in data.get("parameters", [])}
        weights = [
            float(params_by_name.get(f"w_{name}", {}).get("current", 0.0))
            for name in FACTOR_NAMES
        ]
        thresholds = {
            t["name"]: t.get("current") for t in data.get("thresholds", [])
        }
        rules = {r["name"]: r.get("current") for r in data.get("rules", [])}
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
        revert to.
        """
        try:
            runs = self._memory_read_fn(self.runs_path)
        except Exception:
            return None
        for rec in reversed(runs):
            audit = rec.audit if hasattr(rec, "audit") else (rec.get("audit") if isinstance(rec, dict) else None)
            if not audit:
                continue
            if (audit.get("verdict") if isinstance(audit, dict) else None) != "healthy":
                continue
            params = rec.params if hasattr(rec, "params") else rec.get("params", {})
            weights = params.get("weights", [])
            if not weights or len(weights) != len(FACTOR_NAMES):
                continue
            return self._apply_weights(
                weights,
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

    def _apply_weights(self, weights: list[float], reason: str, rule_no: int | None) -> str | None:
        """Apply a full weight-vector update via per-item editor calls."""
        applied = 0
        for path, val in zip(PARAM_PATHS, weights):
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
                self._enter_stopped("control.signal=stop")
                break
            if cmd == "pause":
                self._enter_paused("control.signal=pause")
                # don't break; sit in paused waiting for next control
            if cmd == "resume" and self.loop_state.state == "paused":
                self.loop_state.state = "running"
                self.loop_state.paused_reason = None
                self.loop_state.since_iso = _utcnow_iso()
                self._save_state()

            # Honour SIGUSR1 / control=force-iter
            force_now = bool(_force_iter or cmd == "force-iter")
            if _force_iter:
                _force_iter = False

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

            # ----- Pre-iteration safety: holdout exhausted? -----
            if self._holdout_path_configured():
                try:
                    remaining = self._holdout_remaining_fn(self.holdout_path)
                except Exception:
                    remaining = None
                if remaining is not None and len(remaining) == 0:
                    self._enter_paused("holdout pools exhausted")
                    self._write_heartbeat()
                    self._sleep_fn(min(self.cooldown_s, 1.0))
                    continue

            # ============== ONE ITERATION ==============
            self.loop_state.iteration += 1
            iters_done += 1
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
    # One iteration
    # ------------------------------------------------------------------ #

    def _run_iteration(self) -> dict:
        """Execute one full loop iteration. Always commits + outboxes + saves."""
        it = self.loop_state.iteration

        # 1. Read current params.
        weights, thresholds, rules = self._read_current_params()

        # 2. Run verifier.
        try:
            result = self._verifier_fn(weights, thresholds, rules)
        except Exception as exc:
            # Fall back to a "no metrics" fake — keeps loop alive.
            self._outbox(phase="verifier_error", iteration=it, error=str(exc))
            result = FakeBacktestResult(
                is_metrics={"sharpe": 0.0, "win_rate": 0.0, "total_trades": 0},
                oos_metrics={"sharpe": 0.0, "win_rate": 0.0, "total_trades": 0},
                all_metrics={"sharpe": 0.0, "win_rate": 0.0, "total_trades": 0},
            )

        # 3. Diagnose.
        diagnosis = None
        try:
            diagnosis = self._judge_fn(result, list(weights), list(FACTOR_NAMES))
        except Exception as exc:
            self._outbox(phase="judge_error", iteration=it, error=str(exc))

        # 4. Append a preliminary record (without audit yet) so that audit
        #    sees this iteration in runs.jsonl. We rewrite later? No — runs
        #    is append-only. Strategy: write run AFTER audit completes.

        # 5. Audit (uses runs.jsonl + previously-written records).
        audit_report = None
        try:
            audit_report = self._auditor_fn(self.runs_path, self.holdout_path)
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
                # Not recovered → paused.
                self._enter_paused(f"recovery exhausted after veto: {audit_report.veto_reason}")
        elif verdict == "stagnant":
            self.loop_state.stagnant_streak += 1
            phase = "stagnant"
            if self.loop_state.stagnant_streak >= MAX_STAGNANT_STREAK:
                self._enter_paused("stagnant for too many iterations")
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

        # 9. Outbox.
        oos_sharpe = None
        try:
            oos_sharpe = float(getattr(result, "oos_metrics", {}).get("sharpe", 0.0))
        except Exception:
            oos_sharpe = None

        self._outbox(
            iteration=it,
            phase=phase,
            verdict=verdict,
            oos_sharpe=oos_sharpe,
            change_summary=change_summary,
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
        self._save_state()
        self._outbox(
            phase="paused",
            iteration=self.loop_state.iteration,
            verdict=self.loop_state.last_verdict,
            paused_reason=reason,
            state="paused",
        )

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
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    orch = Orchestrator(
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
    "main",
]
