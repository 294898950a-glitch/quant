# 经验账本

研究流程的"记忆系统". 多次研究累积下来, 沉淀什么该做、什么别做.

## 当前 cb_arb 研究 hard floors (v1.1, 2026-05-15 重校准)

来源: spec v1.1 normal-state baseline (recovery=4, hurdle=0.15) 在 medium_opportunity 数据上跑的 6 年 excess.

| 年份 | hard floor |
| --- | ---: |
| 2019 | 0.161312 |
| 2020 | -0.130604 |
| 2021 | -0.050441 |
| 2022 | 0.014425 |
| 2023 | -0.031027 |
| 2024 | 0.030085 |

**重校准历史**:
- v1.0 (Round 4/5 used, mixed): 2020 ≥ -0.138588 (旧 medium_opportunity recovery=2 hurdle=0.12), 2021 ≥ -0.033534 (current_best_no_opportunity, **不同 baseline**), 2022 ≥ 0.028891, 2023 ≥ -0.027744
- v1.1 (本次): 用当前 baseline (spec v1.1 normal-state) 统一 6 年, 4 floor 来自同一 baseline
- 来源: market breadth panic batch L5 (Codex 2026-05-15 12:18 RESPONSE) 暴露 v1.0 混合来源

**采用门槛**: 候选 selected 在 6 个 holdout 年的 ≥ 5/6 改善或不退化 vs 当前 baseline. **不再用 ≥ 4 floor** (没意义, baseline 自己就是 floor).

下次研究 spec 直接引用本表, 不重写.

---

四个分区:

## 一、已采用方向 (Adopted)

格式: 一行一条

