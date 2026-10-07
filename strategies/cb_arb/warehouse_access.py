"""cb_arb 仓库数据加载 — cb_basic / cb_daily / cb_call / stk_daily_qfq / 交易日历.

从 strategies/cb_arb/verifier.py 拆出(见
docs/2026-09-01-cb-arb-verifier-decouple-spec.txt 组 A)。这里只管"从
data/cb_warehouse/*.parquet 读什么、派生哪些字段、怎么缓存",不管这些字段
怎么被回测规则消费——那是 verifier.py 的事。

哪些字段是"当前快照覆盖了历史时点值", 以及这里怎么处理:
- 最新转股价: 仓库里叫 conv_price_latest, 历史估值不许读它, 用 point_in_time_conv_price()。
- 已退市券的"到期日"其实是摘牌日: 仓库里叫 expire_date_raw; 估值读 contract_maturity_date,
  解析不出合同年限的券该列为空, 消费方排除并报告, 不退回 expire_date_raw。
- rating(最新评级): 还没有时点数据, 原样返回, 消费方自己处理。

估值循环跳过一只券时, 用 ExclusionLog 记下原因, 跑完打印 —— 被静默移出宇宙的券要看得见。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent

CB_BASIC_PARQUET = _REPO_ROOT / "data" / "cb_warehouse" / "cb_basic.parquet"
CB_DAILY_PARQUET = _REPO_ROOT / "data" / "cb_warehouse" / "cb_daily.parquet"
CB_CALL_PARQUET = _REPO_ROOT / "data" / "cb_warehouse" / "cb_call.parquet"
CB_CONV_VALUE_PIT_PARQUET = _REPO_ROOT / "data" / "cb_warehouse" / "cb_conv_value_pit.parquet"
STK_DAILY_QFQ_PARQUET = _REPO_ROOT / "data" / "cb_warehouse" / "stk_daily_qfq.parquet"

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

class ExclusionLog:
    """Why bond-days were left out of a valuation pass. Reported at the end of the pass, never silent."""

    def __init__(self) -> None:
        self.no_contract_maturity: set[str] = set()
        self.no_conv_value_bond_days = 0
        self.no_conv_value_bonds: set[str] = set()

    def summary(self) -> dict[str, object]:
        return {
            "no_contract_maturity_bonds": len(self.no_contract_maturity),
            "no_contract_maturity_codes": sorted(self.no_contract_maturity),
            "no_point_in_time_conv_value_bond_days": self.no_conv_value_bond_days,
            "no_point_in_time_conv_value_bonds": len(self.no_conv_value_bonds),
        }

    def report(self, label: str) -> None:
        s = self.summary()
        if s["no_contract_maturity_bonds"] or s["no_point_in_time_conv_value_bond_days"]:
            print(
                f"[{label}] 被排除: 合同到期日缺失 {s['no_contract_maturity_bonds']} 只 {s['no_contract_maturity_codes']}; "
                f"当日转股价值缺失 {s['no_point_in_time_conv_value_bond_days']} 个券日 "
                f"({s['no_point_in_time_conv_value_bonds']} 只)",
                flush=True,
            )


_CB_BASIC_CACHE: pd.DataFrame | None = None
_CB_DAILY_CACHE: pd.DataFrame | None = None
_CB_CALL_CACHE: pd.DataFrame | None = None
_STK_DAILY_CACHE: pd.DataFrame | None = None
_TRADING_DAYS_CACHE: list[str] | None = None
_CONV_VALUE_PIT_CACHE: dict[tuple[str, str], float] | None = None


def load_cb_basic() -> pd.DataFrame:
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


def load_cb_daily() -> pd.DataFrame:
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


def load_cb_call() -> pd.DataFrame:
    global _CB_CALL_CACHE
    if _CB_CALL_CACHE is None:
        df = pd.read_parquet(CB_CALL_PARQUET)
        df = df.copy()
        # 强赎区间: 按公告日 ann_date → expire_date. 该期间内 CB 视为已强赎.
        df = df[["ts_code", "ann_date", "call_date", "expire_date"]].copy()
        _CB_CALL_CACHE = df
    return _CB_CALL_CACHE


def load_stk_daily() -> pd.DataFrame:
    global _STK_DAILY_CACHE
    if _STK_DAILY_CACHE is None:
        df = pd.read_parquet(STK_DAILY_QFQ_PARQUET)
        df = df[["stk_code", "trade_date", "close"]].copy()
        df = df.sort_values(["stk_code", "trade_date"]).reset_index(drop=True)
        _STK_DAILY_CACHE = df
    return _STK_DAILY_CACHE


def load_conv_value_pit() -> dict[tuple[str, str], float]:
    """{(ts_code, trade_date): 当日真实转股价值}. 来源 scripts/build_cb_conv_value_pit.py."""
    global _CONV_VALUE_PIT_CACHE
    if _CONV_VALUE_PIT_CACHE is None:
        df = pd.read_parquet(CB_CONV_VALUE_PIT_PARQUET, columns=["ts_code", "trade_date", "conv_value"])
        df = df[df["conv_value"] > 0]
        _CONV_VALUE_PIT_CACHE = dict(
            zip(zip(df["ts_code"], df["trade_date"]), df["conv_value"].astype(float))
        )
    return _CONV_VALUE_PIT_CACHE


def point_in_time_conv_price(ts_code: str, date: str, stock_price: float) -> float:
    """当日有效转股价 = 100 * 正股价 / 当日真实转股价值; 查不到返回 NaN (调用方跳过该券当日).

    cb_basic.conv_price 是最新转股价 (含之后所有下修), 不能用于历史日期的估值.
    理论价只通过 正股价/转股价 这个比值依赖两者, 所以这样换算与正股价是否复权无关.
    """
    conv_value = load_conv_value_pit().get((ts_code, date))
    if conv_value is None or stock_price is None or stock_price <= 0:
        return float("nan")
    return 100.0 * float(stock_price) / conv_value


def load_trading_days() -> list[str]:
    """全市场交易日, 升序."""
    global _TRADING_DAYS_CACHE
    if _TRADING_DAYS_CACHE is None:
        cb_daily = load_cb_daily()
        days = sorted(set(cb_daily["trade_date"].astype(str).tolist()))
        _TRADING_DAYS_CACHE = days
    return _TRADING_DAYS_CACHE


def reset_cache() -> None:
    """主要给测试用 — 强制重新读取."""
    global _CB_BASIC_CACHE, _CB_DAILY_CACHE, _CB_CALL_CACHE
    global _STK_DAILY_CACHE, _TRADING_DAYS_CACHE, _CONV_VALUE_PIT_CACHE
    _CONV_VALUE_PIT_CACHE = None
    _CB_BASIC_CACHE = None
    _CB_DAILY_CACHE = None
    _CB_CALL_CACHE = None
    _STK_DAILY_CACHE = None
    _TRADING_DAYS_CACHE = None


__all__ = [
    "CB_BASIC_PARQUET",
    "CB_DAILY_PARQUET",
    "CB_CALL_PARQUET",
    "STK_DAILY_QFQ_PARQUET",
    "RATING_TO_INT",
    "load_cb_basic",
    "load_cb_daily",
    "load_cb_call",
    "load_stk_daily",
    "load_trading_days",
    "load_conv_value_pit",
    "ExclusionLog",
    "point_in_time_conv_price",
    "reset_cache",
]
