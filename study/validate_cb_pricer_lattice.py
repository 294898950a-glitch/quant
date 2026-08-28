#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证脚本: price_cb_lattice 是否达到 spec 第 7 节的验收标准.

对照 `docs/2026-08-28-cb-embedded-call-lattice-pricer-spec.txt` 第 7 节:

1. 远离触发条件(S/K < 1.1 且距到期 > 1 年)时, price_cb_lattice 应大致收敛
   到 price_cb —— 报告实际差值分布, 不只判断通过/不通过。
2. 已满足强赎条件(用 call_condition.is_call_eligible 判定, 不用
   cb_call.parquet)的观测, price_cb_lattice 应明显低于 price_cb, 且向市场价
   方向移动 —— 报告缩小了多少 (理论价-市价)/市价 这个百分比差距, 不要求
   消灭全部落差。

只读数据, 不写 data/research_framework/, 不改 framework/ scripts/ strategies/。
输出落在 study/lattice_pricer_validation/。
"""

from __future__ import annotations

import math
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from strategies.cb_arb.call_condition import is_call_eligible  # noqa: E402
from strategies.cb_arb.cb_pricer import CBSpec, price_cb  # noqa: E402
from strategies.cb_arb.cb_pricer_lattice import price_cb_lattice  # noqa: E402

WAREHOUSE = REPO_ROOT / "data" / "cb_warehouse"
OUT = REPO_ROOT / "study" / "lattice_pricer_validation"
OUT.mkdir(parents=True, exist_ok=True)

VOL_WINDOW_DAYS = 60
VOL_MIN_PERIODS = 15
VOL_CAP = 1.5
FACE_VALUE = 100.0
DEFAULT_SPREAD_BP = 150.0
RATING_TO_INT = {"A": 0, "A+": 1, "AA-": 2, "AA": 3, "AA+": 4, "AAA": 5}

N_PATHS = 4000
SAMPLE_PER_GROUP = 150
HISTORY_LEN = 29  # window - 1


def load_panel() -> pd.DataFrame:
    cb_basic = pd.read_parquet(WAREHOUSE / "cb_basic.parquet")
    cb_daily = pd.read_parquet(WAREHOUSE / "cb_daily.parquet")
    stk = pd.read_parquet(WAREHOUSE / "stk_daily_qfq.parquet")[
        ["stk_code", "trade_date", "close"]
    ].copy()
    cb_call = pd.read_parquet(WAREHOUSE / "cb_call.parquet")

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

    cb_daily = cb_daily.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    cb_daily["amount_yuan"] = cb_daily["close"].astype(float) * cb_daily["vol"].astype(float)
    cb_daily["amount_20d"] = (
        cb_daily.groupby("ts_code")["amount_yuan"]
        .rolling(window=20, min_periods=1).mean()
        .reset_index(level=0, drop=True)
    )

    basic_cols = [
        "ts_code", "stk_code", "conv_price", "list_date", "maturity_date",
        "coupon_rate", "rating",
    ]
    b = cb_basic[basic_cols].drop_duplicates("ts_code").copy()
    b["rating_int"] = b["rating"].map(
        lambda r: RATING_TO_INT.get(r, 0) if isinstance(r, str) else 0
    ).astype(int)

    df = cb_daily.merge(b, on="ts_code", how="inner")
    df = df.merge(
        stk, on=["stk_code", "trade_date"], how="left", suffixes=("", "_stk")
    ).rename(columns={"close_stk": "stk_close", "vol_ann": "sigma_realized_raw"})

    d_mat = pd.to_datetime(df["maturity_date"], format="%Y%m%d", errors="coerce")
    d_now = pd.to_datetime(df["trade_date"], format="%Y%m%d", errors="coerce")
    df["days_to_maturity"] = (d_mat - d_now).dt.days
    df["T_years"] = df["days_to_maturity"] / 365.25
    df["conv_price"] = pd.to_numeric(df["conv_price"], errors="coerce")
    df["coupon_rate"] = pd.to_numeric(df["coupon_rate"], errors="coerce").fillna(0.01)
    df["moneyness"] = df["stk_close"] / df["conv_price"]
    df["sigma_realized"] = np.minimum(
        VOL_CAP, df["sigma_realized_raw"].astype(float)
    )
    df["amount_20d"] = pd.to_numeric(df["amount_20d"], errors="coerce")

    # 强赎公告区间: 排除掉(两边模型在这个区间都走同一个锁顶 corner case, 无
    # 比较意义), 用法跟 diagnose_implied_vs_realized_vol.py 一致
    df["in_call_window"] = False
    cc = cb_call[["ts_code", "ann_date", "call_date", "expire_date"]].copy()
    idx_by_code = df.groupby("ts_code").indices
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

    return df


def build_moneyness_history(df: pd.DataFrame) -> dict[str, pd.Series]:
    """每只券一条按 trade_date 排好序、index 为 trade_date 的 moneyness 序列."""
    out = {}
    for ts_code, g in df.groupby("ts_code"):
        g = g.sort_values("trade_date")
        out[ts_code] = pd.Series(g["moneyness"].values, index=g["trade_date"].values)
    return out


def trailing_history_for(hist_map: dict, ts_code: str, trade_date: str) -> np.ndarray:
    s = hist_map[ts_code]
    pos = s.index.searchsorted(trade_date, side="right")
    window = s.iloc[max(0, pos - HISTORY_LEN) : pos]
    return window.values.astype(float)


def price_both(row: pd.Series, history: np.ndarray, seed: int):
    spec = CBSpec(
        ts_code=row["ts_code"],
        face_value=FACE_VALUE,
        conv_price=float(row["conv_price"]),
        list_date=str(row["list_date"]),
        maturity_date=str(row["maturity_date"]),
        coupon_rate=float(row["coupon_rate"]),
        rating=row["rating"] if isinstance(row["rating"], str) else "AA",
    )
    v_old = price_cb(
        spec=spec,
        valuation_date=str(row["trade_date"]),
        stock_price=float(row["stk_close"]),
        stock_vol=float(row["sigma_realized"]),
        risk_free_rate=0.025,
    )
    v_new = price_cb_lattice(
        spec=spec,
        valuation_date=str(row["trade_date"]),
        stock_price=float(row["stk_close"]),
        stock_vol=float(row["sigma_realized"]),
        moneyness_history=history,
        risk_free_rate=0.025,
        n_paths=N_PATHS,
        seed=seed,
    )
    return v_old, v_new


def main() -> int:
    t0 = time.time()
    print("loading panel...")
    df = load_panel()

    valid = (
        df["stk_close"].notna()
        & df["moneyness"].notna()
        & df["sigma_realized"].notna()
        & (~df["in_call_window"])
        & (df["days_to_maturity"] >= 30)
        & (df["rating_int"] >= 2)  # AA- 及以上, 跟 verifier 默认口径一致
        & (df["amount_20d"] >= 1_000_000)
    )
    df = df[valid].reset_index(drop=True)
    print(f"filtered panel rows: {len(df)}")

    hist_map = build_moneyness_history(df)

    # ---- 组 A: 远离触发条件 ----
    group_a = df[(df["moneyness"] < 1.1) & (df["T_years"] > 1.0)]
    # ---- 组 B: 已满足强赎条件(用 call_condition, 不用 cb_call.parquet) ----
    eligible_col = np.zeros(len(df), dtype=bool)
    for ts_code, g in df.groupby("ts_code"):
        idx = g.index.values
        elig = is_call_eligible(g["moneyness"].values)
        eligible_col[idx] = elig
    df["call_eligible"] = eligible_col
    group_b = df[df["call_eligible"]]

    print(f"group A (far from trigger) candidates: {len(group_a)}")
    print(f"group B (call-eligible) candidates: {len(group_b)}")

    rng = np.random.default_rng(2026)

    def sample(group: pd.DataFrame, n: int) -> pd.DataFrame:
        if len(group) <= n:
            return group
        idx = rng.choice(group.index.values, size=n, replace=False)
        return group.loc[idx]

    sample_a = sample(group_a, SAMPLE_PER_GROUP)
    sample_b = sample(group_b, SAMPLE_PER_GROUP)

    def run_group(sample_df: pd.DataFrame, label: str) -> pd.DataFrame:
        rows = []
        for i, (_, row) in enumerate(sample_df.iterrows()):
            hist = trailing_history_for(hist_map, row["ts_code"], row["trade_date"])
            try:
                v_old, v_new = price_both(row, hist, seed=1000 + i)
            except Exception as exc:  # noqa: BLE001
                print(f"  [{label}] skip {row['ts_code']} {row['trade_date']}: {exc}")
                continue
            if not (math.isfinite(v_old.theoretical) and math.isfinite(v_new.theoretical)):
                continue
            rows.append({
                "ts_code": row["ts_code"],
                "trade_date": row["trade_date"],
                "moneyness": row["moneyness"],
                "T_years": row["T_years"],
                "mkt_price": row["close"],
                "theo_old": v_old.theoretical,
                "theo_new": v_new.theoretical,
                "gap_old_pct": (v_old.theoretical - row["close"]) / row["close"],
                "gap_new_pct": (v_new.theoretical - row["close"]) / row["close"],
                "notes_new": v_new.notes,
            })
            if (i + 1) % 25 == 0:
                print(f"  [{label}] {i+1}/{len(sample_df)} done, {time.time()-t0:.0f}s elapsed")
        return pd.DataFrame(rows)

    print("pricing group A (far from trigger)...")
    res_a = run_group(sample_a, "A")
    print("pricing group B (call-eligible)...")
    res_b = run_group(sample_b, "B")

    res_a.to_csv(OUT / "group_a_far_from_trigger.csv", index=False)
    res_b.to_csv(OUT / "group_b_call_eligible.csv", index=False)

    lines = []
    lines.append("# price_cb_lattice 验收结果\n")
    lines.append(f"生成时间: {datetime.now().isoformat(timespec='seconds')}\n")
    lines.append(f"n_paths={N_PATHS}, 每组抽样上限={SAMPLE_PER_GROUP}\n\n")

    lines.append("## 组 A: 远离触发条件 (S/K<1.1, T>1年) —— 应大致收敛\n")
    if len(res_a):
        diff_pct = (res_a["theo_new"] - res_a["theo_old"]).abs() / res_a["theo_old"]
        lines.append(f"n={len(res_a)}\n")
        lines.append(f"diff_pct 分布: median={diff_pct.median():.4%} "
                      f"p75={diff_pct.quantile(0.75):.4%} p95={diff_pct.quantile(0.95):.4%} "
                      f"max={diff_pct.max():.4%}\n")
    else:
        lines.append("无有效样本\n")

    lines.append("\n## 组 B: 已满足强赎条件 —— 应明显更低, 向市价靠拢\n")
    if len(res_b):
        lines.append(f"n={len(res_b)}\n")
        lines.append(f"gap_old_pct (理论价-市价)/市价 中位数: {res_b['gap_old_pct'].median():.4%}\n")
        lines.append(f"gap_new_pct (理论价-市价)/市价 中位数: {res_b['gap_new_pct'].median():.4%}\n")
        closed = res_b['gap_old_pct'].median() - res_b['gap_new_pct'].median()
        lines.append(f"缩小了: {closed:.4%} (相对诊断报告里全样本落差 22pp 的参照)\n")
        theo_diff_pct = (res_b["theo_old"] - res_b["theo_new"]) / res_b["theo_old"]
        lines.append(f"theo_new 相对 theo_old 的降幅中位数: {theo_diff_pct.median():.4%}\n")
    else:
        lines.append("无有效样本(候选池为空或全部定价失败)\n")

    lines.append(f"\n总耗时: {time.time()-t0:.0f}s\n")

    report = "".join(lines)
    (OUT / "REPORT.txt").write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
