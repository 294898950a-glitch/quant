"""可转债理论价计算引擎(v1: BS 欧式 + 债底).

CB 理论价 = 债底现值 + 转股期权价值

债底:    把未来现金流(每年票息 + 到期面值)按折现率回到今天.
         折现率 = 无风险利率 + 信用利差(看评级).
转股期权: BS 公式算"一股看涨期权"价值, 然后乘转股比例(100/转股价).

corner cases (5 个, 都在测试里覆盖):
1. 距到期 < 30 天          -> max(intrinsic, bond_floor), 不走 BS
2. 已触发强赎               -> 锁顶 ~103 元 (面值 + 剩余利息估算)
3. 仍在转股期前(上市后 6 月) -> 仍走 BS, T 不变
4. 临近回售期(最后 2 年内)   -> 债底设 max(face_value, bond_floor) 防跌穿
5. 转股价已下修              -> 调用方传新 conv_price, 函数透明用最新值

实现要点:
- 单位: T 是年, vol/rate 是年化小数 (0.30 = 30%)
- 默认每年付一次息, 到期年付最后利息+面值
- credit spread 字典 (默认): AAA 50, AA+ 80, AA 150, AA- 250, A+ 400, A 700 bps
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np
from scipy.stats import norm


DEFAULT_CREDIT_SPREAD_BP = {
    "AAA": 50,
    "AA+": 80,
    "AA": 150,
    "AA-": 250,
    "A+": 400,
    "A": 700,
    "A-": 1000,
}

# 强赎触发后, CB 的理论价被锁在 (面值 + 剩余几个月利息) 附近, 通常 ~103 元
REDEMPTION_LOCKED_VALUE = 103.0

# 距到期 < 30 天阈值
NEAR_MATURITY_DAYS = 30

# 临近回售: 最后 2 年算回售期 (条款一般规定最后 2 年持有人可回售)
PUTABLE_PERIOD_DAYS = 2 * 365


@dataclass
class CBSpec:
    """一只转债的合同信息."""

    ts_code: str            # 110001.SH 之类
    face_value: float       # 面值, 默认 100
    conv_price: float       # 当前转股价(下修后会变, 调用方传新值)
    list_date: str          # 上市日 YYYYMMDD
    maturity_date: str      # 到期日 YYYYMMDD
    coupon_rate: float      # 年化票面利率, 0.005 = 0.5%
    rating: str             # 评级 'AAA'/'AA+'/'AA'/'AA-'/'A+'/'A'


@dataclass
class CBValuation:
    """理论价计算结果."""

    theoretical: float       # 理论价 (元)
    bond_floor: float        # 债底现值
    option_value: float      # 期权部分价值
    intrinsic: float         # 转股价值 = (面值/转股价) * 正股价
    method: str              # 'BS' / 'intrinsic' / 'redemption_locked' / 'invalid'
    notes: str = ""          # 走的哪个 corner case


# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------

def _parse_ymd(ymd: str) -> datetime:
    """YYYYMMDD -> datetime."""
    return datetime.strptime(ymd, "%Y%m%d")


def _years_between(d1: str, d2: str) -> float:
    """两个 YYYYMMDD 之间的年数 (用 365.25)."""
    return (_parse_ymd(d2) - _parse_ymd(d1)).days / 365.25


def _days_between(d1: str, d2: str) -> int:
    """两个 YYYYMMDD 之间的天数."""
    return (_parse_ymd(d2) - _parse_ymd(d1)).days


# ----------------------------------------------------------------------
# 债底现值
# ----------------------------------------------------------------------

def bond_floor_pv(
    face_value: float,
    coupon_rate: float,
    years_to_maturity: float,
    discount_rate: float,
) -> float:
    """债底现值: 每年付一次息, 到期年付最后利息+面值.

    Args:
        face_value: 面值 (一般 100)
        coupon_rate: 年化票面利率
        years_to_maturity: 距到期年数
        discount_rate: 折现率 = 无风险利率 + 信用利差

    Returns:
        债底现值 (元)
    """
    if years_to_maturity <= 0:
        # 已到期: 直接拿面值 + 最后一次利息
        return face_value * (1 + coupon_rate)

    coupon = face_value * coupon_rate
    pv = 0.0

    # 整年现金流: 每年末付息
    full_years = int(math.floor(years_to_maturity))
    for t in range(1, full_years + 1):
        pv += coupon / (1 + discount_rate) ** t

    # 到期日: 利息 + 面值, 在 years_to_maturity 时点
    pv += (coupon + face_value) / (1 + discount_rate) ** years_to_maturity

    # 如果剩余时间正好是整数年, 上面循环已经付了到期那年的息, 减掉一次
    if abs(years_to_maturity - full_years) < 1e-9 and full_years > 0:
        pv -= coupon / (1 + discount_rate) ** full_years

    return pv


# ----------------------------------------------------------------------
# Black-Scholes 看涨期权
# ----------------------------------------------------------------------

def bs_call(
    S: float,
    K: float,
    T: float,
    sigma: float,
    r: float,
) -> float:
    """欧式看涨期权 BS 价格.

    Args:
        S: 标的当前价
        K: 行权价 (转股价)
        T: 到期时间 (年)
        sigma: 年化波动率 (0.30 = 30%)
        r: 无风险利率 (年化)

    Returns:
        一份看涨期权价值
    """
    if T <= 0:
        return max(S - K, 0.0)
    if sigma <= 0:
        # 零波动: 价值 = max(S - K*exp(-rT), 0)
        return max(S - K * math.exp(-r * T), 0.0)
    if S <= 0 or K <= 0:
        return 0.0

    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    call = S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return float(call)


# ----------------------------------------------------------------------
# 主入口: price_cb
# ----------------------------------------------------------------------

def price_cb(
    spec: CBSpec,
    valuation_date: str,
    stock_price: float,
    stock_vol: float,
    risk_free_rate: float = 0.025,
    credit_spread_bp: Optional[dict] = None,
    is_force_redeemed: bool = False,
) -> CBValuation:
    """算一只 CB 在某日的理论价.

    Args:
        spec: CB 合同信息
        valuation_date: 估值日 YYYYMMDD
        stock_price: 当日正股价
        stock_vol: 年化历史波动率 (0.30 = 30%)
        risk_free_rate: 无风险利率, 默认 2.5%
        credit_spread_bp: 评级 -> 信用利差(bps) 字典. None 用默认.
        is_force_redeemed: 是否已触发强赎 (从 cb_call 查)

    Returns:
        CBValuation
    """
    if credit_spread_bp is None:
        credit_spread_bp = DEFAULT_CREDIT_SPREAD_BP

    # ---- 缺数据保护 ----
    if (
        stock_price is None
        or (isinstance(stock_price, float) and math.isnan(stock_price))
        or stock_price <= 0
    ):
        return CBValuation(
            theoretical=float("nan"),
            bond_floor=float("nan"),
            option_value=float("nan"),
            intrinsic=float("nan"),
            method="invalid",
            notes="missing_or_invalid_stock_price",
        )

    # ---- 转股价值 (intrinsic) ----
    conv_ratio = spec.face_value / spec.conv_price  # 100/K = 转股比例
    intrinsic = conv_ratio * stock_price

    # ---- corner case 2: 已触发强赎 -> 锁顶 ----
    if is_force_redeemed:
        # 强赎价 = 面值 + 当期未付利息, 通常 ~103, 但实际由公司公告
        return CBValuation(
            theoretical=REDEMPTION_LOCKED_VALUE,
            bond_floor=REDEMPTION_LOCKED_VALUE,
            option_value=0.0,
            intrinsic=intrinsic,
            method="redemption_locked",
            notes="force_redemption_triggered",
        )

    # ---- 计算到期年数 ----
    days_to_maturity = _days_between(valuation_date, spec.maturity_date)
    T_years = days_to_maturity / 365.25

    # ---- 折现率 = 无风险 + 信用利差 ----
    spread_bp = credit_spread_bp.get(spec.rating, DEFAULT_CREDIT_SPREAD_BP["AA"])
    discount_rate = risk_free_rate + spread_bp / 10000.0

    # ---- 债底 ----
    bf = bond_floor_pv(
        face_value=spec.face_value,
        coupon_rate=spec.coupon_rate,
        years_to_maturity=max(T_years, 0.0),
        discount_rate=discount_rate,
    )

    # ---- corner case 4: 临近回售 -> 债底防跌穿 ----
    notes_extras = []
    if 0 < days_to_maturity <= PUTABLE_PERIOD_DAYS:
        # 持有人可按面值卖回, 债底不能低于面值
        bf = max(bf, spec.face_value)
        notes_extras.append("putable_period_floor")

    # ---- corner case 1: 距到期 < 30 天 -> max(intrinsic, bond_floor) ----
    if days_to_maturity < NEAR_MATURITY_DAYS:
        theo = max(intrinsic, bf)
        return CBValuation(
            theoretical=theo,
            bond_floor=bf,
            option_value=0.0,
            intrinsic=intrinsic,
            method="intrinsic",
            notes=";".join(["near_maturity_lt_30d"] + notes_extras),
        )

    # ---- 期权部分: BS ----
    # 极端 vol 防崩
    vol_safe = max(0.0, min(stock_vol if stock_vol is not None else 0.0, 5.0))
    one_call = bs_call(
        S=stock_price,
        K=spec.conv_price,
        T=T_years,
        sigma=vol_safe,
        r=risk_free_rate,
    )
    option_value = conv_ratio * one_call

    # ---- corner case 3: 转股期前 (上市后 6 月内) ----
    # BS 算的就是欧式期权时间价值, T 不变, 这里只在 notes 标记
    list_to_now_days = _days_between(spec.list_date, valuation_date)
    if list_to_now_days < 180:
        notes_extras.append("pre_conversion_period")

    theoretical = bf + option_value

    return CBValuation(
        theoretical=theoretical,
        bond_floor=bf,
        option_value=option_value,
        intrinsic=intrinsic,
        method="BS",
        notes=";".join(notes_extras) if notes_extras else "",
    )


# ----------------------------------------------------------------------
# 实现波动率小工具 (sanity check 用)
# ----------------------------------------------------------------------

def realized_vol(
    close_prices: np.ndarray,
    annualize: bool = True,
) -> float:
    """从一段收盘价算实现波动率.

    Args:
        close_prices: 一维 numpy array, 收盘价时间序列
        annualize: 是否年化 (乘 sqrt(252))

    Returns:
        年化波动率, 如 0.30 = 30%
    """
    if close_prices is None or len(close_prices) < 2:
        return float("nan")
    arr = np.asarray(close_prices, dtype=float)
    arr = arr[~np.isnan(arr)]
    if len(arr) < 2:
        return float("nan")
    log_ret = np.diff(np.log(arr))
    if len(log_ret) == 0:
        return float("nan")
    vol = float(np.std(log_ret, ddof=1))
    if annualize:
        vol *= math.sqrt(252)
    return vol
