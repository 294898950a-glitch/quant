# 经验账本

研究流程的"记忆系统". 多次研究累积下来, 沉淀什么该做、什么别做.

四个分区:

## 一、已采用方向 (Adopted)

格式: 一行一条

- YYYY-MM-DD | 策略 | 改动 | 落地处 (yaml 绿区 / 代码 / 数据)
- e.g. `2026-05-XX | cb_arb | medium signal recovery_days=1 (原 2) | yaml 绿区`

## 二、已确认无效 (Rejected — 不再走)

格式: 一行一条 + 链接到复盘报告

- YYYY-MM-DD | 假设 | 失败原因 | 报告链接
- e.g. `2026-05-14 | CSI500 同日大跌过滤 | 2 胜 2 平 2 负, 2022 反向, dating 修复失败 | reports/cb_arb_csi_market_filter_2026-05-14.md`
- `2026-05-15 | medium_recovery3_hurdle0p10 作为无条件全年参数 | 跨年留出主底线仅 2/4 通过 (2019/2024 ✓, 2022/2023 ✗), 跨 regime 不鲁棒 | reports/cb_arb_round5_retro_2026-05-15.md`
- `2026-05-15 | arxiv 候选 "Arbitrage-Free XVA" (1608.02690) | 通过硬筛但弱关联 cb_arb (XVA = 衍生品估值调整, 不给策略层信号); priority=低, Claude reject + 写入避免下次重复 | data/research_framework/paper_candidates/2026-05-15.md
- `2026-05-15 | cb_arb 自身 PnL lookback rolling excess 作 regime classifier (任何 lookback 10/20/60 + 任何 threshold + 任何风险态参数) | 0/108 过 4 floor, CV 3/6 holdout 通过 (<5/6); lookback fundamental 滞后 (2020 第一次触发 04-03 距 panic 起点 02-03 已 60 日历日); lookback=10 反而比 60 更差 (false positive +); 实际是 51.9% 天数广义 mode switch 不是 panic detector | reports/cb_arb_regime_switch_retro_2026-05-15.md

模式 B 中, AI **绝对不能再提这里的方向**(B5 红线).

## 三、未完成线索 (Open Threads)

格式: 优先级 + 一行描述 + 状态 + 来源

| 优先级 | 描述 | 状态 | 来源 |
| --- | --- | --- | --- |
| 高 | medium signal 的 regime switch 机制 — 按市场状态切换 recovery_days / switch_hurdle_pct (同一组参数 4/4 不可行, 必须看年份) | 待设计 spec, 走完整 HDRF 流程 | 2026-05-15 Round 5 跨年验证 |
| 中 | medium signal 2022 退化的 trade-level 归因 — 哪些仓位 / 触发了什么条件 | 待 trade-level 反向诊断 | 2026-05-15 Round 5 复盘 |
| 中 | panic signal 重新设计 (medium 单参数路径已饱和, 回头研究 panic detector) | 待立项 | 2026-05-15 Round 5 复盘 |
| 中 | 2022/2024 中等亏损年的 regime detector | 待设计 | 2026-05-14 复盘报告 |
| 高 | **exogenous panic signal**: vol surge / market breadth / credit spread spike — 不依赖自身 PnL 滞后, 用市场即时信号 | 来自 2026-05-15 regime switch 失败的 next-step backlog | reports/cb_arb_regime_switch_retro_2026-05-15.md |
| 中 | Forward-looking trigger: 不用 lookback, 用结构性突变检测 (PELT / Bayesian changepoint) | 探索 | reports/cb_arb_regime_switch_retro_2026-05-15.md |
| 中 | L3 schema 改进: 加 baseline trades.csv + daily_equity.csv 导出 (L5 反向诊断需要) | 工程 backlog | Codex L5 side finding 2026-05-15 |
| ~已完成~ | cb_arb medium signal 在 2021 退出节奏过黏 → `recovery_days × switch_hurdle_pct` grid | 已跑完, recovery=3 hurdle=0.10 局部修复 2021 +2.09pp, 但跨年 2/4 不达标 | reports/cb_arb_round5_retro_2026-05-15.md |

## 四、未来探索方向 (Future Backlog)

格式: 优先级 + 一行描述

| 优先级 | 描述 |
| --- | --- |
| 低 | cb_redemption 强赎策略再次审视, iter 60 后状态评估 |
| 低 | 自动循环框架本身的稳健性测试 (跑 100 iter 看是否还 stagnant) |
| 低 | (用户后续添加) |

## 模式 B 选研究的规则

AI 用户离场进入模式 B 时, **从下面顺序选**:

1. 先看分区 "三、未完成线索" 的"高"优先级
2. 没有高优先级 → "中"优先级
3. 全空 → 看分区 "四、未来探索方向" 的"低"优先级
4. 都空 → **不进入模式 B, 写 autonomous_summary 等用户**

注意: **绝对不碰**分区"二、已确认无效".

## 维护规则

- 每次研究 batch 完成时, 必须更新经验账本:
  - 采用结论 → 写入分区一
  - 拒绝结论 → 写入分区二
  - 发现新线索 → 写入分区三
  - 新方向 → 写入分区四
- 已完成的"未完成线索" → 划到分区一或二, 不留在分区三
- 优先级修改:
  - 用户在场时, 用户改
  - 用户离场时, 模式 B 自动按"上次研究的延伸度"调高优先级
- 整张账本每次研究前 Claude 先读, 决定下一步

## 例外

- 用户说"忽略账本" → 临时不用,但事后必须把忽略的研究结果还是写入(避免重复走)
- 账本数据丢失 → 必须从 git 历史重建,不能任意补
