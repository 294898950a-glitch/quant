"""Public API for the reusable strategy evaluation framework."""

from __future__ import annotations

from framework.evaluation.backtest_metrics import BacktestResult, compute_backtest_metrics
from framework.evaluation.benchmark import (
    align_dates,
    compute_cumulative_excess,
    compute_excess_returns,
    load_benchmark,
    load_benchmark_total_return,
)
from framework.evaluation.costs import CostConfig, apply_costs, gross_to_net_cash
from framework.evaluation.contract import EvaluationPlan, EvaluationReport, GateResult, GateStatus, validate_plan_complete
from framework.evaluation.evidence import EvidenceItem, measurement_evidence
from framework.evaluation.validation import evaluate_plan
from framework.evaluation.research_feedback import (
    ClaudeReview,
    ExplorationPacket,
    ResearchInput,
    ResearchQuestion,
    ReviewVerdict,
    ScoreVector,
    accept_claude_review,
    validate_research_input_proposal,
)
from framework.evaluation.research_question import QuestionPolicy, QuestionState, ResearchQuestionLedger, derive_state
from framework.evaluation.lifecycle_cutover import compare_window
from framework.evaluation.metrics import (
    annualized_return,
    avg_hold_days,
    avg_win_loss,
    calmar_ratio,
    cumulative_return,
    max_drawdown,
    profit_factor,
    sharpe_ratio,
    sortino_ratio,
    total_return,
    volatility_annualized,
    win_rate,
)

__all__ = [
    "align_dates",
    "annualized_return",
    "apply_costs",
    "avg_hold_days",
    "avg_win_loss",
    "BacktestResult",
    "calmar_ratio",
    "compute_backtest_metrics",
    "compute_cumulative_excess",
    "compute_excess_returns",
    "CostConfig",
    "EvaluationPlan",
    "EvaluationReport",
    "EvidenceItem",
    "GateResult",
    "GateStatus",
    "cumulative_return",
    "gross_to_net_cash",
    "load_benchmark",
    "load_benchmark_total_return",
    "max_drawdown",
    "measurement_evidence",
    "profit_factor",
    "sharpe_ratio",
    "sortino_ratio",
    "total_return",
    "validate_plan_complete",
    "evaluate_plan",
    "ClaudeReview",
    "ExplorationPacket",
    "ResearchInput",
    "ResearchQuestion",
    "ReviewVerdict",
    "ScoreVector",
    "accept_claude_review",
    "validate_research_input_proposal",
    "QuestionPolicy",
    "QuestionState",
    "ResearchQuestionLedger",
    "derive_state",
    "compare_window",
    "volatility_annualized",
    "win_rate",
]
