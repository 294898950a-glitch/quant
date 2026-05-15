# cb_arb market breadth panic detector 复盘 (拒绝归档 + floor 重校准 ESCALATE)

日期: 2026-05-15
对象: `cb_arb_market_breadth_panic_2026-05-15` + 转债池跌幅截面比例 panic detector

## 研究问题

`reports/cb_arb_regime_switch_retro_2026-05-15.md` 证明 self-PnL rolling regime classifier 有 fundamental 滞后 (2020 首次触发距 panic 起点 60 日历日). 本研究换 signal family: 用转债池当日跌停 / 大跌转债比例 (cross-sectional, 截面统计) 作 exogenous panic signal, 期望解决滞后 + 减少 false positive.

## 假设 ladder (强→弱)

1. 弱: market breadth 比 self-PnL rolling 强 (touched: 2020-02-03 准时触发 + risk-day 3.7% vs 51.9%)
2. 中: market breadth 单 grid 让 cb_arb 跨年表现改善, ≥ 5/6 holdout 不退化
3. 强: 6 个 holdout 都过 4 个 hard floor

**结果**: 弱版本通过 (架构层改善确认), 中/强失败 (0/162 过 4 floor, CV 3/6).

## 数据范围

- 数据源: `data/cb_warehouse/cb_daily.parquet` (543,589 行, 2019-2024, 872 转债, 0 缺失)
- 池子规模: min 111 / median 377 / max 548 转债 / 日
- 5 维 grid: drop_threshold × panic_ratio × recovery × hurdle × min_days = 162 候选
- 6 holdout × 162 = 972 任务
- 算力: spot 10 分钟, 实际 < ¥3 (¥30 预算用 10%)
- 协作: 用户 (架构 + 预算) + Claude (spec + L4 + L6) + Codex (L1.5 + L2 + L3 + L5)

## 第 N 轮发现

### Round 1 - 162 候选 grid

| 指标 | 结果 |
| --- | --- |
| 候选总数 | 162 |
| 过 4 个主底线 | **0** |
| 最佳过几个 floor | 2 / 4 |
| floor 分布 | 0 floor: 2, 1 floor: 18, 2 floor: 142 |
| CV top1 | breadth_dropm0p03_ratio0p2_rec3_h0p08_min1 (最宽 trigger) |
| CV top1 holdout 通过率 | 3 / 6 (2020/2021/2022 vs baseline pass, 但只 2020 above floor) |
| adoption_pass | **false** |

### Round 2 - L4 5 项质疑 + Q6/Q7

- Q1 grid 密度 ✓
- Q2 selection_score ⚠ — `passes_main_floors` boolean + score, 但 0 候选过, 退化为纯 score 排序
- Q3 baseline 对齐 ✗ **架构层警告** — 当前 baseline 自己 fail inherited 2021/2022/2023 floor
- Q4 monotonic ⚠ — selected 是最宽 trigger (-3%, 20%, min_days=1), targeted 但 path-sensitive 不单调
- Q5 trade overlap ✓ (L5 schema 修复后第一次):
  - 2020: 90 base / 76 sel, 42 共同, 12 改 exit, PnL gap -5919
  - 2021: 116 / 152, 68 共同, +12576 改善
  - 2022: 149 / 174, 101 共同, +8662 改善
  - 2024: 255 / 263, 233 共同, 27 改 exit, **PnL gap -16062 (path 污染)**
- Q6 触发时点 ✓ — 2020-01-23 signal → 2020-02-03 effective (T+1 lag), **滞后问题解决**
- Q7 路径污染 ✓ — T+1 lag 严格, no same-day leakage

### Round 3 - L5 反向诊断 + Floor 重校准证据

**架构层重大发现 — 4 个 floor 是混合来源**:

| Floor | 来自 baseline |
| --- | --- |
| 2020 ≥ -0.138588 | 旧 medium_opportunity (recovery=2, hurdle=0.12) |
| 2021 ≥ -0.033534 | **current_best_no_opportunity / weight_mid_100_70_40_10** (不同 baseline!) |
| 2022 ≥ 0.028891 | 旧 medium_opportunity |
| 2023 ≥ -0.027744 | 旧 medium_opportunity |

