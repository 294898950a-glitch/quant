<!-- l0-entry-id: 2 -->
<!-- l0-source: experience_ledger open-thread "exogenous panic signal" (高优, 子方向 B: market breadth) -->
<!-- protocol-redline-v1.3 -->

# 研究 spec: cb_arb market breadth panic detector

日期: 2026-05-15
研究 id: cb_arb_market_breadth_panic_2026-05-15
策略: cb_arb
状态: DRAFT (Claude 起草, 等用户确认方向 + ¥30 预算)

## L0 假设 (一句话)

> "用转债池当日跌停 / 大跌转债比例作 exogenous panic signal, 触发风险态后短 recovery + 紧 hurdle, 避免 regime switch 那种'自身 PnL 滞后 30 日'的固有缺陷, 让 medium signal 在 2020/2022/2023/2024 同时过主底线."

## 来源洞察

`reports/cb_arb_regime_switch_retro_2026-05-15.md` 证明: 自身 PnL rolling 累计 excess 作 regime classifier 必然滞后真实 panic ≥ 30 日 (2020-02-03 panic 起点 → 2020-04-03 第一次 risk 触发). 这是 lookback-based 方法的 fundamental 缺陷, 任何 lookback (10/20/60) 都无法解决.

Codex 在 L5 推荐: "Do not run another same-family grid unless the spec changes signal family, e.g. immediate exogenous panic/vol-surge/market-breadth signal".

**为什么用 market breadth (cross-sectional, 不用 vol surge)**:
1. 数据已有: 转债池每日涨跌幅是 cb_arb 现有 baseline 必备数据
2. 实施快: 不需要 IV 计算 (CB 混合证券 IV 计算复杂)
3. 即时反应: 当日截面统计, 不靠 lookback, 没有滞后
4. 易解释: "今天 N% 转债跌幅 < X% → panic 态" 直接可视化

跟 vol surge / credit spread 比, market breadth 是最近 cb_arb 本身的 cross-sectional signal.

## 关键变量

新增参数 (会进 yaml 黄区):

| 参数名 | 范围 | 含义 |
| --- | --- | --- |
| `breadth_drop_threshold` | {-0.03, -0.05, -0.07} | 单转债当日跌幅低于此 = 算"大跌" |
| `breadth_panic_ratio` | {0.20, 0.30, 0.40} | 池中"大跌"转债比例超此 = 触发风险态 |
| `breadth_risk_recovery_days` | {1, 2, 3} | 风险态下的 recovery_days |
| `breadth_risk_switch_hurdle_pct` | {0.05, 0.08, 0.10} | 风险态下的 switch_hurdle_pct |
| `breadth_normal_to_risk_min_days` | {1, 2} | 风险态触发的最少连续天数 (防 1 天误报) |

正常态固定: `recovery_days=4, switch_hurdle_pct=0.15` (medium baseline, 跟 regime switch 研究一致以便对比)

## Grid 设计

- 维度 1: breadth_drop_threshold ∈ {-0.03, -0.05, -0.07} (3 个)
- 维度 2: breadth_panic_ratio ∈ {0.20, 0.30, 0.40} (3 个)
- 维度 3: breadth_risk_recovery_days ∈ {1, 2, 3} (3 个)
- 维度 4: breadth_risk_switch_hurdle_pct ∈ {0.05, 0.08, 0.10} (3 个)
- 维度 5: breadth_normal_to_risk_min_days ∈ {1, 2} (2 个)
- **总候选**: 3 × 3 × 3 × 3 × 2 = **162**

## 评估指标

主指标 (4 主底线, 跟 regime switch 一致):
- replay_2020 ≥ -0.138588
- replay_2021 ≥ -0.033534
- replay_2022 ≥ 0.028891
- replay_2023 ≥ -0.027744

辅助指标:
- selection_avg_excess
- **风险态切换日分布** (新机制必检): 期望 2020-02 立刻触发, 不像 regime switch 那样滞后 60 日
- 风险态总天数比例 (期望 < 20%, 避免 regime switch 那种 51.9% 广义 mode switch)

## leave-N-out 设计

- holdout 年: 2019/2020/2021/2022/2023/2024 (6 个)
- 每年单独跑 search (6 × 162 = 972 候选-年任务)
- top1 CV: 用 leave-Y 训练取 top1, 看 Y 表现 vs baseline
- 采用门槛: ≥ 5/6 holdout 改善或不退化

## Stop conditions

- 第 1 轮 grid 0 候选过 4 主底线 → 停, 写 retro
- > 50% 候选过 floor → 表面通过, 必跑 5 项质疑
- 真 CV < 5/6 → 信号不够泛化, 写"已确认无效"
- 修复尝试 2 次仍 fail → 写"已确认无效"
- 总算力预算 ¥30 → 暂停

## 算力估算

- 162 候选 × 6 holdout × ~30s = ~8.1 hr sig
- spot (16 核) ~ 8.1 / 8 ≈ **60 分钟**
- spot 协议: ≥ 2 hr 必起 → 这次 60 分钟 = 介于 30-120 之间 = **建议起 spot**

## 预算上限

- 总算力预算: ¥30 (按 spot ~60 min × 单价 ~¥0.5/min ≈ ¥30)
- 总时间预算: 2 小时

## 必出产物

- `data/cb_arb_market_breadth_panic_2026-05-15/ranked.csv`
- `summary.csv`
- `trades.csv` (+ baseline trades.csv, per L5 schema 改进 backlog)
- `daily_equity.csv` (+ baseline daily_equity.csv)
- `trigger_dates.csv` (新机制必出, 含触发当日 breadth 截面统计)
- `reports/cb_arb_market_breadth_panic_2026-05-15.md`

## 已有脚本和函数 (供 Codex 复用)

- evaluator: 基于 `scripts/evaluate_cb_arb_selfpnl_regime_switch.py` 改造 (替换 regime classifier 输入: 自身 rolling excess → market breadth daily 截面)
- search 脚本: 基于现有 grid search, 增加 1 个维度 (min_days)
- baseline kind: medium_opportunity (recovery=4 hurdle=0.15)
- breadth 数据源: **待 L1.5 验证** — 期望 sig 上 cb_arb 用的转债池每日 OHLC 文件含 daily_return 列, 路径 TBD

## 数据可用性 (L1.5 数据预检 — Codex 跑)

- breadth 输入: 转债池每日 close-to-close 涨跌幅, 跨 2019-01 to 2024-12, 池子 ~300-400 转债
- 期望路径: sig 上 `data/cb_arb_concurrent_supervised_20260511_094500/` 子目录或 `cb_pool` 类似名
- 缺失率期望: < 1%
- lookahead: 当日截面统计, 当日决策只用 T-1 数据 (i.e. yesterday's breadth → today's regime) — Codex 评估是否允许"当日 close 后立刻 regime 切换" (T 日 close 看到 T 日 breadth, T 日操作用 T-0 breadth) — 需要 Codex 在 L1.5 给出意见

## 升级条件

- 算力预估超 ¥30 → 必须 ESCALATE 用户
- breadth 数据源不存在 / 缺失率 > 5% → ESCALATE 用户改方向
- 中途发现 lookahead 风险 → stop + RESPONSE
- 用户离场 ≥ 30 分钟且 spot 在烧 → auto-shutdown
