# cb_redemption (真强赎策略) 状态评估

日期: 2026-05-15 (today)
评估人: Claude (自决, 不烧钱本地审查)

## 背景

cb_arb 主线今晚 final 归档 (两路线 cross-validation 后确认 HDRF 是 winner, 自循环路线次于 HDRF)。下一步候选: **真 cb_redemption (强赎策略)** 立项, 作为多策略组合的第 2 个 baseline。

但 `strategies/cb_redemption/` 目录是历史命名 (framework 通用代码), 真 cb_redemption 策略**实际完成度未知**。本评估检查当前状态。

## 完成度盘点

### ✅ 已就位

1. **tunable_space.yaml** 已定义:
   - 5 个 weight 维度 (CMA-ES 搜索):
     - `w_redeem_progress` [0.5, 5.0] — 强赎触发率累计
     - `w_premium_ratio` [-5.0, -0.5] — 转股溢价率 (符号必负)
     - `w_remaining_size` [-4.0, -0.5] — 剩余规模
     - `w_stock_momentum` [-1.0, 2.0] — 5 日正股动量
     - `w_market_sentiment` [-1.0, 2.0] — 市场情绪代理
   - 5 个 factor (formula + status)
   - last_updated: 2026-05-07

2. **framework 9 角色** 已成熟 (经 cb_arb 自循环 60 iter 验证)

3. **强赎数据源** 已在 warehouse:
   - `data/cb_warehouse/cb_call.parquet` (997 行强赎公告, 2009 起)
   - 字段: ts_code / code / ann_date / call_date / call_price / is_call / expire_date

4. **basic / daily 数据** 已就位 (cb_basic.parquet / cb_daily.parquet 等)

### ❌ 缺失 (需要工程投入)

1. **`data.py` 不存在** — yaml 引用 "见 data.py:build_historical_snapshots()" 但 `strategies/cb_redemption/data.py` 没有
   - 必须实现: 从 cb_call + cb_daily + stk_daily 构造历史 snapshot, 用 ann_date 作为强赎事件起点

2. **strategy signal/scoring** — 5 个 weight 算出 score 的逻辑没有清晰位置
   - 可能在 evaluator.py 内部, 也可能要新写
   - 待 sig 上看代码细节确认

3. **backtest engine** — 是否复用 cb_arb 的 backtest, 还是写新的?
   - 强赎逻辑跟 cb_arb 套利不同 (强赎是事件驱动, cb_arb 是日度持仓)
   - 需要专属 entry/exit 逻辑

4. **历史强赎事件 universe** — 997 个事件足够吗?
   - 时间分布需检查 (2009-2026 还是集中在某段?)
   - 每年事件数 / pool 大小

## 算力 + 时间估算

按 cb_arb 自循环 60 iter ~ 5 天 (2026-05-05 ~ 2026-05-10) 节奏:
- cb_redemption 单 iter 估 1-2 hr (事件驱动比日度持仓慢)
- 60 iter ~ 5-10 天
- + 工程实现 (data.py + signal + backtest 接入) 估 3-5 天

**整体投入估算: 8-15 天 (人月级)**

## 收益评估 (用户长期目标 "最少钱+全自动")

**值得做的理由**:
1. 强赎逻辑跟 cb_arb 套利**独立** — 多策略组合的真分散
2. yaml 已立, framework 已成熟, **不是从零**
3. 数据基础 (cb_call.parquet 997 事件) 已经齐
4. 经验账本"低优 cb_redemption 强赎策略再次审视"已记录是值得探索的方向

**不值得做的理由**:
1. **8-15 天人月级投入**, 不是几小时小工程
2. cb_arb HDRF baseline 已经 stable (5 年 4/5 holdout 合理), 单策略已有真实可用价值
3. 用户长期目标是"最少钱+全自动", 多策略组合是锦上添花, 不是必需
4. 强赎事件数量小 (997 个总历史, 2022+ 可能 < 200 个) — 统计意义可能不足
5. arxiv 14 天周期还在跑 (下次 2026-05-29), 可能有更高 leverage 的 idea 出来

## 自决判断 (Claude)

**不立马起 cb_redemption 立项**. 而是:

### 推荐: 等 + 准备

1. **不立 spec** (等用户白天看本文档判断是否值得 8-15 天投入)
2. **arxiv 14 天周期持续** (2026-05-29) 作 idea source
3. **cb_arb 主线归档保持 final** — 不再触
4. **autonomous_summary** 给用户晚安

### 备选 (如果用户拍板继续 cb_redemption)

按优先级:
- (a) Codex sig 上看 strategies/cb_redemption/ 当前完整度 (evaluator/signal 是否在某处隐式存在)
- (b) 1-2 天写 data.py + signal layer
- (c) framework reuse, 跑 iter 1-30 看基线
- (d) 然后判断是否值得继续

## 升级窗口

**本评估升级用户**: cb_redemption 是 8-15 天工程, 不能 Claude 自决, 必须用户白天看完本文档**架构层**拍板:
- A. 继续 cb_redemption (大投入)
- B. 缓 cb_redemption, 等 arxiv 周期 + 其他线索
- C. cb_arb baseline 已够, 不再扩 (单策略稳态)

A/B/C 才是真正的架构层决策 (跟之前 cb_arb A/B 不同 — 那是 task 级研究方向, 这个是**是否花几周投入新方向**, 是预算 + 时间方向开关). 升级合理.

## 算力成本

- 本评估: 100% Claude 端本地 read, ¥0
- 不烧钱准备工作, 自主推进

## 后续待办

- [x] 写本状态评估
- [ ] commit
- [ ] autonomous_summary 给用户 (cb_arb 今天 final + cb_redemption 待用户架构层拍板)
- [ ] 设长 wakeup, 等用户白天回来
