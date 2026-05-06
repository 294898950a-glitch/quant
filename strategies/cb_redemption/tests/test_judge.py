"""Unit tests for strategies.cb_redemption.judge — 分析师层。

测试目标：
  - 构造 fake BacktestResult，验证 diagnose 返回非空 Diagnosis
  - weak_factors 正确识别 |w|<0.1 的因子
  - by_quarter / by_year 分组聚合正确
  - is_oos_gap 计算正确
  - weakness_text 非空
  - 真实 backtest 通路：load_historical_snapshots 在则跑全链路；不在则跳过
"""

from __future__ import annotations

import pytest

from strategies.cb_redemption.backtest import (
    BacktestResult,
    TradeRecord,
)
from strategies.cb_redemption.judge import (
    Diagnosis,
    diagnose,
)


FACTOR_NAMES = [
    "redeem_progress",
    "premium_ratio",
    "remaining_size",
    "stock_momentum",
    "market_sentiment",
]


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _trade(
    code: str = "123001",
    entry: str = "20240115",
    exit_: str = "20240122",
    pnl_pct: float = 5.0,
    pos: float = 20_000.0,
    holding_days: int = 5,
    reason: str = "take_profit",
) -> TradeRecord:
    return TradeRecord(
        cb_code=code,
        cb_name="测试转债",
        entry_date=entry,
        entry_price=100.0,
        prob_entry=0.7,
        premium_entry=10.0,
        exit_date=exit_,
        exit_price=100.0 * (1 + pnl_pct / 100),
        pnl_pct=pnl_pct,
        pnl_amount=round(pos * pnl_pct / 100, 2),
        holding_days=holding_days,
        exit_reason=reason,
    )


@pytest.fixture
def fake_result() -> BacktestResult:
    """构造一个含 IS / OOS 双窗口的 fake BacktestResult。"""
    is_trades = [
        _trade(code="100001", entry="20230315", exit_="20230322", pnl_pct=8.0),
        _trade(code="100002", entry="20230615", exit_="20230622", pnl_pct=-3.0),
        _trade(code="100003", entry="20231115", exit_="20231122", pnl_pct=12.0),
        _trade(code="100004", entry="20240115", exit_="20240122", pnl_pct=4.0),
        _trade(code="100005", entry="20240515", exit_="20240522", pnl_pct=6.0),
        _trade(code="100006", entry="20240815", exit_="20240822", pnl_pct=-5.0),
    ]
    oos_trades = [
        _trade(code="200001", entry="20250115", exit_="20250122", pnl_pct=2.0),
        _trade(code="200002", entry="20250415", exit_="20250422", pnl_pct=-6.0),
        _trade(code="200003", entry="20250515", exit_="20250522", pnl_pct=-4.0),
        _trade(code="200004", entry="20250815", exit_="20250822", pnl_pct=3.0),
    ]
    all_trades = is_trades + oos_trades

    return BacktestResult(
        trades=all_trades,
        all_metrics={
            "total_trades": len(all_trades),
            "win_rate": 60.0,
            "avg_return": 1.7,
            "sharpe": 0.6,
            "total_pnl": 17000.0,
        },
        is_metrics={
            "total_trades": len(is_trades),
            "win_rate": 66.7,
            "avg_return": 3.67,
            "sharpe": 0.85,
        },
        oos_metrics={
            "total_trades": len(oos_trades),
            "win_rate": 50.0,
            "avg_return": -1.25,
            "sharpe": 0.20,
        },
        date_range=("20230101", "20260424"),
    )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_diagnose_returns_non_empty_diagnosis(fake_result):
    weights = [2.2, -0.7, -3.7, 1.9, -0.6]
    diag = diagnose(fake_result, weights, FACTOR_NAMES)

    assert isinstance(diag, Diagnosis)
    # 非空字段
    assert diag.weakness_text
    assert len(diag.factor_contributions) == 5
    assert diag.by_quarter, "by_quarter 应非空（fake_result 有交易）"
    assert diag.by_year, "by_year 应非空"

    # to_dict 自洽
    d = diag.to_dict()
    assert set(d.keys()) >= {
        "is_oos_gap_sharpe",
        "is_oos_gap_winrate",
        "by_quarter",
        "by_year",
        "factor_contributions",
        "weak_factors",
        "drawdown_max",
        "drawdown_periods",
        "weakness_text",
    }


def test_weak_factors_threshold(fake_result):
    # 第 2、第 5 个因子权重 |w| < 0.1
    weights = [2.0, 0.05, -1.0, 0.5, -0.09]
    diag = diagnose(fake_result, weights, FACTOR_NAMES)

    assert "premium_ratio" in diag.weak_factors
    assert "market_sentiment" in diag.weak_factors
    assert "redeem_progress" not in diag.weak_factors
    assert "remaining_size" not in diag.weak_factors
    assert "stock_momentum" not in diag.weak_factors

    # 边界：恰好 0.1 不算弱
    weights_edge = [0.1, -0.1, 0.099, -0.099, 0.5]
    diag2 = diagnose(fake_result, weights_edge, FACTOR_NAMES)
    assert diag2.weak_factors == ["remaining_size", "stock_momentum"]


