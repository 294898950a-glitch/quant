# cb_arb regime switch 机制 复盘 (拒绝归档)

日期: 2026-05-15
对象: `cb_arb_regime_switch_2026-05-15` + self-PnL rolling-excess regime detector

## 研究问题

Round 5 (reports/cb_arb_round5_retro_2026-05-15.md) 显示 medium signal 的"理想退出参数"跨年不稳定 — 同一组参数 2020 完全保留但 2022/2023 退化. 本研究想验证: 用 cb_arb 自身近 N 日累计 excess return 判断 regime, 风险态下用更短 recovery + 更紧 hurdle, 能否让 medium signal 在 2020/2022/2023/2024 同时过主底线.

## 假设 ladder (强→弱)

1. 弱: 风险态切换能改善任一年表现, 同时其他年不退化
2. 中: 2020 floor 保 + 2021/2022/2023/2024 改善
3. 强: 6 holdout 年全部通过主底线 (≥ 5/6 采用门槛)

**结果命中**: 0 个版本通过. 弱版本都没达成 — 2019/2020/2024 全退化.

## 数据范围

- 数据源: `data/cb_arb_concurrent_supervised_20260511_094500/phase_loss_review/current_2019_2024_equity_vs_benchmark.csv` (benchmark_return 列) + in-run baseline derivation
- 参数维度: regime_lookback_days × regime_risk_threshold × regime_risk_recovery_days × regime_risk_switch_hurdle_pct = 108 候选
- 验证年: 2019/2020/2021/2022/2023/2024 (6 holdout)
- 硬约束: replay_2020 ≥ -0.138588, replay_2021 ≥ -0.033534, replay_2022 ≥ 0.028891, replay_2023 ≥ -0.027744
- 采用门槛: ≥ 5/6 holdout 改善或不退化
- 算力: spot 7 分钟 (远低于估的 41 分钟), 实际花费 < ¥5 (远低于 ¥30 预算)
- 协作: 用户 (架构 + 预算拍板) + Claude (spec + L4 + L6) + Codex (L2 实施 + L3 spot + L5 反向诊断)

## 第 N 轮发现

### Round 1 - 108 候选 grid 跑完

| 指标 | 结果 |
| --- | --- |
| 候选总数 | 108 |
| 过 4 个主底线 | **0** |
| 最多过几个 floor | 3 / 4 |
| CV top1 | selfpnl_lb60_thr0p01_rec1_h0p05 (最激进收手) |
| CV top1 holdout 通过率 | 3 / 6 (2021/2022/2023 pass, 2019/2020/2024 fail) |
| adoption_pass | **false** (< 5/6) |

2020 是双重 fail: selected -0.142 < baseline -0.131 (退化) + 跌穿 floor -0.139.

### Round 2 - L4 5 项质疑

- Q1 网格 ✓ — 108 已够密
- Q2 selection_score ⚠ — CV top1 是 average-best 不是 floor-pass-best; 但因 0/108 过 floor, 改 filter-then-rank 也救不了
- Q3 baseline 对齐 ✓ — spec v1.1 normal-state params
- Q4 monotonic ✗ **关键反直觉** — 最激进收手 (recovery=1, hurdle=0.05) 反而 2020 fail. 猜测: lookback=60 滞后, 收手时高峰已过, 激进反加大损失

### Round 3 - L5 反向诊断 (Codex sig 上跑)

**Q6 触发时点 — 滞后猜测完全验证**:
- 2020 第一次 risk-state 进入 = **2020-04-03**
- 距 panic 起点 2020-02-03 = 60 日历日 / 44 交易日
- 2020-02-03 panic 当天 selected 跌 -5.45%, regime 还是 normal 态
- 等 04-03 风险态触发时, 大跌已结束 ~6 周

**Q7 lookahead — 实施正确 + 但路径滞后** (lookback 本身固有, 不是 bug):
- T 日 classification 用 T-1 及更早数据 (代码: `daily_excess.shift(1).rolling(lookback_days).sum()`)
- 没有同期泄漏
- 但 60 日累计窗口意味着 ≥30 日响应滞后

