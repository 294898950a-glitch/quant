# cb_arb medium_recovery3_hurdle0p10 跨年验证复盘

日期: 2026-05-15
对象: `cb_arb_concurrent_supervised_20260511_094500` + medium signal recovery_days/switch_hurdle_pct 调整

## 研究问题

medium opportunity 信号在 2021 年退出节奏过粘, 导致 2021 年累计 fail floor (-3.35%). 想验证: 调小 recovery_days (从 4 调到 3) 和调小 switch_hurdle_pct (从 0.15 调到 0.10), 能否让 medium 同时保住 2020 不出血 + 修好 2021, 并且不伤其他年.

## 假设 ladder (强→弱)

1. 弱版本: 改这两参数能让 2021 不退化
2. 中版本: 2021 改善 + 2020 完全保留
3. 强版本: 2021 改善 + 2020 完全保留 + 跨 4 个留出年都过主底线 (作为无条件全年部署)

最后命中: 中版本通过, 强版本失败.

## 数据范围

- 数据源: data/cb_arb_concurrent_supervised_20260511_094500
- 参数维度: recovery_days × switch_hurdle_pct, 5×5 = 25 组合
- 验证年份: 2020 / 2021 (Round 4 网格); 2019 / 2022 / 2023 / 2024 (Round 5 跨年留出)
- 硬约束: replay_2020 ≥ -0.138588, replay_2021 ≥ -0.033534
- 算力: sig VM 跑 Round 4 + Round 5 (2 vCPU, 长开零边际); 没起 spot
- 协作: 用户 (拍板) + Claude (质疑 + 框架) + Codex (跑回测 + 健全性检查)

## 第 N 轮发现

### Round 4 - 5×5 参数网格

| 指标 | 结果 |
| --- | --- |
| 候选总数 | 25 |
| 同时过 2020/2021 底线 | 3 (recovery=3 配 hurdle ∈ {0.10, 0.12, 0.15}) |
| 排名 1 (按 selection_score) | recovery=3 hurdle=0.15 |
| Claude 选 | recovery=3 hurdle=0.10 (筛后再排, 排除 hurdle=0.15 漏选 2020 重要仓位的可能) |

关键发现: hurdle 在 0.10-0.15 间存在 path-stable 子区. 不是 hurdle 越宽收益越高, 是 hurdle 适中 + recovery 短才同时稳 2020 + 修 2021.

### Round 4.5 - L4 5 项质疑

按 questioning_checklist 跑了 Q1-Q5 + 条件 Q7:

- Q1 (网格是否够密): ✓ — 5×5 已覆盖关键转折区
- Q2 (selection_score 是否编码 2021 floor): ⚠ — 不编码; 改用"filter-then-rank" (先过两条 floor 再按 selection_score 排) 解决
- Q3 (baseline 是否对齐): ✓ — medium baseline 来自同一回测产物
- Q4 (recovery=4 应不应该比 recovery=3 好?): ⚠ — r4 反而比 r3 差, 来自路径污染 (晶科转债 -¥21871 case: r4 留太久错过更好的退出时机)
- Q5 (2020 trade list 是否完全一致): ✓ — 跟 medium baseline 0 trade diff
- Q7 (2021 改善来源是新仓 vs 路径优化): 路径优化 (+¥23480), 新进仓反而拖累 (-¥2545) — 也就是改善不来自更多机会, 来自相同仓位更聪明的退出

### Round 5 - 4 个留出年跨年验证

| 留出年 | r3_h010 | medium baseline | 过主底线 | replay_2020 | selection_delta |
|---|---|---|---|---|---|
| 2019 | 0.168 | 0.164 | ✓ | -0.139 | +0.004 |
| 2022 | 0.017 | 0.029 | ✗ | -0.139 | +0.007 |
| 2023 | -0.028 | -0.028 | ✗ | -0.139 | +0.005 |
| 2024 | 0.028 | 0.017 | ✓ | -0.139 | +0.003 |

通过率 2/4 — 不达"4/4 才采用"的事先约定门槛.

详细数据: `data/cb_arb_concurrent_supervised_20260511_094500/round5_cross_year_r3_h010_20260515_0033/`
决策记录: `data/research_framework/decisions.jsonl`

## 整体判断

**结论**: 拒绝采用 (HDRF L6 出口 = 拒绝归档).

理由:
1. 主底线通过率 2/4, 不到事先约定的 4/4 — 这是事先定的硬门槛, 不能事后放松
2. 2022 退化最严重 (0.017 vs medium 0.029, 退 1.14pp), 在该年算"伤"
3. 虽然 2020 完全保留 (-0.139 全程不变) + selection_avg 4 年全部小幅改进, 但主收益指标的年份分裂说明这组参数对 regime 敏感, 不适合无条件全年部署
4. medium signal 在 2021 的"粘性"问题, 这条单参数路径已确认走到底

**真正确认的发现** (有研究价值, 留档):
1. medium signal 的 2021 退出问题, 通过 recovery_days=3 + switch_hurdle_pct=0.10 可以局部修好 (2021 baseline -3.35% → 改善后 -1.26%)
2. 2021 改善不来自更多机会捕捉, 而来自共同仓位的路径优化 — 这意味着 medium 在 2021 选股没问题, 是退出时机问题
3. 同一组参数在 2022/2023 反向退化 — medium signal 的"理想退出参数"跨年不稳定
4. recovery_days 非单调 — recovery=3 比 recovery=4 好, 不是"等越久越好", 路径污染是真实风险

**已确认无效方向** (留档, 不要再投入算力):
- medium_recovery3_hurdle0p10 (或同族任何全年统一参数) 作为无条件全年部署 ✗ — 主底线通过率 2/4, 跨年不鲁棒

**未来值得探索方向**:
1. **看年份切换 (regime switch)** — 同一组 medium signal 参数, 按市场状态 (vol / 信用利差 / cb_arb 自身近 N 日 PnL) 切换 hurdle 和 recovery
2. **medium signal 触发归因** — 2022 退化的具体是哪些仓位 / 触发了什么不该触发的条件 (待 trade-level 反向诊断)
3. **panic signal 重新设计** — medium 这条路饱和, 考虑回头从 panic detector 入手

## 算力成本

- spot 跑了: 0 小时 (Round 4 + Round 5 都跑在 sig VM 上, 4 回测 ×30s ≈ 2 分钟)
- sig 跑了: ≈ 10 分钟 (长开, 边际成本 ~ 0)
- 单次研究成本: 接近零, 主要在协作往返时间

## 后续待办

- [ ] 关 spot VM (已起但本研究没用, 等用户确认是否关)
- [x] 把 "已确认无效方向" 写入经验账本分区二
- [ ] 下次研究方向: regime switch 机制设计 (走完整 HDRF 流程)
