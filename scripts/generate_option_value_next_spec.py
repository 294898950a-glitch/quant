"""Generate the next option false-undervaluation research spec from injected context.

This is the ideation entrypoint for option_value_loop. It injects the current
machine-readable research state into a Claude prompt, asks for one
implementable next test, records the prompt/response, then compiles that idea
into a READY spec.yaml.
"""

from __future__ import annotations

import argparse
import sys
import csv
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from hermes_access_guard import require_ticket


REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = REPO_ROOT / "data" / "research_framework" / "option_value_loop.yaml"
CURRENT_PATH = REPO_ROOT / "data" / "research_framework" / "current.yaml"
INSIGHTS_PATH = REPO_ROOT / "data" / "research_framework" / "research_insights.yaml"
EXPERIMENTS_PATH = REPO_ROOT / "data" / "research_framework" / "experiments.yaml"
RUNTIME_ENTRYPOINTS_PATH = REPO_ROOT / "data" / "research_framework" / "runtime_entrypoints.yaml"
IDEATION_LOG_DIR = REPO_ROOT / "logs" / "option_value_ideation"
BASE_RANKS_PATH = (
    "data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/"
    "daily_value_gap_amounts.parquet"
)
POSITION_EXECUTOR = "option_position_sizing"
PNL_FEEDBACK_EXECUTOR = "option_source_pnl_feedback_scaler"
SUPPORTED_EXECUTORS = {POSITION_EXECUTOR, PNL_FEEDBACK_EXECUTOR}
SUPPORTED_FAMILIES = {"option_position_sizing", "option_source_pnl_feedback"}
RESULT_FILES = (
    "summary.json",
    "report.yaml",
    "diagnostic.yaml",
    "l4_ack.yaml",
    "summary_option_value_haircut.csv",
    "summary_option_entry_gate.csv",
    "summary_regime_option_entry_gate.csv",
    "summary_universe_filter.csv",
)


