#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断脚本: 满足强赎条件到公司实际公告之间, 真实延迟有多长?

背景
----
`price_cb_lattice`(见 `strategies/cb_arb/cb_pricer_lattice.py`)假设一旦满足
强赎条件, 发行人立刻收场。用 `study/validate_cb_pricer_lattice.py` 在真实数据
上验收时发现, 这个假设导致的理论价偏高问题, 在 moneyness 越高(转股价值越
深度价内)、剩余期限越长的债上越严重(见该脚本产出的 `REPORT.txt`)。本脚本
检验一个猜测: 是不是因为现实里"满足条件"到"公司真的公告"之间存在真实延迟,
而这段延迟里股价还在继续往上走?

方法
----
`cb_call.parquet` 的 `ann_date` 字段本身不可靠, 不能当"是否已强赎"的实时状态
判断(见 knowledge 库 `cb_call表不能当强赎标记.md`)。但对于**确实记录了一个
具体日期**的债, 这个日期本身作为"公司哪天公告的"这条孤立事实, 仍然可以拿来
跟"用 `call_condition.is_call_eligible` 从价格历史算出来的、最近一次连续满足
区间的起点"做对比, 量出延迟。

已知局限(如实列出)
------------------
- 一只债可能多次穿越 130% 阈值又跌回去, 本脚本只取"紧挨着 ann_date 之前那一段
  连续满足区间"的起点, 不是"历史上第一次满足"——后者会把早年一次短暂冲高也
  算进去, 严重高估延迟(初版做法, 已发现问题并改正, 结果差 2 倍)。
- "连续满足区间"对区间中间偶尔一两天跌破阈值很敏感, 会把本该算一段的延迟
  切成两段。没有做平滑处理。
- `ann_date` 本身的准确含义无法独立验证(同上, cb_call.parquet 的已知局限),
  这里只能假设它记的是"这次强赎相关公告"的日期, 不是别的事件。
- 只统计了 `ann_date` 落在满足区间**之后**的样本, 顺序不对的(公告日反而更早)
  被跳过, 不计入分布, 这些"异常"本身也是信息, 只是本脚本没细分类。

只读数据, 不写 data/research_framework/。输出落在
study/call_announcement_delay/。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from strategies.cb_arb.call_condition import is_call_eligible  # noqa: E402

WAREHOUSE = REPO_ROOT / "data" / "cb_warehouse"
OUT = REPO_ROOT / "study" / "call_announcement_delay"
OUT.mkdir(parents=True, exist_ok=True)


def load_moneyness_panel() -> pd.DataFrame:
    cb_basic = pd.read_parquet(WAREHOUSE / "cb_basic.parquet")
    cb_daily = pd.read_parquet(WAREHOUSE / "cb_daily.parquet")
    stk = pd.read_parquet(WAREHOUSE / "stk_daily_qfq.parquet")[
        ["stk_code", "trade_date", "close"]
    ]
    b = cb_basic[["ts_code", "stk_code", "conv_price"]].drop_duplicates("ts_code")
    df = cb_daily[["ts_code", "trade_date"]].merge(b, on="ts_code", how="inner")
    df = df.merge(stk, on=["stk_code", "trade_date"], how="left")
    df["moneyness"] = df["close"] / df["conv_price"]
    return df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


def last_eligible_run_start(dates: np.ndarray, elig: np.ndarray, ann_date: str):
    """紧挨 ann_date 之前那段连续 True 区间的起点 index(在 dates 里的位置).

    没有满足过, 或顺序不对(ann_date 早于任何满足), 返回 None。
    """
    before = dates <= ann_date
    if not before.any():
        return None
    elig_before = elig[before]
    if not elig_before.any():
        return None
    last_true = len(elig_before) - 1 - int(np.argmax(elig_before[::-1]))
    start = last_true
    while start > 0 and elig_before[start - 1]:
        start -= 1
    return start, last_true


def main() -> int:
    print("loading panel...")
    df = load_moneyness_panel()
    cb_call = pd.read_parquet(WAREHOUSE / "cb_call.parquet")
    cc = cb_call[cb_call["ann_date"].astype(str).str.len() == 8][
        ["ts_code", "ann_date"]
    ].drop_duplicates("ts_code")

    rows = []
    skipped_no_history = 0
    skipped_bad_order = 0
    for _, r in cc.iterrows():
        g = df[df["ts_code"] == r["ts_code"]]
        if g.empty:
            skipped_no_history += 1
            continue
        dates = g["trade_date"].values
        elig = is_call_eligible(g["moneyness"].values)
        result = last_eligible_run_start(dates, elig, r["ann_date"])
        if result is None:
            skipped_bad_order += 1
            continue
        start_idx, ann_idx = result
        delay_trading_days = ann_idx - start_idx
        rows.append({
            "ts_code": r["ts_code"],
            "ann_date": r["ann_date"],
            "first_eligible_date": dates[start_idx],
            "delay_trading_days": delay_trading_days,
        })

    res = pd.DataFrame(rows)
    res.to_csv(OUT / "delay_detail.csv", index=False)

    delay = res["delay_trading_days"]
    lines = []
    lines.append("# 满足强赎条件到公司实际公告之间的延迟(交易日)\n\n")
    lines.append(f"有效样本 n={len(res)}; 跳过(该债无价格历史)={skipped_no_history}; "
                 f"跳过(公告日早于任何满足日, 顺序异常)={skipped_bad_order}\n\n")
    lines.append("## 分布\n")
    lines.append(f"mean={delay.mean():.1f}  median={delay.median():.1f}  "
                 f"std={delay.std():.1f}  min={delay.min()}  max={delay.max()}\n")
    q = delay.quantile([0.1, 0.25, 0.5, 0.75, 0.9])
    lines.append(f"分位数(10/25/50/75/90): {q.round(1).to_dict()}\n\n")
    lines.append("## 累计占比(说明这是双峰分布, 不是单一典型值)\n")
    for thr in [1, 5, 10, 20, 60, 120, 250, 500]:
        pct = (delay <= thr).mean()
        lines.append(f"<= {thr} 个交易日: {pct:.1%}\n")

    report = "".join(lines)
    (OUT / "REPORT.txt").write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
