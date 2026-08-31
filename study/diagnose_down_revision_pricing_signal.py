#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断脚本: 下修事件前后市场价格反应 + 触发条件的假阳性率。

背景
----
探讨要不要把"下修"(向下修正转股价)也做成格子定价器的内生变量, 就像 v2
(`docs/2026-08-28-cb-embedded-call-lattice-pricer-spec.txt`)对"强赎"做的
那样。跟 codex 第一性原理讨论后的共识: **不直接立项做完整格子建模**,
先做两个便宜的检验, 拿到证据再决定值不值得投入。

方法
----
1. **事件反应**: 对每个真实的下修决议(股东大会日 `meeting_date`), 比较
   会议日前后债券收盘价的变化, `approved`(真的生效)组跟
   `rejected`/`cancelled`(没生效)组对比——如果市场真的在乎这件事,
   生效组的价格反应应该系统性比没生效组更正面。
2. **假阳性率**: 用"过去 30 个交易日里有 N 天股价跌破转股价某个比例"这个
   跟强赎方向相反、结构相同的条件, 在全市场债券的**完整历史**上扫一遍,
   看有多少债券曾经满足过这个条件, 但从来没有真的跟进一次下修——这是
   之前只看"已经确认下修的债券"反推阈值时, 漏掉的那个分母(codex 指出的
   问题)。

已知局限(如实列出, 不假装精确)
------------------------------
- 只有股东大会日(`meeting_date`), 没有更早的"董事会提议"公告日——jisilu
  数据没有单独记录这个日期, 本脚本只能研究"投票结果公开"这一个事件点,
  不是"提议公告"那个更早的事件点(codex 建议两个都看, 本脚本只能看后者)。
- 价格反应用的是原始收益率(前一日收盘 -> 后 N 日收盘), 没有控制信用/
  波动率/流动性等其他因素, 不是严格的"异常收益率"事件研究——只回答
  "有没有一个粗略可见的方向性反应", 不回答"精确多大"。
- 假阳性率检验按**债券**为单位算首次触发(不按每一次独立的"触发区间"算),
  简化了"同一只债多次穿越阈值"的情况——本脚本只关心"这只债有没有在
  历史上某个时点满足过条件、之后有没有真的下修过", 不做更细的时间对齐。
- 转股价时间序列用 `cb_conv_price_history.parquet` 里已确认的 `approved`
  事件重建分段历史, 首段(第一次下修前)用该债第一次下修事件的
  `old_conv_price`(如果这只债从未下修过, 全程用 `cb_basic.parquet` 的
  静态 `conv_price`)——继承了 `cb_conv_price_history.parquet` 已知的
  "old_conv_price 偶尔因未记录的除权除息调整而失真"这个局限(2.6% 的
  历史事件受影响, 见 `docs/2026-08-31-cb-conv-price-history-spec.txt`
  第 1 节), 本脚本不重新修正。

只读数据, 不写 data/research_framework/。输出落在
study/down_revision_signal/。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "study" / "down_revision_signal"

TRAILING_WINDOW = 30
TRAILING_THRESHOLD = 0.90
TRAILING_REQUIRED_DAYS = 15
FOLLOWUP_TRADING_DAYS = 90  # 触发之后多少个交易日内算"跟进了下修"


def load_data():
    cb_basic = pd.read_parquet(ROOT / "data/cb_warehouse/cb_basic.parquet")
    hist = pd.read_parquet(ROOT / "data/cb_warehouse/cb_conv_price_history.parquet")
    cb_daily = pd.read_parquet(ROOT / "data/cb_warehouse/cb_daily.parquet")
    stk = pd.read_parquet(ROOT / "data/cb_warehouse/stk_daily_qfq.parquet")

    cb_daily = cb_daily.copy()
    cb_daily["bond_id"] = cb_daily["ts_code"].str[:6]
    cb_daily["trade_date"] = pd.to_datetime(cb_daily["trade_date"], format="%Y%m%d")

    stk = stk.copy()
    stk["stk_code"] = stk["ts_code"].str[:6]
    stk["trade_date"] = pd.to_datetime(stk["trade_date"], format="%Y%m%d")

    hist = hist.copy()
    hist["meeting_date"] = pd.to_datetime(hist["meeting_date"])
    hist["effective_date"] = pd.to_datetime(hist["effective_date"])

    return cb_basic, hist, cb_daily, stk