def _load_yaml(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return default if data is None else data


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...[truncated]\n"


def _runtime_context(max_file_chars: int = 12000) -> dict[str, Any]:
    entry = _load_yaml(RUNTIME_ENTRYPOINTS_PATH, {})
    files = ((entry.get("runtime_context") or {}).get("files") or {}) if isinstance(entry, dict) else {}
    loaded: dict[str, Any] = {}
    for name, meta in files.items():
        if not isinstance(meta, dict):
            continue
        rel_path = meta.get("path")
        if not rel_path:
            continue
        path = REPO_ROOT / str(rel_path)
        if not path.exists():
            loaded[name] = {"missing": str(rel_path)}
            continue
        loaded[name] = {
            "path": str(rel_path),
            "role": meta.get("role"),
            "content": _truncate(path.read_text(encoding="utf-8"), max_file_chars),
        }
    return loaded


def _recent_result_context(max_runs: int = 8, max_chars: int = 6000) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    for spec_path in sorted((REPO_ROOT / "data").glob("*/spec.yaml"), key=lambda p: p.stat().st_mtime, reverse=True):
        run_dir = spec_path.parent
        item: dict[str, Any] = {"run_id": run_dir.name, "files": {}}
        try:
            spec = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
            if isinstance(spec, dict):
                item["status"] = spec.get("status")
                item["strategy_id"] = spec.get("strategy_id")
                item["hypothesis"] = spec.get("hypothesis")
        except Exception:
            pass
        for file_name in RESULT_FILES:
            path = run_dir / file_name
            if not path.exists():
                continue
            if path.suffix == ".csv":
                try:
                    with path.open("r", encoding="utf-8", newline="") as fh:
                        rows = list(csv.DictReader(fh))[:8]
                    item["files"][file_name] = rows
                except Exception:
                    item["files"][file_name] = _truncate(path.read_text(encoding="utf-8", errors="ignore"), max_chars)
            else:
                item["files"][file_name] = _truncate(path.read_text(encoding="utf-8", errors="ignore"), max_chars)
        runs.append(item)
        if len(runs) >= max_runs:
            break
    return {"recent_runs": runs}


def _safe_yaml_from_text(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    data = yaml.safe_load(cleaned)
    if not isinstance(data, dict):
        raise ValueError("LLM response must be a YAML object")
    return data


def _build_prompt(
    state: dict[str, Any],
    strategy_id: str,
    retry_feedback: dict[str, Any] | None = None,
) -> str:
    context = _runtime_context()
    recent_results = _recent_result_context()
    forbidden = state.get("forbidden_families") if isinstance(state, dict) else []
    payload = {
        "task": "根据现有研究记录，提出下一轮可自动执行的 cb_arb 期权错误估值修复测试。",
        "strategy_id": strategy_id,
        "hard_limits": [
            "不能要求本地跑 cb_arb 回测，只能生成 READY spec 后交给 VM/spot host。",
            "不能重复 forbidden_families 里的方向。",
            "不能修改 current.yaml 或 baseline_registry.yaml。",
            "必须保留 2025-2026 作为测试段。",
            "你必须自己选择 available_executors 中已经存在的 executor/family，不能输出新名字。",
            "输出必须是 YAML 对象，不能有 Markdown 解释。",
        ],
        "forbidden_families": forbidden,
        "available_executors": [
            {
                "executor": POSITION_EXECUTOR,
                "family": "option_position_sizing",
                "meaning": "不改理论价格，不禁买；只对可疑期权型候选降低排序金额和实际买入金额。",
                "script": "scripts/evaluate_cb_arb_option_position_sizing.py",
                "reason": "已有证据显示禁买和整体期权折价会砍掉后续期权收益；降低暴露更适合先测。",
            },
            {
                "executor": PNL_FEEDBACK_EXECUTOR,
                "family": "option_source_pnl_feedback",
                "meaning": "不改理论价格，不禁买；如果最近已平仓的期权来源交易在亏钱，就降低后续期权来源候选排序金额和实际买入金额。",
                "script": "scripts/evaluate_cb_arb_option_pnl_feedback.py",
                "reason": "当前卡住的方向就是期权来源错误估值的反馈修正，已实现为可自动运行执行器。",
            },
        ],
        "execution_environment": {
            "local_role": "only prompt injection, spec generation, validation, sync, and monitoring",
            "backtest_host": state.get("vm_host") if isinstance(state, dict) else None,
            "remote_repo": state.get("remote_repo") if isinstance(state, dict) else None,
            "max_auto_budget_yuan": state.get("max_auto_budget_yuan") if isinstance(state, dict) else 100,
            "poll_seconds": state.get("poll_seconds") if isinstance(state, dict) else 600,
        },
        "recent_result_artifacts": recent_results,
        "required_output_schema": {
            "proposal_id": "short_snake_case",
            "executor": f"one of {sorted(SUPPORTED_EXECUTORS)}",
            "family": f"one of {sorted(SUPPORTED_FAMILIES)}, and must match executor",
            "hypothesis": "中文，一句话说明为什么这个测试可能有效",
            "source_insight": "中文，说明从哪些失败/结果推出来",
            "why_not_rejected_repeat": "中文，说明为什么不是重复旧的禁买/整体折价方向",
            "risk": "中文，说明最大风险",
        },
        "executor_family_contract": {
            POSITION_EXECUTOR: "family 必须是 option_position_sizing",
            PNL_FEEDBACK_EXECUTOR: "family 必须是 option_source_pnl_feedback",
        },
        "runtime_context": context,
    }
    if retry_feedback:
        payload["previous_attempt_failed_local_check"] = retry_feedback
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


def _fallback_idea(insight: dict[str, Any]) -> dict[str, Any]:
    return {
        "proposal_id": "rolling_option_source_pnl_feedback",
        "executor": PNL_FEEDBACK_EXECUTOR,
        "family": "option_source_pnl_feedback",
        "hypothesis": "当最近已平仓的期权来源交易正在亏钱时，后续期权来源候选只降低投入和排序权重，可能减少错误期权估值暴露，同时保留正常期权收益。",
        "source_insight": str(
            insight.get("summary")
            or "禁买和整体期权折价可以改善 2020，但会伤害 2025-2026 的期权来源收益。"
        ),
        "why_not_rejected_repeat": "它不是永久禁买，也不是整体期权折价；只有已实现的期权来源交易开始亏钱后，才暂时降低后续投入。",
        "risk": "如果错误来自估值本身而不是仓位暴露，降低仓位只能缓解亏损，不能修复判断。",
    }


def _validate_idea_contract(idea: dict[str, Any]) -> None:
    executor = str(idea.get("executor") or "").strip()
    family = str(idea.get("family") or "").strip()
    if executor not in SUPPORTED_EXECUTORS:
        raise RuntimeError(f"Claude proposed unsupported executor={executor!r}; no READY spec generated")
    if family not in SUPPORTED_FAMILIES:
        raise RuntimeError(f"Claude proposed unsupported family={family!r}; no READY spec generated")
    expected = {
        POSITION_EXECUTOR: "option_position_sizing",
        PNL_FEEDBACK_EXECUTOR: "option_source_pnl_feedback",
    }[executor]
    if family != expected:
        raise RuntimeError(
            f"Claude proposed mismatched executor/family: executor={executor!r}, family={family!r}, "
            f"expected family={expected!r}; no READY spec generated"
        )


def _local_check_feedback(exc: Exception, idea: dict[str, Any]) -> dict[str, Any]:
    return {
        "error": str(exc),
        "bad_output": idea,
        "allowed_executor_family_pairs": [
            {
                "executor": POSITION_EXECUTOR,
                "family": "option_position_sizing",
            },
            {
                "executor": PNL_FEEDBACK_EXECUTOR,
                "family": "option_source_pnl_feedback",
            },
        ],
        "rewrite_instruction": (
            "保留研究判断，但必须重写 executor/family。"
            "如果你的想法无法用这两组动作表达，就换一个能用现有动作表达的新想法。"
            "不要解释，只输出完整 YAML。"
        ),
    }


def _ideate(state: dict[str, Any], strategy_id: str, insight: dict[str, Any], force_fallback: bool) -> dict[str, Any]:
    IDEATION_LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if force_fallback:
        idea = _fallback_idea(insight)
        idea["_ideation_source"] = "fallback"
        idea["_fallback_reason"] = "forced fallback"
        _validate_idea_contract(idea)
        (IDEATION_LOG_DIR / f"{stamp}_idea.yaml").write_text(
            yaml.safe_dump(idea, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return idea
    raise RuntimeError(
        "background LLM ideation is disabled; provide --idea-yaml generated by Hermes in the current turn"
    )


def _run_known(run_id: str) -> bool:
    run_dir = REPO_ROOT / "data" / run_id
    if run_dir.exists():
        return True
    experiments = _load_yaml(EXPERIMENTS_PATH, [])
    if isinstance(experiments, list):
        return any(isinstance(x, dict) and x.get("id") == run_id for x in experiments)
    if isinstance(experiments, dict):
        values = experiments.get("experiments") or []
        return any(isinstance(x, dict) and x.get("id") == run_id for x in values)
    return False


def _option_insight() -> dict[str, Any]:
    insights = _load_yaml(INSIGHTS_PATH, {})
    items = insights.get("insights") if isinstance(insights, dict) else insights
    if not isinstance(items, list):
        return {}
    for item in reversed(items):
        if not isinstance(item, dict):
            continue
        text = " ".join(
            str(item.get(k, ""))
            for k in ("id", "summary", "decision_use")
        ).lower()
        if "option" in text or "期权" in text:
            return item
    return {}


def _current_strategy_id() -> str:
    current = _load_yaml(CURRENT_PATH, {})
    if not isinstance(current, dict):
        return "cb_arb_value_gap_switch"
    runtime = current.get("runtime") or {}
    if isinstance(runtime, dict) and runtime.get("current_main_strategy_id"):
        return str(runtime["current_main_strategy_id"])
    return str(current.get("current_main_strategy_id") or "cb_arb_value_gap_switch")


def _build_position_sizing_spec(run_id: str, strategy_id: str, idea: dict[str, Any], insight: dict[str, Any]) -> dict[str, Any]:
    source = idea.get("source_insight") or insight.get("summary") or (
        "Prior option-source runs show global entry gates and global option haircuts can repair "
        "2020 but destroy later option upside."
    )
    output_dir = f"data/{run_id}"
    return {
        "schema_version": 1,
        "run_id": run_id,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "strategy_id": strategy_id,
        "l0_entry_id": 1,
        "l0_source": (
            "option_value_loop Claude prompt injection from current.yaml, research_insights.yaml, "
            "experiments.yaml, baseline_registry.yaml, strategies.yaml, and protocol_rules.yaml"
        ),
        "hypothesis": str(
            idea.get("hypothesis")
            or "期权来源的错误低估不应继续用禁买或整体折价处理；对已经明显股票化、远离债底的期权型候选，只降低买入金额和排序权重，可能减少错误期权暴露，同时保留正常期权修复收益。"
        ),
        "source_insight": str(source),
        "parameter_space": [
            {
                "name": "position_scale_variant",
                "range": [0, 16],
                "type": "int",
                "description": "17 个固定候选仓位调整方案，包括 baseline、价格/债底触发、正股/转股价触发、任一触发、同时触发和渐进调整。",
            },
            {
                "name": "close_to_bond_floor_threshold",
                "range": [1.5, 1.7],
                "type": "float",
                "description": "期权型候选中，转债价格相对债底过高的触发线。",
            },
            {
                "name": "moneyness_stock_to_conv_threshold",
                "range": [1.6, 2.0],
                "type": "float",
                "description": "期权型候选中，正股价格相对转股价过高的触发线。",
            },
            {
                "name": "position_cash_scale",
                "range": [0.25, 1.0],
                "type": "float",
                "description": "触发后实际买入金额和排序金额保留比例。",
            },
        ],
        "new_data_sources": [
            {
                "path": BASE_RANKS_PATH,
                "description": "已有 value-gap 估值与可交易金额，避免重复计算基础基准。",
            },
            {
                "path": "data/cb_warehouse/cb_basic.parquet",
                "description": "转股价和正股代码，用于计算正股/转股价。",
            },
            {
                "path": "data/cb_warehouse/stk_daily_qfq.parquet",
                "description": "前复权正股价格，用于计算正股/转股价。",
            },
        ],
        "grid": {
            "dimensions": ["position_scale_variant"],
            "candidates_count": 17,
            "description": "固定 17 组候选仓位调整方案，全量跑 train/test/2019-2024 yearly，并输出 2020 和 test 的入口来源拆解。",
        },
        "hard_floors": {
            "train_excess_return": 0.0,
            "train_max_drawdown": -0.3,
            "test_excess_return": 0.0,
            "test_max_drawdown": -0.3,
            "replay_2020_total_return": -0.090593,
        },
        "hard_floors_baseline_source": "current cb_arb_value_gap_switch cost-aware branch and option-value-haircut rejection evidence",
        "auxiliary_metrics": [
            "train_excess_return",
            "test_excess_return",
            "yearly_2020_total_return",
            "yearly_2020_excess_return",
            "max_drawdown",
            "total_trades",
            "win_rate",
            "entry_source_2020_option_loss",
            "entry_source_test_option_profit",
            "average_position_scale",
        ],
        "cv_design": "single-window",
        "cv_holdout_years": [2025, 2026],
        "cv_adoption_threshold": "必须同时优于 baseline 的训练段、2020 修复和 2025-2026 测试段；否则只作为诊断资产。",
        "compute_estimate": {
            "sig_minutes": 0,
            "spot_minutes": 60,
            "local_minutes": 0,
            "estimated_cost_yuan": 0.0,
        },
        "budget_cap_yuan": 100,
        "spot_decision": "本轮使用已有 daily_value_gap_amounts，不重复计算基础基准；必须在 VM/spot host 执行。",
        "stop_conditions": [
            "脚本退出非 0",
            "缺少 summary_option_position_sizing.csv",
            "缺少 yearly_option_position_sizing.csv",
            "缺少 entry_source_2020_option_position_sizing.csv",
            "所有非 baseline 方案无法同时优于 train、2020 和 test baseline",
        ],
        "artifacts_required": [
            "summary_option_position_sizing.csv",
            "yearly_option_position_sizing.csv",
            "entry_source_2020_option_position_sizing.csv",
            "entry_source_test_option_position_sizing.csv",
            "adjustment_option_position_sizing.csv",
            "summary.json",
            "report.yaml",
            "l4_ack.yaml",
            "diagnostic.yaml",
        ],
        "automation": {
            "output_dir": output_dir,
            "gatekeeper_after_run": False,
            "sync_paths": [
                "scripts/evaluate_cb_arb_option_position_sizing.py",
            "scripts/evaluate_cb_arb_value_gap_switch.py",
            "scripts/generate_option_value_next_spec.py",
            "data/research_framework/runtime_entrypoints.yaml",
                BASE_RANKS_PATH,
                "data/cb_warehouse/cb_basic.parquet",
                "data/cb_warehouse/stk_daily_qfq.parquet",
            ],
            "command": [
                ".venv/bin/python",
                "scripts/evaluate_cb_arb_option_position_sizing.py",
                "--data-root",
                "data/cb_arb_concurrent_supervised_20260511_094500",
                "--train-start",
                "20190101",
                "--train-end",
                "20241231",
                "--test-start",
                "20250101",
                "--test-end",
                "20260508",
                "--fixed-source",
                "2",
                "--rule",
                "score_4state",
                "--output-dir",
                "{output_dir}",
                "--base-ranks-path",
                BASE_RANKS_PATH,
                "--reuse-ranks",
                "--cost-model-enabled",
            ],
            "verdict": {
                "table_path": "summary_option_position_sizing.csv",
                "yearly_table_path": "yearly_option_position_sizing.csv",
                "filters": {"period": "train"},
                "rank_by": "score",
                "rank_desc": True,
                "thresholds": {
                    "excess_return": {"min": 0.0},
                    "max_drawdown": {"min": -0.3},
                },
                "train_single_year_dd_ceiling": -0.15,
            },
        },
        "status": "READY",
        "escalation": [
            "训练段过关但 2020 仍显著跑输",
            "2020 改善但 2025-2026 测试段被明显伤害",
            "运行时间超过 60 分钟",
        ],
        "notes": (
            "这是循环根据既有复盘自动注入上下文后生成的新方向，不改 current.yaml，不改 baseline_registry。"
            "关键区别：不禁止买入，不整体折价期权价值，只降低可疑期权型候选的实际暴露。"
        ),
        "ideation": {
            "source": idea.get("_ideation_source"),
            "proposal_id": idea.get("proposal_id"),
            "family": idea.get("family"),
            "why_not_rejected_repeat": idea.get("why_not_rejected_repeat"),
            "risk": idea.get("risk"),
            "fallback_reason": idea.get("_fallback_reason"),
        },
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _build_pnl_feedback_spec(run_id: str, strategy_id: str, idea: dict[str, Any], insight: dict[str, Any]) -> dict[str, Any]:
    source = idea.get("source_insight") or insight.get("summary") or (
        "Prior option-source runs show static gates and global option haircuts can repair "
        "some 2020 losses but damage later option upside."
    )
    output_dir = f"data/{run_id}"
    return {
        "schema_version": 1,
        "run_id": run_id,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "strategy_id": strategy_id,
        "l0_entry_id": 1,
        "l0_source": (
            "option_value_loop Claude prompt injection from current.yaml, research_insights.yaml, "
            "experiments.yaml, baseline_registry.yaml, strategies.yaml, and protocol_rules.yaml"
        ),
        "hypothesis": str(
            idea.get("hypothesis")
            or "当最近已平仓的期权来源交易正在亏钱时，后续期权来源候选只降低投入和排序权重，可能减少错误期权估值暴露，同时保留正常期权收益。"
        ),
        "source_insight": str(source),
        "parameter_space": [
            {
                "name": "option_source_pnl_feedback_lookback_days",
                "range": [20, 60],
                "type": "int",
                "description": "只看最近已平仓期权来源交易的天数。",
            },
            {
                "name": "option_source_pnl_feedback_min_trades",
                "range": [2, 3],
                "type": "int",
                "description": "触发反馈前至少需要看到的已平仓期权来源交易数。",
            },
            {
                "name": "option_source_pnl_feedback_scale",
                "range": [0.25, 0.75],
                "type": "float",
                "description": "触发后期权来源候选排序金额和实际买入金额保留比例。",
            },
        ],
        "new_data_sources": [
            {
                "path": BASE_RANKS_PATH,
                "description": "已有 value-gap 估值与可交易金额，避免重复计算基础基准。",
            },
            {
                "path": "data/cb_warehouse/cb_basic.parquet",
                "description": "转股价和正股代码，复用共享 rank loader。",
            },
            {
                "path": "data/cb_warehouse/stk_daily_qfq.parquet",
                "description": "前复权正股价格，复用共享 rank loader。",
            },
        ],
        "grid": {
            "dimensions": [
                "option_source_pnl_feedback_lookback_days",
                "option_source_pnl_feedback_min_trades",
                "option_source_pnl_feedback_scale",
            ],
            "candidates_count": 19,
            "description": "baseline + 18 组滚动期权来源盈亏反馈，全量跑 train/test/2019-2024 yearly，并输出 2020 和 test 的入口来源拆解。",
        },
        "hard_floors": {
            "train_excess_return": 0.0,
            "train_max_drawdown": -0.3,
            "test_excess_return": 0.0,
            "test_max_drawdown": -0.3,
            "replay_2020_total_return": -0.090593,
        },
        "hard_floors_baseline_source": "current cb_arb_value_gap_switch cost-aware branch and option-source false-undervaluation evidence",
        "auxiliary_metrics": [
            "train_excess_return",
            "test_excess_return",
            "yearly_2020_total_return",
            "yearly_2020_excess_return",
            "max_drawdown",
            "total_trades",
            "win_rate",
            "entry_source_2020_option_loss",
            "entry_source_test_option_profit",
            "average_position_scale",
        ],
        "cv_design": "single-window",
        "cv_holdout_years": [2025, 2026],
        "cv_adoption_threshold": "必须同时优于 baseline 的训练段、2020 修复和 2025-2026 测试段；否则只作为诊断资产。",
        "compute_estimate": {
            "sig_minutes": 0,
            "spot_minutes": 90,
            "local_minutes": 0,
            "estimated_cost_yuan": 0.0,
        },
        "budget_cap_yuan": 100,
        "spot_decision": "本轮使用已有 daily_value_gap_amounts，不重复计算基础基准；必须在 VM/spot host 执行。",
        "stop_conditions": [
            "脚本退出非 0",
            "缺少 summary_option_pnl_feedback.csv",
            "缺少 yearly_option_pnl_feedback.csv",
            "缺少 entry_source_2020_option_pnl_feedback.csv",
            "所有非 baseline 方案无法同时优于 train、2020 和 test baseline",
        ],
        "artifacts_required": [
            "summary_option_pnl_feedback.csv",
            "yearly_option_pnl_feedback.csv",
            "entry_source_2020_option_pnl_feedback.csv",
            "entry_source_test_option_pnl_feedback.csv",
            "feedback_option_pnl_feedback.csv",
            "summary.json",
            "report.yaml",
            "l4_ack.yaml",
            "diagnostic.yaml",
        ],
        "automation": {
            "output_dir": output_dir,
            "gatekeeper_after_run": False,
            "sync_paths": [
                "scripts/evaluate_cb_arb_option_pnl_feedback.py",
                "scripts/evaluate_cb_arb_option_position_sizing.py",
                "scripts/evaluate_cb_arb_value_gap_switch.py",
                "scripts/generate_option_value_next_spec.py",
                "data/research_framework/runtime_entrypoints.yaml",
                BASE_RANKS_PATH,
                "data/cb_warehouse/cb_basic.parquet",
                "data/cb_warehouse/stk_daily_qfq.parquet",
            ],
            "command": [
                ".venv/bin/python",
                "scripts/evaluate_cb_arb_option_pnl_feedback.py",
                "--data-root",
                "data/cb_arb_concurrent_supervised_20260511_094500",
                "--train-start",
                "20190101",
                "--train-end",
                "20241231",
                "--test-start",
                "20250101",
                "--test-end",
                "20260508",
                "--fixed-source",
                "2",
                "--rule",
                "score_4state",
                "--output-dir",
                "{output_dir}",
                "--base-ranks-path",
                BASE_RANKS_PATH,
                "--reuse-ranks",
                "--cost-model-enabled",
            ],
            "verdict": {
                "table_path": "summary_option_pnl_feedback.csv",
                "yearly_table_path": "yearly_option_pnl_feedback.csv",
                "filters": {"period": "train"},
                "rank_by": "score",
                "rank_desc": True,
                "thresholds": {
                    "excess_return": {"min": 0.0},
                    "max_drawdown": {"min": -0.3},
                },
                "train_single_year_dd_ceiling": -0.15,
            },
        },
        "status": "READY",
        "escalation": [
            "训练段过关但 2020 仍显著跑输",
            "2020 改善但 2025-2026 测试段被明显伤害",
            "运行时间超过 90 分钟",
        ],
        "notes": (
            "这是循环根据既有复盘自动注入上下文后生成的新方向，不改 current.yaml，不改 baseline_registry。"
            "关键区别：不禁止买入，不整体折价期权价值，只在已实现的期权来源交易近期亏钱后降低后续暴露。"
        ),
        "ideation": {
            "source": idea.get("_ideation_source"),
            "proposal_id": idea.get("proposal_id"),
            "family": idea.get("family"),
            "executor": idea.get("executor"),
            "why_not_rejected_repeat": idea.get("why_not_rejected_repeat"),
            "risk": idea.get("risk"),
            "fallback_reason": idea.get("_fallback_reason"),
        },
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _build_spec(run_id: str, strategy_id: str, idea: dict[str, Any], insight: dict[str, Any]) -> dict[str, Any]:
    if str(idea.get("executor") or "") == PNL_FEEDBACK_EXECUTOR:
        return _build_pnl_feedback_spec(run_id, strategy_id, idea, insight)
    return _build_position_sizing_spec(run_id, strategy_id, idea, insight)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=STATE_PATH)
    parser.add_argument("--force-fallback", action="store_true")
    parser.add_argument(
        "--idea-yaml",
        type=Path,
        default=None,
        help="Compile a Hermes-provided idea YAML into a READY spec. This script does not call an LLM.",
    )
    args = parser.parse_args()

    require_ticket("generate_option_value_spec")
    state = _load_yaml(args.state, {})
    strategy_id = str(state.get("strategy_id") or _current_strategy_id()) if isinstance(state, dict) else _current_strategy_id()
    insight = _option_insight()
    if args.idea_yaml is None:
        raise RuntimeError(
            "background LLM ideation is disabled; Hermes must provide --idea-yaml from its current turn"
        )
    idea = _load_yaml(args.idea_yaml, {})
    if not isinstance(idea, dict):
        raise ValueError("--idea-yaml must contain a YAML object")
    idea.setdefault("_ideation_source", "hermes_current_turn")
    _validate_idea_contract(idea)
    suffix = (
        "option-pnl-feedback"
        if str(idea.get("executor") or "") == PNL_FEEDBACK_EXECUTOR
        else "option-position-sizing"
    )
    run_id = f"{strategy_id}_{suffix}_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}"
    if _run_known(run_id):
        print(f"exists {run_id}")
        return 0

    run_dir = REPO_ROOT / "data" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    spec = _build_spec(run_id, strategy_id, idea, insight)
    spec_path = run_dir / "spec.yaml"
    spec_path.write_text(
        yaml.safe_dump(spec, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"generated {spec_path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
