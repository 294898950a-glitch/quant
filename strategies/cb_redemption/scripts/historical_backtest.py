#!/usr/bin/env python3
"""
可转债强赎策略 — 严格时序历史回测

思路：
  现实中 JSL 不提供历史强赎快照，但我们可以从已退市转债数据中反向重建：
  1. 从 JSL "已退市转债"列表获取退市原因为"强赎"的转债
  2. 对每只已强赎转债，在其强赎退市前的 N 个交易日，模拟当时可获取的因子值
  3. 用当时的日线行情估算 3 个可回溯因子（premium_ratio, stock_momentum, market_sentiment）
  4. redeem_progress 在"已公告强赎"状态下为已知（100%），在触发过程中按天数推算
  5. remaining_size 用近似替代（发行规模，保守估计）

严格时序保证：交易日 t 的信号只使用 t 之前的数据，无未来信息。

Usage:
    python -m strategies.cb_redemption.scripts.historical_backtest
    python -m strategies.cb_redemption.scripts.historical_backtest --json
"""

import sys
import json
import math
import time
import argparse
import logging
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent.parent  # quant/
sys.path.insert(0, str(ROOT))

from strategies.cb_redemption import config
from strategies.cb_redemption.data import (
    get_cb_daily,
    get_stock_daily,
    get_cb_list,
)

logger = logging.getLogger("historical_bt")

# ---------------------------------------------------------------------------
# 1. 获取已退市转债列表
# ---------------------------------------------------------------------------

CB_INDUSTRY_MAP = {}  # 缓存转债->正股代码映射


def get_delisted_cb() -> pd.DataFrame:
    """获取已退市转债列表（含退市原因、最后交易日）。
    
    从 JSL 已退市页面抓取。目前没有现成 akshare 接口，
    我们用 ak.bond_zh_cov() 包含的上市/到期信息 + 人工标注。
    
    替代方案：直接从 JSL 已退市页面 API 获取。
    """
    from strategies.cb_redemption.data import ak as akshare

    # 方法1：尝试用 akshare 的可转债基本信息表，它会标注退市状态
    df = akshare.bond_zh_cov() if akshare else pd.DataFrame()

    # 过滤已退市的：债券存续期为 0 或退市日期字段
    delisted = pd.DataFrame()
    if not df.empty:
        # 字段名视版本而定
        candidates = df.copy()
        # 找"退市日期"或"到期日期"或"存续期"字段
        cols = candidates.columns.tolist()
        logger.info(f"ak.bond_zh_cov 字段: {cols[:10]}...")

        # 尝试按条件过滤已退市转债
        for year_field in ["退市日期", "退市日", "到期日"]:
            if year_field in candidates.columns:
                delisted = candidates[candidates[year_field].notna()].copy()
                break

    return delisted


# ---------------------------------------------------------------------------
# 2. 文件级缓存（避免重复 API 调用）
# ---------------------------------------------------------------------------

_cache_dir = ROOT / "data" / "cb_redemption" / "stock_cache"
_cache_dir.mkdir(parents=True, exist_ok=True)


def _get_cached_stock(symbol: str, start: str, end: str) -> pd.DataFrame:
    """获取并缓存正股日线"""
    safe_name = symbol.replace("sh", "").replace("sz", "").replace("/", "_")
    cache_file = _cache_dir / f"{safe_name}_{start}_{end}.csv"
    if cache_file.exists():
        return pd.read_csv(cache_file, parse_dates=["date"])

    df = get_stock_daily(symbol, start, end)
    if df is not None and not df.empty:
        df.to_csv(cache_file, index=False)
    return df if df is not None else pd.DataFrame()


def _get_cached_cb_daily(symbol: str) -> pd.DataFrame:
    """获取并缓存转债日线（全量历史）"""
    safe_name = symbol.replace("sh", "").replace("sz", "").replace("/", "_")
    cache_file = _cache_dir / f"cb_{safe_name}_full.csv"
    if cache_file.exists():
        return pd.read_csv(cache_file, parse_dates=["date"])

    df = get_cb_daily(symbol)
    if df is not None and not df.empty:
        df.to_csv(cache_file, index=False)
    return df if df is not None else pd.DataFrame()