**Lookback 对比 — 反直觉**:
- lookback=10 反而比 lookback=60 在 2020 更差 (best -0.146 vs -0.140)
- 更快反应 = 风险态触发更频繁 = 错过反弹 = 更亏
- 同一 family 调参不解决问题

**触发频率 — 不是 panic detector 是 mode switch**:
- 总 risk-state 天数比例 51.9% (755/1456 天)
- 2020 高达 74.9% (182/243)
- 2021/2022/2023 也 40-60%
- "panic detector" 命名跟实际行为完全不符 — 这是 broad mode switch

## 整体判断

**结论**: 拒绝归档 (HDRF L6 出口 A).

理由:
1. 0 / 108 候选过 4 个主底线, 最佳也只过 3/4 — 比 Round 5 的 r3_h010 还差 (那个至少 6 holdout 过 4)
2. 真 CV 6 holdout 通过率 3/6, 远低于 5/6 采用门槛
3. **lookback-based 自身 PnL regime detector 这类方法本身有 fundamental 滞后**: 用过去 N 日累计判断 panic, 必然滞后真实 panic ≥ 30 日; 这不是参数调优能解决的, 是方法论局限
4. lookback=10/20/60 三档都没救 2020 → 同 family 调参彻底没救
5. 当前实施实际是 "broad path-dependent mode switch" (51.9% 天数 risk 态), 不是 targeted panic detector

**真正确认的发现** (有研究价值, 留档):
1. cb_arb 自身近 N 日累计 excess 作为 regime classifier, 滞后 ≥ 30 日真实 panic
2. 即使 lookback 缩短到 10 日, 2020 表现反而更差 (false positive 频率提高错过反弹)
3. spot 实际 7 分钟跑完 108×6 grid, 远低于估的 41 分钟 (in-run baseline derivation 设计有效)
4. trade-level overlap 分析需要 baseline trades.csv 一同导出 (L3 schema 改进 backlog)
5. selection_score 不带 hard-floor 约束时, CV top1 可能选 floor-fail 候选 (filter-then-rank 比 sort-only 更稳, 但 0/108 时也救不了)

**已确认无效方向** (经验账本"已确认无效"区, B5 不准再提):
- cb_arb 自身 PnL rolling 累计 excess 作 regime classifier, 任何 lookback (10/20/60) 任何 threshold 任何风险态参数 → 不能解决 medium signal 的跨年泛化问题
- 同 family 调参不解决滞后, 也不解决 51.9% over-trigger

**未来值得探索方向**:
1. **Exogenous panic signal** — 不依赖自身 PnL 滞后, 用市场即时信号:
   - 转债池跌停 / 涨停转债比例 (market breadth)
   - cb 隐含波动率突变 (vol surge, 实时计算)
   - 信用利差跳变 (credit spread spike, 中证信用)
   - 大盘指数同日跌幅 (CSI500 已试, 已确认无效; 可考虑 HS300 + 创业板)
2. **Forward-looking trigger** — 不用 lookback, 用结构性突变检测 (PELT / Bayesian online changepoint)
3. **Multi-signal ensemble** — 多个独立 signal 联合 (any-fire trigger), 减少单 signal 滞后

## 算力成本

- spot 跑了: ~7 分钟 (10:33 起 - 10:34 修文件 - 10:41 完成 + 关), 算力费 < ¥5
- sig 跑了: L2 实施 9 分钟 + L5 反向诊断 3 分钟 ≈ 12 分钟 (长开零边际)
- 总成本: < ¥5, 远低于 ¥30 预算
- 单次研究产出 vs 成本: 高效率, 即使结论 fail 也快速决断

## 后续待办

- [x] 关 spot VM (Codex 10:41 已关, ins-5lb9zo12 STOPPED)
- [x] 把"已确认无效方向"写入经验账本分区二
- [x] L5 side findings 写入经验账本分区三 (artifact schema 改进 backlog)
- [ ] 下次研究方向: 在 "未来值得探索" 里挑一个 (建议优先 vol surge 或 market breadth)
- [ ] L3 schema 改进: 加 baseline trades.csv + daily_equity.csv 导出 (Codex L5 提的 side finding)
