# cb_arb breadth + pool-mean confirm ensemble 复盘 (拒绝归档 + 架构反思)

日期: 2026-05-15
对象: `cb_arb_breadth_confirm_ensemble_2026-05-15` + 双信号 AND 触发

## 研究问题

market breadth 单 signal (`reports/cb_arb_market_breadth_panic_retro_2026-05-15.md`) 在 2024 有 5 个 false positive trigger (-¥16062 path 污染). 本研究加 pool-mean confirm 作 AND 第二信号, 期望过滤"分化跌但整体不跌"型 false positive.

## 假设 ladder (强→弱)

1. 弱: pool-mean confirm 减少 2024 trigger 数 (< 5)
2. 中: 2024 改善 + ≥ 5/6 holdout 通过
3. 强: 全过 4 hard floor (recalibrated v1.1)

**结果**: 全部失败. pool-mean confirm **没有减少** 2024 trigger — 5 个原 false positive 日期 pool mean **都低于 -0.005** (AND 也触发).

## 数据范围

- 数据: 复用 `cb_daily.parquet`
- 6 维 grid: drop_threshold × panic_ratio × recovery × hurdle × min_days (固定 1) × **pool_mean_threshold (新)** = 108 候选
- hard floor v1.1 (重校准): 2020 ≥ -0.131, 2021 ≥ -0.050, 2022 ≥ 0.014, 2023 ≥ -0.031
- 算力: sig VM 1.5 hr, < ¥3 (sig 长开零边际, spot 没起)

## 第 N 轮发现

### Round 1 - 648 任务

| 指标 | 结果 |
| --- | --- |
| 0/108 过 4 floor | 跟前 2 batch 一致 |
| 最佳过 3/4 (96 候选) | 比 market breadth 单 (2/4 best) 进步, 但仍 0 全过 |
| CV top1 holdout 通过率 | 3 / 6 (2021/2022/2023 pass, 2019/2020/2024 fail) |
| 选定参数 | 待 ranked.csv 看 |

详情几乎逐位等于 market breadth 单 signal:
| 年 | ensemble | breadth-only | 差 |
|---|---|---|---|
| 2019 | 0.150459 | 0.150459 | **0** |
| 2020 | -0.136525 | -0.136525 | **0** |
| 2021 | -0.037862 | -0.037862 | **0** |
| 2022 | 0.019834 | 0.023088 | -0.003 |
| 2023 | -0.029066 | -0.029066 | **0** |
| 2024 | 0.014018 | 0.014018 | **0** |

### Round 2 - 关键发现

**pool-mean confirm 没有起作用** (Codex L3 RESPONSE):
- 2024 上次 5 个 false positive 日期 (01-22, 02-05, 02-28, 06-24, 10-09)
- pool mean 在这 5 个日子 **都低于 -0.005** (confirm threshold)
- AND 触发, ensemble = breadth 单 signal 在 2024 行为一致

**根因**: confirm 跟 breadth 高度相关 — 当 N% 转债跌 ≥3% 时, pool-mean 必然也下跌 (数学上 correlation = -0.71). 但 corr ≠ 同方向阈值, Codex L1.5 时已警告"2024 false positive 也有 pool mean -0.017 to -0.043". 实际跑跟警告一致.

## 整体判断

**结论**: 拒绝归档 (HDRF L6 出口 A).

**架构层反思 (3 batch 同 pattern)**:

3 次研究 panic signal family 已穷举:
1. self-PnL rolling (regime switch): lookback fundamental 滞后, 0/108 全过, CV 3/6
2. market breadth (cross-section): 触发对齐 ✓ 但单 signal, 0/162 全过, CV 3/6
3. breadth + pool-mean confirm (ensemble AND): 触发对齐 ✓ 但 confirm 跟 breadth 相关, 0/108 全过, CV 3/6

**所有 3 次都 fail 2019/2020/2024,数字几乎相同**.

这意味着:
- panic signal family 不是问题(3 种都覆盖了)
- 真正问题在 **action mapping**:激进收手 (recovery=1 hurdle=0.05) 在 2024 false positive 触发时打 path
- 或 **更深策略局限**:cb_arb 在 panic 后反弹年(2024 类) 本质不适合"识别 panic 就收手"

**已确认无效方向** (经验账本):
- 单 / 双 / 多 panic signal AND-trigger + 同种 action mapping (recovery + hurdle 调) 救不了 cb_arb 跨年泛化
- AND-ensemble 当两 signal 高相关时退化为单 signal

**未来值得探索方向**:
1. **改 action mapping** — 不调 recovery+hurdle, 试 position scaling / hedge overlay / pair trade
2. **多 signal OR / weighted** — 而不是 AND (OR 减少 missed panic, 不增 false positive 风险)
3. **2024 单独研究** — 看 2024 真实 vs false panic 的市场状态差异
4. **重新审视 cb_arb 本身** — 是否需要重新设计基础策略, 而不只调 panic 模块

## 算力成本

- sig VM: 1.5 hr (零边际)
- spot: 没起
- 总: 实际 ¥0 (sig 长开)
- 累计 3 batch 总 ¥8 (¥90 总预算的 9%, 远低于预算)

## 后续待办

- [x] sig 运行结束 (无 spot 需关闭)
- [x] 经验账本 "AND-ensemble 退化" 已确认无效
- [x] 经验账本加"3 batch 同 pattern + action mapping 是真问题"高优
- [ ] 用户决策:换 action mapping family / 2024 单独研究 / 重审 cb_arb