def test_by_quarter_grouping(fake_result):
    weights = [1.0, 1.0, 1.0, 1.0, 1.0]
    diag = diagnose(fake_result, weights, FACTOR_NAMES)

    quarters = {row["period"] for row in diag.by_quarter}
    # IS 6 笔分布在 2023Q1/Q2/Q4 + 2024Q1/Q2/Q3；OOS 4 笔在 2025Q1/Q2/Q3
    assert "2023Q1" in quarters
    assert "2024Q3" in quarters
    assert "2025Q2" in quarters

    # 2025Q2 应该有 2 笔（200002 + 200003）
    q_2025q2 = next(r for r in diag.by_quarter if r["period"] == "2025Q2")
    assert q_2025q2["n_trades"] == 2
    # 两笔均亏 → winrate 0
    assert q_2025q2["winrate"] == 0.0

    # by_year 同理
    years = {row["period"] for row in diag.by_year}
    assert {"2023", "2024", "2025"} <= years
    y_2025 = next(r for r in diag.by_year if r["period"] == "2025")
    assert y_2025["n_trades"] == 4


def test_is_oos_gap_calculation(fake_result):
    weights = [1.0, 1.0, 1.0, 1.0, 1.0]
    diag = diagnose(fake_result, weights, FACTOR_NAMES)

    # is_sharpe=0.85, oos_sharpe=0.20 → gap=0.65
    assert diag.is_oos_gap_sharpe == pytest.approx(0.65, abs=1e-6)
    # is_winrate=66.7, oos_winrate=50.0 → gap=16.7
    assert diag.is_oos_gap_winrate == pytest.approx(16.7, abs=1e-6)


def test_weakness_text_non_empty_and_descriptive(fake_result):
    weights = [2.0, 0.05, -1.0, 0.5, -0.5]  # premium_ratio 弱
    diag = diagnose(fake_result, weights, FACTOR_NAMES)

    text = diag.weakness_text
    assert text and isinstance(text, str)
    # 含 IS/OOS 描述
    assert "IS" in text and "OOS" in text
    # 含弱因子提示
    assert "premium_ratio" in text
    # 不含建议性词汇（机械检查避免 LLM 化）
    forbidden = ["建议", "应该", "推荐", "你去", "我建议"]
    for w in forbidden:
        assert w not in text, f"weakness_text 不应含建议词 '{w}'"


def test_factor_contributions_ranked():
    """abs_weight_rank 应按 |weight| 降序赋值。"""
    bt = BacktestResult(trades=[], date_range=("20230101", "20260424"))
    weights = [2.2, -0.7, -3.7, 1.9, -0.6]  # |w| 排序: -3.7, 2.2, 1.9, -0.7, -0.6
    diag = diagnose(bt, weights, FACTOR_NAMES)

    by_name = {c["name"]: c for c in diag.factor_contributions}
    assert by_name["remaining_size"]["abs_weight_rank"] == 1
    assert by_name["redeem_progress"]["abs_weight_rank"] == 2
    assert by_name["stock_momentum"]["abs_weight_rank"] == 3
    assert by_name["premium_ratio"]["abs_weight_rank"] == 4
    assert by_name["market_sentiment"]["abs_weight_rank"] == 5

    # 空 trades 时 drawdown 降级
    assert diag.drawdown_max == 0.0
    assert diag.drawdown_periods == 0
    assert "无 equity 序列" in diag.weakness_text


def test_drawdown_estimated_from_trades():
    """构造一段 PnL 序列，验证回撤估算。"""
    # 交易序列：+10k, +10k, -30k, +5k → equity: 0, 10k, 20k, -10k, -5k
    # 峰值 20k → 谷 -10k：回撤幅度 (20-(-10))/20 = 150%
    trades = [
        _trade(code=f"00{i}", entry="20240101", exit_=f"2024010{i+2}", pnl_pct=p)
        for i, p in enumerate([5.0, 5.0, -15.0, 2.5])
    ]
    bt = BacktestResult(
        trades=trades,
        is_metrics={"sharpe": 0.5, "win_rate": 75.0, "total_trades": 4},
        oos_metrics={"sharpe": 0.0, "win_rate": 0.0, "total_trades": 0},
        date_range=("20240101", "20240110"),
    )
    weights = [1.0, 1.0, 1.0, 1.0, 1.0]
    diag = diagnose(bt, weights, FACTOR_NAMES)

    assert diag.drawdown_max > 5.0, "应识别出 >5% 的回撤"
    assert diag.drawdown_periods >= 1


def test_dimension_mismatch_raises(fake_result):
    with pytest.raises(ValueError, match="长度不一致"):
        diagnose(fake_result, [1.0, 2.0], FACTOR_NAMES)


# --------------------------------------------------------------------------- #
# 真实 backtest 通路（数据存在才跑）
# --------------------------------------------------------------------------- #


def test_diagnose_against_real_backtest():
    """跑一次真实 backtest 入口，验证 judge 能正常诊断。

    若历史快照不存在 → 跳过，不让 CI 因数据缺失炸掉。
    """
    from strategies.cb_redemption import config
    from strategies.cb_redemption.backtest import (
        BacktestConfig,
        run_backtest_core,
    )

    try:
        from strategies.cb_redemption.data import load_historical_snapshots
        snapshots = load_historical_snapshots("20230101", "20260424")
    except (FileNotFoundError, Exception) as exc:
        pytest.skip(f"历史快照不可用，跳过真实回测诊断：{exc}")

    if snapshots is None or len(snapshots) == 0:
        pytest.skip("历史快照为空")

    cfg = BacktestConfig(alert_threshold=config.DEFAULT_THRESHOLDS_CONFIG.get("alert", 0.6))
    result = run_backtest_core(
        snapshots, config.LOGIT_WEIGHTS, config.DEFAULT_THRESHOLDS_CONFIG, cfg
    )

    diag = diagnose(result, config.LOGIT_WEIGHTS, FACTOR_NAMES)
    assert isinstance(diag, Diagnosis)
    assert diag.weakness_text
    assert len(diag.factor_contributions) == len(config.LOGIT_WEIGHTS)
    # 不出建议
    for forbidden in ["建议", "应该", "推荐"]:
        assert forbidden not in diag.weakness_text