**当前 baseline (spec v1.1 recovery=4 hurdle=0.15) 6 年精确**:
- 2019: 0.161312
- 2020: -0.130604 (**pass** inherited floor by +0.008)
- 2021: -0.050441 (**fail** inherited floor by -0.017)
- 2022: 0.014425 (**fail** inherited floor by -0.014)
- 2023: -0.031027 (**fail** inherited floor by -0.003)
- 2024: 0.030085

**Floor 重校准影响**:
- 用现 inherited (mixed): selected 通过 1/4
- 用当前 baseline 重校准: selected 通过 3/4 (还 fail 2020 by -0.006)
- 用旧 medium_opportunity 重校准: selected 通过 2/4
- **任何 floor 集** 都救不了 — full grid 仍 0/162 全过

**2024 fail 是 path contamination**: 5 个触发窗口都不在大 panic, 但替换 opportunity-date 集导致后续 common-trade path 受损 (-16062 PnL gap).

## 整体判断

**结论**: 拒绝归档 (HDRF L6 出口 A).

理由:
1. 0 / 162 过 4 main floor (跟 regime switch 同样 0/X)
2. CV 6 holdout 通过 3/6 (跟 regime switch 同样数字)
3. 即使 floor 重校准也救不了 (0/162 全过, 任何 baseline floor 集)
4. 2024 严重 path contamination (-16062 PnL gap), 即使触发对齐 + targeted 也无法 net positive

**真正确认的发现** (有研究价值, 留档):
1. **market breadth 比 self-PnL rolling fundamentally 强**: 2020-02-03 准时触发 (vs regime switch 滞后 60 日历日)
2. **market breadth targeted, not sticky**: 3.7% 天数触发 (vs regime switch 51.9%)
3. **架构层发现 — 4 个 hard floor 混合来源**: 2021 floor 来自 different baseline (current_best_no_opportunity), 其他 3 个 floor 来自 medium_opportunity. 这是历史遗留架构 mess.
4. **当前 spec v1.1 normal-state baseline 自己 fail 3 个 inherited floor**: floor 跟 baseline 不对齐
5. **Q5 trade overlap 修复 — L5 schema 改进有效**: 现在能精确算 baseline vs selected 重叠 / 改 exit 数

**已确认无效方向** (经验账本"已确认无效", B5 不准再提):
- breadth-only threshold + action grid (162 候选, 5 维) — 单 signal + 单 action mapping 救不了 cb_arb 跨年泛化

**未来值得探索方向**:
1. **架构层 floor 重校准** — 用单一 baseline (spec v1.1 或重新校准) 取代 4 floor 混合来源 (用户拍板)
2. **多 signal ensemble** — market breadth + vol surge + credit spread 等多 signal 联合
3. **改 action mapping** — 不只调 recovery + hurdle, 试不同 action (e.g. position scaling, hedge overlay)
4. **breadth + market index combo** — Codex L5 推荐, breadth 跟 CSI500 / HS300 联合

## 算力成本

- spot: 11:47 起 - 12:00 关, 13 分钟, ~ ¥3 (¥30 预算用 10%)
- sig: L1.5 + L2 + L5 ≈ 15 分钟 (长开零边际)
- 总: < ¥3
- 单次研究产出: 即使 fail 也快速决断 (用户预算 ¥30 实际只花 ¥3)

## 后续待办

- [x] 关 spot VM (Codex 12:00 已关, ins-5lb9zo12 STOPPED)
- [x] 经验账本分区二: "breadth-only threshold/action grid" 加为已确认无效
- [x] 经验账本分区三: "多 signal ensemble" + "架构层 floor 重校准" 加为高优
- [ ] **架构层 ESCALATE 用户**: floor 重校准要不要做, 用哪个 baseline 作基准
- [ ] 下次研究方向: 等用户 B 决策, 然后可能跑 multi-signal ensemble 或换 action mapping
