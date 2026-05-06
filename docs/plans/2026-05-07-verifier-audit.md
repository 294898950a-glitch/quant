# cb_redemption Verifier — strong_timeline_snapshots 时序污染审计

**审计日期**：2026-05-07
**审计对象**：`data/cb_warehouse/strong_timeline_snapshots.parquet`（391,807 行 × 15 列，覆盖 20230103 ~ 20260424）
**生成代码**：`strategies/cb_redemption/data.py::build_historical_snapshots()`

## TL;DR — 时序干净度判断

**结论：部分干净，AI 信号严重污染、remaining_size 中度污染、redeem_progress 与 holder 特征基本干净。**

| 因子 | 时序干净？ | 严重度 | 备注 |
|------|:----------:|:------:|------|
| `close` / `premium_ratio` / `stock_momentum` | ✅ 干净 | — | 直接来自 cb_daily（trade_date 字段已经是 t 日观测值），rolling 仅用历史 |
| `market_sentiment` | ✅ 干净 | — | 全市场 5 日 rolling，不前视 |
| `redeem_progress` | ✅ 干净 | — | 用 `records[records["ann_date"] <= date_str]` 过滤公告；正股触发天数用 `stk_sub[trade_date <= date_str].tail(30)`，时序正确 |
| `top1_ratio_latest` / `top1_ratio_slope` / `top1_ratio_drawdown` | ✅ 干净 | — | 关键点：`report_date_int` 来自 `announcement_time`（实际披露日，毫秒时间戳），不是报告期末日；slope/drawdown 用 `ratios[:i+1]` 累积计算；merge_asof 用 `direction="backward"`。**前提：`announcement_time` 反映 PDF 披露日**（已通过 cninfo crawler 抓取，应可信） |
| `remaining_size` | ❌ **污染** | 中 | data.py:315 `size_map = basic.set_index("ts_code")["remain_size"] / 1e8` — `cb_basic` 是单一最新快照（`_updated_at = 2026-04-25`），所有历史日期都用同一个值。转债转股后 remain_size 会下降，2023 年的真实 remain_size > 当前；但因为该值在排序中静态、所有 t 都偏低同样幅度，对相对排名影响有限 |
| `ai_signal_score` / `ai_reduction_score` / `ai_is_original` | ❌ **严重污染** | 高 | `ai_holder_signals.parquet` 整张表只有 90 行，**没有任何日期列**（仅 `holder_type, stability, reduction, is_original, signal, stock_code, ts_code`），是 AI 一次性扫描当前持有人结构生成的静态判断。`build_historical_snapshots` 直接 `merge(ai, on="ts_code")` 把同一组 AI 标签复制到每个 ts_code 的所有交易日。验证：`127056.SZ` 的 `ai_signal_score=1.0` 从 20230103 一路贯穿到 20260424。这是**典型的 future-state leakage**——回测在 2023 年就"知道"了 2026 年 AI 对该转债的观感 |

## 详细分析

### 1. AI 信号（最严重）

代码位置：`data.py:531-547`
```python
ai = pd.read_parquet(str(ai_path))  # 90 行，无日期
ai["ai_signal_score"] = ai["signal"].map({"bullish":1, "neutral":0, "bearish":-1})
result = result.merge(ai[ai_cols], on="ts_code", how="left")  # 把单条标签广播到所有 t
```

`ai_holder_signals.parquet` 是 `ai_holder_analysis.py` 在某个时间点跑 LLM 对 *当前* 持有人结构（top1 性质、稳定度、减持迹象）做的语义判断。它本质上是"截至判断日的多年累计观察"——把它倒灌到 2023 年等于让回测看到 2.5 年后的信息。

**影响**：权重 -0.5494 (`ai_signal`) / -0.1504 (`ai_reduction`) / -0.419 (`ai_is_original`) 在 logit 中是非零项，过去优化器跑出来的"最优权重"很可能是被这层信号反向压榨过的产物——回测胜率高估的部分原因就在这里。

### 2. remaining_size（中等）

代码位置：`data.py:315`，`cb_basic.parquet` 无历史快照（只有 `_updated_at` 单一时间戳）。

**影响**：相对排序受影响有限（所有日期同方向偏移），但绝对值在 logit 中权重为 -3.68（最大权重之一），偏低的 remain_size 会系统性推高所有历史日期的得分。建议中长期补一个 historical remain_size 表（从 conv_record 累加）。

### 3. holder 特征（干净，仅需信任披露时间）

`announcement_time` 是巨潮咨讯爬虫抓的实际披露毫秒时间戳，merge_asof backward + 按 ts_code by-group ⇒ 严格 ≤ t 日已披露的最新报告。slope/drawdown 在 `_compute_stock_features` 中用 `ratios[:i+1]` 累积，没有前视。**唯一隐患**：如果某些 PDF 的 `announcement_time` 缺失（meta 中 `announcement_time` 为空），会被拉到很早的时间或丢弃——已通过 `dropna(subset=["report_date_int"])` 处理。

### 4. redeem_progress（干净）

`_calc_redeem_progress_at` 严格按 `ann_date <= date_str` 过滤公告，并按 `trade_date <= date_str` 取正股最近 30 日触发判断。

## 修复优先级

1. **必修**：AI 信号要么删掉、要么重建带 `valid_from` 日期的版本（每次披露持有人变更后跑一次 LLM）。当前回测/优化结论**作废**。
2. **建议修**：remaining_size → 用 cb_call/conv_record 累加重建历史 remain_size 时间序列。
3. **可保留**：其他因子。

## 本次改动范围（声明）

按用户要求，本次仅做 **verifier 框架修复**（data.py 接口收敛 + backtest.py 严格时序 + IS/OOS 切分），**不重新计算因子**。在 data.py 的污染因子位置加 `# FIXME: lookahead leak` 注释，把"重建无污染 AI/remain_size"留到下一个工单。

回测结果在 AI 信号污染未清理之前**只能用作框架验证**，绝对数字不能作为实盘决策依据。
