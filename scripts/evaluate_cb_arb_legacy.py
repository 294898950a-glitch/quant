#!/usr/bin/env python3
"""Evaluate legacy cb_arb parameter sets with the shared evaluator.

The script is intentionally partial-data tolerant: if the local mirror lacks
the 240-run history or a remote benchmark cannot be fetched, the report records
that gap and continues with the available sections.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from strategies.cb_arb.verifier import OOS_START, run_backtest
from strategies.cb_redemption.benchmarks import (
    BenchmarkConfig,
    BenchmarkDataError,
    load_benchmarks,
)
from strategies.cb_redemption.evaluator import (
    EvaluationConfig,
    evaluate,
    format_evaluation_report,
)


DEFAULT_RUNS = REPO_ROOT / "data" / "cb_arb" / "runs.jsonl"
DEFAULT_SPACE = REPO_ROOT / "strategies" / "cb_arb" / "tunable_space.yaml"
DEFAULT_REPORT = REPO_ROOT / "reports" / "cb_arb_evaluation_2026-05-10.md"
DEFAULT_BENCHMARKS = ("cash", "cb_equal", "csi300", "dividend", "sixty_forty")


@dataclass(frozen=True)
class Params:
    weights: list[float]
    thresholds: dict[str, Any]
    rules: dict[str, Any]


@dataclass
class EvaluationSection:
    label: str
    params_source: str
    markdown: str
    metrics: dict[str, Any]


def main() -> int:
    args = _parse_args()
    runs = read_runs(args.runs)
    sections: list[EvaluationSection] = []
    notes: list[str] = []

    initial_params = params_from_first_run(runs) or params_from_yaml(args.space)
    if initial_params is None:
        notes.append("缺少初始参数: runs.jsonl 和 tunable_space.yaml 都不可用。")
    else:
        sections.append(
            evaluate_params(
                "初始参数",
                "runs.jsonl first run" if runs else "tunable_space.yaml current",
                initial_params,
                refresh_benchmarks=args.refresh_benchmarks,
            )
        )

    run240 = find_run(runs, 240)
    if run240 is None:
        notes.append(
            f"本地 `{args.runs}` 只有 {len(runs)} 条记录, 未找到 iteration=240; "
            "240 轮末段参数未评测。"
        )
    else:
        sections.append(
            evaluate_params(
                "240 轮末段参数",
                "runs.jsonl iteration=240",
                params_from_run(run240),
                refresh_benchmarks=args.refresh_benchmarks,
            )
        )

    report = build_report(sections, runs, notes)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(args.output)
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    p.add_argument("--space", type=Path, default=DEFAULT_SPACE)
    p.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    p.add_argument(
        "--refresh-benchmarks",
        action="store_true",
        help="allow akshare refresh for missing non-local benchmark caches",
    )
    return p.parse_args()


def read_runs(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        out.append(json.loads(line))
    return out


def find_run(runs: list[dict], iteration: int) -> dict | None:
    for rec in runs:
        if int(rec.get("iteration", -1)) == iteration:
            return rec
    return None


def params_from_first_run(runs: list[dict]) -> Params | None:
    return params_from_run(runs[0]) if runs else None


def params_from_run(rec: dict) -> Params:
    params = rec.get("params") or {}
    return Params(
        weights=list(params.get("weights") or []),
        thresholds=dict(params.get("thresholds") or {}),
        rules=dict(params.get("rules") or {}),
    )


def params_from_yaml(path: Path) -> Params | None:
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Params(
        weights=[p.get("current") for p in data.get("parameters", [])],
        thresholds={
            t["name"]: t.get("current") for t in data.get("thresholds", []) or []
        },
        rules={r["name"]: r.get("current") for r in data.get("rules", []) or []},
    )


def evaluate_params(
    label: str,
    params_source: str,
    params: Params,
    *,
    refresh_benchmarks: bool,
) -> EvaluationSection:
    result = run_backtest(params.weights, params.thresholds, params.rules)
    strategy_returns = strategy_returns_from_result(result)
    start = strategy_returns.index.min().strftime("%Y-%m-%d")
    end = strategy_returns.index.max().strftime("%Y-%m-%d")
    benchmarks, missing = load_available_benchmarks(
        start, end, refresh_benchmarks=refresh_benchmarks
    )
    cfg = config_for_available_benchmarks(benchmarks)
    ev = evaluate(strategy_returns, benchmarks, cfg)
    tier_note = ""
    if "dividend" not in benchmarks and ev.tier in {"明确好", "值得真上"}:
        ev.tier = "底线档"
        ev.thresholds["beats_stretch"] = False
        ev.reasons = [
            "tier=底线档; dividend benchmark missing, stretch-tier judgement unavailable"
        ]
        tier_note = (
            "\n\n## Tier Cap\n\n"
            "- `dividend` benchmark missing; tier capped at `底线档` until the "
            "stretch benchmark is available."
        )
    md = format_evaluation_report(ev, title=f"cb_arb {label}")
    md += tier_note
    if missing:
        md += "\n\n## Missing Benchmarks\n\n" + "\n".join(
            f"- `{name}`: {reason}" for name, reason in missing.items()
        )
    metrics = {
        "tier": ev.tier,
        "date_range": [start, end],
        "backtest_metrics": result.cumulative_metrics or result.oos_metrics,
        "benchmarks": list(benchmarks),
        "missing_benchmarks": missing,
    }
    return EvaluationSection(label, params_source, md, metrics)


def strategy_returns_from_result(result: Any) -> pd.Series:
    curve = getattr(result, "equity_curve", None)
    if curve is None or len(curve) < 2:
        raise RuntimeError("backtest result has no usable equity_curve")
    curve = curve[curve.index >= pd.to_datetime(OOS_START)]
    returns = curve.astype(float).pct_change().dropna()
    if returns.empty:
        raise RuntimeError("OOS equity_curve produced no daily returns")
    returns.name = "cb_arb"
    return returns


def load_available_benchmarks(
    start: str,
    end: str,
    *,
    refresh_benchmarks: bool,
) -> tuple[dict[str, pd.Series], dict[str, str]]:
    cfg = BenchmarkConfig()
    loaded: dict[str, pd.Series] = {}
    missing: dict[str, str] = {}
    for name in DEFAULT_BENCHMARKS:
        try:
            loaded.update(
                load_benchmarks(
                    [name],
                    start,
                    end,
                    config=cfg,
                    refresh=refresh_benchmarks,
                )
            )
        except Exception as exc:
            # Keep the report moving; the missing benchmark is visible.
            if not isinstance(exc, BenchmarkDataError):
                missing[name] = f"{type(exc).__name__}: {exc}"
            else:
                missing[name] = str(exc)
    if "cash" not in loaded or "cb_equal" not in loaded:
        raise RuntimeError(
            f"required benchmarks missing: {missing}. Need at least cash + cb_equal."
        )
    return loaded, missing


def config_for_available_benchmarks(benchmarks: dict[str, pd.Series]) -> EvaluationConfig:
    names = tuple(name for name in DEFAULT_BENCHMARKS if name in benchmarks)
    stretch = "dividend" if "dividend" in benchmarks else "cb_equal"
    return EvaluationConfig(benchmarks=names, stretch_benchmark=stretch)


def build_report(
    sections: list[EvaluationSection],
    runs: list[dict],
    notes: list[str],
) -> str:
    lines = [
        "# cb_arb Evaluation Report",
        "",
        "Generated by `scripts/evaluate_cb_arb_legacy.py`.",
        "",
        "## Notes",
        "",
    ]
    if notes:
        lines.extend(f"- {note}" for note in notes)
    else:
        lines.append("- No data gaps detected.")

    lines.extend(["", "## Summary", ""])
    if sections:
        lines.append("| section | params_source | tier | date_range | benchmarks |")
        lines.append("| --- | --- | --- | --- | --- |")
        for s in sections:
            m = s.metrics
            lines.append(
                "| "
                + " | ".join(
                    [
                        s.label,
                        s.params_source,
                        str(m["tier"]),
                        "~".join(m["date_range"]),
                        ", ".join(m["benchmarks"]),
                    ]
                )
                + " |"
            )
    else:
        lines.append("_No sections evaluated._")

    lines.extend(["", "## LLM Cumulative Excess Trajectory", ""])
    lines.append(trajectory_markdown(runs))

    for s in sections:
        lines.extend(["", f"## {s.label}", "", s.markdown])
    lines.append("")
    return "\n".join(lines)


def trajectory_markdown(runs: list[dict]) -> str:
    if not runs:
        return "_No runs available._"
    rows = []
    for rec in runs:
        bt = rec.get("backtest") or {}
        cum = bt.get("cumulative_metrics") or {}
        oos = bt.get("oos_metrics") or {}
        val = cum.get("excess_return", oos.get("excess_return"))
        hyp = rec.get("hypothesis_attempt") or {}
        rows.append(
            [
                str(rec.get("iteration")),
                _fmt_float(val),
                str(hyp.get("item_path", "")),
                _fmt_float(hyp.get("new_value")),
            ]
        )
    out = ["| iteration | cumulative_excess_return | item_path | new_value |"]
    out.append("| --- | --- | --- | --- |")
    out.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(out)


def _fmt_float(value: Any) -> str:
    try:
        return f"{float(value):.6f}"
    except (TypeError, ValueError):
        return ""


if __name__ == "__main__":
    raise SystemExit(main())
