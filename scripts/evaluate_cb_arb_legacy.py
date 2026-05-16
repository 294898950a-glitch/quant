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
DEFAULT_REPORT = REPO_ROOT / "data" / "cb_arb" / "evaluation" / "cb_arb_evaluation_2026-05-10.yaml"
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
    report_text: str
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
    if run240 is not None:
        sections.append(
            evaluate_params(
                "240 轮末段参数",
                "runs.jsonl iteration=240",
                params_from_run(run240),
                refresh_benchmarks=args.refresh_benchmarks,
            )
        )
    elif runs:
        latest = max(runs, key=lambda r: int(r.get("iteration", -1)))
        latest_iter = int(latest.get("iteration", -1))
        notes.append(
            f"本地 `{args.runs}` 只有 {len(runs)} 条记录, 未找到 iteration=240; "
            f"使用 max iteration={latest_iter} 作为末段代理参数。"
        )
        sections.append(
            evaluate_params(
                f"末段代理参数 iteration={latest_iter}",
                f"runs.jsonl max iteration={latest_iter}",
                params_from_run(latest),
                refresh_benchmarks=args.refresh_benchmarks,
            )
        )
    else:
        notes.append("runs.jsonl 为空, 无法评测末段参数。")

    report = build_report(sections, runs, notes)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "title": "cb_arb legacy parameter evaluation",
        "runs_path": str(args.runs),
        "space_path": str(args.space),
        "notes": notes,
        "sections": [
            {
                "label": s.label,
                "params_source": s.params_source,
                "metrics": s.metrics,
                "report_text": s.report_text,
            }
            for s in sections
        ],
        "combined_report_text": report,
    }
    args.output.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
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
    if "dividend" not in benchmarks and ev.tier == "明确好":
        ev.tier = "值得真上"
        ev.reasons = [
            "tier=值得真上; dividend benchmark missing, 明确好档位不可判定"
        ]
        tier_note = (
            "\n\n## Tier Cap\n\n"
            "- `dividend` benchmark missing; tier capped at `值得真上` until "
            "the stretch benchmark is available."
        )
    report_text = format_evaluation_report(ev, title=f"cb_arb {label}")
    report_text += tier_note
    if missing:
        report_text += "\n\n## Missing Benchmarks\n\n" + "\n".join(
            f"- `{name}`: {reason}" for name, reason in missing.items()
        )
    metrics = {
        "tier": ev.tier,
        "date_range": [start, end],
        "backtest_metrics": result.cumulative_metrics or result.oos_metrics,
        "benchmarks": list(benchmarks),
        "missing_benchmarks": missing,
    }
    return EvaluationSection(label, params_source, report_text, metrics)


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
    payload = {
        "schema_version": 1,
        "title": "cb_arb Evaluation Report",
        "generated_by": "scripts/evaluate_cb_arb_legacy.py",
        "notes": notes or ["No data gaps detected."],
        "network_note": (
            "Network unavailable: csi300 / dividend / sixty_forty may be missing "
            "when eastmoney/akshare refresh is blocked."
        ),
        "summary": [
            {
                "section": s.label,
                "params_source": s.params_source,
                "tier": s.metrics.get("tier"),
                "date_range": s.metrics.get("date_range"),
                "benchmarks": s.metrics.get("benchmarks"),
            }
            for s in sections
        ],
        "llm_cumulative_excess_trajectory": trajectory_rows(runs),
        "sections": [
            {
                "label": s.label,
                "params_source": s.params_source,
                "metrics": s.metrics,
                "evaluation_report_text": s.report_text,
            }
            for s in sections
        ],
    }
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


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


def trajectory_rows(runs: list[dict]) -> list[dict[str, Any]]:
    rows = []
    for rec in runs:
        bt = rec.get("backtest") or {}
        cum = bt.get("cumulative_metrics") or {}
        oos = bt.get("oos_metrics") or {}
        hyp = rec.get("hypothesis_attempt") or {}
        rows.append({
            "iteration": rec.get("iteration"),
            "cumulative_excess_return": cum.get("excess_return", oos.get("excess_return")),
            "item_path": hyp.get("item_path", ""),
            "new_value": hyp.get("new_value"),
        })
    return rows


def _fmt_float(value: Any) -> str:
    try:
        return f"{float(value):.6f}"
    except (TypeError, ValueError):
        return ""


if __name__ == "__main__":
    raise SystemExit(main())
