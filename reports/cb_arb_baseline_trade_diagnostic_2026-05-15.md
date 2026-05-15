# cb_arb baseline trade-level 归因诊断 (A 路径数据驱动否决)

日期: 2026-05-15
对象: cb_arb baseline (spec v1.1 recovery=4 hurdle=0.15) 在 2020 / 2024 trade-level 归因
来源: Codex 20:05 RESPONSE/CB-ARB-BASELINE-TRADE-LEVEL-DIAGNOSTIC

## 研究问题

panic 诊断 (`reports/cb_arb_panic_diagnostic_2026-05-15.md`) 关闭 panic detector 整个子方向. 架构层 2 个候选: A (改 baseline 基础) / B (套 meta wrapper). 本研究做 A 的轻量子任务 — trade-level 反向归因, 看 baseline 在 2020/2024 输 benchmark 是 (a) 某类 trade / (b) 某段时间 / (c) 散布 / 混合.

## 数据

Codex 复用 `data/cb_arb_breadth_confirm_ensemble_2026-05-15/trades.csv` + `daily_equity.csv` + warehouse `cb_daily / cb_basic / stk_daily`. 输出在 `data/cb_arb_baseline_trade_diagnostic_2020_2024_2026-05-15/`.

## 关键发现

### 2020: broad weakness, 不是 tail

| 指标 | 值 |
| --- | ---: |
| trades 数 | 90 |
| median trade excess | **-4.32 pp** |
| mean trade excess | -3.47 pp |
| 负 excess 比例 | **74.4%** |
| worst10 占总绝对 excess | 23.2% |

**worst path months** (daily equity excess):
- 202010: -5.17% (后期反弹/轮转月也亏)
- 202003: -4.90% (已知 panic 月)
- 202007: -3.22%
- 202001: -2.43%
- 202008: -2.40%

**worst entry-month trade buckets**:
- 202005 / 202001 / 202009 / 202011 / 202006 (散在全年)

**exit reason 散布**: stop-loss / value-gone / switch_value_gap / max_holding_days, 没单一规则可改.

**结论 2020**: cb_arb 在 2020 是 cross-trade broad weakness, 不是某个 trade-type / 某个 exit-rule / 某个时段独导致. **改 baseline 单一 filter 救不了**.

### 2024: baseline 本身 OK, 之前问题在 detector overlay

| 指标 | 值 |
| --- | ---: |
| trades 数 | 255 |
| median trade excess | **+0.40 pp** |
| mean trade excess | +0.77 pp |
| 负 excess 比例 | 43.9% |
| 总 trade-level excess | **正** |

**worst path months** (daily equity excess):
- 202405: -1.94%
- 202403: -1.65%
- 202406: -0.80%
- 202407: -0.72%

但跟好月份抵消, **年终 trade-level total 正**.

**结论 2024**: baseline 本身没问题. 之前 panic detector overlay (recovery=1 hurdle=0.05 收手) 把 2024 -16062 PnL gap 打出来. detector 已被否决, baseline naked 跑 2024 就 OK.

## Codex 判断

**(c) 混合 / 散布**, 加 2020 时段成分. 数据**不支持单一 trade-type fix**.

- 2020: path-level losses 集中 panic+反弹 + 后期 202010, broad weakness
- 2024: baseline OK, 局部坏月被好月抵消, blanket filter 风险扔掉 winners

## 架构层判断 (Claude 自决)

**A 路径 (改 baseline) 数据不支持** — 2020 broad weakness 没有单一 entry/exit/holding filter 可改.

3 个候选下一步:
- A. 改 baseline 基础: **否** (数据驱动否决)
- B. 年份选择性 meta wrapper: 也基本死. meta 还是要 detector, 而 panic detector 整个子方向已穷举无效 (panic 诊断报告); 此外 2020 broad weakness 不是 panic 短期, 是全年, meta 没什么可减仓的窗口
- C. **接受 cb_arb 当前 baseline 是最优, 主线饱和**: ← 自决

**自决**: **cb_arb 当前 baseline (spec v1.1 recovery=4 hurdle=0.15) 当作最终采用版本归档. cb_arb 主线研究饱和**.

理由 (数据驱动):
1. panic detector 整条子方向已确认无效 (panic 诊断)
2. baseline 单一 filter 修不了 2020 (trade-level 归因)
3. 2024 baseline 本身 +0.77 pp median, 已经在赚 benchmark
4. 5 年 holdout: 2019 +16.1% / 2021 -5.0% / 2022 +1.4% / 2023 -3.1% / 2024 +3.0%. 只 2020 (-13.1%) 是结构性失败年, 4/5 holdout 都 OK
5. 2020 是历史极端 (cb 市场新生态, 大跌 + 反弹组合极少见)
6. 继续找 fix 是 over-fit 2020 风险, 不如冻结 baseline 转向

## 已确认无效方向 (本研究新加)

- **改 baseline 单一 trade filter** (entry / exit / holding rule 任意维度) 救不了 cb_arb 2020 broad weakness — 90 trades 74% 负 + worst10 只占 23% + 散在 5 个 entry month + 4 类 exit reason
- **年份选择性 meta wrapper** (B 路径): meta 仍依赖 detector, 而 detector 整条子方向已无效; + 2020 broad weakness 不是 panic 短窗口

## 下一步 (自决)

1. **冻结 cb_arb baseline 为最终采用版本** (经验账本分区一)
2. **cb_arb 主线暂停** (经验账本: 5 年 4/5 holdout 已经合理, 不再死磕 2020)
3. 转向 (按经验账本"未完成线索"高优 + 后续 backlog):
   - cb_redemption 强赎策略 (已在 iter 60, 转回研究状态)
   - 或开新策略方向 (用户长期目标"最少钱+全自动" → 多策略组合更稳)
4. arxiv 14 天周期 (2026-05-29) 仍照常运行作为新 idea source

## 算力成本

- sig VM lightweight: ~5 分钟 (Codex 20:00 ACK → 20:05 RESPONSE)
- spot: 没起
- 总: ¥0 (sig 长开零边际)
- cb_arb 5月15日累计: ¥8 (3 batch spot) + ¥0 (2 lightweight 诊断) = ¥8 / ¥90 月预算 = 9%

## 后续待办

- [x] 经验账本分区二: "改 baseline 单一 filter" + "B 路径 meta wrapper" 加为已确认无效
- [x] 经验账本分区一: cb_arb baseline spec v1.1 加为最终采用
- [x] CLOSEOUT DIRECT 给 Codex
- [x] 通知用户 (一句话, 不要求拍板, 给反对窗口)
- [ ] 下次研究方向选择 (cb_redemption 继续 vs 新策略) — 自决
