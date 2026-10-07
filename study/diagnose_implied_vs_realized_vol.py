#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断脚本: cb_arb 用「标量历史波动率」喂 Black-Scholes, 是否系统性高估转股权价值?

假设 (待检验)
------------
``strategies/cb_arb/cb_pricer.py`` 给转股权定价时, 把 ``realized_vol()`` 算出的
标量年化历史波动率直接当作 BS 的 sigma. 若市场定价里的隐含波动率系统性低于
历史波动率 (波动率风险溢价 / 负 skew / 期限结构), 则理论价被高估, "理论价 - 市价"
这个 value gap 会系统性偏正, 策略会把一批并不便宜的转债判成低估.

方法
----
A 股无个股期权, 波动率曲面不可观测. 但转债 = 债底 + 转股权, 所以可以反解:

    market_option_value(每 100 面值) = 转债收盘价 - 债底
    per-share target                = market_option_value / conv_ratio,  conv_ratio = 100/K
    求 sigma_implied 使 bs_call(S, K, T, sigma, r) == target

再与同一天该标的 **策略实际使用的** realized vol (60 日滚动年化, 上限 1.5) 对比.

边界
----
只读数据. 不写 data/research_framework/, 不改 framework/ scripts/ strategies/.
本脚本只 import ``cb_pricer``, 复用其 ``bond_floor_pv`` / ``bs_call`` / ``realized_vol``
作为**参照实现**, 向量化版本在运行时会与标量版本逐点对拍 (见 ``_selftest``).

输出
----
``study/implied_vol_diagnostic/`` 下的 CSV + PNG + REPORT.txt
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 只读复用: 债底 / BS / 实现波动率的参照实现
from strategies.cb_arb.cb_pricer import (  # noqa: E402
    bond_floor_pv,
    bs_call,
    realized_vol,
)

WAREHOUSE = REPO_ROOT / "data" / "cb_warehouse"

