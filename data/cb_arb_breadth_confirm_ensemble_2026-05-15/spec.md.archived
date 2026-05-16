<!-- l0-entry-id: 2 -->
<!-- l0-source: experience_ledger open-thread "multi-signal ensemble" (高优) -->
<!-- protocol-redline-v1.3 -->

# 研究 spec: cb_arb breadth + pool-mean confirm ensemble panic detector

日期: 2026-05-15
研究 id: cb_arb_breadth_confirm_ensemble_2026-05-15
策略: cb_arb
状态: READY (Claude 起草; ¥30 预算用户 standing order 已批; 走完整流程)

## L0 假设 (一句话)

> "在 market breadth signal (跌停转债比例) 之外加 'pool-mean daily return' 作 confirm 信号, 双信号 AND 触发风险态. 期望解决 market breadth 单 signal 在 2024 的 false positive 问题 (5 个 trigger 都不在大 panic, 但 path 污染 -¥16062), 让 ≥ 5/6 holdout 改善 vs 当前 baseline."

## 来源洞察

`reports/cb_arb_market_breadth_panic_retro_2026-05-15.md` L5 揭示: 2024 selected risk-state trigger 5 次 (01-22, 02-05, 02-28, 06-24, 10-09) 都不在大 panic, 但 path 污染导致 -16062 PnL gap.

原因分析: market breadth 单 signal 在"短期局部跌幅"很敏感, 容易被"分化跌"误触发 (部分转债跌 ≥3% + 20% 池比例) 即使大盘整体平稳. 加一个 "pool-mean 当日整体 daily return" 作 confirm 信号, 双信号 AND 才触发 → 拒绝"分化跌"型 false positive.

Codex L5 推荐: "Any retry should change signal/action family, e.g. breadth combined with market index/vol/credit context".

为什么选 pool-mean (不用市场指数 / vol / credit):
1. 数据已有: 转债池 daily return 已在 cb_daily.parquet
2. 跟 breadth 同一数据源, 一致性好
3. 实施简单, 不引入新数据源 (B2 沙盒约束)
4. 直觉清晰: breadth = "局部破", pool-mean = "整体破", AND = 真正系统性 panic

## 关键变量

新加 1 维 (pool-mean confirm):

| 参数名 | 范围 | 含义 |
| --- | --- | --- |
| `confirm_pool_mean_threshold` | {-0.005, -0.010, -0.015} | 池子整体 daily mean return 低于此 = confirm panic |

复用 market breadth 的 4 个维度:

| 参数名 | 范围 |
| --- | --- |
| `breadth_drop_threshold` | {-0.03, -0.05} (压缩 3→2 档, 减网格) |
| `breadth_panic_ratio` | {0.20, 0.30} |
| `breadth_risk_recovery_days` | {1, 2, 3} |
| `breadth_risk_switch_hurdle_pct` | {0.05, 0.08, 0.10} |
| `breadth_normal_to_risk_min_days` | {1} (固定 1 减网格) |

正常态固定: recovery=4, hurdle=0.15 (medium baseline).

## Grid 设计

- 2 × 2 × 3 × 3 × 1 × 3 = **108** 候选 (跟 regime switch 一样规模)
- 6 holdout × 108 = 648 任务
- spot 估 10-15 分钟 (按之前 market breadth 10 分钟 162 候选推断), ¥3 以内

## 评估指标

主指标 (用 hard floors v1.1 重校准版本, per experience_ledger):

- replay_2020 ≥ -0.130604 (当前 baseline 自身)
- replay_2021 ≥ -0.050441
- replay_2022 ≥ 0.014425
- replay_2023 ≥ -0.031027

辅助指标:
- 6 holdout 通过率 (≥ 5/6 采用门槛)
- 风险态触发频率 (期望 < 2% 天数, 比 market breadth 单 signal 的 3.7% 更精)
- **2024 false positive 触发次数** (期望 0-1 次, 单 signal 是 5 次)

## leave-N-out

- holdout: 2019/2020/2021/2022/2023/2024
- top1 CV 用 leave-Y 训练
- 采用门槛: ≥ 5/6 改善或不退化

## Stop conditions

- 0 候选过 4 floor → 停
- > 50% 候选过 floor → 必跑 5 项质疑
- 真 CV < 5/6 → "已确认无效"
- 总算力 ¥30 → 暂停

## 算力估算

- 108 × 6 = 648 任务 × ~10s = ~108 分钟 sig
- spot (16 核) ≈ 108 / 16 ≈ **7 分钟**
- spot 协议 (≥30 min 建议起 spot, ≥2hr 必起): 7 min < 30 min → **可在 sig 上跑 ~2 hr** OR 起 spot ~7 分钟
- 建议起 spot (跟前 2 次一致, 协作方便; spot 已有 stopped 实例 ins-5lb9zo12)

## 预算上限

- 总算力预算: ¥3 (实际 spot 7 min, < 5% ¥30 总预算)
- 总时间: ≤ 1 hr

## 必出产物

- `data/cb_arb_breadth_confirm_ensemble_2026-05-15/ranked.csv`
- `summary.csv` (baseline 行 + 候选汇总)
- `trades.csv` (baseline + selected detail, per L5 schema 修复)
- `daily_equity.csv` (baseline + selected)
- `trigger_dates.csv` (双信号: breadth 触发日 + pool-mean confirm 日 + effective 日)
- `breadth_daily.csv` (复用)
- `pool_mean_daily.csv` (新, 池子整体 daily mean return)
- `reports/cb_arb_breadth_confirm_ensemble_2026-05-15.md`

## 已有脚本复用

- evaluator: 基于 `scripts/evaluate_cb_arb_market_breadth_panic.py` 扩展, 加 pool-mean confirm 条件
- search 脚本: 扩展 grid, 加 confirm 维度
- baseline kind: medium_opportunity (recovery=4 hurdle=0.15)
- 数据源: 复用 `data/cb_warehouse/cb_daily.parquet` (无新数据)

## 数据可用性

- breadth: 已通过 L1.5 (market breadth batch 12:18 RESPONSE 确认)
- pool-mean: **同一文件**, 日内对所有转债 daily return 求 mean. Codex 在 L1.5 重检时确认计算逻辑无 lookahead.
- lookahead 硬约束: 双 signal 在 T 日 evaluation → T+1 effective (跟 breadth 一致)

## 升级条件

- 算力超 ¥30 → ESCALATE 用户
- pool-mean 数据计算异常 → stop + RESPONSE
- 中途发现 confirm signal 跟 breadth 信号高相关 (correlation > 0.95) → 提示用户, 因为 confirm 等于无效约束
- 用户离场 ≥ 30 分钟且 spot 在烧 → auto-shutdown
