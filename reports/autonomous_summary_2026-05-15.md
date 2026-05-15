# 今日研究自治总结 (2026-05-15)

## TL;DR

- **cb_arb 主线 final 归档** (两路线 cross-validation 后 HDRF 是 winner, 自循环路线确认次)
- **下一步候选: cb_redemption (真强赎策略)** — 8-15 天工程, **需用户架构层拍板**
- **今日累计算力: ¥8** (¥90 月预算的 9%)
- **架构 commits**: dce5c0a (cb_redemption iter 11 paused 前) → 9f1ea3c (panic 诊断闭环) → a73daae (trade-level 归因 A 路径否决) → 908318a (cb_redemption=cb_arb 修正) → d4905c9 (cross-validation 主线 final) → a7c7632 (cb_redemption 状态评估)

## 今日完整时间轴

### 上午 (~07:00-12:00) — Round 5 + panic detector 立项
- Round 5 跨年验证: 2/4 holdout 通过 → reject (Exit A)
- 协议升级: U12 (3 轮 Codex 让步) + U13 (L0 hardening) + U14 (PROGRESS heartbeat)
- HDRF v3.0 → v3.3, 协议红线 v1.0 → v1.3

### 中午 (~12:00-15:00) — panic detector 3 batch
- Batch 1: self-PnL regime switch (108 候选) — 0/108 过 4 floor, CV 3/6 holdout. **reject**
- Batch 2: market breadth panic (162 候选) — 0/162 过, CV 3/6. **reject + 暴露 floor 混合来源**
- Batch 3: breadth + pool-mean confirm AND-ensemble (108 候选) — 0/108 过, CV 3/6. **reject + 退化为单 signal**

### 下午 (~15:00-17:00) — 2024 panic 诊断 + cb_arb HDRF 闭环
- 跨 batch 反思: panic signal family 已穷举但都 fail
- 2024 真假 panic 诊断: same-day cross-sectional **不能区分**真假 panic + 真 panic 之后 baseline 自己也输 benchmark **-3.8%**
- 结论: **panic detector 整个子方向结构性无效**
- A vs B 架构候选升级用户 (后被纠正这俩是 task 级, 不该问)

### 晚上 (~20:00-21:10) — cb_arb trade-level + 两路线 cross-validation + final 归档
- trade-level 归因 (5 min sig): 2020 broad weakness (74% 负 + worst10 只占 23%), A 路径数据驱动否决
- 自决 cb_arb 主线饱和 → "cb_arb 归档" → 后发现 cb_redemption iter loop 实际是 cb_arb 第二条路线 → **撤回归档判断**
- 两路线 cross-validation (6 min sig): HDRF 0.287 vs 自循环 0.313, 但 **2019 自循环 broken -10.1% vs HDRF +16.1%** (26pp gap)
- 结论: HDRF 是 winner, 自循环路线无效, **cb_arb 主线 final 归档**
- cb_redemption 状态评估: 8-15 天工程, 升级用户架构层拍板

## 经验账本变化

### 分区一 (已采用):
- `cb_arb baseline 最终采用 (final, two-line confirmed): medium signal recovery=4 hurdle=0.15` — 6 年 holdout 4/5 合理

### 分区二 (已确认无效, B5 红线 — 不再提):
- 整个 panic detector 子方向 (任何 signal + 任何 action mapping)
- cb_arb baseline 单一 trade filter (A 路径 entry/exit/holding 改造)
- cb_arb 年份选择性 meta wrapper (B 路径)
- cb_arb 自循环 LLM 调参路线 (2019 broken -26pp)
- 其他 specific batch 否定 (CSI500 filter / Round 5 medium recovery3 hurdle0.10 / regime switch / breadth-only / breadth+confirm ensemble)

### 分区三 (未完成线索 高优先级):
- **真 cb_redemption (强赎策略) 立项** — 等用户架构层拍板
- 多策略组合 (cb_arb + cb_redemption baseline)
- arxiv 14 天周期 (2026-05-29 next)

## 协作通道协议变化

- U12: Claude↔Codex 辩论 ≤3 轮无共识 → 听 Codex (除非红线)
- U13: L0 hardening — outbox preflight 自动跑 7 项 schema check
- U14: Codex 任务 >10 min 必每 10 分钟 PROGRESS

## 我学到的 (memory 新加)

- `feedback_research_direction_is_task_level.md` — 研究方向 A/B/C 选择不是架构层
- 用户的 architecture-only 边界比预想严: 真正架构层 = 多周投入决策 / 协议改变 / 预算重大变更
- 自循环数据归类要看 sealed_pools.json strategy 字段, 不能凭 commit message 直觉

## 升级用户 (需架构层拍板)

**cb_redemption 立项 A/B/C** (在 `reports/cb_redemption_state_assessment_2026-05-15.md`):
- A. 继续 cb_redemption (8-15 天工程, 多策略组合基础)
- B. 缓 cb_redemption, 等 arxiv + 其他 idea source
- C. cb_arb baseline 已够, 不再扩 (单策略稳态)

这是**真的**架构层决策 (预算 + 时间方向开关), 跟之前 task 级研究方向不同, 升级合理.

## Codex 端状态

- 当前: idle / standby (等 cb_redemption spec 或下一个 DIRECT)
- 今日协作: 6 次 DIRECT + 6 次 ACK + 4 次 RESPONSE + 1 次 closeout
- sig VM 长开零边际, 今天纯用 sig 跑了 trade-level / cross-validation, ¥0
- spot 今天起了 3 次 (上午 3 panic batch), ~ ¥8

## 下次开会入口

1. **必看**: `reports/cb_redemption_state_assessment_2026-05-15.md` — 你 A/B/C 拍板
2. 选看: `reports/cb_arb_two_line_cross_validation_2026-05-15.md` — cb_arb 主线 final 闭环理由
3. 选看: `reports/cb_arb_panic_diagnostic_2026-05-15.md` — panic detector 整子方向无效根因
4. 经验账本最新版: `docs/research_framework/experience_ledger.md`

## 待 loop 监控

- /loop continue monitoring codex/outbox.md 继续, 30min 兜底 (Codex idle, 等明天 cb_redemption 拍板)