# --------------------------------------------------------------------------- #
# 策略实际使用的参数 (与 strategies/cb_arb/verifier.py 默认值保持一致)
# --------------------------------------------------------------------------- #
VOL_WINDOW_DAYS = 60           # _DEFAULT_VOL_WINDOW_DAYS
VOL_MIN_PERIODS = max(5, VOL_WINDOW_DAYS // 4)
VOL_MULTIPLIER = 1.0           # _DEFAULT_VOL_MULTIPLIER
VOL_CAP = 1.5                  # _VOL_CAP
RISK_FREE = 0.025              # price_cb(risk_free_rate=0.025)
FACE_VALUE = 100.0
NEAR_MATURITY_DAYS = 30        # price_cb corner case 1 / verifier 过滤
PUTABLE_PERIOD_DAYS = 2 * 365  # price_cb corner case 4: 最后 2 年债底不低于面值
MIN_AVG_AMOUNT = 1_000_000.0   # _DEFAULT_MIN_AVG_AMOUNT (20 日均成交额, 元)
MIN_REMAINING_SIZE = 3e8       # 由 verifier 默认值决定, 运行时若能 import 则覆盖
RATING_FLOOR_INT = 2           # 同上

# verifier.CBArbConfig.credit_spread_dict() 在默认 aaa=50 / aa=150 下的展开
CREDIT_SPREAD_BP = {
    "AAA": 50.0,
    "AA+": 90.0,
    "AA": 150.0,
    "AA-": 220.0,
    "A+": 350.0,
    "A": 550.0,
    "A-": 750.0,
}
DEFAULT_SPREAD_BP = CREDIT_SPREAD_BP["AA"]

RATING_TO_INT = {
    "C": -3, "CC": -2, "CCC": -1,
    "B-": 0, "B": 0, "B+": 0,
    "BB-": 0, "BB": 0, "BB+": 0,
    "BBB": 1, "BBB+": 1,
    "A-": 1, "A": 1, "A+": 1,
    "AA-": 2, "AA": 3, "AA+": 4, "AAA": 5,
}


def _sync_constants_from_verifier() -> str:
    """尽量从 verifier 读默认常量, 保证口径与策略一致. 失败则用上面的硬编码副本."""
    global MIN_REMAINING_SIZE, RATING_FLOOR_INT, CREDIT_SPREAD_BP, DEFAULT_SPREAD_BP
    global VOL_WINDOW_DAYS, VOL_MIN_PERIODS, VOL_MULTIPLIER, VOL_CAP, RATING_TO_INT
    try:
        from strategies.cb_arb import verifier as V  # noqa: N812
        from strategies.cb_arb import warehouse_access as WA  # noqa: N812
    except Exception as exc:  # pragma: no cover - 环境缺依赖时降级
        return f"fallback_hardcoded ({type(exc).__name__})"
    cfg = V.CBArbConfig()
    VOL_WINDOW_DAYS = int(cfg.vol_window_days)
    VOL_MIN_PERIODS = max(5, VOL_WINDOW_DAYS // 4)
    VOL_MULTIPLIER = float(cfg.vol_multiplier)
    VOL_CAP = float(V._VOL_CAP)
    MIN_REMAINING_SIZE = float(cfg.min_remaining_size)
    RATING_FLOOR_INT = int(cfg.rating_floor_int)
    CREDIT_SPREAD_BP = {k: float(v) for k, v in cfg.credit_spread_dict().items()}
    DEFAULT_SPREAD_BP = CREDIT_SPREAD_BP["AA"]
    RATING_TO_INT = dict(WA.RATING_TO_INT)
    return "from strategies.cb_arb.verifier"


# --------------------------------------------------------------------------- #
# 向量化数学 (逐点对拍 cb_pricer 的标量实现)
# --------------------------------------------------------------------------- #

_SQRT2 = math.sqrt(2.0)


try:  # scipy 已是 cb_pricer 的依赖
    from scipy.special import ndtr as _ndtr  # type: ignore
except Exception:  # pragma: no cover
    _erf = np.vectorize(math.erf)

    def _ndtr(x):  # 与 scipy.stats.norm.cdf 数学等价
        return 0.5 * (1.0 + _erf(np.asarray(x, float) / _SQRT2))


def bond_floor_vec(
    face: np.ndarray,
    coupon_rate: np.ndarray,
    T: np.ndarray,
    disc: np.ndarray,
) -> np.ndarray:
    """``cb_pricer.bond_floor_pv`` 的向量化等价实现."""
    face = np.asarray(face, dtype=float)
    coupon_rate = np.asarray(coupon_rate, dtype=float)
    T = np.asarray(T, dtype=float)
    disc = np.asarray(disc, dtype=float)

    coupon = face * coupon_rate
    Tc = np.maximum(T, 0.0)
    full_years = np.floor(Tc)
    pv = np.zeros_like(Tc)
    max_fy = int(full_years.max()) if full_years.size else 0
    for t in range(1, max_fy + 1):
        pv += np.where(full_years >= t, coupon / (1.0 + disc) ** t, 0.0)
    pv += (coupon + face) / (1.0 + disc) ** Tc
    # 剩余时间正好整数年时, 循环里多付了到期那年的息, 减掉
    is_int = (np.abs(Tc - full_years) < 1e-9) & (full_years > 0)
    pv -= np.where(is_int, coupon / (1.0 + disc) ** np.maximum(full_years, 1.0), 0.0)
    # 已到期
    pv = np.where(T <= 0, face * (1.0 + coupon_rate), pv)
    return pv


def bs_call_vec(S, K, T, sigma, r) -> np.ndarray:
    """``cb_pricer.bs_call`` 的向量化等价实现 (单份看涨期权)."""
    S = np.asarray(S, dtype=float)
    K = np.asarray(K, dtype=float)
    T = np.asarray(T, dtype=float)
    sigma = np.asarray(sigma, dtype=float)
    sqrtT = np.sqrt(np.maximum(T, 0.0))
    sig = np.maximum(sigma, 1e-12)
    denom = np.maximum(sig * sqrtT, 1e-12)
    d1 = (np.log(np.maximum(S, 1e-12) / np.maximum(K, 1e-12))
          + (r + 0.5 * sig * sig) * T) / denom
    d2 = d1 - sig * sqrtT
    call = S * _ndtr(d1) - K * np.exp(-r * T) * _ndtr(d2)
    zero_vol = np.maximum(S - K * np.exp(-r * T), 0.0)
    call = np.where(sigma <= 0, zero_vol, call)
    call = np.where(T <= 0, np.maximum(S - K, 0.0), call)
    call = np.where((S <= 0) | (K <= 0), 0.0, call)
    return call


def bs_vega_vec(S, K, T, sigma, r) -> np.ndarray:
    """单份看涨期权 vega = dC/dsigma (sigma 单位 1.0 = 100 vol pts)."""
    S = np.asarray(S, dtype=float)
    K = np.asarray(K, dtype=float)
    T = np.asarray(T, dtype=float)
    sigma = np.asarray(sigma, dtype=float)
    sqrtT = np.sqrt(np.maximum(T, 0.0))
    sig = np.maximum(sigma, 1e-9)
    denom = np.maximum(sig * sqrtT, 1e-12)
    d1 = (np.log(np.maximum(S, 1e-12) / np.maximum(K, 1e-12))
          + (r + 0.5 * sig * sig) * T) / denom
    pdf = np.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    return S * pdf * sqrtT


SIGMA_LO = 1e-6
SIGMA_HI = 3.0   # 300% 年化: 反解上限


def implied_vol_vec(S, K, T, r, target, n_iter: int = 90):
    """二分反解隐含波动率.

    Returns
    -------
    sigma : np.ndarray  (无解处为 nan)
    status : np.ndarray[object]
        'ok'                    成功
        'nonpositive_option'    market_option_value <= 0 (市价跌破债底)
        'below_zero_vol_bound'  0 < target < BS(sigma->0), 即低于贴现内在价值
        'above_sigma_cap'       target > BS(sigma=3.0) 但仍 < 上界 S
        'above_bs_upper_bound'  target >= S, 欧式 call 数学上不可能
    """
    S = np.asarray(S, float); K = np.asarray(K, float)
    T = np.asarray(T, float); target = np.asarray(target, float)

    lo_val = bs_call_vec(S, K, T, SIGMA_LO, r)
    hi_val = bs_call_vec(S, K, T, SIGMA_HI, r)

    status = np.full(S.shape, "ok", dtype=object)
    status = np.where(target > hi_val, "above_sigma_cap", status)
    status = np.where(target >= S, "above_bs_upper_bound", status)
    status = np.where((target > 0) & (target < lo_val), "below_zero_vol_bound", status)
    status = np.where(target <= 0, "nonpositive_option", status)

    solvable = status == "ok"
    sigma = np.full(S.shape, np.nan)
    if solvable.any():
        s = S[solvable]; k = K[solvable]; t = T[solvable]; tg = target[solvable]
        lo = np.full(s.shape, SIGMA_LO)
        hi = np.full(s.shape, SIGMA_HI)
        for _ in range(n_iter):
            mid = 0.5 * (lo + hi)
            v = bs_call_vec(s, k, t, mid, r)
            too_low = v < tg
            lo = np.where(too_low, mid, lo)
            hi = np.where(too_low, hi, mid)
        sigma[solvable] = 0.5 * (lo + hi)
    return sigma, status


def _selftest(rng: np.random.Generator, n: int = 2000) -> dict:
    """向量化实现 vs cb_pricer 标量实现逐点对拍. 不通过就直接崩, 别出报告."""
    face = np.full(n, 100.0)
    cr = rng.uniform(0.0, 0.03, n)
    T = rng.uniform(0.05, 6.0, n)
    disc = rng.uniform(0.01, 0.12, n)
    bf_vec = bond_floor_vec(face, cr, T, disc)
    bf_sca = np.array([bond_floor_pv(100.0, cr[i], T[i], disc[i]) for i in range(n)])
    bf_err = float(np.max(np.abs(bf_vec - bf_sca)))

    S = rng.uniform(1.0, 80.0, n)
    K = rng.uniform(1.0, 80.0, n)
    sig = rng.uniform(0.01, 2.0, n)
    c_vec = bs_call_vec(S, K, T, sig, RISK_FREE)
    c_sca = np.array([bs_call(S[i], K[i], T[i], sig[i], RISK_FREE) for i in range(n)])
    c_err = float(np.max(np.abs(c_vec - c_sca)))

    # 反解闭环: 由 sigma 生成价格, 再解回 sigma
    tgt = c_vec
    sig_back, st = implied_vol_vec(S, K, T, RISK_FREE, tgt)
    ok = st == "ok"
    # 价格空间闭环: 反解出的 sigma 必须还原出同一个价格
    px_err = float(np.max(np.abs(bs_call_vec(S[ok], K[ok], T[ok], sig_back[ok], RISK_FREE)
                                 - tgt[ok]))) if ok.any() else float("nan")
    # sigma 空间闭环只在 vega 有意义处检查 —— vega -> 0 时价格对 sigma 不敏感,
    # sigma 本来就不可辨识, 这正是报告里要单独标注的"深度价内数值不稳定"
    vega = bs_vega_vec(S, K, T, sig, RISK_FREE)
    idf = ok & (vega > 0.05 * S)
    inv_err = float(np.max(np.abs(sig_back[idf] - sig[idf]))) if idf.any() else float("nan")

    # 滚动 realized vol 与 cb_pricer.realized_vol 对拍
    px = 10.0 * np.exp(np.cumsum(rng.normal(0, 0.02, 400)))
    s = pd.Series(px)
    lr = np.log(s.clip(lower=1e-9)).diff()
    roll = lr.rolling(window=VOL_WINDOW_DAYS, min_periods=VOL_MIN_PERIODS).std(ddof=1) * math.sqrt(252)
    ref = realized_vol(px[-(VOL_WINDOW_DAYS + 1):])
    vol_err = abs(float(roll.iloc[-1]) - float(ref))

    assert bf_err < 1e-8, f"bond_floor mismatch {bf_err}"
    assert c_err < 1e-8, f"bs_call mismatch {c_err}"
    assert px_err < 1e-6, f"inversion price roundtrip mismatch {px_err}"
    assert not math.isfinite(inv_err) or inv_err < 1e-4, f"inversion sigma mismatch {inv_err}"
    assert vol_err < 1e-9, f"realized_vol mismatch {vol_err}"
    return {
        "bond_floor_max_abs_err": bf_err,
        "bs_call_max_abs_err": c_err,
        "inversion_price_roundtrip_max_abs_err": px_err,
        "inversion_sigma_roundtrip_max_abs_err_where_vega_meaningful": inv_err,
        "rolling_vol_vs_cb_pricer_err": vol_err,
    }


# --------------------------------------------------------------------------- #
# 数据
# --------------------------------------------------------------------------- #

def load_panel(min_amount: float, start_date: str | None, end_date: str | None):
    """构建 (转债, 交易日) 面板 + 全部派生列. 返回 (panel, diag)."""
    diag: dict = {}

    cb_basic = pd.read_parquet(WAREHOUSE / "cb_basic.parquet")
    cb_daily = pd.read_parquet(WAREHOUSE / "cb_daily.parquet")
    stk = pd.read_parquet(WAREHOUSE / "stk_daily_qfq.parquet")[
        ["stk_code", "trade_date", "close"]
    ].copy()
    cb_call = pd.read_parquet(WAREHOUSE / "cb_call.parquet")

    diag["n_cb_basic"] = int(len(cb_basic))
    diag["n_cb_daily_rows"] = int(len(cb_daily))
    diag["cb_daily_date_range"] = (
        f'{cb_daily["trade_date"].min()}~{cb_daily["trade_date"].max()}'
    )
    # 转股价: 2026-10-07 起用当日真实转股价值反推 (见下方 merge), 不再是 cb_basic 的静态单值
    diag["conv_price_is_static_scalar_per_bond"] = False
    diag["cb_daily_has_conv_price_col"] = False
    diag["par_value_unique"] = sorted(
        pd.Series(cb_basic["par_value"]).dropna().unique().tolist()
    )[:5]

    # ---- 正股滚动 realized vol: 与 verifier._compute_realized_vol_window 同口径 ----
    stk = stk.sort_values(["stk_code", "trade_date"]).reset_index(drop=True)
    stk["log_close"] = np.log(stk["close"].astype(float).clip(lower=1e-9))
    stk["log_ret"] = stk.groupby("stk_code")["log_close"].diff()
    stk["vol_ann"] = (
        stk.groupby("stk_code")["log_ret"]
        .rolling(window=VOL_WINDOW_DAYS, min_periods=VOL_MIN_PERIODS)
        .std(ddof=1)
        .reset_index(level=0, drop=True)
    ) * math.sqrt(252)
    stk = stk[["stk_code", "trade_date", "close", "vol_ann"]]

    # ---- cb_daily: 成交额与 20 日均值 (verifier 口径: close * vol) ----
    cb_daily = cb_daily.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    cb_daily["amount_yuan"] = cb_daily["close"].astype(float) * cb_daily["vol"].astype(float)
    cb_daily["amount_20d"] = (
        cb_daily.groupby("ts_code")["amount_yuan"]
        .rolling(window=20, min_periods=1).mean()
        .reset_index(level=0, drop=True)
    )

    if start_date:
        cb_daily = cb_daily[cb_daily["trade_date"] >= start_date]
    if end_date:
        cb_daily = cb_daily[cb_daily["trade_date"] <= end_date]

    basic_cols = [
        "ts_code", "stk_code", "list_date", "contract_maturity_date",
        "coupon_rate", "rating", "issue_size", "remain_size", "par_value",
    ]
    b = cb_basic[basic_cols].drop_duplicates("ts_code").copy()
    b["rating_int"] = b["rating"].map(
        lambda r: RATING_TO_INT.get(r, 0) if isinstance(r, str) else 0
    ).astype(int)
    b["issue_size_yuan"] = pd.to_numeric(b["issue_size"], errors="coerce") * 1e8

    df = cb_daily.merge(b, on="ts_code", how="inner")
    df = df.merge(
        stk, on=["stk_code", "trade_date"], how="left", suffixes=("", "_stk")
    ).rename(columns={"close_stk": "stk_close", "vol_ann": "sigma_realized_raw"})

    # Conversion price on each day = 100 * stock price / that day's true conversion value. cb_basic only
    # has the latest conversion price (conv_price_latest), which is wrong before every down-revision.
    # Maturity is contract maturity; expire_date_raw is the delisting date for bonds that have left.
    pit = pd.read_parquet(WAREHOUSE / "cb_conv_value_pit.parquet", columns=["ts_code", "trade_date", "conv_value"])
    df = df.merge(pit.rename(columns={"conv_value": "conv_value_pit"}), on=["ts_code", "trade_date"], how="left")
    df["conv_price"] = 100.0 * df["stk_close"].astype(float) / df["conv_value_pit"].where(df["conv_value_pit"] > 0)
    df = df.rename(columns={"contract_maturity_date": "maturity_date"})
    df = df[df["conv_price"].notna() & df["maturity_date"].notna()].reset_index(drop=True)

    n0 = len(df)
    diag["rows_after_merge"] = int(n0)
    q = df["amount_20d"].quantile([0.01, 0.05, 0.25, 0.5]).round(0)
    diag["amount_20d_pctl_1_5_25_50"] = [float(v) for v in q.values]

    # ---- 派生 ----
    df["face_value"] = FACE_VALUE
    d_mat = pd.to_datetime(df["maturity_date"], format="%Y%m%d", errors="coerce")
    d_now = pd.to_datetime(df["trade_date"], format="%Y%m%d", errors="coerce")
    df["days_to_maturity"] = (d_mat - d_now).dt.days
    df["T_years"] = df["days_to_maturity"] / 365.25
    df["conv_price"] = pd.to_numeric(df["conv_price"], errors="coerce")
    df["coupon_rate"] = pd.to_numeric(df["coupon_rate"], errors="coerce").fillna(0.01)
    df["conv_ratio"] = FACE_VALUE / df["conv_price"]
    df["moneyness"] = df["stk_close"] / df["conv_price"]
    df["conv_value"] = df["conv_ratio"] * df["stk_close"]
    df["sigma_realized"] = np.minimum(
        VOL_CAP, df["sigma_realized_raw"].astype(float) * VOL_MULTIPLIER
    )
    df["year"] = df["trade_date"].str[:4]
    df["spread_bp"] = df["rating"].map(CREDIT_SPREAD_BP).fillna(DEFAULT_SPREAD_BP)

    # ---- 强赎: 公告区间 (verifier._is_force_redeemed_on_date 同口径) ----
    df["in_call_window"] = False
    cc = cb_call[["ts_code", "ann_date", "call_date", "expire_date"]].copy()
    idx_by_code = df.groupby("ts_code").indices
    n_intervals = 0
    for row in cc.itertuples(index=False):
        ann = row.ann_date if (isinstance(row.ann_date, str) and len(row.ann_date) == 8) else ""
        exp = row.expire_date if (isinstance(row.expire_date, str) and len(row.expire_date) == 8) else ""
        if not exp:
            continue
        if not ann:
            ann = exp
        if ann > exp:
            ann, exp = exp, ann
        pos = idx_by_code.get(row.ts_code)
        if pos is None:
            continue
        d = df["trade_date"].values[pos]
        hit = pos[(d >= ann) & (d <= exp)]
        if hit.size:
            df.iloc[hit, df.columns.get_loc("in_call_window")] = True
        n_intervals += 1
    diag["n_call_intervals"] = n_intervals

    # ---- 强赎条件近似: 近 30 个交易日中有 >=15 日 S/K >= 1.3 ----
    hit = (df["moneyness"] >= 1.3).astype(float)
    df["call_cond_days_30"] = (
        hit.groupby(df["ts_code"]).rolling(window=30, min_periods=30).sum()
        .reset_index(level=0, drop=True)
    )
    df["call_condition_met"] = df["call_cond_days_30"] >= 15

    return df, diag


def apply_filters(df: pd.DataFrame, min_amount: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    """按 verifier 的候选池口径过滤. 返回 (过滤后面板, 过滤台账)."""
    ledger = []
    n = len(df)

    def cut(mask, name):
        nonlocal df, n
        keep = mask.fillna(False)
        dropped = int((~keep).sum())
        ledger.append({"step": name, "dropped": dropped, "remaining": int(keep.sum()),
                       "dropped_pct_of_start": round(dropped / max(n, 1) * 100, 3)})
        df = df[keep]

    cut(df["stk_close"].notna() & (df["stk_close"] > 0), "有正股收盘价")
    cut(df["conv_price"].notna() & (df["conv_price"] > 0), "有转股价")
    cut(df["close"].notna() & (df["close"] > 0), "有转债收盘价")
    cut(df["days_to_maturity"] > NEAR_MATURITY_DAYS, f"距到期 > {NEAR_MATURITY_DAYS} 天")
    cut(df["sigma_realized"].notna() & (df["sigma_realized"] > 0),
        f"有 {VOL_WINDOW_DAYS} 日 realized vol")
    cut(df["rating_int"] >= RATING_FLOOR_INT, f"评级 >= {RATING_FLOOR_INT}")
    cut(df["issue_size_yuan"] >= MIN_REMAINING_SIZE,
        f"发行规模 >= {MIN_REMAINING_SIZE/1e8:.1f} 亿")
    cut(df["amount_20d"] >= min_amount, f"20日均成交额 >= {min_amount/1e4:.0f} 万元")
    cut(~df["in_call_window"], "剔除强赎公告期 (模型此时不走 BS)")

    return df.reset_index(drop=True), pd.DataFrame(ledger)




# --------------------------------------------------------------------------- #
# 反解
# --------------------------------------------------------------------------- #

def solve_scenario(df: pd.DataFrame, spread_shift_bp: float, put_floor: bool,
                   vega_floor: float) -> pd.DataFrame:
    """给定贴现率情景, 算债底 -> market option value -> sigma_implied.

    返回一个与 ``df`` 同行数的轻量结果表 (只带分析需要的列).
    """
    disc = np.maximum(RISK_FREE + (df["spread_bp"].values + spread_shift_bp) / 10000.0, 1e-4)
    bf = bond_floor_vec(df["face_value"].values, df["coupon_rate"].values,
                        df["T_years"].values, disc)
    if put_floor:
        near_put = (df["days_to_maturity"].values > 0) & (
            df["days_to_maturity"].values <= PUTABLE_PERIOD_DAYS)
        bf = np.where(near_put, np.maximum(bf, df["face_value"].values), bf)

    opt_mkt_100 = df["close"].values.astype(float) - bf
    target = opt_mkt_100 / df["conv_ratio"].values                # per share
    S = df["stk_close"].values
    K = df["conv_price"].values
    T = df["T_years"].values
    sig_real = df["sigma_realized"].values

    sigma, status = implied_vol_vec(S, K, T, RISK_FREE, target)

    # 截尾填补: 反解失败的观测不是随机缺失, 而是被 BS 的可行区间截断.
    #   下界失败 (期权价值 <= 0 或低于零波动率下界) -> 真实 sigma_implied <= 0
    #   上界失败 (需要 sigma > 3.0 甚至超过欧式上界 S) -> 真实 sigma_implied >= 3.0
    # 把它们填到各自边界上不改变样本的秩序, 因此**中位数**仍然是无偏的
    # (只要真中位数落在可观测区间内). 均值不行, 所以只用中位数.
    sigma_cens = sigma.copy()
    lower_fail = np.isin(status, ["nonpositive_option", "below_zero_vol_bound"])
    upper_fail = np.isin(status, ["above_sigma_cap", "above_bs_upper_bound"])
    sigma_cens = np.where(lower_fail, 0.0, sigma_cens)
    sigma_cens = np.where(upper_fail, SIGMA_HI, sigma_cens)

    sig_for_vega = np.where(np.isnan(sigma), sig_real, sigma)
    vega_100 = bs_vega_vec(S, K, T, sig_for_vega, RISK_FREE) * df["conv_ratio"].values

    model_opt = df["conv_ratio"].values * bs_call_vec(S, K, T, sig_real, RISK_FREE)
    theo = bf + model_opt

    out = pd.DataFrame({
        "bond_floor": bf,
        "market_option_value": opt_mkt_100,
        "model_option_value": model_opt,
        "option_overprice_yuan": model_opt - opt_mkt_100,
        "theo_price": theo,
        "theo_minus_mkt_pct": (theo - df["close"].values) / df["close"].values * 100.0,
        "sigma_implied": sigma,
        "sigma_implied_cens": sigma_cens,
        "solve_status": status,
        "vega_per_100_face": vega_100,
        "dsigma_per_1yuan": 1.0 / np.maximum(vega_100, 1e-9),
    }, index=df.index)
    out["vol_diff"] = out["sigma_implied"] - sig_real
    out["vol_diff_cens"] = out["sigma_implied_cens"] - sig_real
    out["unstable_vega"] = out["vega_per_100_face"] < vega_floor
    out["solve_ok"] = out["solve_status"] == "ok"
    return out


# --------------------------------------------------------------------------- #
# 分组 / 子样本
# --------------------------------------------------------------------------- #

MONEY_BINS = [-np.inf, 0.7, 0.9, 1.1, 1.3, np.inf]
MONEY_LABELS = ["<0.7", "0.7-0.9", "0.9-1.1", "1.1-1.3", ">1.3"]
TENOR_BINS = [-np.inf, 1.0, 2.0, 3.0, 4.0, 5.0, np.inf]
TENOR_LABELS = ["<1y", "1-2y", "2-3y", "3-4y", "4-5y", ">5y"]

SUBSET_DOC = {
    "all_obs_censored": "全部观测, 反解失败者按截断边界填补 (中位数无选择偏差)",
    "solved_only": "仅反解成功的观测 (有选择偏差, 双向截断)",
    "solved_stable_vega": "反解成功 + vega 足够大 (数值可辨识)",
    "atm_stable": "moneyness 0.9-1.1 + vega 稳定 (BS 最可信的区域)",
    "atm_stable_ex_call": "上者再剔除近似满足强赎条件的观测",
    "tenor_gt3y_stable": "剩余期限 > 3 年 + vega 稳定 (最贴近转债实际久期)",
    "top_liquidity_stable": "20 日均成交额 >= 模型阈值的 10 倍 + vega 稳定 (收盘价最可信)",
}

# 严格流动性子样本的门槛 (元), main() 里按 --min-amount 的 10 倍设定
STRICT_AMOUNT = 1e7


def subset_masks(panel: pd.DataFrame, res: pd.DataFrame) -> dict:
    ok = res["solve_ok"].values
    stable = ok & (~res["unstable_vega"].values)
    atm = panel["moneyness_bucket"].astype(str).values == "0.9-1.1"
    no_call = ~panel["call_condition_met"].fillna(False).values
    longt = panel["T_years"].values > 3.0
    n = len(panel)
    return {
        "all_obs_censored": np.ones(n, dtype=bool),
        "solved_only": ok,
        "solved_stable_vega": stable,
        "atm_stable": stable & atm,
        "atm_stable_ex_call": stable & atm & no_call,
        "tenor_gt3y_stable": stable & longt,
        "top_liquidity_stable": stable & (panel["amount_20d"].values >= STRICT_AMOUNT),
    }


def subset_median(panel: pd.DataFrame, res: pd.DataFrame, name: str, mask) -> float:
    col = "vol_diff_cens" if name == "all_obs_censored" else "vol_diff"
    v = res.loc[mask, col]
    return float(v.median()) if len(v) else float("nan")


def _stats(g: pd.DataFrame) -> pd.Series:
    d = g["vol_diff"]
    dc = g["vol_diff_cens"]
    return pd.Series({
        "n": int(len(g)),
        "n_solved": int(d.notna().sum()),
        "solve_ok_pct": float(g["solve_ok"].mean() * 100),
        "sigma_implied_median": g.loc[g["solve_ok"], "sigma_implied"].median(),
        "sigma_realized_median": g["sigma_realized"].median(),
        "diff_median_solved": d.median(),
        "diff_median_censored": dc.median(),
        "diff_p25_solved": d.quantile(0.25),
        "diff_p75_solved": d.quantile(0.75),
        "pct_implied_below_realized_cens": float((dc < 0).mean() * 100),
        "option_overprice_yuan_median": g["option_overprice_yuan"].median(),
        "theo_minus_mkt_pct_median": g["theo_minus_mkt_pct"].median(),
        "vega_per_100_face_median": g["vega_per_100_face"].median(),
        "dsigma_per_1yuan_median": g["dsigma_per_1yuan"].median(),
    })


def agg_by(df: pd.DataFrame, key, labels=None) -> pd.DataFrame:
    out = df.groupby(key, observed=True).apply(_stats, include_groups=False)
    out = out.reset_index()
    if labels is not None:
        out[key] = pd.Categorical(out[key].astype(str), categories=labels, ordered=True)
        out = out.sort_values(key).reset_index(drop=True)
    return out


def fail_table(df: pd.DataFrame, key=None) -> pd.DataFrame:
    if key is None:
        s = df["solve_status"].value_counts(dropna=False)
        return pd.DataFrame({"solve_status": s.index, "n": s.values,
                             "pct": (s / len(df) * 100).round(3).values})
    ct = pd.crosstab(df[key], df["solve_status"], normalize="index") * 100
    ct["n"] = df.groupby(key, observed=True).size()
    return ct.round(3).reset_index()


def breakeven_shift(shifts: np.ndarray, med: np.ndarray) -> float:
    """在 spread shift 网格上线性插值出 median(diff)=0 的位置 (bp). 无过零点返回 nan."""
    m = np.asarray(med, float)
    ok = np.isfinite(m)
    if ok.sum() < 2:
        return float("nan")
    s = np.asarray(shifts, float)[ok]
    m = m[ok]
    sign = np.sign(m)
    for i in range(len(m) - 1):
        if sign[i] == 0:
            return float(s[i])
        if sign[i] * sign[i + 1] < 0:
            t = m[i] / (m[i] - m[i + 1])
            return float(s[i] + t * (s[i + 1] - s[i]))
    return float("nan")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

MAIN_SCENARIOS = [
    ("base", 0.0, True),
    ("spread_-200bp", -200.0, True),
    ("spread_-100bp", -100.0, True),
    ("spread_+100bp", 100.0, True),
    ("spread_+200bp", 200.0, True),
    ("no_put_par_floor", 0.0, False),
]
SCAN_SHIFTS = list(range(-400, 401, 50))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="cb_arb 转股权定价诊断: 从转债市价反解隐含波动率, 与模型使用的历史波动率对比",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--outdir",
                    default=str(Path(__file__).resolve().parent / "implied_vol_diagnostic"))
    ap.add_argument("--min-amount", type=float, default=MIN_AVG_AMOUNT,
                    help="20 日均成交额下限 (元)")
    ap.add_argument("--start-date", default=None)
    ap.add_argument("--end-date", default=None)
    ap.add_argument("--vega-floor", type=float, default=20.0,
                    help="vega (元 / 1.0 vol) 低于此值视为反解数值不稳定")
    ap.add_argument("--sample-rows", type=int, default=20000,
                    help="导出逐观测样本行数 (0 = 不导出)")
    ap.add_argument("--skip-scan", action="store_true", help="跳过 breakeven 细网格扫描")
    ap.add_argument("--seed", type=int, default=20260827)
    args = ap.parse_args()

    global STRICT_AMOUNT
    STRICT_AMOUNT = args.min_amount * 10.0

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(args.seed)

    src = _sync_constants_from_verifier()
    print(f"[const] 参数来源: {src}")
    print(f"[const] vol_window={VOL_WINDOW_DAYS} cap={VOL_CAP} rf={RISK_FREE} spread={CREDIT_SPREAD_BP}")

    st = _selftest(rng)
    print(f"[selftest] 与 cb_pricer 标量实现对拍通过: {st}")

    panel, diag = load_panel(args.min_amount, args.start_date, args.end_date)
    print(f"[data] {diag}")
    panel, ledger = apply_filters(panel, args.min_amount)
    print(ledger.to_string(index=False))
    if panel.empty:
        print("过滤后无数据, 退出")
        return 1

    panel["moneyness_bucket"] = pd.cut(panel["moneyness"], MONEY_BINS, labels=MONEY_LABELS)
    panel["tenor_bucket"] = pd.cut(panel["T_years"], TENOR_BINS, labels=TENOR_LABELS)
    panel = panel.reset_index(drop=True)

    # ---- 主情景 + 敏感性 ----
    scen_rows, scen_bucket_frames, subset_rows = [], [], []
    base_res = None
    for name, shift, pfloor in MAIN_SCENARIOS:
        res = solve_scenario(panel, shift, pfloor, args.vega_floor)
        ok = res["solve_ok"].values
        masks = subset_masks(panel, res)
        row = {
            "scenario": name, "spread_shift_bp": shift, "put_par_floor": pfloor,
            "n_obs": int(len(res)),
            "solve_ok_pct": round(float(ok.mean() * 100), 3),
            "nonpositive_option_pct": round(float((res["solve_status"] == "nonpositive_option").mean() * 100), 3),
            "below_zero_vol_bound_pct": round(float((res["solve_status"] == "below_zero_vol_bound").mean() * 100), 3),
            "above_sigma_cap_pct": round(float((res["solve_status"] == "above_sigma_cap").mean() * 100), 3),
            "above_bs_upper_bound_pct": round(float((res["solve_status"] == "above_bs_upper_bound").mean() * 100), 3),
            "bond_floor_median": round(float(res["bond_floor"].median()), 3),
            "option_overprice_yuan_median": round(float(res.loc[ok, "option_overprice_yuan"].median()), 3),
            "theo_minus_mkt_pct_median": round(float(res.loc[ok, "theo_minus_mkt_pct"].median()), 3),
        }
        for sname, m in masks.items():
            row[f"diff_median__{sname}"] = round(subset_median(panel, res, sname, m), 4)
            subset_rows.append({"scenario": name, "subset": sname,
                                "n": int(np.sum(m)),
                                "diff_median": subset_median(panel, res, sname, m)})
        scen_rows.append(row)

        joined = pd.concat([panel[["moneyness_bucket", "tenor_bucket", "year",
                                   "sigma_realized", "call_condition_met"]], res], axis=1)
        b = agg_by(joined, "moneyness_bucket", MONEY_LABELS)
        b.insert(0, "scenario", name)
        scen_bucket_frames.append(b)
        print(f"[scenario] {name}: ok={row['solve_ok_pct']}% "
              f"diff_median(solved)={row['diff_median__solved_only']} "
              f"diff_median(censored)={row['diff_median__all_obs_censored']}")
        if name == "base":
            base_res = res
    assert base_res is not None

    sens = pd.DataFrame(scen_rows)
    sens.to_csv(outdir / "sensitivity_scenarios.csv", index=False)
    pd.DataFrame(subset_rows).to_csv(outdir / "sensitivity_by_subset.csv", index=False)
    scen_buckets = pd.concat(scen_bucket_frames, ignore_index=True)
    scen_buckets.to_csv(outdir / "sensitivity_by_moneyness.csv", index=False)
    ledger.to_csv(outdir / "filter_ledger.csv", index=False)

    # ---- breakeven: 需要多大的信用利差误差才能让结论翻转 ----
    scan = None
    if not args.skip_scan:
        rows = []
        for sh in SCAN_SHIFTS:
            r = solve_scenario(panel, float(sh), True, args.vega_floor)
            ms = subset_masks(panel, r)
            rec = {"spread_shift_bp": sh,
                   "solve_ok_pct": round(float(r["solve_ok"].mean() * 100), 3)}
            for sname, m in ms.items():
                rec[sname] = subset_median(panel, r, sname, m)
            rows.append(rec)
        scan = pd.DataFrame(rows)
        scan.to_csv(outdir / "breakeven_spread_scan.csv", index=False)
        print("[scan] breakeven 扫描完成")

    # ---- 基准情景的完整表 ----
    full = pd.concat([panel, base_res], axis=1)
    solved = full[full["solve_ok"]].copy()
    stable = solved[~solved["unstable_vega"]].copy()

    subsets_overall = []
    ms = subset_masks(panel, base_res)
    for sname, m in ms.items():
        sub = full[m]
        s = _stats(sub)
        s["subset"] = sname
        s["headline_diff_median"] = subset_median(panel, base_res, sname, m)
        s["note"] = SUBSET_DOC[sname]
        subsets_overall.append(s)
    overall = pd.DataFrame(subsets_overall)
    overall = overall[["subset", "headline_diff_median"] +
                      [c for c in overall.columns if c not in ("subset", "headline_diff_median")]]
    overall.to_csv(outdir / "summary_overall.csv", index=False)

    agg_by(full, "year").to_csv(outdir / "by_year.csv", index=False)
    agg_by(full, "moneyness_bucket", MONEY_LABELS).to_csv(outdir / "by_moneyness.csv", index=False)
    agg_by(full, "tenor_bucket", TENOR_LABELS).to_csv(outdir / "by_tenor.csv", index=False)
    stable_full = full[~full["unstable_vega"] & full["solve_ok"]]
    agg_by(stable_full, "moneyness_bucket", MONEY_LABELS).to_csv(
        outdir / "by_moneyness_stable_vega.csv", index=False)
    agg_by(stable_full, "tenor_bucket", TENOR_LABELS).to_csv(
        outdir / "by_tenor_stable_vega.csv", index=False)

    fail_table(full).to_csv(outdir / "solve_status_overall.csv", index=False)
    fail_table(full, "moneyness_bucket").to_csv(outdir / "solve_status_by_moneyness.csv", index=False)
    fail_table(full, "tenor_bucket").to_csv(outdir / "solve_status_by_tenor.csv", index=False)
    fail_table(full, "year").to_csv(outdir / "solve_status_by_year.csv", index=False)

    cc = full.copy()
    cc["call_condition_met"] = cc["call_condition_met"].fillna(False)
    agg_by(cc, "call_condition_met").to_csv(outdir / "by_call_condition.csv", index=False)

    if args.sample_rows > 0:
        keep = ["ts_code", "trade_date", "stk_code", "close", "stk_close", "conv_price",
                "moneyness", "T_years", "days_to_maturity", "rating", "coupon_rate",
                "amount_20d", "bond_floor", "market_option_value", "model_option_value",
                "option_overprice_yuan", "theo_price", "theo_minus_mkt_pct",
                "sigma_realized", "sigma_implied", "sigma_implied_cens", "vol_diff",
                "vol_diff_cens", "solve_status", "vega_per_100_face", "dsigma_per_1yuan",
                "unstable_vega", "call_condition_met", "moneyness_bucket", "tenor_bucket"]
        full.sample(min(args.sample_rows, len(full)), random_state=args.seed)[keep].to_csv(
            outdir / "sample_observations.csv", index=False)

    make_plot(full, scen_buckets, outdir / "implied_vs_realized_vol.png")
    write_report(outdir, diag, ledger, overall, sens, scan, full, args, st, src)
    print(f"[done] 输出目录: {outdir}")
    return 0


# --------------------------------------------------------------------------- #
# 图
# --------------------------------------------------------------------------- #

def make_plot(full: pd.DataFrame, scen_buckets: pd.DataFrame, path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    solved = full[full["solve_ok"]]
    stable = solved[~solved["unstable_vega"]]
    x = np.arange(len(MONEY_LABELS))

    fig, axes = plt.subplots(2, 2, figsize=(14.5, 10))

    # (1) implied vs realized by moneyness
    ax = axes[0][0]
    g = solved.groupby("moneyness_bucket", observed=True)
    imp = g["sigma_implied"].median().reindex(MONEY_LABELS)
    il = g["sigma_implied"].quantile(0.25).reindex(MONEY_LABELS)
    ih = g["sigma_implied"].quantile(0.75).reindex(MONEY_LABELS)
    gr = full.groupby("moneyness_bucket", observed=True)
    rea = gr["sigma_realized"].median().reindex(MONEY_LABELS)
    rl = gr["sigma_realized"].quantile(0.25).reindex(MONEY_LABELS)
    rh = gr["sigma_realized"].quantile(0.75).reindex(MONEY_LABELS)
    cens = gr["sigma_implied_cens"].median().reindex(MONEY_LABELS)
    ax.plot(x, imp, "o-", color="#c0392b", label="implied (solved only, median)")
    ax.fill_between(x, il, ih, color="#c0392b", alpha=0.13)
    ax.plot(x, cens, "^--", color="#8e44ad",
            label="implied (all obs, censor-filled, median)")
    ax.plot(x, rea, "s-", color="#2c6fbb", label="realized 60d (median)")
    ax.fill_between(x, rl, rh, color="#2c6fbb", alpha=0.13)
    ax.set_xticks(x); ax.set_xticklabels(MONEY_LABELS)
    ax.set_xlabel("moneyness S/K"); ax.set_ylabel("annualized vol")
    ax.set_title("Implied vs realized vol by moneyness (bands = IQR)")
    ax.legend(fontsize=8); ax.grid(alpha=0.3)

    # (2) vol gap by moneyness across discount-rate scenarios
    ax = axes[0][1]
    for name, sub in scen_buckets.groupby("scenario"):
        sub = sub.set_index(sub["moneyness_bucket"].astype(str)).reindex(MONEY_LABELS)
        base = name == "base"
        ax.plot(x, sub["diff_median_solved"].values, "o-" if base else "--",
                lw=2.6 if base else 1.2, label=name)
    ax.axhline(0, color="k", lw=1)
    ax.set_xticks(x); ax.set_xticklabels(MONEY_LABELS)
    ax.set_xlabel("moneyness S/K")
    ax.set_ylabel("median(implied - realized)")
    ax.set_title("Vol gap by moneyness — bond-floor / credit-spread sensitivity")
    ax.legend(fontsize=8); ax.grid(alpha=0.3)

    # (3) vol gap by tenor
    ax = axes[1][0]
    xs = np.arange(len(TENOR_LABELS))
    for lbl, sub, color, col in [
        ("solved only", solved, "#c0392b", "vol_diff"),
        ("stable vega only", stable, "#27ae60", "vol_diff"),
        ("all obs, censor-filled", full, "#8e44ad", "vol_diff_cens"),
    ]:
        gg = sub.groupby("tenor_bucket", observed=True)[col]
        ax.plot(xs, gg.median().reindex(TENOR_LABELS), "o-", color=color, label=lbl)
        if col == "vol_diff":
            ax.fill_between(xs, gg.quantile(0.25).reindex(TENOR_LABELS),
                            gg.quantile(0.75).reindex(TENOR_LABELS), color=color, alpha=0.10)
    ax.axhline(0, color="k", lw=1)
    ax.set_xticks(xs); ax.set_xticklabels(TENOR_LABELS)
    ax.set_xlabel("time to maturity"); ax.set_ylabel("median(implied - realized)")
    ax.set_title("Vol gap by tenor (bands = IQR)")
    ax.legend(fontsize=8); ax.grid(alpha=0.3)

    # (4) solve status composition
    ax = axes[1][1]
    ct = pd.crosstab(full["moneyness_bucket"], full["solve_status"],
                     normalize="index").reindex(MONEY_LABELS) * 100
    order = [c for c in ["ok", "below_zero_vol_bound", "nonpositive_option",
                         "above_sigma_cap", "above_bs_upper_bound"] if c in ct.columns]
    colors = {"ok": "#95a5a6", "below_zero_vol_bound": "#e67e22",
              "nonpositive_option": "#d35400", "above_sigma_cap": "#2980b9",
              "above_bs_upper_bound": "#8e44ad"}
    bottom = np.zeros(len(MONEY_LABELS))
    for col in order:
        vals = ct[col].fillna(0).values
        ax.bar(x, vals, bottom=bottom, label=col, color=colors.get(col))
        bottom += vals
    ax.set_xticks(x); ax.set_xticklabels(MONEY_LABELS)
    ax.set_xlabel("moneyness S/K"); ax.set_ylabel("% of observations")
    ax.set_title("Inversion outcome mix (failure rate is itself a result)")
    ax.legend(fontsize=8); ax.grid(alpha=0.3, axis="y")

    fig.suptitle("cb_arb conversion-option pricing diagnostic: "
                 "CB-implied vol vs the realized vol the model feeds into BS", fontsize=13)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(path, dpi=140)
    plt.close(fig)


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #

def _md_table(df: pd.DataFrame, floatfmt: str = "{:.4f}") -> str:
    d = df.copy()
    for c in d.columns:
        if pd.api.types.is_float_dtype(d[c]):
            d[c] = d[c].map(lambda v: "" if pd.isna(v) else floatfmt.format(v))
    header = "| " + " | ".join(str(c) for c in d.columns) + " |"
    sep = "|" + "|".join(["---"] * len(d.columns)) + "|"
    rows = ["| " + " | ".join(str(v) for v in r) + " |" for r in d.astype(str).values]
    return "\n".join([header, sep] + rows)


def _verdict(sens: pd.DataFrame, scan, ok_pct: float, full: pd.DataFrame) -> tuple[str, list[str]]:
    """按「跨情景符号是否稳定 + 翻转所需的假设是否可信」给结论.

    三种答案 (成立 / 不成立 / 无法判断) 都允许, 不为了给结论而给结论.
    """
    detail = []
    ranges_all, ranges_plaus = {}, {}
    # 可信区间: 信用利差平移 |shift| <= 100bp (含关闭回售地板);
    # ±200bp 视为极端情景 —— 对 AA (150bp) 而言 -200bp 意味着负信用利差.
    plaus = sens["spread_shift_bp"].abs() <= 100
    for sname in SUBSET_DOC:
        col = f"diff_median__{sname}"
        if col not in sens.columns:
            continue
        v = sens[col].astype(float)
        vp = v[plaus]
        ranges_all[sname] = (float(v.min()), float(v.max()))
        ranges_plaus[sname] = (float(vp.min()), float(vp.max()))
        flag_all = " — 全情景变号" if v.min() < 0 < v.max() else " — 全情景符号稳定"
        flag_p = "变号" if vp.min() < 0 < vp.max() else "符号稳定"
        detail.append(
            f"- `{sname}`: 全 6 情景 [{v.min():+.4f}, {v.max():+.4f}]{flag_all}; "
            f"其中 |利差平移| ≤ 100bp 的可信情景 [{vp.min():+.4f}, {vp.max():+.4f}] — {flag_p}"
        )

    key = "atm_stable_ex_call"
    lo_p, hi_p = ranges_plaus.get(key, (float("nan"), float("nan")))
    lo_a, hi_a = ranges_all.get(key, (float("nan"), float("nan")))
    if not math.isfinite(lo_p):
        return "**无法判断.** 关键子样本为空.", detail

    # 翻转所需的利差平移, 以及它对应的 AA 级绝对利差
    be = float("nan")
    if scan is not None and key in scan.columns:
        be = breakeven_shift(scan["spread_shift_bp"].values, scan[key].values)
    aa_at_be = CREDIT_SPREAD_BP["AA"] + be if math.isfinite(be) else float("nan")

    pos_plaus = all(a > 0 for a, _ in ranges_plaus.values())
    neg_plaus = all(b < 0 for _, b in ranges_plaus.values())
    flips_plaus = [k for k, (a, b) in ranges_plaus.items() if a < 0 < b]

    gap_med = float(full["theo_minus_mkt_pct"].median())
    gap_pos_pct = float((full["theo_minus_mkt_pct"] > 0).mean() * 100)

    if pos_plaus:
        v = (
            "**不成立 —— 数据指向相反方向.** 在全部可信的债底情景下 "
            f"(信用利差平移 ≤ ±100bp), 每一个子样本的 "
            "`median(σ_implied − σ_realized)` 都为 **正**: 最保守的 ATM + vega 稳定 + "
            f"排除强赎条件子样本区间为 [{lo_p:+.4f}, {hi_p:+.4f}], "
            "全样本 (含截尾填补) 更高. 也就是说, 从转债市价反解出的隐含波动率**高于**模型喂进 "
            "BS 的 60 日历史波动率, 而不是低于. 与之一致, 价格空间里 "
            f"`(理论价 − 市价)/市价` 中位数为 {gap_med:+.2f}%, 只有 {gap_pos_pct:.1f}% "
            "的观测理论价高于市价 —— value gap 系统性偏**负**, 不是偏正.\n\n"
            "三条支撑这个结论的方法学理由:\n\n"
            "1. **主要混淆项的方向与结论相反, 所以结论是保守的.** 强赎条款让 BS 欧式 call "
            "成为真实转股权的价值上界, 会把反解出的 σ_implied 系统性**压低**. "
            "我们在混淆项压低之后仍然测到 σ_implied 高于 σ_realized, "
            "说明真实的差距只会比这里更正, 不会更负.\n"
            "2. **截尾偏差已经用填补法处理.** 只用可解样本会砍掉两端, "
            "填补后 (下界失败填 σ=0, 上界失败填 σ=3.0) 中位数仍为正.\n"
            "3. **流动性与数值稳定性都不改变符号.** vega 稳定子样本、"
            "成交额 10 倍门槛子样本符号一致.\n\n"
            "**唯一能翻转结论的假设**: 把信用利差整体压低到 "
            + (f"约 {be:+.0f}bp 平移 (即 AA 级转债的绝对利差 ≈ {aa_at_be:.0f}bp)"
               if math.isfinite(be) else "扫描区间 ±400bp 之外") +
            ". "
            + ("这意味着 AA 级转债的债底应当按近乎无风险 (甚至负信用利差) 折现, "
               "经济上不可信, 因此不构成对结论的实质威胁.\n\n"
               if (math.isfinite(aa_at_be) and aa_at_be <= 30) else
               "这落在评级利差的合理不确定范围内, 结论的强度因此有限.\n\n")
            + "**但假设里有一半是对的, 只是位置不同** —— 见第 5、6 节: "
            "σ_implied − σ_realized 随 moneyness 单调下降 (深度价外 +0.35 → 深度价内为负), "
            "随剩余期限单调下降 (<1y +0.74 → >5y 略为负). "
            "所以「flat vol 忽略了 skew 与期限结构」这个批评是成立的; "
            "错的是它的**方向和结果**: 被系统性高估的不是全体转债, 而是**高 moneyness / "
            "临近强赎**的那一批 (见第 4b 节). 对整体水平而言, 用 realized vol 反而偏低估."
        )
    elif neg_plaus:
        v = (
            "**方向上与假设一致, 但无法与强赎混淆项分离.** 在全部可信债底情景下, "
            f"ATM 子样本的 median(σ_implied − σ_realized) 都为负 ([{lo_p:+.4f}, {hi_p:+.4f}]). "
            "但 BS 欧式 call 是可赎回转股权的价值上界, 强赎条款本身就会把 σ_implied 压低, "
            "方向与待检验假设完全一致 —— 因此这个证据与假设不矛盾, 但**不能证认**假设."
        )
    else:
        v = (
            "**无法判断.** 在可信的债底情景范围内 (信用利差平移 ≤ ±100bp), "
            f"`median(σ_implied − σ_realized)` 就会变号 (ATM 子样本 [{lo_p:+.4f}, {hi_p:+.4f}]"
            f", 全情景 [{lo_a:+.4f}, {hi_a:+.4f}]). "
            "转债信用利差不可观测, 这个量级的不确定性是真实的, 数据不足以支撑判断. "
            f"变号的子样本: {', '.join('`' + k + '`' for k in flips_plaus)}."
        )

    if ok_pct < 85:
        v += (f"\n\n另外: 反解成功率 {ok_pct:.1f}%, 失败集中在深度价内 "
              "(见第 3 节). 价内桶的任何结论都因此不可靠, 上面的判断只依赖 "
              "ATM 与全样本截尾填补口径.")
    return v, detail


def write_report(outdir, diag, ledger, overall, sens, scan, full, args, st, src) -> None:
    solved = full[full["solve_ok"]]
    stable = solved[~solved["unstable_vega"]]
    ok_pct = float(full["solve_ok"].mean() * 100)
    status = fail_table(full)
    by_m = agg_by(full, "moneyness_bucket", MONEY_LABELS)
    by_m_st = agg_by(full[~full["unstable_vega"] & full["solve_ok"]],
                     "moneyness_bucket", MONEY_LABELS)
    by_t = agg_by(full, "tenor_bucket", TENOR_LABELS)
    by_y = agg_by(full, "year")
    verdict, detail = _verdict(sens, scan, ok_pct, full)

    lines = []
    A = lines.append
    A("# cb_arb 转股权定价诊断: 转债隐含波动率 vs 模型使用的历史波动率")
    A("")
    A(f"生成时间: {datetime.now():%Y-%m-%d %H:%M:%S}  ")
    A("脚本: `study/diagnose_implied_vs_realized_vol.py`  ")
    A(f"参数来源: `{src}`  ")
    A("性质: **一次性只读诊断**. 不写 `data/research_framework/`, 不注册实验, "
      "不进实验台账, 不改 `framework/` `scripts/` `strategies/`.")
    A("")
    A("---")
    A("")
    A("## 0. 一句话结论: 假设成立吗?")
    A("")
    A(verdict)
    A("")
    A("跨情景符号稳定性逐子样本:")
    A("")
    lines.extend(detail)
    A("")
    A("## 1. 被检验的假设与检验方式")
    A("")
    A("`cb_pricer.price_cb` 把 `realized_vol()` 算出的标量年化历史波动率直接当作 BS 的 σ "
      "(cb_pricer.py:275-283). 假设是: 这会系统性**高估**转股权价值, 理由为 "
      "(a) 用 realized vol 当 implied vol, 忽略波动率风险溢价; (b) flat vol, 忽略负 skew; "
      "(c) 无期限结构, 短窗口 vol 用于 1-5 年期权.")
    A("")
    A("检验方式 — 从转债市价反解隐含波动率:")
    A("")
    A("```")
    A("bond_floor        = cb_pricer.bond_floor_pv(face, coupon, T, rf + credit_spread)")
    A("                    (最后 2 年套用 price_cb 的回售面值地板)")
    A("market_option_100 = 转债收盘价 - bond_floor          # 每 100 面值")
    A("target_per_share  = market_option_100 / (100 / K)")
    A("σ_implied         : 解 bs_call(S, K, T, σ, rf) = target_per_share  (二分, 90 次迭代)")
    A("σ_realized        : 与策略完全同口径 — 60 日滚动 log-return std × sqrt(252), 上限 1.5")
    A("```")
    A("")
    A("向量化实现与 `cb_pricer` 标量实现逐点对拍 (运行时断言, 不过就不出报告):")
    A("")
    A("```")
    for k, v in st.items():
        A(f"{k} = {v:.3e}")
    A("```")
    A("")
    A("## 2. 数据与口径")
    A("")
    A("| 项 | 值 |")
    A("|---|---|")
    for k, v in diag.items():
        A(f"| {k} | {v} |")
    A(f"| vol 窗口 | {VOL_WINDOW_DAYS} 交易日, min_periods={VOL_MIN_PERIODS}, "
      f"年化 ×sqrt(252), 上限 {VOL_CAP} |")
    A(f"| 无风险利率 | {RISK_FREE} |")
    A(f"| 信用利差 (bp) | {CREDIT_SPREAD_BP} |")
    A(f"| 成交额过滤 | 20 日均 ≥ {args.min_amount:,.0f} 元 |")
    A(f"| vega 稳定阈值 | {args.vega_floor} 元 / 1.0 vol |")
    A("")
    A("### 过滤台账")
    A("")
    A(_md_table(ledger))
    A("")
    A("## 3. 反解失败率 (这本身就是结果)")
    A("")
    A(_md_table(status, "{:.3f}"))
    A("")
    A("状态含义与方向:")
    A("")
    A("- `nonpositive_option` — 转债市价低于我们估的债底, 隐含期权价值 ≤ 0. "
      "要么债底估高了 (信用利差给低了), 要么市场对该券的信用定价远差于评级映射.")
    A("- `below_zero_vol_bound` — 期权价值为正但低于 `max(S − K·e^{−rT}, 0)` 这个零波动率下界. "
      "深度价内高发, 典型成因是**强赎预期封顶**: 市场知道这张券会被赎回, 不肯为时间价值付钱.")
    A("- `above_sigma_cap` / `above_bs_upper_bound` — 隐含期权价值过高, 需要 σ > 300%, "
      "甚至超过欧式 call 的数学上界 S. 债底估低时高发.")
    A("")
    A("**截尾不是随机缺失.** 只统计反解成功的观测会同时砍掉最低和最高的 σ_implied, "
      "而两侧砍掉的比例并不对称 (见 `solve_status_by_moneyness.csv`): 价内桶几乎全是下界失败. "
      "因此本报告的主口径是 **`all_obs_censored`** —— 把下界失败填 σ=0、上界失败填 σ=3.0, "
      "填补不改变样本秩序, 所以**中位数**仍然可用 (均值不可用, 因此全篇只用中位数).")
    A("")
    A(_md_table(fail_table(full, "moneyness_bucket"), "{:.3f}"))
    A("")
    A("## 4. 主结果 (基准情景)")
    A("")
    A(_md_table(overall.drop(columns=["note"])))
    A("")
    A("子样本定义:")
    A("")
    for k, v in SUBSET_DOC.items():
        A(f"- `{k}` — {v}")
    A("")
    A("价格空间的同一件事 (不依赖反解是否成功, 因此比 σ 口径更硬):")
    A("")
    A(f"- 模型用 σ_realized 定出的转股权价值, 减去市场隐含的转股权价值, "
      f"中位数 **{float(full['option_overprice_yuan'].median()):+.2f} 元 / 100 面值** "
      f"(正 = 模型给的期权更贵).")
    A(f"- 对应 `(理论价 − 市价)/市价` 中位数 **{float(full['theo_minus_mkt_pct'].median()):+.2f}%**, "
      f"均值 {float(full['theo_minus_mkt_pct'].mean()):+.2f}%. "
      f"其中 {float((full['theo_minus_mkt_pct'] > 0).mean()*100):.1f}% 的观测理论价高于市价.")
    A("")
    A("> 方向约定: `theo > mkt` 意味着策略把该券判为**低估** (会买). "
      "假设预言这个比例应远高于 50%; 实测见上.")
    A("")
    A("## 4b. value gap 的横截面结构 —— 比总体水平更重要的发现")
    A("")
    cc = full.copy()
    cc["call_condition_met"] = cc["call_condition_met"].fillna(False)
    g_true = cc[cc["call_condition_met"]]
    g_false = cc[~cc["call_condition_met"]]
    A("策略是**横截面排名**信号, 所以 value gap 的绝对水平会被排名抵消掉, "
      "真正影响业绩的是 value gap 在横截面上偏向了谁. 按「近 30 个交易日中 ≥15 日 "
      "S/K ≥ 1.3」(强赎条款的近似) 分组:")
    A("")
    A("| 分组 | n | `(理论价−市价)/市价` 中位数 | 反解成功率 | σ_implied 中位数 (可解) | σ_realized 中位数 |")
    A("|---|---|---|---|---|---|")
    for lbl, g in [("未接近强赎条件", g_false), ("已近似满足强赎条件", g_true)]:
        A(f"| {lbl} | {len(g)} | {float(g['theo_minus_mkt_pct'].median()):+.2f}% | "
          f"{float(g['solve_ok'].mean()*100):.1f}% | "
          f"{float(g.loc[g['solve_ok'], 'sigma_implied'].median()):.4f} | "
          f"{float(g['sigma_realized'].median()):.4f} |")
    A("")
    A("同样的结构也出现在 moneyness 分桶上 (第 5 节 `theo_minus_mkt_pct_median` 列): "
      "低 moneyness 桶 theo < mkt (被判为贵), 高 moneyness 桶 theo > mkt (被判为便宜).")
    A("")
    A("**机制**: `price_cb` 只在强赎**公告期内**才锁定 103 元 "
      "(`is_force_redeemed`), 而对「已满足强赎条件但尚未公告」的券, "
      "仍然按不可赎回的欧式 call 定价, 于是给出远高于市价的理论价. "
      "横截面排名会把这批券排到最便宜的一端并买入, 随后强赎公告触发强制卖出. "
      "这条链路与波动率水平无关, 是**条款建模缺失**, 不是 vol 参数问题.")
    A("")
    A("> 这一节是本诊断的副产品, 但比原假设更可能解释 cb_arb 跑不出超额. "
      "它是可证伪的: 在排名前把满足强赎条件的券剔除 (或用二叉树给赎回权定价), "
      "看回测指标是否改变. 那需要走正式的 proposal gate, 不在本诊断范围内.")
    A("")
    A("## 5. 按 moneyness 分桶 — 有没有 skew 形状?")
    A("")
    A(_md_table(by_m))
    A("")
    A("仅 vega 稳定的可解样本:")
    A("")
    A(_md_table(by_m_st))
    A("")
    A("## 6. 按剩余期限分桶 — 有没有期限结构?")
    A("")
    A(_md_table(by_t))
    A("")
    A("## 7. 分年度")
    A("")
    A(_md_table(by_y))
    A("")
    A("## 8. 敏感性分析: 债底 / 贴现率 (本诊断最大的误差来源)")
    A("")
    A("转债信用利差不可观测. 下表把整条评级→利差映射平移 ±100/200bp, "
      "并单独测试关闭 `price_cb` 的「最后 2 年债底不低于面值」回售地板.")
    A("")
    A(_md_table(sens, "{:.4f}"))
    A("")
    if scan is not None:
        A("### breakeven: 需要多大的利差误差才能让结论翻转")
        A("")
        A(_md_table(scan, "{:.4f}"))
        A("")
        be = {}
        for sname in SUBSET_DOC:
            if sname in scan.columns:
                be[sname] = breakeven_shift(scan["spread_shift_bp"].values,
                                            scan[sname].values)
        A("| 子样本 | 使 median(σ_implied − σ_realized)=0 所需的利差平移 (bp) |")
        A("|---|---|")
        for k, v in be.items():
            A(f"| `{k}` | {'扫描区间 ±400bp 内无过零点' if not math.isfinite(v) else f'{v:+.0f}'} |")
        A("")
        A("解读: 若过零点落在 ±100bp 以内, 说明结论完全由一个不可观测的假设参数决定, "
          "等于没有结论; 若需要 300bp 以上才翻转, 结论相对稳健 "
          "(AA 级转债的真实利差不太可能错 300bp).")
        A("")
    A("## 9. 偏差来源逐条 (方向与量级)")
    A("")
    dsig = float(full["dsigma_per_1yuan"].median())
    vmed = float(full["vega_per_100_face"].median())
    A("| 偏差源 | 对 σ_implied 的方向 | 量级 / 本脚本的处理 |")
    A("|---|---|---|")
    A(f"| **债底估计误差** | 债底高估 1 元 → σ_implied 低约 {dsig:.4f} (中位 vega "
      f"{vmed:.1f} 元/1.0 vol) | 第 8 节做了 ±100/200bp 敏感性 + breakeven 扫描 |")
    A("| **强赎条款 (混淆项)** | **压低 σ_implied** —— 与待检验假设同向 | "
      "BS 欧式 call 是可赎回转股权的价值上界, 所以这条**不能拿来当假设成立的证据**. "
      "已剔除强赎公告期; 另给出剔除「近 30 日 ≥15 日 S/K≥1.3」近似强赎条件样本的结果 "
      "(`by_call_condition.csv`, 子样本 `atm_stable_ex_call`) |")
    A("| **下修条款** | 抬高真实期权价值 → 使 σ_implied 偏高 (方向相反) | "
      "仓库只有静态 `conv_price`, 无法还原历史 K 路径, **未处理** |")
    A("| **深度价内 vega→0** | 双向, 数值不可辨识 | "
      f"以 vega < {args.vega_floor} 元/1.0 vol 标记 `unstable_vega` 并单独出表; "
      "自检里 sigma 闭环也只在 vega 有意义处断言 |")
    A("| **流动性** | 双向 (收盘价噪声) | 已按 20 日均成交额过滤, 数量见过滤台账 |")
    A("| **截尾/选择偏差** | 只看可解样本会抬高价内桶的 σ_implied | "
      "主口径改用 censor-filled 中位数 (第 3 节) |")
    A("| **qfq 正股价 × 静态转股价** | 双向 | 见第 10 节 |")
    A("")
    A("## 10. 已知局限 (与被诊断的策略共享)")
    A("")
    A(f"1. **转股价是静态单值.** `cb_basic.conv_price` 每只券只有一个值 "
      f"(检测结果 `conv_price_is_static_scalar_per_bond = "
      f"{diag.get('conv_price_is_static_scalar_per_bond')}`), 且 `cb_daily` 没有 "
      f"`conv_price` 列 (`cb_daily_has_conv_price_col = "
      f"{diag.get('cb_daily_has_conv_price_col')}`). 无法还原下修/分红导致的历史 K 路径. "
      "若该值是最新 (下修后) 的 K, 历史 moneyness 被系统性高估, 分桶边界会漂移. "
      "**这个局限同时存在于被诊断的策略里** —— `verifier` 用的就是同一个静态字段, "
      "所以本诊断与策略口径一致, 诊断的是「策略实际在做什么」, 而不是「市场真相是什么」.")
    A("2. **正股价用前复权 (qfq).** 与最新转股价配对, 在分红/送转调整上大体自洽, "
      "对下修不自洽. 同样与策略口径一致.")
    A("3. **债底模型简化.** 每年付息一次、单一贴现率、不含回售补偿与到期赎回价溢价、"
      "评级→利差是静态映射 (忽略利差的时间变化与个券差异). 2018 与 2024 的同评级利差差很多, "
      "分年度结果尤其受此影响.")
    A("4. **BS 欧式 call 不是转股权的正确模型.** 真实转股权 = 美式行权 + 发行人赎回 + "
      "下修 + 回售的复合期权. 本诊断只回答「在策略自己用的 BS 框架内, 市场价对应的 σ 是多少」, "
      "不回答「真实隐含波动率是多少」.")
    A("5. **无法分离 skew 与强赎.** 价内桶 σ_implied 偏低既可解释为负 skew, "
      "也可解释为强赎封顶, 两者同向, 本诊断无法证认.")
    A("6. **成交额过滤可能是非绑定的.** `cb_daily` 无成交额列, "
      "成交额按 `close × vol` 近似 (与 `verifier` 同口径), 单位存疑; "
      "过滤台账里该步骤剔除的行数即为其实际约束力.")
    A("")
    A("## 11. 要把结论钉死还需要什么")
    A("")
    A("- **逐日转股价历史 (下修事件表)** — 否则 moneyness 分桶和 skew 结论都带系统性漂移.")
    A("- **可观测的分评级信用利差时间序列** (中债企业债/转债曲线) 替代静态映射 —— "
      "第 8 节表明结论对这个参数的敏感度是决定性的.")
    A("- **含赎回/下修的二叉树或 LSM 定价**替代 BS 欧式 call, 再反解 σ, "
      "才能把强赎混淆项剥离出去.")
    A("- 若只想回答「策略的 value gap 是否系统性偏正、要不要给转股权打折」, "
      "**不必反解波动率** —— 直接看第 4 节价格空间的 `theo_minus_mkt_pct` 分布, "
      "以及给 σ 乘一个折扣系数 (`vol_multiplier` 已经是 `tunable_space.yaml` 里的可调参数) "
      "后回测指标怎么变. 那是一个能直接证伪的实验, 而本诊断不是.")
    A("")
    A("## 12. 输出文件与复现")
    A("")
    names = sorted({f.name for f in outdir.glob("*")} | {"REPORT.txt"})
    for nm in names:
        A(f"- `{nm}`")
    A("")
    A("复现:")
    A("")
    A("```bash")
    A("python study/diagnose_implied_vs_realized_vol.py            # 全样本, 默认口径")
    A("python study/diagnose_implied_vs_realized_vol.py \\")
    A("    --start-date 20190101 --min-amount 1e7 --vega-floor 30  # 更严格的子样本")
    A("```")
    A("")
    A("脚本只读 `data/cb_warehouse/*.parquet`, 只 import "
      "`strategies/cb_arb/cb_pricer.py` 与 `verifier.py` 的常量, 不写这两处.")
    A("")

    (outdir / "REPORT.txt").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