- YYYY-MM-DD | 策略 | 改动 | 落地处 (yaml 绿区 / 代码 / 数据)
- e.g. `2026-05-XX | cb_arb | medium signal recovery_days=1 (原 2) | yaml 绿区`
- `2026-05-15 | cb_arb | **baseline 最终采用版本: medium signal recovery=4 hurdle=0.15** (spec v1.1 normal-state). 5 年 holdout: 2019 +16.1% / 2020 -13.1% / 2021 -5.0% / 2022 +1.4% / 2023 -3.1% / 2024 +3.0%. 4/5 holdout 合理, 2020 是历史极端 cb_arb 结构性不适配, 已确认不再死磕. cb_arb 主线研究饱和. | yaml 绿区 (recovery_days / switch_hurdle_pct), `reports/cb_arb_baseline_trade_diagnostic_2026-05-15.md` |

## 二、已确认无效 (Rejected — 不再走)

格式: 一行一条 + 链接到复盘报告

- YYYY-MM-DD | 假设 | 失败原因 | 报告链接
- e.g. `2026-05-14 | CSI500 同日大跌过滤 | 2 胜 2 平 2 负, 2022 反向, dating 修复失败 | reports/cb_arb_csi_market_filter_2026-05-14.md`
- `2026-05-15 | medium_recovery3_hurdle0p10 作为无条件全年参数 | 跨年留出主底线仅 2/4 通过 (2019/2024 ✓, 2022/2023 ✗), 跨 regime 不鲁棒 | reports/cb_arb_round5_retro_2026-05-15.md`
- `2026-05-15 | arxiv 候选 "Arbitrage-Free XVA" (1608.02690) | 通过硬筛但弱关联 cb_arb (XVA = 衍生品估值调整, 不给策略层信号); priority=低, Claude reject + 写入避免下次重复 | data/research_framework/paper_candidates/2026-05-15.md
- `2026-05-15 | cb_arb 自身 PnL lookback rolling excess 作 regime classifier (任何 lookback 10/20/60 + 任何 threshold + 任何风险态参数) | 0/108 过 4 floor, CV 3/6 holdout 通过 (<5/6); lookback fundamental 滞后 (2020 第一次触发 04-03 距 panic 起点 02-03 已 60 日历日); lookback=10 反而比 60 更差 (false positive +); 实际是 51.9% 天数广义 mode switch 不是 panic detector | reports/cb_arb_regime_switch_retro_2026-05-15.md
- `2026-05-15 | cb_arb 转债池跌幅截面比例作 breadth panic detector (5 维 162 候选 grid: drop_threshold × panic_ratio × recovery × hurdle × min_days) | 0/162 过 4 floor, CV 3/6 通过; **比 regime switch 强**(2020-02-03 准时触发 + targeted 3.7% 天数 vs 51.9%), 但仍 path-sensitive; 2024 -16062 PnL gap path 污染; 任何 floor 重校准都救不了 (full grid 仍 0/162 全过); 单 signal + 单 action mapping 救不了 cb_arb 跨年泛化 | reports/cb_arb_market_breadth_panic_retro_2026-05-15.md
- `2026-05-15 | breadth + pool-mean confirm ensemble AND-trigger (6 维 108 候选) | 0/108 过 4 floor (recalibrated v1.1), CV 3/6 通过; **结果跟 market breadth 单 signal 几乎逐位相同** (2024 5 个 false positive 日子 pool mean 都低于 -0.005 同时触发, AND 没起过滤作用); confirm 跟 breadth 高相关 (corr -0.71) 导致 AND 退化为单 signal | reports/cb_arb_breadth_confirm_ensemble_retro_2026-05-15.md
- `2026-05-15 | **panic detector 整条子方向** (任何 signal family / 任何 action mapping) | 2024 真假 panic 诊断 (reports/cb_arb_panic_diagnostic_2026-05-15.md) 证明: (1) same-day cross-sectional 不能区分真假 panic (2024-10-09 比 2020-01-23 还剧烈); (2) **真 panic 2020-01-23 之后 30 天 baseline 自己也输 benchmark -3.8%, detector 即使完美也救不了**; (3) baseline 已隐含 panic 行为, detector overlay 在假 panic 上反而误伤. **stop 整个 panic detector 子方向**. 真问题是 cb_arb 在 panic+反弹组合年的策略结构性弱点 | reports/cb_arb_panic_diagnostic_2026-05-15.md
- `2026-05-15 | **cb_arb baseline 单一 trade filter 改造** (entry/exit/holding 任意单维度 rule) | trade-level 归因 (reports/cb_arb_baseline_trade_diagnostic_2026-05-15.md) 证明: 2020 是 cross-trade broad weakness — 90 trades 74% 负 excess + median -4.32pp + worst10 只占 23% + 散在 5 个 entry month + 4 类 exit reason; 没有单一 trade-type 或 single filter 可救. 2024 baseline 本身 +0.77pp 已经在赚, 不需要改. **A 路径 (改 baseline) 数据驱动否决** | reports/cb_arb_baseline_trade_diagnostic_2026-05-15.md
- `2026-05-15 | **cb_arb 年份选择性 meta wrapper** (B 路径) | meta-detector 仍依赖某种 detector, panic detector 整条子方向已确认无效; 而 2020 broad weakness 是全年级 + 散布(74% 负 + 5 个 entry month), 不是 panic 短窗口, meta 没什么可减仓的窗口. **B 路径数据驱动否决** | reports/cb_arb_baseline_trade_diagnostic_2026-05-15.md

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
| 高 | **架构层 floor 重校准**: 4 个 hard floor 混合来源 (2020/2022/2023 来自旧 medium_opportunity, 2021 来自 current_best_no_opportunity 不同 baseline). 当前 spec v1.1 baseline 自己 fail 3 个 floor. 该重新校准用单一 baseline | 等用户拍板 | reports/cb_arb_market_breadth_panic_retro_2026-05-15.md L5 数据 |
| 高 | **multi-signal ensemble**: market breadth + vol surge + credit spread + market index 多 signal 联合 (any-fire / weighted vote / Bayesian) — 单 signal 单 action 已确认无效, 跨研究方向 | 待立项 | reports/cb_arb_market_breadth_panic_retro_2026-05-15.md |
| 中 | **新 action mapping**: 不只调 recovery+hurdle, 试 position scaling / hedge overlay / pair trade overlay 等 action family | 跨研究 | reports/cb_arb_market_breadth_panic_retro_2026-05-15.md |
| **关键** | **3 batch 同 pattern 反思**: regime switch / breadth-only / breadth+confirm 3 种 panic signal family 都 fail 2019/2020/2024 数字几乎相同. 问题不在 signal 选择, 在 action mapping (recovery+hurdle 激进收手在 false positive 时打 path), 或 cb_arb 在反弹年本质不适合 panic-then-pull | 架构层反思 | reports/cb_arb_breadth_confirm_ensemble_retro_2026-05-15.md |
| 中 | Forward-looking trigger: 不用 lookback, 用结构性突变检测 (PELT / Bayesian changepoint) | 探索 | reports/cb_arb_regime_switch_retro_2026-05-15.md |
| 中 | L3 schema 改进: 加 baseline trades.csv + daily_equity.csv 导出 (L5 反向诊断需要) | 工程 backlog | Codex L5 side finding 2026-05-15 |
| ~已完成~ | cb_arb medium signal 在 2021 退出节奏过黏 → `recovery_days × switch_hurdle_pct` grid | 已跑完, recovery=3 hurdle=0.10 局部修复 2021 +2.09pp, 但跨年 2/4 不达标 | reports/cb_arb_round5_retro_2026-05-15.md |
| ~已完成~ | cb_arb baseline 2020/2024 trade-level 归因 | 已跑完, broad weakness 不是 tail, A/B 双路径数据驱动否决, cb_arb 主线研究饱和归档 | reports/cb_arb_baseline_trade_diagnostic_2026-05-15.md |
| **关键** | **cb_arb 主线饱和, 转向下一个策略** — cb_redemption 继续 (iter 60+) / 多策略组合 / arxiv 14 天周期外部 idea source. 用户长期目标"最少钱+全自动"建议多策略组合 | 待 Claude+Codex 自决 | 本研究 |

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
