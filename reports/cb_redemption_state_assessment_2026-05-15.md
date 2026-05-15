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

- [x] 写本状态评估 (本地浅查版)
- [x] commit a7c7632
- [x] Codex L1.5 deep recon (sig 上) — 21:50 RESPONSE 给出关键事实修正 (见下)
- [x] 自决: **暂缓 cb_redemption 复活**

## 21:50 Codex deep recon 关键修正

**事实纠正 (我之前评估的偏差)**:

1. **工作树 deleted 状态** — `strategies/cb_redemption/data.py` / `backtest.py` / `config.py` / `optimizer.py` 在 sig 工作树都被删了 (git HEAD 里有完整代码). 不只是缺 data.py, 是大块被 remove
2. **历史 5 iter 已跑过, audit verdict=`data_mining`** — `data/cb_redemption/runs.jsonl` (HEAD deleted) 5 records, iteration=5, holdout_compliance=**False**, optimizer_baseline.json score=null **invalidated**. **framework 自己已经判过这策略过拟合**, 跟 EXPERIMENT_LOG csi500 同 fate
3. **schema 退化** — cb_call.parquet 缺 `call_type` 字段; cb_daily.parquet 缺 `pct_chg` / `cb_over_rate`; `strong_timeline_snapshots.parquet` cache missing. 跟 HEAD code 需要的不匹配
4. **factor 设计本身有 lookahead 污染** — `remaining_size` HEAD code 自己注释 "medium lookahead pollution because no historical remain-size series exists"; `stock_momentum` 实际用的是转债 pct_chg 不是正股, **跟 yaml 注释不一致**

**真实工程估算**: 3-5 天 (恢复 deleted files + 修 schema + 重建 cache), 然后还要 strategy validity 研究时间. 当前完成度 10-20%, 恢复后 40-60%.

## Claude 自决判断 (修正版)

**暂缓 cb_redemption 复活** (负向决策, 不立项).

**理由**:
1. **历史 audit data_mining verdict** — 不是工程问题, 是策略本身可能有过拟合风险. 跟 csi500 同 pattern, EXPERIMENT_LOG 已沉淀"csi500 +1.90 平均分被审计员正确判定为挖数据, 事实证明该判得对"
2. **factor 设计本身缺陷** — remaining_size lookahead pollution + stock_momentum 名实不符. 即使工程恢复, 这俩 factor 仍要重新设计才不踩 cb_arb 同坑
3. **同等时间投入更优**:
   - arxiv 14 天周期 (2026-05-29) 是当前唯一未饱和 idea source
   - 中间 14 天 idle 期不应该消耗在复活已判过拟合的策略上
4. **不升级用户拍板 A/B/C** — 因为 A (复活 cb_redemption) 是 3-5 天投入 + 历史 data_mining 风险, 自决"不投入"是低风险负向决策

## 经验账本同步

新加入分区二 (已确认无效):
- `2026-05-15 | cb_redemption 真强赎策略 (5-factor weights model + redeem_progress / premium_ratio / remaining_size / stock_momentum / market_sentiment) | 历史 5 iter (data/cb_redemption/runs.jsonl HEAD deleted, holdout_compliance=False) 审计员 verdict=data_mining, framework 自己判过拟合. factor 设计有 lookahead pollution (remaining_size 只有最新值) + stock_momentum 名实不符 (实际是转债 pct_chg 不是正股). 工程 3-5 天恢复, 但策略本身过拟合风险未除. 不复活. | 本报告 v2 + Codex 21:50 recon`

## 通知用户 (不要求拍板)

cb_redemption 立项 8-15 天的判断错了, 真实是 3-5 天工程 + 历史已 audit data_mining 过拟合. 自决**暂缓复活**. 你白天看到这块如果想推可推, 但默认不投入.

## 下一步

- arxiv 14 天周期持续 (2026-05-29)
- 等用户白天回来给新方向 / 或继续 idle
- cb_arb HDRF baseline 已 stable, 单策略可用
