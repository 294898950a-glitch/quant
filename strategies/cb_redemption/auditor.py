"""Loop-level auditor for the self-loop optimization framework.

The auditor is the 8th role in the framework. Unlike ``judge.py`` (which
diagnoses a single backtest), the auditor reads the **whole trajectory**
of recent runs and decides whether the loop is *learning* or merely
*data-mining* — i.e. driving IS metrics up while OOS stagnates or rots.

It has veto power: if its verdict implies the loop is going nowhere
healthy, the orchestrator MUST pause until a human reviews.

Inputs
------
- ``runs.jsonl`` produced by ``memory.py`` (one ``RunRecord`` per line)
- ``sealed_pools.json`` produced by ``holdout.py`` (optional)

Outputs
-------
- :class:`AuditReport` with a verdict in ``{"healthy", "stagnant",
  "data_mining", "diverging"}`` and a ``veto`` flag.

Design notes
------------
- During cold-start (< 3 runs) we never down-vote — we don't have signal.
- Rolling-window stability (perturbing the IS/OOS cutoff by ~30 trading
  days and re-running) is genuinely expensive and needs the backtest
  engine; it is intentionally **not implemented in P1** and surfaces as
  ``rolling_window_stability=None`` plus a TODO note in ``text``.
- ``holdout_compliance`` is checked by inspecting whether at least one
  pool has been read (``first_read_at != None``). The orchestrator is
  trusted to actually route OOS evaluation through ``read_pool``; this
  audit only confirms that *some* read happened (vs the loop bypassing
  the holdout layer entirely).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------- #
# Thresholds (tunable knobs)
# --------------------------------------------------------------------------- #

#: Minimum runs before we issue any non-healthy verdict.
COLD_START_MIN_RUNS = 3

#: How many recent runs count as "recent" for stagnation / divergence.
RECENT_N = 3

#: |delta| below which oos_sharpe is considered flat (stagnation).
STAGNANT_DELTA = 0.02

#: Per-step drop in oos_sharpe that counts as "big drop" (divergence).
DIVERGING_DROP = 0.1

#: Minimum oos_improvement (last vs first in window) required for healthy.
HEALTHY_IMPROVEMENT = 0.0

#: Minimum reduction in is_oos_gap required for healthy (gap shrinks).
HEALTHY_GAP_SHRINK = 0.0


# --------------------------------------------------------------------------- #
# Data class
# --------------------------------------------------------------------------- #


@dataclass
class AuditReport:
    """Cross-run audit result.

    Attributes
    ----------
    verdict : str
        One of ``"healthy" | "stagnant" | "data_mining" | "diverging"``.
    iteration : int
        Iteration of the latest run inspected (0 if none).
    window : int
        How many recent runs were actually inspected.
    evidence : dict
        Raw numbers backing the verdict. Keys::

            is_oos_gap_trend          : list[float]  # per-run gap
            oos_sharpe_trend          : list[float]
            oos_improvement           : float        # last - first in window
            rolling_window_stability  : float | None
            holdout_compliance        : bool

    veto : bool
        ``True`` means the orchestrator MUST pause.
    veto_reason : str | None
        Human-readable reason, only set when ``veto=True``.
    text : str
        One-paragraph human summary.
    """

    verdict: str
    iteration: int
    window: int
    evidence: dict = field(default_factory=dict)
    veto: bool = False
    veto_reason: str | None = None
    text: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialisable dict (for runs.jsonl ``audit`` slot / Telegram)."""
        return asdict(self)


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


