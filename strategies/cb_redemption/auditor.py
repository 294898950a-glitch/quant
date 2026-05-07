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
from datetime import datetime, timezone
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

#: Default freshness threshold (days). data 比 snapshot_summary.max_date 老
#: 超过这么多天就 veto。可被 :func:`audit` 调用方覆盖。
DEFAULT_MAX_DATA_AGE_DAYS = 7


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


def _read_last_refresh(
    last_refresh_path: Path | None,
) -> tuple[int | None, str | None, int | None]:
    """读 ``last_refresh.json``，返回 ``(freshness_days, ts_iso, exit_code)``。

    - ``last_refresh_path`` 为 None 或文件不存在 → ``(None, None, None)``。
    - 文件无 ``snapshot_summary.max_date`` 或解析失败 → ``freshness_days=None``，
      仍尽量带回 ``ts_iso`` / ``exit_code``（供 :func:`audit` 决策）。
    - ``freshness_days = (today_utc - max_date).days``，向下取整。
    """
    if last_refresh_path is None:
        return None, None, None
    if not last_refresh_path.exists():
        return None, None, None
    try:
        with open(last_refresh_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None, None, None

    ts_iso = payload.get("ts_iso") if isinstance(payload, dict) else None
    exit_code_raw = payload.get("exit_code") if isinstance(payload, dict) else None
    exit_code: int | None
    try:
        exit_code = int(exit_code_raw) if exit_code_raw is not None else None
    except (TypeError, ValueError):
        exit_code = None

    snapshot = (payload.get("snapshot_summary") if isinstance(payload, dict) else None) or {}
    max_date_raw = snapshot.get("max_date") if isinstance(snapshot, dict) else None
    if not max_date_raw:
        return None, ts_iso, exit_code

    # max_date 可能是 'YYYY-MM-DD' 或 'YYYYMMDD' 等字符串。
    s = str(max_date_raw).strip()
    parsed: datetime | None = None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            parsed = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return None, ts_iso, exit_code

    parsed = parsed.replace(tzinfo=timezone.utc)
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    delta_days = (today - parsed.replace(hour=0, minute=0, second=0, microsecond=0)).days
    if delta_days < 0:
        delta_days = 0
    return delta_days, ts_iso, exit_code


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
    # data freshness 行 — 三种状态: 没 marker / 有 max_date / 仅有 marker。
    freshness_days = evidence.get("data_freshness_days")
    last_exit = evidence.get("last_refresh_exit_code")
    last_iso = evidence.get("last_refresh_iso")
    if freshness_days is None and last_iso is None and last_exit is None:
        lines.append(
            "data_freshness: marker not present (cold start / manual run)."
        )
    else:
        parts = [f"data_freshness_days={freshness_days}"]
        if last_iso is not None:
            parts.append(f"last_refresh_iso={last_iso}")
        if last_exit is not None:
            parts.append(f"last_refresh_exit_code={last_exit}")
        lines.append("data_freshness: " + ", ".join(parts) + ".")
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
    last_refresh_path: Path | None = None,
    window: int = 10,
    max_data_age_days: int = DEFAULT_MAX_DATA_AGE_DAYS,
) -> AuditReport:
    """Audit the trajectory in ``runs_path`` over the last ``window`` runs.

    Parameters
    ----------
    runs_path : Path
        Path to ``runs.jsonl`` written by ``memory.py``.
    holdout_pool_path : Path
        Path to ``sealed_pools.json``. Pass a non-existent path if no
        holdout layer is configured — that will be flagged as a veto.
    last_refresh_path : Path | None
        Optional path to ``last_refresh.json`` written by
        ``scripts/refresh_warehouse.py``. When supplied:

        - ``exit_code != 0``        → veto (last refresh failed).
        - ``snapshot_summary.max_date`` 距今 > ``max_data_age_days`` 天 → veto。

        ``None`` 或文件不存在 → 不 veto（冷启动 / 手动跑容忍）。
    window : int
        Number of most-recent runs to examine.
    max_data_age_days : int
        Days threshold for the freshness veto. Default
        :data:`DEFAULT_MAX_DATA_AGE_DAYS`.

    Returns
    -------
    AuditReport

    Notes
    -----
    Veto priority (when multiple conditions trigger simultaneously, the
    earliest one wins as ``veto_reason``):

        1. ``holdout_compliance=False``
        2. data freshness (refresh failed OR data stale)
        3. ``verdict == "data_mining"``
        4. ``verdict == "diverging"``
    """
    if window < 1:
        raise ValueError(f"window must be >= 1, got {window}")

    runs = _read_runs(runs_path)

    # ---------- 数据新鲜度 ---------- #
    freshness_days, last_refresh_iso, last_refresh_exit = _read_last_refresh(
        last_refresh_path
    )
    # 是否有 marker 这件事决定 evidence 是否带 freshness 字段。
    has_marker = (
        last_refresh_path is not None and last_refresh_path.exists()
    )
    freshness_evidence: dict[str, Any] = {}
    if has_marker:
        freshness_evidence = {
            "data_freshness_days": freshness_days,
            "last_refresh_iso": last_refresh_iso,
            "last_refresh_exit_code": last_refresh_exit,
        }

    # 计算 freshness veto（不立刻施加，holdout 优先级更高，留给后面 merge）。
    freshness_veto_reason: str | None = None
    if has_marker:
        if last_refresh_exit is not None and last_refresh_exit != 0:
            freshness_veto_reason = (
                f"last refresh failed (exit_code={last_refresh_exit}); "
                "data may be stale"
            )
        elif (
            freshness_days is not None and freshness_days > max_data_age_days
        ):
            freshness_veto_reason = (
                f"data is {freshness_days} days stale "
                f"(threshold: {max_data_age_days})"
            )

    # ---------- Cold start ---------- #
    if len(runs) < COLD_START_MIN_RUNS:
        # 即使在冷启动期，freshness 仍然 veto（data marker 已存在则要看）。
        # holdout 在冷启动期不强制（保持原行为）。
        cold_evidence: dict[str, Any] = {}
        cold_evidence.update(freshness_evidence)
        veto = freshness_veto_reason is not None
        return AuditReport(
            verdict="healthy",
            iteration=runs[-1].get("iteration", 0) if runs else 0,
            window=len(runs),
            evidence=cold_evidence,
            veto=veto,
            veto_reason=freshness_veto_reason,
            text=(
                f"Cold start: only {len(runs)} run(s) on record "
                f"(need >= {COLD_START_MIN_RUNS}); no verdict issued."
                + (f" VETO: {freshness_veto_reason}" if veto else "")
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
        evidence.update(freshness_evidence)
        # 优先级: holdout > freshness > 其它
        if not holdout_ok:
            veto = True
            veto_reason: str | None = (
                "holdout_compliance=False — sealed_pools.json missing or no "
                "pool has been read; OOS evaluation is bypassing the holdout "
                "guard (red line)."
            )
        elif freshness_veto_reason is not None:
            veto = True
            veto_reason = freshness_veto_reason
        else:
            veto = False
            veto_reason = None
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
    evidence.update(freshness_evidence)

    # Veto rules — priority: holdout > freshness > data_mining > diverging.
    veto = False
    veto_reason = None
    if not holdout_ok:
        veto = True
        veto_reason = (
            "holdout_compliance=False — sealed_pools.json missing or no "
            "pool has been read; OOS evaluation is bypassing the holdout "
            "guard (red line)."
        )
    elif freshness_veto_reason is not None:
        veto = True
        veto_reason = freshness_veto_reason
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
