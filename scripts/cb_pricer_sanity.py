#!/usr/bin/env python3
"""可转债定价引擎 sanity check (3 个验证, 不写策略).

依赖 data/cb_warehouse 已经构建好:
    cb_basic.parquet, cb_daily.parquet, cb_call.parquet,
    stk_daily.parquet, stk_daily_qfq.parquet

输出:
    打印 3 个验证结果
    reports/cb_pricer_sanity_check_3.png 全市场散点图

验证 #1: 历史强赎事件前 30 天收敛
验证 #2: 单点对照
验证 #3: 某天全市场散点图

跑法:
    .venv/bin/python scripts/cb_pricer_sanity.py
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import matplotlib

matplotlib.use("Agg")  # 无 GUI

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from strategies.cb_arb.cb_pricer import (  # noqa: E402
    CBSpec,
    CBValuation,
    price_cb,
    realized_vol,
)

WAREHOUSE = ROOT / "data" / "cb_warehouse"
REPORTS = ROOT / "reports"
REPORTS.mkdir(exist_ok=True)


# =============================================================================
# 数据加载
# =============================================================================

def load_warehouse() -> dict:
    """读 cb_warehouse 几个 parquet."""
    paths = {
        "basic": WAREHOUSE / "cb_basic.parquet",
        "daily": WAREHOUSE / "cb_daily.parquet",
        "call": WAREHOUSE / "cb_call.parquet",
        "stk_qfq": WAREHOUSE / "stk_daily_qfq.parquet",
        "stk_raw": WAREHOUSE / "stk_daily.parquet",
    }
    out = {}
    for name, path in paths.items():
        if path.exists():
            out[name] = pd.read_parquet(path)
            print(f"  loaded {name:8s}: {len(out[name]):>9,} rows")
        else:
            print(f"  MISSING {name:8s}: {path}")
            out[name] = pd.DataFrame()
    return out


def make_spec_from_basic(row: pd.Series) -> CBSpec:
    """cb_basic 一行 -> CBSpec."""
    return CBSpec(
        ts_code=str(row["ts_code"]),
        face_value=float(row.get("par_value") or 100.0),
        conv_price=float(row["conv_price"]) if pd.notna(row.get("conv_price")) else 100.0,
        list_date=str(row.get("list_date") or "20180101"),
        maturity_date=str(row.get("maturity_date") or "20300101"),
        coupon_rate=float(row.get("coupon_rate") or 0.01),
        rating=str(row.get("rating") or "AA"),
    )


def get_stock_close(stk_qfq: pd.DataFrame, stk_code: str, date: str) -> Optional[float]:
    """从 qfq 日线拿某天收盘价."""
    if stk_qfq.empty:
        return None
    code = stk_code.zfill(6) if isinstance(stk_code, str) else None
    if not code:
        return None
    sub = stk_qfq[(stk_qfq["stk_code"] == code) & (stk_qfq["trade_date"] == date)]
    if sub.empty:
        return None
    return float(sub["close"].iloc[0])


def get_stock_vol(stk_qfq: pd.DataFrame, stk_code: str, date: str, window: int = 60) -> Optional[float]:
    """正股最近 window 天的 HV."""
    if stk_qfq.empty:
        return None
    code = stk_code.zfill(6) if isinstance(stk_code, str) else None
    if not code:
        return None
    sub = stk_qfq[(stk_qfq["stk_code"] == code) & (stk_qfq["trade_date"] <= date)]
    if len(sub) < 5:
        return None
    sub = sub.sort_values("trade_date").tail(window)
    closes = sub["close"].values
    return realized_vol(closes)


def get_cb_close(cb_daily: pd.DataFrame, ts_code: str, date: str) -> Optional[float]:
    """从 cb_daily 拿某天收盘价."""
    if cb_daily.empty:
        return None
    sub = cb_daily[(cb_daily["ts_code"] == ts_code) & (cb_daily["trade_date"] == date)]
    if sub.empty:
        return None
    return float(sub["close"].iloc[0])


# =============================================================================
# 验证 #1: 强赎前 30 天收敛
# =============================================================================

def verify_redemption_convergence(data: dict, n_events: int = 30) -> dict:
    """取过去 5 年 ≤ 30 个强赎事件, 看 30 天前价格收敛."""
    cb_basic = data["basic"]
    cb_daily = data["daily"]
    cb_call = data["call"]
    stk_qfq = data["stk_qfq"]

    if cb_call.empty or cb_basic.empty or cb_daily.empty:
        return {"ok": False, "reason": "missing data"}

    # 过去 5 年的强赎事件
    cutoff_5y = (datetime.now() - timedelta(days=5 * 365)).strftime("%Y%m%d")
    events = cb_call.dropna(subset=["ann_date"]).copy()
    events = events[events["ann_date"] >= cutoff_5y]
    events = events.sort_values("ann_date", ascending=False)

    print(f"  候选强赎事件 (过去 5 年, 有 ann_date): {len(events)}")

    # 抽前 n_events 个 (按 ann_date 降序)
    sample = events.head(n_events).reset_index(drop=True)
    if len(sample) == 0:
        return {"ok": False, "reason": "no events"}

    print(f"  抽样事件数: {len(sample)}")

    # 检查: 公告前 30 天内, 市场和理论应该 track each other (差距 < 15 元)
    # 注: 强赎触发条件是正股 30 天里 15 天 ≥ 130% 转股价. 此时:
    #   - 市场价上涨到 ~130+ 元 (intrinsic 主导)
    #   - 理论价 (BS) 也应跟随上涨, 因为 itm 期权 + 高 vol
    # 通过条件: |市场-理论| 中位数 < 15 元 OR 相对差异 < 15%
    n_close_track = 0  # 市场和理论 track 的事件数 (中位 |diff| < 15 元)
    deviations = []  # 每个事件 30 天里 |market - theo| 中位数
    rel_dev_pcts = []  # 相对偏差 % 中位数

    for _, ev in sample.iterrows():
        ts_code = ev["ts_code"]
        ann_date = ev["ann_date"]

        spec_row = cb_basic[cb_basic["ts_code"] == ts_code]
        if spec_row.empty:
            continue
        spec = make_spec_from_basic(spec_row.iloc[0])
        stk_code = str(spec_row.iloc[0].get("stk_code") or "").strip()

        ann_dt = pd.to_datetime(ann_date)
        d_start = (ann_dt - timedelta(days=45)).strftime("%Y%m%d")
        d_end = ann_date

        cb_window = cb_daily[
            (cb_daily["ts_code"] == ts_code)
            & (cb_daily["trade_date"] >= d_start)
            & (cb_daily["trade_date"] <= d_end)
        ].sort_values("trade_date").tail(30)

        if len(cb_window) < 5:
            continue

        abs_dev = []
        rel_dev = []

        for _, day in cb_window.iterrows():
            d = day["trade_date"]
            mkt = float(day["close"])

            sp = get_stock_close(stk_qfq, stk_code, d)
            sv = get_stock_vol(stk_qfq, stk_code, d, window=60)
            if sp is None or sv is None or math.isnan(sv) or sv <= 0:
                continue

            v = price_cb(
                spec=spec,
                valuation_date=d,
                stock_price=sp,
                stock_vol=sv,
                is_force_redeemed=False,
            )
            if v.method == "invalid" or not math.isfinite(v.theoretical) or v.theoretical <= 0:
                continue

            abs_dev.append(abs(mkt - v.theoretical))
            rel_dev.append(abs(mkt - v.theoretical) / v.theoretical * 100)

        if len(abs_dev) < 3:
            continue

        ev_median_abs = float(np.median(abs_dev))
        ev_median_rel = float(np.median(rel_dev))
        deviations.append(ev_median_abs)
        rel_dev_pcts.append(ev_median_rel)
        if ev_median_abs < 15.0 or ev_median_rel < 15.0:
            n_close_track += 1

    n_used = len(deviations)
    pct_close = n_close_track / n_used if n_used else 0.0
    median_dev = float(np.nanmedian(deviations)) if deviations else float("nan")
    median_rel = float(np.nanmedian(rel_dev_pcts)) if rel_dev_pcts else float("nan")

    print(f"  使用事件: {n_used}, 跟踪良好(|市场-理论| 中位 < 15 元 或 相对 < 15%) "
          f"{n_close_track} ({pct_close*100:.0f}%)")
    print(f"  30 天 |市场-理论| 偏差中位数: {median_dev:.2f} 元 (相对 {median_rel:.1f}%)")

    pass_thresh = 0.50
    return {
        "n_events": n_used,
        "converged_count": n_close_track,
        "pct_converged": pct_close,
        "median_dev": median_dev,
        "median_rel_pct": median_rel,
        "passed": (pct_close >= pass_thresh and median_dev < 25.0),
    }


# =============================================================================
# 验证 #2: 单点对照
# =============================================================================

def verify_single_point(data: dict) -> dict:
    """选一个 AAA 评级 CB 的某天对照."""
    cb_basic = data["basic"]
    cb_daily = data["daily"]
    stk_qfq = data["stk_qfq"]

    # 优先选 AAA / AA+, 次选 AA, 已退市的优先 (历史完整)
    candidates = cb_basic[cb_basic["rating"].isin(["AAA", "AA+", "AA"])].copy()
    candidates = candidates[candidates["conv_price"].notna()]
    candidates = candidates[candidates["maturity_date"].notna()]
    candidates = candidates[candidates["list_date"].notna()]
    if candidates.empty:
        return {"ok": False, "reason": "no AAA/AA+ candidate"}

    chosen = None
    test_date = "20230615"

    for _, row in candidates.iterrows():
        spec = make_spec_from_basic(row)
        # 必须距到期 > 30 天 且 list_date < test_date < maturity
        if not (spec.list_date <= test_date <= spec.maturity_date):
            continue
        # cb_daily 该天必须有
        cb_close = get_cb_close(cb_daily, spec.ts_code, test_date)
        if cb_close is None:
            continue
        # 正股该天必须有
        stk_close = get_stock_close(stk_qfq, str(row.get("stk_code") or ""), test_date)
        if stk_close is None:
            continue
        # vol 必须能算
        sv = get_stock_vol(stk_qfq, str(row.get("stk_code") or ""), test_date, window=60)
        if sv is None or math.isnan(sv):
            continue
        chosen = {
            "spec": spec,
            "row": row,
            "cb_close": cb_close,
            "stk_close": stk_close,
            "vol": sv,
            "test_date": test_date,
        }
        break

    if chosen is None:
        return {"ok": False, "reason": "no candidate with all data on " + test_date}

    sp = chosen["spec"]
    v = price_cb(
        spec=sp,
        valuation_date=chosen["test_date"],
        stock_price=chosen["stk_close"],
        stock_vol=chosen["vol"],
    )

    market = chosen["cb_close"]
    theo = v.theoretical
    dev_pct = (market - theo) / theo * 100 if theo > 0 else float("nan")

    print(f"  CB: {sp.ts_code} ({chosen['row'].get('bond_short_name')}) 评级 {sp.rating}")
    print(f"  日期: {chosen['test_date']}")
    print(f"  正股: {chosen['stk_close']:.2f} | HV60 = {chosen['vol']*100:.1f}%")
    print(f"  转股价: {sp.conv_price:.2f} | 票面: {sp.coupon_rate*100:.2f}% | 到期: {sp.maturity_date}")
    print(f"  实际收盘: {market:.2f} 元 | 理论: {theo:.2f} 元 | 偏差: {dev_pct:+.2f}%")
    print(f"  方法: {v.method} | 债底: {v.bond_floor:.2f} | 期权: {v.option_value:.2f} | 内在: {v.intrinsic:.2f}")

    abs_pct = abs(dev_pct) if math.isfinite(dev_pct) else 1e9
    if abs_pct < 5:
        verdict = "<5% 大致对得上"
        passed = True
    elif abs_pct < 15:
        verdict = "5-15% 可解释市场情绪"
        passed = True
    else:
        verdict = ">15% 引擎可能有 bug"
        passed = False

    return {
        "ts_code": sp.ts_code,
        "test_date": chosen["test_date"],
        "market": market,
        "theoretical": theo,
        "dev_pct": dev_pct,
        "verdict": verdict,
        "passed": passed,
    }


# =============================================================================
# 验证 #3: 全市场散点图
# =============================================================================

def verify_cross_section(data: dict, target_date: Optional[str] = None) -> dict:
    """选一天全市场活跃 CB, 算理论 vs 市场散点图."""
    cb_basic = data["basic"]
    cb_daily = data["daily"]
    cb_call = data["call"]
    stk_qfq = data["stk_qfq"]

    if cb_basic.empty or cb_daily.empty or stk_qfq.empty:
        return {"ok": False, "reason": "missing data"}

    if target_date is None:
        # 选一个有大量样本 + 市场状态相对正常的历史日 (2024-12-31).
        # 避免最新交易日, 因为最新可能受短期热点扰动.
        # 如果该日没数据, 退回到 cb_daily 最新一天.
        candidate = "20241231"
        if not (cb_daily["trade_date"] == candidate).any():
            candidate = str(cb_daily["trade_date"].max())
        target_date = candidate

    print(f"  目标日: {target_date}")

    # 该日有交易的 CB
    today = cb_daily[cb_daily["trade_date"] == target_date]
    print(f"  当日活跃 CB: {len(today)}")

    # 已经强赎的 CB (公告日 <= target_date) 排除
    redeemed_codes = set()
    if not cb_call.empty:
        rd = cb_call.dropna(subset=["ann_date"])
        rd = rd[rd["ann_date"] <= target_date]
        redeemed_codes = set(rd["ts_code"].unique())

    # 计算理论价
    pairs = []
    n_skip_no_basic = 0
    n_skip_no_stk = 0
    n_skip_invalid = 0
    n_skip_redeemed = 0
    n_skip_near_maturity = 0

    for _, row in today.iterrows():
        ts_code = row["ts_code"]
        market = float(row["close"])

        # 强赎已公告: 跳过 (理论被锁不参考)
        if ts_code in redeemed_codes:
            n_skip_redeemed += 1
            continue

        spec_row = cb_basic[cb_basic["ts_code"] == ts_code]
        if spec_row.empty:
            n_skip_no_basic += 1
            continue
        spec = make_spec_from_basic(spec_row.iloc[0])

        # 距到期 > 30 天
        try:
            mat_dt = datetime.strptime(spec.maturity_date, "%Y%m%d")
            tgt_dt = datetime.strptime(target_date, "%Y%m%d")
            days_to_mat = (mat_dt - tgt_dt).days
        except Exception:
            n_skip_invalid += 1
            continue

        if days_to_mat <= 30:
            n_skip_near_maturity += 1
            continue

        stk_code = str(spec_row.iloc[0].get("stk_code") or "").strip()
        sp = get_stock_close(stk_qfq, stk_code, target_date)
        if sp is None:
            n_skip_no_stk += 1
            continue
        sv = get_stock_vol(stk_qfq, stk_code, target_date, window=60)
        if sv is None or math.isnan(sv) or sv <= 0:
            n_skip_no_stk += 1
            continue

        v = price_cb(
            spec=spec,
            valuation_date=target_date,
            stock_price=sp,
            stock_vol=sv,
        )
        if v.method == "invalid" or not math.isfinite(v.theoretical):
            n_skip_invalid += 1
            continue

        pairs.append((v.theoretical, market, ts_code))

    n = len(pairs)
    print(f"  样本: {n}, 跳过 (no_basic={n_skip_no_basic}, no_stk={n_skip_no_stk}, "
          f"invalid={n_skip_invalid}, redeemed={n_skip_redeemed}, near_maturity={n_skip_near_maturity})")

    if n < 20:
        return {"ok": False, "reason": f"too few samples ({n})"}

    theos = np.array([p[0] for p in pairs])
    mkts = np.array([p[1] for p in pairs])

    # 计算 r² 跟 45 度线 (y=x)
    ss_tot = np.sum((mkts - mkts.mean()) ** 2)
    ss_res = np.sum((mkts - theos) ** 2)
    r2_45 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")

    # Pearson r²
    pearson_r = np.corrcoef(theos, mkts)[0, 1] if n > 1 else float("nan")
    r2_pearson = pearson_r ** 2

    diff = mkts - theos
    median_diff = float(np.median(diff))
    std_diff = float(np.std(diff))

    # 加 winsorized r² (剔除两端 5% 极端值后) 减少 散户高溢价 outlier 干扰
    n_outlier = max(1, int(0.05 * n))
    sorted_diffs = np.argsort(diff)
    keep_idx = sorted_diffs[n_outlier:-n_outlier] if n > 2 * n_outlier else np.arange(n)
    if len(keep_idx) > 5:
        theos_w = theos[keep_idx]
        mkts_w = mkts[keep_idx]
        pearson_r_w = np.corrcoef(theos_w, mkts_w)[0, 1]
        r2_pearson_winsor = pearson_r_w ** 2
    else:
        r2_pearson_winsor = r2_pearson

    print(f"  r² (vs 45 度线): {r2_45:.3f}")
    print(f"  Pearson r²: {r2_pearson:.3f}  (winsorized 5% trim: {r2_pearson_winsor:.3f})")
    print(f"  偏差中位数 (市场-理论): {median_diff:+.2f} 元 (std={std_diff:.2f})")

    # 画散点图
    fig, ax = plt.subplots(figsize=(8, 8))
    ax.scatter(theos, mkts, alpha=0.4, s=15, edgecolors="none", color="steelblue")
    lo = min(theos.min(), mkts.min()) - 5
    hi = max(theos.max(), mkts.max()) + 5
    ax.plot([lo, hi], [lo, hi], "r--", lw=1, label="y = x (45 deg)")
    ax.set_xlabel("Theoretical price (yuan)")
    ax.set_ylabel("Market close price (yuan)")
    ax.set_title(
        f"CB pricer sanity check #3: {target_date}\n"
        f"n={n}  Pearson r={pearson_r:.3f}  r2(45deg)={r2_45:.3f}  "
        f"median_diff={median_diff:+.2f}"
    )
    ax.legend(loc="upper left")
    ax.grid(True, alpha=0.3)
    out_path = REPORTS / "cb_pricer_sanity_check_3.png"
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    print(f"  图保存: {out_path}")

    # 判断: 用 winsorized r² > 0.7 (避开散户极端高溢价干扰)
    return {
        "target_date": target_date,
        "n": n,
        "r2_45deg": float(r2_45),
        "r2_pearson": float(r2_pearson),
        "r2_pearson_winsor": float(r2_pearson_winsor),
        "median_diff": median_diff,
        "std_diff": std_diff,
        "image_path": str(out_path),
        "passed": (r2_pearson_winsor > 0.7),
    }


# =============================================================================
# 主流程
# =============================================================================

def main():
    print("=" * 70)
    print("CB 定价引擎 sanity check (3 个验证)")
    print("=" * 70)

    print("\n[setup] 加载 cb_warehouse...")
    data = load_warehouse()
    if data["basic"].empty:
        print("FATAL: cb_basic 为空, 无法继续")
        sys.exit(1)

    # ---- 验证 1 ----
    print("\n=== 验证 #1: 强赎前 30 天收敛 ===")
    r1 = verify_redemption_convergence(data, n_events=30)

    # ---- 验证 2 ----
    print("\n=== 验证 #2: 单点对照 ===")
    r2 = verify_single_point(data)

    # ---- 验证 3 ----
    print("\n=== 验证 #3: 全市场散点 ===")
    r3 = verify_cross_section(data)

    # ---- 总结 ----
    print("\n" + "=" * 70)
    print("总结")
    print("=" * 70)

    summary_lines = []
    n_pass = 0

    if r1.get("passed"):
        n_pass += 1
        summary_lines.append(
            f"#1 强赎前 30 天市场-理论跟踪: PASS  "
            f"({r1['converged_count']}/{r1['n_events']} 跟踪良好, "
            f"|市场-理论| 中位 {r1['median_dev']:.2f} 元 / 相对 {r1.get('median_rel_pct', float('nan')):.1f}%)"
        )
    else:
        reason = r1.get("reason", "")
        if reason:
            summary_lines.append(f"#1 强赎前 30 天跟踪: FAIL ({reason})")
        else:
            summary_lines.append(
                f"#1 强赎前 30 天跟踪: FAIL "
                f"({r1.get('converged_count','?')}/{r1.get('n_events','?')} 跟踪良好, "
                f"|市场-理论| 中位 {r1.get('median_dev', float('nan')):.2f} 元 / "
                f"相对 {r1.get('median_rel_pct', float('nan')):.1f}%)"
            )

    if r2.get("passed"):
        n_pass += 1
        summary_lines.append(
            f"#2 单点对照 ({r2['ts_code']} @ {r2['test_date']}): PASS  "
            f"实际 {r2['market']:.2f} 元 vs 理论 {r2['theoretical']:.2f} 元 (偏差 {r2['dev_pct']:+.2f}%) — {r2['verdict']}"
        )
    else:
        reason = r2.get("reason", "")
        if reason:
            summary_lines.append(f"#2 单点对照: FAIL ({reason})")
        else:
            summary_lines.append(
                f"#2 单点对照 ({r2.get('ts_code','?')} @ {r2.get('test_date','?')}): FAIL  "
                f"偏差 {r2.get('dev_pct', float('nan')):+.2f}% — {r2.get('verdict','')}"
            )

    if r3.get("passed"):
        n_pass += 1
        summary_lines.append(
            f"#3 全市场散点 ({r3['target_date']}): PASS  "
            f"n={r3['n']}, Pearson r²={r3['r2_pearson']:.3f} (winsor {r3['r2_pearson_winsor']:.3f}), "
            f"偏差中位数 {r3['median_diff']:+.2f} 元 -> {r3['image_path']}"
        )
    else:
        reason = r3.get("reason", "")
        if reason:
            summary_lines.append(f"#3 全市场散点: FAIL ({reason})")
        else:
            summary_lines.append(
                f"#3 全市场散点 ({r3.get('target_date','?')}): FAIL  "
                f"n={r3.get('n','?')}, Pearson r²={r3.get('r2_pearson', float('nan')):.3f} "
                f"(winsor {r3.get('r2_pearson_winsor', float('nan')):.3f})"
            )

    for line in summary_lines:
        print("  " + line)

    print("\n" + "-" * 70)
    if n_pass == 3:
        print(f"  总评: 3/3 通过  ->  引擎可信, 进入策略层")
    elif n_pass >= 2:
        print(f"  总评: {n_pass}/3 通过  ->  引擎大致可信, 看具体哪条 fail 决定下一步")
    else:
        print(f"  总评: {n_pass}/3 通过  ->  引擎有毛病, 不进策略, 看具体哪里出问题")
    print("=" * 70)


if __name__ == "__main__":
    main()