def _read_runs(runs_path: Path) -> list[dict]:
    """Read ``runs.jsonl``. Missing file or malformed lines → []."""
    if not runs_path.exists():
        return []
    out: list[dict] = []
    with open(runs_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # A corrupt record shouldn't kill the audit — skip.
                continue
    return out


def _safe_metric(record: dict, kind: str, key: str) -> float | None:
    """Read ``record["backtest"][kind][key]`` defensively."""
    bt = record.get("backtest") or {}
    block = bt.get(kind) or {}
    val = block.get(key)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _check_holdout_compliance(holdout_pool_path: Path | None) -> bool:
    """True iff the pool file exists AND at least one pool has been read.

    "Has been read" = ``first_read_at`` is set (not ``None``).
    """
    if holdout_pool_path is None:
        return False
    if not holdout_pool_path.exists():
        return False
    try:
        with open(holdout_pool_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False
    pools = data.get("pools") or []
    if not pools:
        return False
    return any(p.get("first_read_at") is not None for p in pools)


def _classify(
    is_sharpe: list[float],
    oos_sharpe: list[float],
    gap: list[float],
) -> tuple[str, float]:
    """Decide verdict from the three trend lists. Returns
    ``(verdict, oos_improvement)``.
    """
    n = len(oos_sharpe)
    oos_improvement = oos_sharpe[-1] - oos_sharpe[0] if n >= 2 else 0.0

    # 1. Diverging — last RECENT_N runs each drop by > DIVERGING_DROP.
    if n >= RECENT_N + 1:
        recent = oos_sharpe[-(RECENT_N + 1):]  # need RECENT_N deltas
        diffs = [recent[i + 1] - recent[i] for i in range(RECENT_N)]
        if all(d < -DIVERGING_DROP for d in diffs):
            return "diverging", oos_improvement

    # 2. Data-mining — IS up, OOS flat-or-down, gap widening.
    if n >= 2:
        is_up = is_sharpe[-1] > is_sharpe[0]
        oos_flat_or_down = oos_sharpe[-1] <= oos_sharpe[0] + STAGNANT_DELTA
        gap_widening = gap[-1] > gap[0]
        if is_up and oos_flat_or_down and gap_widening:
            return "data_mining", oos_improvement

    # 3. Stagnant — recent RECENT_N runs all within STAGNANT_DELTA of each other.
    if n >= RECENT_N:
        recent = oos_sharpe[-RECENT_N:]
        spread = max(recent) - min(recent)
        if spread < STAGNANT_DELTA:
            return "stagnant", oos_improvement

    # 4. Healthy — OOS improving + gap shrinking.
    if n >= 2:
        gap_shrinking = gap[-1] < gap[0] - HEALTHY_GAP_SHRINK
        oos_improving = oos_improvement > HEALTHY_IMPROVEMENT
        if gap_shrinking and oos_improving:
            return "healthy", oos_improvement

    # Default fallthrough — not enough signal to fault, call it healthy.
    return "healthy", oos_improvement


def _compose_text(
    verdict: str,
    window: int,
    evidence: dict,
    veto_reason: str | None,
) -> str:
    """Render a one-paragraph human summary."""
    lines: list[str] = []
    lines.append(
        f"Audit verdict={verdict} over last {window} run(s)."
    )
    oos_trend = evidence.get("oos_sharpe_trend") or []
    gap_trend = evidence.get("is_oos_gap_trend") or []
    if oos_trend:
        lines.append(
            f"oos_sharpe trend: {[round(x, 3) for x in oos_trend]} "
            f"(improvement={evidence.get('oos_improvement', 0.0):+.3f})."
        )
    if gap_trend:
        lines.append(
            f"is_oos_gap trend: {[round(x, 3) for x in gap_trend]}."
        )
    lines.append(
        f"holdout_compliance={evidence.get('holdout_compliance', False)}."
    )
    if evidence.get("rolling_window_stability") is None:
        lines.append(
            "rolling_window_stability: not implemented in P1 "
            "(TODO: perturb IS/OOS split by +/- 30 trading days and "
            "re-run inner loop)."
        )
    if veto_reason:
        lines.append(f"VETO: {veto_reason}")
    return " ".join(lines)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def audit(
    runs_path: Path,
    holdout_pool_path: Path,
    window: int = 10,
) -> AuditReport:
    """Audit the trajectory in ``runs_path`` over the last ``window`` runs.

    Parameters
    ----------
    runs_path : Path
        Path to ``runs.jsonl`` written by ``memory.py``.
    holdout_pool_path : Path
        Path to ``sealed_pools.json``. Pass a non-existent path if no
        holdout layer is configured — that will be flagged as a veto.
    window : int
        Number of most-recent runs to examine.

    Returns
    -------
    AuditReport
    """
    if window < 1:
        raise ValueError(f"window must be >= 1, got {window}")

    runs = _read_runs(runs_path)

    # Cold start — too little data to issue any opinion.
    if len(runs) < COLD_START_MIN_RUNS:
        return AuditReport(
            verdict="healthy",
            iteration=runs[-1].get("iteration", 0) if runs else 0,
            window=len(runs),
            evidence={},
            veto=False,
            veto_reason=None,
            text=(
                f"Cold start: only {len(runs)} run(s) on record "
                f"(need >= {COLD_START_MIN_RUNS}); no verdict issued."
            ),
        )

    recent = runs[-window:]

    # Build trends. Drop runs that have neither IS nor OOS sharpe.
    oos_trend: list[float] = []
    is_trend: list[float] = []
    gap_trend: list[float] = []
    for rec in recent:
        oos = _safe_metric(rec, "oos_metrics", "sharpe")
        is_ = _safe_metric(rec, "is_metrics", "sharpe")
        if oos is None or is_ is None:
            continue
        oos_trend.append(oos)
        is_trend.append(is_)
        gap_trend.append(is_ - oos)

    holdout_ok = _check_holdout_compliance(holdout_pool_path)

    # Not enough numeric data among recent runs — defer.
    if len(oos_trend) < 2:
        evidence = {
            "is_oos_gap_trend": gap_trend,
            "oos_sharpe_trend": oos_trend,
            "oos_improvement": 0.0,
            "rolling_window_stability": None,
            "holdout_compliance": holdout_ok,
        }
        veto = not holdout_ok
        veto_reason = (
            "holdout_compliance=False — sealed_pools.json missing or no "
            "pool has been read; OOS evaluation is bypassing the holdout "
            "guard (red line)."
            if veto
            else None
        )
        return AuditReport(
            verdict="healthy",
            iteration=recent[-1].get("iteration", 0),
            window=len(recent),
            evidence=evidence,
            veto=veto,
            veto_reason=veto_reason,
            text=_compose_text("healthy", len(recent), evidence, veto_reason),
        )

    verdict, oos_improvement = _classify(is_trend, oos_trend, gap_trend)

    evidence = {
        "is_oos_gap_trend": gap_trend,
        "oos_sharpe_trend": oos_trend,
        "oos_improvement": oos_improvement,
        # TODO(P2): perturb IS/OOS cutoff +/- 30 trading days, rerun the
        # inner CMA-ES loop, and report the std of the resulting OOS
        # sharpe. Out of scope for P1 — needs the backtest engine.
        "rolling_window_stability": None,
        "holdout_compliance": holdout_ok,
    }

    # Veto rules.
    veto = False
    veto_reason: str | None = None
    if not holdout_ok:
        veto = True
        veto_reason = (
            "holdout_compliance=False — sealed_pools.json missing or no "
            "pool has been read; OOS evaluation is bypassing the holdout "
            "guard (red line)."
        )
    elif verdict == "data_mining":
        veto = True
        veto_reason = (
            "data_mining: IS sharpe rising while OOS flat/down and "
            "is_oos_gap widening — loop is overfitting the IS window."
        )
    elif verdict == "diverging":
        veto = True
        veto_reason = (
            f"diverging: oos_sharpe dropped > {DIVERGING_DROP} for "
            f"{RECENT_N} consecutive runs."
        )

    return AuditReport(
        verdict=verdict,
        iteration=recent[-1].get("iteration", 0),
        window=len(recent),
        evidence=evidence,
        veto=veto,
        veto_reason=veto_reason,
        text=_compose_text(verdict, len(recent), evidence, veto_reason),
    )