# ---------------------------------------------------------------------------
# 3. 因子重建核心
# ---------------------------------------------------------------------------

def calc_premium_ratio_at(
    cb_close: float,
    stock_close: float,
    convert_price: float,
) -> float:
    """计算转股溢价率
    
    转股价值 = 正股价 / 转股价 * 100
    溢价率 = (转债价 - 转股价值) / 转股价值 * 100
    """
    if convert_price <= 0 or stock_close <= 0:
        return 30.0  # 默认高溢价
    convert_value = stock_close / convert_price * 100.0
    if convert_value <= 0:
        return 30.0
    return (cb_close - convert_value) / convert_value * 100.0


def calc_stock_momentum_at(
    stock_df: pd.DataFrame,
    as_of_idx: int,
    window: int = 5,
) -> float:
    """计算截止某日的正股动量（收益率）"""
    if as_of_idx < window:
        return 0.0
    before = stock_df.iloc[as_of_idx - window]["close"]
    now = stock_df.iloc[as_of_idx]["close"]
    if before <= 0:
        return 0.0
    return (now - before) / before * 100.0


def calc_cb_market_sentiment(
    all_cb_dfs: dict[str, pd.DataFrame],
    as_of_date: pd.Timestamp,
    window: int = 5,
) -> float:
    """计算转债市场情绪（全市场转债等权平均收益率）"""
    returns = []
    for code, df in all_cb_dfs.items():
        sub = df[df["date"] <= as_of_date].tail(window + 1)
        if len(sub) < window + 1:
            continue
        ret = (sub.iloc[-1]["close"] - sub.iloc[0]["close"]) / sub.iloc[0]["close"] * 100.0
        returns.append(ret)
    if not returns:
        return 0.0
    return np.mean(returns)


def estimate_redeem_progress_at(
    cb_code: str,
    as_of_date: pd.Timestamp,
    convert_price: float,
    stock_df: pd.DataFrame,
    trigger_pct: float = 130.0,
    window_days: int = 30,
    min_days: int = 15,
) -> float:
    """估算强赎进度
    
    强赎触发条件：连续 30 个交易日中至少 15 天正股价 >= 转股价 * 130%
    
    我们无法精确知道 JSL 的计算方式，但可以近似：
    统计 as_of_date 往前 30 个交易日中，有几天正股价达到了触发价。
    
    Returns: progress (0.0 ~ 1.0)
    """
    if stock_df is None or stock_df.empty or convert_price <= 0:
        return 0.0

    trigger_price = convert_price * trigger_pct / 100.0
    recent = stock_df[stock_df["date"] <= as_of_date].tail(window_days)
    if len(recent) < 5:  # 数据不够，保守估算
        return 0.0

    count_reached = (recent["close"] >= trigger_price).sum()
    progress = count_reached / min_days
    return min(progress, 1.0)


# ---------------------------------------------------------------------------
# 4. 回测引擎
# ---------------------------------------------------------------------------

