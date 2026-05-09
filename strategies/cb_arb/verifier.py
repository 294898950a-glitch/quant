"""cb_arb verifier — 横截面排名信号 + 投资组合回测.

策略:
    每天对全集 CB (~500 只) 算理论价 (cb_pricer.price_cb), 把市场价 / 理论价
    偏离率拿来排名. 排名最低 (相对最便宜) 的进入买入候选, 排名最高 (相对最贵)
    的从持仓里卖掉. 横截面排名自动适应市场冷热 → 不需要宏观过滤器.

接口与其它策略保持一致:

    run_backtest(weights, thresholds, rules, oos_event_ids=None) -> BacktestResult

weights 解包顺序对应 ``tunable_space.yaml`` 的 ``parameters`` 段顺序; 见
:func:`_unpack_weights` 的 docstring.

数据加载:
    cb_basic / cb_daily / cb_call / stk_daily_qfq 均在模块加载时一次性 read,
    缓存于模块级常量, 后续调用只 build view, 不重复 IO.

实现要点:
    - "信号" → "交易" 在 Python 内单线程顺序回放, 方便维护持仓字典.
    - 每天卖出先于买入 (腾仓再补). 强赎触发优先级最高.
    - IS = 全段开始 → 2021-12-31, OOS = 2022-01-01 → 全段末.
    - oos_event_ids 给定时, oos_metrics 在该日期子集上独立算 (sub-curve).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from strategies.cb_arb.cb_pricer import (
    CBSpec,
    CBValuation,
    DEFAULT_CREDIT_SPREAD_BP,
    price_cb,
    realized_vol,
)
from strategies.cb_redemption.result_types import BacktestResult, TradeRecord


# --------------------------------------------------------------------------- #
# 路径常量
# --------------------------------------------------------------------------- #

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent

CB_BASIC_PARQUET = _REPO_ROOT / "data" / "cb_warehouse" / "cb_basic.parquet"
CB_DAILY_PARQUET = _REPO_ROOT / "data" / "cb_warehouse" / "cb_daily.parquet"
CB_CALL_PARQUET = _REPO_ROOT / "data" / "cb_warehouse" / "cb_call.parquet"
STK_DAILY_QFQ_PARQUET = _REPO_ROOT / "data" / "cb_warehouse" / "stk_daily_qfq.parquet"

DEFAULT_YAML_PATH = _HERE / "tunable_space.yaml"

#: IS / OOS 切分日期 (YYYYMMDD).
IS_END = "20211231"
OOS_START = "20220101"

#: 默认 weights 顺序 (与 yaml.parameters 同序).
_DEFAULT_VOL_WINDOW_DAYS = 60
_DEFAULT_VOL_MULTIPLIER = 1.0
_DEFAULT_RANK_BUY_PCT = 0.10
_DEFAULT_RANK_SELL_PCT = 0.50
_DEFAULT_MAX_POSITION_PCT = 0.03
_DEFAULT_MAX_HOLDINGS = 30
_DEFAULT_MAX_HOLDING_DAYS = 90
_DEFAULT_STOP_LOSS_PCT = -0.08
_DEFAULT_MIN_REMAINING_SIZE = 100_000_000.0
_DEFAULT_MIN_AVG_AMOUNT = 1_000_000.0
_DEFAULT_CREDIT_SPREAD_AAA_BP = 50.0
_DEFAULT_CREDIT_SPREAD_AA_BP = 150.0

_DEFAULT_RATING_FLOOR = 2  # AA-
_DEFAULT_FEE_PCT = 0.0003
_DEFAULT_INITIAL_CAPITAL = 1_000_000.0

#: 评级 → int. AA- = 2.
RATING_TO_INT: dict[str, int] = {
    "C": -3, "CC": -2, "CCC": -1,
    "B-": 0, "B": 0, "B+": 0,
    "BB-": 0, "BB": 0, "BB+": 0,
    "BBB": 1, "BBB+": 1,
    "A-": 1,
    "A": 1,
    "A+": 1,
    "AA-": 2,
    "AA": 3,
    "AA+": 4,
    "AAA": 5,
}

#: 算理论价时 vol 上限 (避免极端值 → 期权炸天).
_VOL_CAP = 1.5


# --------------------------------------------------------------------------- #
# 模块级数据缓存
# --------------------------------------------------------------------------- #

_CB_BASIC_CACHE: pd.DataFrame | None = None
_CB_DAILY_CACHE: pd.DataFrame | None = None
_CB_CALL_CACHE: pd.DataFrame | None = None
_STK_DAILY_CACHE: pd.DataFrame | None = None
_TRADING_DAYS_CACHE: list[str] | None = None


def _load_cb_basic() -> pd.DataFrame:
    global _CB_BASIC_CACHE
    if _CB_BASIC_CACHE is None:
        df = pd.read_parquet(CB_BASIC_PARQUET)
        df = df.copy()
        # 派生字段
        df["rating_int"] = df["rating"].map(
            lambda r: RATING_TO_INT.get(r, 0) if isinstance(r, str) else 0
        ).astype(int)
        df["issue_size_yuan"] = df["issue_size"].astype(float) * 1e8  # 单位是亿
        # set ts_code as index for fast lookup
        df = df.set_index("ts_code", drop=False)
        _CB_BASIC_CACHE = df
    return _CB_BASIC_CACHE


def _load_cb_daily() -> pd.DataFrame:
    global _CB_DAILY_CACHE
    if _CB_DAILY_CACHE is None:
        df = pd.read_parquet(CB_DAILY_PARQUET)
        df = df.copy()
        # 单位元的成交额 (vol * close ≈ 万元成交)
        # cb_daily.vol 单位: 张 (1 张面值 100 元).
        # 成交额 ≈ vol * close. 单位元.
        df["amount_yuan"] = df["close"].astype(float) * df["vol"].astype(float)
        df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
        _CB_DAILY_CACHE = df
    return _CB_DAILY_CACHE


def _load_cb_call() -> pd.DataFrame:
    global _CB_CALL_CACHE
    if _CB_CALL_CACHE is None:
        df = pd.read_parquet(CB_CALL_PARQUET)
        df = df.copy()
        # 强赎区间: 按公告日 ann_date → expire_date. 该期间内 CB 视为已强赎.
        df = df[["ts_code", "ann_date", "call_date", "expire_date"]].copy()
        _CB_CALL_CACHE = df
    return _CB_CALL_CACHE


def _load_stk_daily() -> pd.DataFrame:
    global _STK_DAILY_CACHE
    if _STK_DAILY_CACHE is None:
        df = pd.read_parquet(STK_DAILY_QFQ_PARQUET)
        df = df[["stk_code", "trade_date", "close"]].copy()
        df = df.sort_values(["stk_code", "trade_date"]).reset_index(drop=True)
        _STK_DAILY_CACHE = df
    return _STK_DAILY_CACHE


def _load_trading_days() -> list[str]:
    """全市场交易日, 升序."""
    global _TRADING_DAYS_CACHE
    if _TRADING_DAYS_CACHE is None:
        cb_daily = _load_cb_daily()
        days = sorted(set(cb_daily["trade_date"].astype(str).tolist()))
        _TRADING_DAYS_CACHE = days
    return _TRADING_DAYS_CACHE


def reset_cache() -> None:
    """主要给测试用 — 强制重新读取."""
    global _CB_BASIC_CACHE, _CB_DAILY_CACHE, _CB_CALL_CACHE
    global _STK_DAILY_CACHE, _TRADING_DAYS_CACHE
    _CB_BASIC_CACHE = None
    _CB_DAILY_CACHE = None
    _CB_CALL_CACHE = None
    _STK_DAILY_CACHE = None
    _TRADING_DAYS_CACHE = None


# --------------------------------------------------------------------------- #
# weights / rules 解包
# --------------------------------------------------------------------------- #


@dataclass
class CBArbConfig:
    """运行时配置, 由 weights + rules + thresholds 合成."""
    vol_window_days: int = _DEFAULT_VOL_WINDOW_DAYS
    vol_multiplier: float = _DEFAULT_VOL_MULTIPLIER
    rank_buy_pct: float = _DEFAULT_RANK_BUY_PCT
    rank_sell_pct: float = _DEFAULT_RANK_SELL_PCT
    max_position_pct: float = _DEFAULT_MAX_POSITION_PCT
    max_holdings: int = _DEFAULT_MAX_HOLDINGS
    max_holding_days: int = _DEFAULT_MAX_HOLDING_DAYS
    stop_loss_pct: float = _DEFAULT_STOP_LOSS_PCT
    min_remaining_size: float = _DEFAULT_MIN_REMAINING_SIZE
    min_avg_amount: float = _DEFAULT_MIN_AVG_AMOUNT
    credit_spread_aaa_bp: float = _DEFAULT_CREDIT_SPREAD_AAA_BP
    credit_spread_aa_bp: float = _DEFAULT_CREDIT_SPREAD_AA_BP
    rating_floor_int: int = _DEFAULT_RATING_FLOOR
    fee_pct: float = _DEFAULT_FEE_PCT
    initial_capital: float = _DEFAULT_INITIAL_CAPITAL

    def credit_spread_dict(self) -> dict[str, float]:
        """构造 cb_pricer 的 credit_spread_bp dict, 用本配置的 AAA/AA 拟合."""
        # 简单粗暴: 按相对距离插/外推
        aaa = self.credit_spread_aaa_bp
        aa = self.credit_spread_aa_bp
        gap = aa - aaa
        return {
            "AAA": aaa,
            "AA+": aaa + 0.4 * gap,
            "AA": aa,
            "AA-": aa + 0.7 * gap,
            "A+": aa + 2.0 * gap,
            "A": aa + 4.0 * gap,
            "A-": aa + 6.0 * gap,
        }


def _pick(weights: list[float], i: int, default: float) -> float:
    if i < len(weights):
        v = weights[i]
        if isinstance(v, (int, float)) and math.isfinite(float(v)):
            return float(v)
    return default


def _unpack_config(
    weights: list[float],
    thresholds: dict | None,
    rules: dict | None,
) -> CBArbConfig:
    """从 weights/thresholds/rules 合成 CBArbConfig.

    weights 顺序固定 (与 yaml 同):
        [0] vol_window_days        int
        [1] vol_multiplier         float
        [2] rank_buy_pct           float
        [3] rank_sell_pct          float
        [4] max_position_pct       float
        [5] max_holdings           int
        [6] max_holding_days       int
        [7] stop_loss_pct          float (负数)
        [8] min_remaining_size     float
        [9] min_avg_amount         float
        [10] credit_spread_aaa_bp  float
        [11] credit_spread_aa_bp   float

    rules 字典:
        rating_floor_int (int), fee_pct (float), initial_capital (float)
    """
    weights = list(weights or [])
    rules = dict(rules or {})

    cfg = CBArbConfig()
    cfg.vol_window_days = max(5, int(round(_pick(weights, 0, _DEFAULT_VOL_WINDOW_DAYS))))
    cfg.vol_multiplier = max(0.1, _pick(weights, 1, _DEFAULT_VOL_MULTIPLIER))
    cfg.rank_buy_pct = min(0.95, max(0.005, _pick(weights, 2, _DEFAULT_RANK_BUY_PCT)))
    cfg.rank_sell_pct = min(0.99, max(0.05, _pick(weights, 3, _DEFAULT_RANK_SELL_PCT)))
    cfg.max_position_pct = min(0.30, max(0.001, _pick(weights, 4, _DEFAULT_MAX_POSITION_PCT)))
    cfg.max_holdings = max(1, int(round(_pick(weights, 5, _DEFAULT_MAX_HOLDINGS))))
    cfg.max_holding_days = max(5, int(round(_pick(weights, 6, _DEFAULT_MAX_HOLDING_DAYS))))
    cfg.stop_loss_pct = min(-0.001, _pick(weights, 7, _DEFAULT_STOP_LOSS_PCT))
    cfg.min_remaining_size = max(1e6, _pick(weights, 8, _DEFAULT_MIN_REMAINING_SIZE))
    cfg.min_avg_amount = max(1e4, _pick(weights, 9, _DEFAULT_MIN_AVG_AMOUNT))
    cfg.credit_spread_aaa_bp = max(1.0, _pick(weights, 10, _DEFAULT_CREDIT_SPREAD_AAA_BP))
    cfg.credit_spread_aa_bp = max(cfg.credit_spread_aaa_bp + 1.0,
                                  _pick(weights, 11, _DEFAULT_CREDIT_SPREAD_AA_BP))

    rf = rules.get("rating_floor_int")
    if isinstance(rf, (int, float)) and math.isfinite(float(rf)):
        cfg.rating_floor_int = int(round(float(rf)))
    fee = rules.get("fee_pct")
    if isinstance(fee, (int, float)) and math.isfinite(float(fee)):
        cfg.fee_pct = float(fee)
    cap = rules.get("initial_capital")
    if isinstance(cap, (int, float)) and math.isfinite(float(cap)):
        cfg.initial_capital = float(cap)

    return cfg


# --------------------------------------------------------------------------- #
# 信号 / 持仓 / 交易
# --------------------------------------------------------------------------- #


@dataclass
class _Position:
    ts_code: str
    cb_name: str
    entry_date: str
    entry_price: float
    qty: float                # 张数
    cost: float               # 入场支付金额 (含手续费)
    deviation_at_entry: float  # 入场时的偏离率 (relative-rank score)


def _build_call_index(cb_call: pd.DataFrame) -> dict[str, list[tuple[str, str]]]:
    """{ts_code: [(ann_date, expire_date), ...]} 用于查 force_redemption 区间.

    若 ann_date 缺失, 用 call_date - 30 天近似 (拿不到的话退到 expire_date).
    null / 非字符串记录直接丢弃.
    """
    idx: dict[str, list[tuple[str, str]]] = {}
    for row in cb_call.itertuples(index=False):
        ann = row.ann_date
        exp = row.expire_date
        # 类型保护: 把 NaN/None 替成 ""
        ann_s = ann if (isinstance(ann, str) and len(ann) == 8) else ""
        exp_s = exp if (isinstance(exp, str) and len(exp) == 8) else ""
        if not exp_s:
            continue
        if not ann_s:
            # 没公告日 — 用 expire_date 兜底 (强赎当日才标记)
            ann_s = exp_s
        if ann_s > exp_s:
            ann_s, exp_s = exp_s, ann_s
        idx.setdefault(row.ts_code, []).append((ann_s, exp_s))
    return idx


def _is_force_redeemed_on_date(
    ts_code: str, date: str, call_index: dict[str, list[tuple[str, str]]]
) -> bool:
    for ann, exp in call_index.get(ts_code, []):
        if ann <= date <= exp:
            return True
    return False


# --------------------------------------------------------------------------- #
# 主回测引擎
# --------------------------------------------------------------------------- #


def _restrict_dates_for_pool(
    trading_days: list[str], oos_event_ids: set[str] | None
) -> tuple[list[str], set[str]]:
    """如果 oos_event_ids 给了, 全回测只在那段窗口运行 (含 IS 暖身).

    pool 通常是 OOS 子集; 我们把回测起点落在 pool 起始之前 90 天 (留够 vol 窗口)
    再跑到 pool 结束.
    """
    if not oos_event_ids:
        return trading_days, set()

    pool_set = {str(d) for d in oos_event_ids}
    if not pool_set:
        return [], set()

    pool_min = min(pool_set)
    pool_max = max(pool_set)
    # 在交易日历上找 pool_min 的 idx, 往前 120 个交易日做暖身 + vol 窗口
    try:
        idx_min = trading_days.index(pool_min)
    except ValueError:
        # pool 日期不在交易日历里 — 退化全段
        return trading_days, pool_set
    try:
        idx_max = trading_days.index(pool_max)
    except ValueError:
        idx_max = len(trading_days) - 1
    warmup = 120
    start = max(0, idx_min - warmup)
    return trading_days[start:idx_max + 1], pool_set


def _compute_metrics(
    equity_curve: list[tuple[str, float]],
    trades: list[TradeRecord],
    initial_capital: float,
) -> dict:
    """从 equity_curve 和 trades 算 sharpe / total_return / max_drawdown / win_rate."""
    n_days = len(equity_curve)
    if n_days == 0:
        return {
            "sharpe": 0.0,
            "win_rate": 0.0,
            "total_trades": 0,
            "total_return": 0.0,
            "max_drawdown": 0.0,
            "n_days": 0,
        }
    eq = np.array([e for _, e in equity_curve], dtype=float)
    base = float(initial_capital) if initial_capital > 0 else 1.0
    total_return = float(eq[-1] / base - 1.0)
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / np.where(peak > 0, peak, 1.0)
    max_drawdown = float(dd.min()) if dd.size else 0.0

    if n_days >= 2:
        rets = np.diff(eq) / np.where(eq[:-1] != 0, eq[:-1], 1.0)
        if rets.size and np.std(rets, ddof=1) > 1e-12:
            sharpe = float(np.mean(rets) / np.std(rets, ddof=1) * math.sqrt(252))
        else:
            sharpe = 0.0
    else:
        sharpe = 0.0
    if not math.isfinite(sharpe):
        sharpe = 0.0

    n_trades = len(trades)
    wins = sum(1 for t in trades if t.pnl_pct > 0)
    win_rate = (wins / n_trades) if n_trades > 0 else 0.0

    return {
        "sharpe": round(sharpe, 4),
        "win_rate": round(win_rate, 4),
        "total_trades": n_trades,
        "total_return": round(total_return, 6),
        "max_drawdown": round(max_drawdown, 6),
        "n_days": n_days,
    }


def _compute_avg_amount_window(
    cb_daily_subset: pd.DataFrame, window: int = 20
) -> pd.Series:
    """在 (ts_code, trade_date) 排好序的 cb_daily_subset 上计算 20 日均 amount."""
    return (
        cb_daily_subset.groupby("ts_code")["amount_yuan"]
        .rolling(window=window, min_periods=1)
        .mean()
        .reset_index(level=0, drop=True)
    )


def _compute_realized_vol_window(
    stk_daily: pd.DataFrame,
    stk_codes: set[str],
    trade_dates: list[str],
    window: int,
) -> dict[tuple[str, str], float]:
    """对涉及到的 stk_codes, 在每个日期算过去 window 天的年化波动率.

    返回: {(stk_code, date): annualized_vol}
    """
    res: dict[tuple[str, str], float] = {}
    if not stk_codes:
        return res
    sub = stk_daily[stk_daily["stk_code"].isin(stk_codes)].copy()
    if sub.empty:
        return res
    # group by stk_code, 算 log return 滚动 std
    sub["log_close"] = np.log(sub["close"].astype(float).clip(lower=1e-9))
    sub["log_ret"] = sub.groupby("stk_code")["log_close"].diff()
    sub["vol_std"] = (
        sub.groupby("stk_code")["log_ret"]
        .rolling(window=window, min_periods=max(5, window // 4))
        .std(ddof=1)
        .reset_index(level=0, drop=True)
    )
    sub["vol_ann"] = sub["vol_std"] * math.sqrt(252)
    # 转换成 dict
    for row in sub.itertuples(index=False):
        v = row.vol_ann
        if isinstance(v, float) and math.isfinite(v) and v > 0:
            res[(row.stk_code, row.trade_date)] = v
    return res


def _resolve_position_size(
    cash: float, equity: float, max_position_pct: float, max_holdings: int
) -> float:
    """单只仓位资金: equity * max_position_pct, 不能超过现金."""
    target = equity * max_position_pct
    return min(target, cash)


def run_backtest(
    *args: Any,
    **kwargs: Any,
) -> BacktestResult:
    """cb_arb verifier 入口 — 兼容 orchestrator-style 与 cb-style 调用.

    orchestrator-style:
        run_backtest(weights, thresholds, rules, oos_event_ids=None)

    cb-style:
        run_backtest(snapshots, weights, thresholds, cfg, oos_event_ids=None)
    """
    snapshots: Any = None
    cfg_obj: Any = None
    oos_event_ids = kwargs.get("oos_event_ids", None)

    if len(args) >= 4 and not isinstance(args[0], list):
        snapshots = args[0]
        weights = list(args[1] or [])
        thresholds = dict(args[2] or {})
        cfg_obj = args[3]
        rules = {}
        if cfg_obj is not None and hasattr(cfg_obj, "fee_pct"):
            rules["fee_pct"] = float(getattr(cfg_obj, "fee_pct"))
    else:
        weights = list(args[0] if len(args) >= 1 else kwargs.get("weights", []))
        thresholds = dict(args[1] if len(args) >= 2 else kwargs.get("thresholds", {}) or {})
        rules = dict(args[2] if len(args) >= 3 else kwargs.get("rules", {}) or {})
    _ = snapshots  # ignored

    cfg = _unpack_config(weights, thresholds, rules)

    return _run_backtest_core(cfg, oos_event_ids)


def _run_backtest_core(
    cfg: CBArbConfig, oos_event_ids: set[str] | None
) -> BacktestResult:
    """核心循环: 跑一遍, 在 IS / OOS / pool 子集分别算 metrics."""
    # ---- 数据 ----
    cb_basic = _load_cb_basic()
    cb_daily = _load_cb_daily()
    cb_call = _load_cb_call()
    stk_daily = _load_stk_daily()
    trading_days = _load_trading_days()

    # 限定回测日期段
    days_to_run, pool_set = _restrict_dates_for_pool(trading_days, oos_event_ids)
    if not days_to_run:
        # 空池 — 直接返回空结果
        return BacktestResult(
            trades=[],
            all_metrics={"sharpe": 0.0, "win_rate": 0.0, "total_trades": 0,
                         "total_return": 0.0, "max_drawdown": 0.0, "n_days": 0},
            is_metrics={"sharpe": 0.0, "win_rate": 0.0, "total_trades": 0,
                        "total_return": 0.0, "max_drawdown": 0.0, "n_days": 0},
            oos_metrics={"sharpe": 0.0, "win_rate": 0.0, "total_trades": 0,
                         "total_return": 0.0, "max_drawdown": 0.0, "n_days": 0},
            date_range=("", ""),
        )

    days_set = set(days_to_run)
    # 切 cb_daily 子集 (vectorized 提速)
    cb_daily_sub = cb_daily[cb_daily["trade_date"].isin(days_set)].copy()
    cb_daily_sub = cb_daily_sub.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    # 滚动均成交额 (20 日)
    cb_daily_sub["amount_20d"] = _compute_avg_amount_window(cb_daily_sub, 20)

    # 取所有涉及到的 stk_codes (再把 stk_daily 切到对应区间)
    cb_basic_active = cb_basic[cb_basic["ts_code"].isin(set(cb_daily_sub["ts_code"]))]
    relevant_stk_codes = set(cb_basic_active["stk_code"].dropna().tolist())

    stk_daily_sub = stk_daily[
        (stk_daily["stk_code"].isin(relevant_stk_codes))
        & (stk_daily["trade_date"].isin(days_set))
    ].copy()
    # 算每个 stk_code 的滚动年化波动
    vol_map = _compute_realized_vol_window(
        stk_daily, relevant_stk_codes, days_to_run, cfg.vol_window_days
    )
    # 当日正股价
    stk_close_map: dict[tuple[str, str], float] = {
        (row.stk_code, row.trade_date): float(row.close)
        for row in stk_daily_sub.itertuples(index=False)
    }

    # cb_call 索引
    call_index = _build_call_index(cb_call)

    # cb_basic 切 ts_code → spec 字段
    basic_lookup: dict[str, dict] = {}
    for row in cb_basic.itertuples(index=False):
        basic_lookup[row.ts_code] = {
            "bond_short_name": row.bond_short_name or row.ts_code,
            "stk_code": row.stk_code,
            "issue_size_yuan": float(row.issue_size_yuan)
                if math.isfinite(row.issue_size_yuan) else 0.0,
            "conv_price": float(row.conv_price)
                if (row.conv_price is not None and math.isfinite(row.conv_price))
                else float("nan"),
            "list_date": row.list_date or "",
            "maturity_date": row.maturity_date or "",
            "coupon_rate": float(row.coupon_rate) if math.isfinite(row.coupon_rate) else 0.01,
            "rating": row.rating or "AA",
            "rating_int": int(row.rating_int),
        }

    # 将 cb_daily_sub 转为 trade_date 索引快速取
    daily_by_date = {
        d: g for d, g in cb_daily_sub.groupby("trade_date")
    }

    # ---- 状态 ----
    cash = cfg.initial_capital
    equity_history: list[tuple[str, float]] = []
    holdings: dict[str, _Position] = {}
    trades: list[TradeRecord] = []
    credit_spread_dict = cfg.credit_spread_dict()
    today_dt_cache: dict[str, datetime] = {}

    # ---- 主循环 ----
    for date in days_to_run:
        rows_today = daily_by_date.get(date)
        if rows_today is None or rows_today.empty:
            # 无市价 — equity 按上日结
            equity_history.append((date, _compute_equity(cash, holdings, prev_prices(holdings))))
            continue

        # 当日 close map (按 ts_code)
        close_map: dict[str, float] = {}
        for r in rows_today.itertuples(index=False):
            close_map[r.ts_code] = float(r.close)

        # 算每只 active CB 的偏离率
        deviations: list[tuple[str, float, float]] = []  # (ts_code, deviation, mkt_price)
        for r in rows_today.itertuples(index=False):
            ts = r.ts_code
            mkt = float(r.close)
            spec_d = basic_lookup.get(ts)
            if spec_d is None:
                continue
            stk_code = spec_d["stk_code"]
            if not stk_code:
                continue
            # 过滤池
            if spec_d["rating_int"] < cfg.rating_floor_int:
                continue
            if spec_d["issue_size_yuan"] < cfg.min_remaining_size:
                continue
            avg_amt = float(getattr(r, "amount_20d", 0.0))
            if avg_amt < cfg.min_avg_amount:
                continue
            mat = spec_d["maturity_date"]
            if not mat or len(mat) != 8:
                continue
            try:
                mat_dt = datetime.strptime(mat, "%Y%m%d")
                tdy_dt = today_dt_cache.get(date)
                if tdy_dt is None:
                    tdy_dt = datetime.strptime(date, "%Y%m%d")
                    today_dt_cache[date] = tdy_dt
                days_to_mat = (mat_dt - tdy_dt).days
            except Exception:
                continue
            if days_to_mat <= 30:
                continue

            # 强赎过滤 — 已触发就跳过买入候选, 但继续计算偏离 (force_redemption 触发时
            # 期权值锁定, 排名可能变贵, 自然过排序卖出)
            is_redeemed = _is_force_redeemed_on_date(ts, date, call_index)

            # 取正股价 + vol
            stock_price = stk_close_map.get((stk_code, date))
            if stock_price is None or stock_price <= 0:
                continue
            vol = vol_map.get((stk_code, date))
            if vol is None or not math.isfinite(vol) or vol <= 0:
                continue
            vol_use = min(_VOL_CAP, vol * cfg.vol_multiplier)

            conv_price = spec_d["conv_price"]
            if not math.isfinite(conv_price) or conv_price <= 0:
                continue

            spec = CBSpec(
                ts_code=ts,
                face_value=100.0,
                conv_price=conv_price,
                list_date=spec_d["list_date"],
                maturity_date=mat,
                coupon_rate=spec_d["coupon_rate"],
                rating=spec_d["rating"],
            )
            try:
                val = price_cb(
                    spec=spec,
                    valuation_date=date,
                    stock_price=stock_price,
                    stock_vol=vol_use,
                    risk_free_rate=0.025,
                    credit_spread_bp=credit_spread_dict,
                    is_force_redeemed=is_redeemed,
                )
            except Exception:
                continue
            theo = val.theoretical
            if not math.isfinite(theo) or theo <= 0:
                continue
            dev = (mkt - theo) / theo
            if not math.isfinite(dev):
                continue
            deviations.append((ts, dev, mkt))

        if not deviations:
            equity_history.append((date, _compute_equity(cash, holdings, close_map)))
            continue

        # 按偏离率升序排序 (低 = 便宜)
        deviations.sort(key=lambda x: x[1])
        n = len(deviations)
        # 排名: 0 = 最便宜
        rank_map: dict[str, int] = {ts: i for i, (ts, _, _) in enumerate(deviations)}
        n_buy = max(1, int(round(n * cfg.rank_buy_pct)))
        # 排名 < n_buy 是买入候选
        # rank_sell_pct: 持仓中 rank/n >= rank_sell_pct → 卖出
        sell_threshold_rank = cfg.rank_sell_pct * n

        # ---- 1. 卖出 ----
        # 优先级: 强赎 / max_holding_days / stop_loss / rank_sell
        to_sell: list[tuple[str, str]] = []  # (ts_code, exit_reason)
        for ts, pos in list(holdings.items()):
            cur_close = close_map.get(ts)
            if cur_close is None:
                # 当日无报价 — 跳过 (等下次)
                continue
            # 强赎触发 → 强制卖
            if _is_force_redeemed_on_date(ts, date, call_index):
                to_sell.append((ts, "force_redemption"))
                continue
            # 持仓天数
            try:
                holding_days = (
                    datetime.strptime(date, "%Y%m%d")
                    - datetime.strptime(pos.entry_date, "%Y%m%d")
                ).days
            except Exception:
                holding_days = 0
            if holding_days >= cfg.max_holding_days:
                to_sell.append((ts, "max_holding_days"))
                continue
            # 止损
            pnl = (cur_close - pos.entry_price) / pos.entry_price
            if pnl <= cfg.stop_loss_pct:
                to_sell.append((ts, "stop_loss"))
                continue
            # rank_sell: 持仓在当日还能算偏离 → 看 rank
            r = rank_map.get(ts)
            if r is not None and r >= sell_threshold_rank:
                to_sell.append((ts, "rank_sell"))

        for ts, reason in to_sell:
            pos = holdings.pop(ts, None)
            if pos is None:
                continue
            exit_price = close_map.get(ts, pos.entry_price)
            proceeds = exit_price * pos.qty * (1 - cfg.fee_pct)
            cash += proceeds
            try:
                hd = (datetime.strptime(date, "%Y%m%d")
                      - datetime.strptime(pos.entry_date, "%Y%m%d")).days
            except Exception:
                hd = 0
            pnl_pct = (exit_price - pos.entry_price) / pos.entry_price
            pnl_amount = proceeds - pos.cost
            trades.append(TradeRecord(
                cb_code=ts,
                cb_name=pos.cb_name,
                entry_date=pos.entry_date,
                entry_price=round(pos.entry_price, 4),
                prob_entry=round(pos.deviation_at_entry, 6),
                premium_entry=0.0,
                exit_date=date,
                exit_price=round(exit_price, 4),
                pnl_pct=round(pnl_pct, 6),
                pnl_amount=round(pnl_amount, 2),
                holding_days=int(hd),
                exit_reason=reason,
            ))

        # ---- 2. 买入 ----
        slots_avail = cfg.max_holdings - len(holdings)
        if slots_avail > 0 and cash > 100:
            # 取 buy candidates rank < n_buy 且不在 holdings
            cur_equity = _compute_equity(cash, holdings, close_map)
            for i in range(min(n_buy, n)):
                if slots_avail <= 0:
                    break
                ts, dev, mkt = deviations[i]
                if ts in holdings:
                    continue
                # 强赎触发的不买
                if _is_force_redeemed_on_date(ts, date, call_index):
                    continue
                # 资金上限
                pos_cash = _resolve_position_size(
                    cash, cur_equity, cfg.max_position_pct, cfg.max_holdings
                )
                if pos_cash < mkt * 1.0:  # 至少买 1 张
                    continue
                qty = math.floor(pos_cash / mkt / (1 + cfg.fee_pct))
                if qty < 1:
                    continue
                cost = qty * mkt * (1 + cfg.fee_pct)
                if cost > cash:
                    continue
                cash -= cost
                holdings[ts] = _Position(
                    ts_code=ts,
                    cb_name=basic_lookup.get(ts, {}).get("bond_short_name", ts),
                    entry_date=date,
                    entry_price=mkt,
                    qty=float(qty),
                    cost=cost,
                    deviation_at_entry=dev,
                )
                slots_avail -= 1

        equity_history.append((date, _compute_equity(cash, holdings, close_map)))

    # ---- 收尾: 强制平仓所有未平 ----
    last_date = days_to_run[-1]
    last_rows = daily_by_date.get(last_date)
    last_close: dict[str, float] = {}
    if last_rows is not None:
        for r in last_rows.itertuples(index=False):
            last_close[r.ts_code] = float(r.close)
    for ts, pos in list(holdings.items()):
        exit_price = last_close.get(ts, pos.entry_price)
        proceeds = exit_price * pos.qty * (1 - cfg.fee_pct)
        cash += proceeds
        try:
            hd = (datetime.strptime(last_date, "%Y%m%d")
                  - datetime.strptime(pos.entry_date, "%Y%m%d")).days
        except Exception:
            hd = 0
        pnl_pct = (exit_price - pos.entry_price) / pos.entry_price
        pnl_amount = proceeds - pos.cost
        trades.append(TradeRecord(
            cb_code=ts,
            cb_name=pos.cb_name,
            entry_date=pos.entry_date,
            entry_price=round(pos.entry_price, 4),
            prob_entry=round(pos.deviation_at_entry, 6),
            premium_entry=0.0,
            exit_date=last_date,
            exit_price=round(exit_price, 4),
            pnl_pct=round(pnl_pct, 6),
            pnl_amount=round(pnl_amount, 2),
            holding_days=int(hd),
            exit_reason="end_of_period",
        ))
    holdings.clear()

    # ---- 算 metrics: all / IS / OOS / (pool subset) ----
    all_m = _compute_metrics(equity_history, trades, cfg.initial_capital)
    is_curve = [(d, e) for d, e in equity_history if d <= IS_END]
    is_trades = [t for t in trades if t.entry_date <= IS_END]
    is_m = _compute_metrics(is_curve, is_trades, cfg.initial_capital)

    if pool_set:
        pool_curve = [(d, e) for d, e in equity_history if d in pool_set]
        pool_trades = [t for t in trades if t.entry_date in pool_set]
        # 起始 base = pool_curve[0] 的前一日 equity (近似 cur)
        pool_base = pool_curve[0][1] if pool_curve else cfg.initial_capital
        oos_m = _compute_metrics(pool_curve, pool_trades, pool_base)
    else:
        oos_curve = [(d, e) for d, e in equity_history if d >= OOS_START]
        oos_trades = [t for t in trades if t.entry_date >= OOS_START]
        oos_base = oos_curve[0][1] if oos_curve else cfg.initial_capital
        oos_m = _compute_metrics(oos_curve, oos_trades, oos_base)

    return BacktestResult(
        trades=trades,
        all_metrics=all_m,
        is_metrics=is_m,
        oos_metrics=oos_m,
        date_range=(days_to_run[0], days_to_run[-1]),
    )


# --------------------------------------------------------------------------- #
# 持仓估值小工具
# --------------------------------------------------------------------------- #


def _compute_equity(
    cash: float, holdings: dict[str, _Position], close_map: dict[str, float]
) -> float:
    eq = float(cash)
    for ts, pos in holdings.items():
        px = close_map.get(ts)
        if px is None or not math.isfinite(px):
            px = pos.entry_price
        eq += pos.qty * float(px)
    return eq


def prev_prices(holdings: dict[str, _Position]) -> dict[str, float]:
    """退化: 用 entry_price 当 placeholder."""
    return {ts: pos.entry_price for ts, pos in holdings.items()}


__all__ = [
    "run_backtest",
    "CBArbConfig",
    "RATING_TO_INT",
    "IS_END",
    "OOS_START",
    "DEFAULT_YAML_PATH",
    "reset_cache",
]