# ----------------------------------------------------------------------
# 1. 事件反应: approved vs rejected/cancelled
# ----------------------------------------------------------------------

def event_price_reaction(hist: pd.DataFrame, cb_daily: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, ev in hist.iterrows():
        if ev["outcome"] not in ("approved", "rejected", "cancelled"):
            continue
        sub = cb_daily[cb_daily["bond_id"] == ev["bond_id"]].sort_values("trade_date")
        before = sub[sub["trade_date"] < ev["meeting_date"]]
        after = sub[sub["trade_date"] > ev["meeting_date"]]
        if before.empty or len(after) < 3:
            continue
        p0 = before.iloc[-1]["close"]
        if p0 is None or p0 <= 0:
            continue
        row = {"bond_id": ev["bond_id"], "meeting_date": ev["meeting_date"], "outcome": ev["outcome"]}
        for n in (1, 3, 5):
            a = after.iloc[: n]
            if len(a) < n:
                continue
            row[f"ret_t{n}"] = (a.iloc[-1]["close"] - p0) / p0
        rows.append(row)
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------
# 2. 假阳性率: 触发条件 vs 真的跟进下修
# ----------------------------------------------------------------------

def build_conv_price_segments(bond_id: str, hist: pd.DataFrame, static_price: float):
    """返回 [(生效起始日期 或 None, 转股价)] 按时间正序的分段序列。
    `None` 起始日期表示"从最早开始适用", 直到下一段的起始日期。
    """
    events = hist[(hist["bond_id"] == bond_id) & (hist["outcome"] == "approved")]
    events = events.dropna(subset=["effective_date", "old_conv_price", "new_conv_price"])
    events = events.sort_values("effective_date")
    if events.empty:
        return [(None, static_price)]
    segments = [(None, float(events.iloc[0]["old_conv_price"]))]
    for _, ev in events.iterrows():
        segments.append((ev["effective_date"], float(ev["new_conv_price"])))
    return segments


def conv_price_on(segments, date) -> float:
    price = segments[0][1]
    for start, p in segments:
        if start is not None and start <= date:
            price = p
        elif start is not None and start > date:
            break
    return price


def false_positive_rate(cb_basic: pd.DataFrame, hist: pd.DataFrame, stk: pd.DataFrame) -> pd.DataFrame:
    bond_to_stk = dict(zip(cb_basic["code"].astype(str), cb_basic["stk_code"].astype(str)))
    bond_to_static_price = dict(zip(cb_basic["code"].astype(str), cb_basic["conv_price"]))
    # 债券存续期上下界——之前的版本漏了这个, 直接拿正股的全部历史价格去跑
    # 触发条件, 会把债券根本还不存在的年份(比如正股 2000 年的价格)也算
    # 进去, 产生毫无意义的"触发日"(人工核对特发转2/127021 时发现"首次
    # 触发日=2000-06-21", 但这只债 2020 年才发行, 是个真实 bug)。
    cb_basic = cb_basic.copy()
    cb_basic["_value_date"] = pd.to_datetime(cb_basic["value_date"], format="%Y%m%d", errors="coerce")
    cb_basic["_end_date"] = pd.to_datetime(
        cb_basic["delist_date"].fillna(cb_basic["maturity_date"]), format="%Y%m%d", errors="coerce",
    )
    bond_lifetime = {
        row["code"]: (row["_value_date"], row["_end_date"] if pd.notna(row["_end_date"]) else pd.Timestamp.today())
        for _, row in cb_basic.iterrows()
    }

    approved = hist[hist["outcome"] == "approved"].dropna(subset=["effective_date"])
    approved_dates_by_bond = approved.groupby("bond_id")["meeting_date"].apply(list).to_dict()

    rows = []
    for bond_id, stk_code in bond_to_stk.items():
        static_price = bond_to_static_price.get(bond_id)
        lifetime = bond_lifetime.get(bond_id)
        if pd.isna(stk_code) or static_price is None or pd.isna(static_price):
            continue
        if lifetime is None or pd.isna(lifetime[0]):
            continue
        life_start, life_end = lifetime
        sub = stk[
            (stk["stk_code"] == stk_code)
            & (stk["trade_date"] >= life_start)
            & (stk["trade_date"] <= life_end)
        ].sort_values("trade_date")
        if len(sub) < TRAILING_WINDOW:
            continue

        segments = build_conv_price_segments(bond_id, hist, float(static_price))
        dates = sub["trade_date"].to_numpy()
        closes = sub["close"].to_numpy()
        conv_prices = np.array([conv_price_on(segments, pd.Timestamp(d)) for d in dates])
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = closes / conv_prices
        hit = (ratio <= TRAILING_THRESHOLD).astype(float)
        trailing = pd.Series(hit).rolling(TRAILING_WINDOW).sum().to_numpy()
        eligible = trailing >= TRAILING_REQUIRED_DAYS

        first_idx = np.argmax(eligible) if eligible.any() else None
        if first_idx is None or not eligible[first_idx]:
            continue  # 这只债从没满足过条件, 不在假阳性率的分母里

        first_trigger_date = pd.Timestamp(dates[first_idx])
        followup_cutoff_idx = min(first_idx + FOLLOWUP_TRADING_DAYS, len(dates) - 1)
        followup_cutoff_date = pd.Timestamp(dates[followup_cutoff_idx])

        real_events = approved_dates_by_bond.get(bond_id, [])
        followed_up = any(first_trigger_date <= d <= followup_cutoff_date for d in real_events)
        ever_revised_after = any(d >= first_trigger_date for d in real_events)

        rows.append({
            "bond_id": bond_id, "first_trigger_date": first_trigger_date,
            "followed_up_within_90d": followed_up, "ever_revised_after_trigger": ever_revised_after,
        })
    return pd.DataFrame(rows)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cb_basic, hist, cb_daily, stk = load_data()

    reaction = event_price_reaction(hist, cb_daily)
    reaction.to_csv(OUT_DIR / "event_reaction.csv", index=False)

    fpr = false_positive_rate(cb_basic, hist, stk)
    fpr.to_csv(OUT_DIR / "trigger_false_positive.csv", index=False)

    lines = []
    lines.append("=== 1. 事件反应: 会议日前后收盘价变化 ===")
    for outcome in ("approved", "rejected", "cancelled"):
        sub = reaction[reaction["outcome"] == outcome]
        lines.append(f"\n{outcome}: n={len(sub)}")
        for col in ("ret_t1", "ret_t3", "ret_t5"):
            if col in sub.columns and sub[col].notna().sum() > 0:
                s = sub[col].dropna()
                lines.append(f"  {col}: n={len(s)} mean={s.mean():+.4%} median={s.median():+.4%}")

    if {"approved", "rejected"}.issubset(set(reaction["outcome"].unique())):
        a = reaction[reaction["outcome"] == "approved"]["ret_t3"].dropna()
        r = reaction[reaction["outcome"] == "rejected"]["ret_t3"].dropna()
        if len(a) > 5 and len(r) > 5:
            from scipy import stats
            t_stat, p_val = stats.ttest_ind(a, r, equal_var=False)
            lines.append(f"\napproved vs rejected (ret_t3) t检验: t={t_stat:.2f} p={p_val:.4f}")

    lines.append("\n\n=== 2. 触发条件假阳性率(30天窗口, 15天要求, 阈值0.90) ===")
    lines.append(f"总样本(历史上曾经触发过条件的债): {len(fpr)}")
    if len(fpr) > 0:
        lines.append(f"触发后90个交易日内跟进下修: {fpr['followed_up_within_90d'].mean():.1%}")
        lines.append(f"触发后(不限时间窗口)最终跟进下修: {fpr['ever_revised_after_trigger'].mean():.1%}")

    report = "\n".join(lines)
    (OUT_DIR / "REPORT.txt").write_text(report, encoding="utf-8")
    print(report, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