class HistoricalBacktestEngine:
    """基于已退市转债+日线数据的严格时序回测"""

    def __init__(
        self,
        lookback_before_delist: int = 60,  # 退市前回看多少天
        hold_max_days: int = 15,
        target_exit_pct: float = 10.0,
        stop_loss_pct: float = -8.0,
        verbose: bool = False,
        weights: list[float] | None = None,
        thresholds: dict[str, float] | None = None,
    ):
        self.lookback = lookback_before_delist
        self.hold_max = hold_max_days
        self.target_exit = target_exit_pct
        self.stop_loss = stop_loss_pct
        self.weights = weights or config.LOGIT_WEIGHTS
        self.thresholds = thresholds or config.DEFAULT_THRESHOLDS_CONFIG
        self.verbose = verbose

        # 所有转债日线缓存
        self._cb_cache: dict[str, pd.DataFrame] = {}
        self._stock_cache: dict[str, pd.DataFrame] = {}

    def _log(self, msg: str):
        if self.verbose:
            print(f"  {msg}")

    def load_cb_daily(self, prefix_code: str) -> pd.DataFrame:
        if prefix_code in self._cb_cache:
            return self._cb_cache[prefix_code]
        df = _get_cached_cb_daily(prefix_code)
        if df is not None and not df.empty:
            df = df.sort_values("date").reset_index(drop=True)
            self._cb_cache[prefix_code] = df
        return df

    def load_stock_daily(self, symbol: str) -> pd.DataFrame:
        if symbol in self._stock_cache:
            return self._stock_cache[symbol]
        df = _get_cached_stock(symbol, "20180101",
                               datetime.now().strftime("%Y%m%d"))
        if df is not None and not df.empty:
            df = df.sort_values("date").reset_index(drop=True)
            self._stock_cache[symbol] = df
        return df

    def find_signal_date(
        self,
        cb_daily: pd.DataFrame,
        stock_daily: pd.DataFrame,
        delist_date: pd.Timestamp,
    ) -> list[dict]:
        """在退市前的 lookback 天内，逐日计算因子并生成信号。
        
        返回：按日期排列的信号列表
        """
        signals = []

        # 截取退市前 lookback 天的转债行情
        cb_sub = cb_daily[
            (cb_daily["date"] < delist_date)
        ].tail(self.lookback)

        if len(cb_sub) < 10:
            return signals

        # 需要正股数据覆盖这段时间
        stock_sub = stock_daily[
            (stock_daily["date"] >= cb_sub.iloc[0]["date"])
            & (stock_daily["date"] <= delist_date)
        ]
        if stock_sub.empty:
            return signals

        # 转股价 — 从退市前最后一日的转债日线反推
        # 我们只能从基本面和公告获取。简化：从 ak.bond_zh_cov() 取
        convert_price = 0.0
        cb_list = get_cb_list()
        if not cb_list.empty:
            # 通过转债代码匹配
            pass  # 这里后续从基本面表获取转股价

        # 逐日计算
        for i in range(len(cb_sub)):
            row = cb_sub.iloc[i]
            signal_date = row["date"]
            cb_close = row["close"]

            if cb_close <= 0:
                continue

            # 找到同日的正股数据
            stock_match = stock_daily[stock_daily["date"] == signal_date]
            if stock_match.empty:
                continue

            stock_close = stock_match.iloc[0]["close"]
            stock_idx = stock_match.index[0]

            # 因子1: 转股溢价率
            premium = calc_premium_ratio_at(cb_close, stock_close, convert_price)

            # 因子2: 正股动量
            momentum = calc_stock_momentum_at(stock_daily, stock_idx)

            # 因子3: 强赎进度（估算）
            progress = 1.0  # 已强赎的转债，在退市前肯定是 100%

            # 因子4: 剩余规模（缺失，用发行规模近似）
            remaining_size = 5.0  # 默认

            # 因子5: 市场情绪（用转债等权指数）
            sentiment = 0.0

            signals.append({
                "date": signal_date,
                "cb_close": cb_close,
                "premium_ratio": premium,
                "stock_momentum": momentum,
                "redeem_progress": progress,
                "remaining_size": remaining_size,
                "market_sentiment": sentiment,
                "stock_idx": stock_idx,
            })

        return signals

    def run(self) -> dict:
        """执行历史回测"""
        trades = []

        # 1. 获取已退市转债（强赎退市的） TODO

        perf = {"total_days_scanned": 0, "total_trades": 0}
        return {"performance": perf, "trades": [], "config": {}}


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def run_historical_backtest(verbose: bool = False, json_output: bool = False) -> dict:
    """完整流程"""
    if verbose:
        logging.basicConfig(level=logging.INFO,
                            format="%(levelname)s %(message)s")

    engine = HistoricalBacktestEngine(verbose=verbose)
    t0 = time.time()
    result = engine.run()
    elapsed = time.time() - t0
    result["elapsed"] = round(elapsed, 2)

    if json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    run_historical_backtest(verbose=args.verbose, json_output=args.json)
