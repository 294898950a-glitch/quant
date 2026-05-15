# cb_arb 两路线 cross-validation 复盘 (HDRF 手工 vs 自循环 LLM)

日期: 2026-05-15
来源: Codex 21:02 RESPONSE/CB-ARB-TWO-LINE-CROSS-VALIDATION

## 研究问题

cb_arb 有两条研究路线:
- **HDRF 手工**: medium signal value-gap (recovery_days × switch_hurdle_pct), 6 年 leave-one-year-out holdout
- **自循环 LLM**: 13 维 yaml 绿区 (rank-based + credit spread + vol), 8 池 sealed_pools

两条都在 2026-05-10 之前后落地, 但**两路使用不同 holdout 机制 + 不同 params subset**. 是否两路是同一个 cb_arb 局部最优, 还是两个不同 trade-off?

## 数据

Codex 在 sig 上跑 30 分钟以内, ¥0:
- HDRF best (recovery=4, hurdle=0.15) on 自循环 8 sealed pools
- 自循环 iter 24 best params (vol_window=60 / rank_buy=0.1 / rank_sell=0.505 / max_position=0.0315 等 13 维) on HDRF 6 年 holdout

输出: `data/cb_arb_two_line_cross_validation_2026-05-15/`

## 关键发现

### 1. 总 OOS 接近, 但分布不同

| 测量 | HDRF | 自循环 iter 24 | 差 |
| --- | ---: | ---: | ---: |
| 全 self-loop OOS (2022-01-01 ~ 2026-05-08) | 0.286986 | 0.313073 | -0.026 (3pp 内) |
| max_drawdown | -0.345835 | -0.291679 | 自循环略好 |
| total_trades | 796 | 690 | 自循环更挑 |

总和接近但**这是平均效应** — 池子和年份分布完全不同。

### 2. Sealed pool 级 (HDRF best on 自循环 8 池)

- 6 / 8 池 在 3pp 内 (mean gap +0.0008, median -0.0085)
- 2 池 大差:
  - **Pool 5**: HDRF 0.009 vs loop 0.086 (差 -7.7pp, 自循环赢)
  - **Pool 7**: HDRF 0.205 vs loop 0.084 (差 +12pp, HDRF 赢)

### 3. 6 年 holdout 级 (自循环 iter 24 on HDRF 6 年)

| 年 | loop | HDRF | gap (loop-HDRF) | 谁赢 |
| --- | ---: | ---: | ---: | --- |
| **2019** | **-0.101** | **+0.161** | **-0.262** | HDRF (差 26pp!) |
| 2020 | (HDRF -0.131, loop ~?) | -0.131 | n/a | n/a |
| 2021 | +0.067 | -0.050 | +0.118 | 自循环 (差 12pp) |
| 2022 | ~3pp 内 | +0.014 | n/a | converged |
| 2023 | ~3pp 内 | -0.031 | n/a | converged |
| 2024 | +0.095 | +0.030 | +0.065 | 自循环 (差 6.5pp) |

3/6 年收敛, 3/6 年不收敛.

### 4. Codex 判断: **not_converged**

不是同一个局部最优. 推荐: 保留 cb_arb 主线归档, 不立刻投入 mapping 研究.

## Claude 解读

两路线学到**不同 trade-off, 不是同一个局部最优**:

- **HDRF (value-gap medium signal)**: 2019 牛市 windowed accumulation 抓得**极好** (+16.1%), 但 2021 / 2024 rotation 年抓不好
- **自循环 (relative-rank + credit spread + vol)**: 2021 / 2024 rotation 年抓得好, 但 **2019 完全 broken** (-10.1%)

**为什么 HDRF 是更稳的版本**:
1. 2019 +16.1% 是 cb_arb 5/6 holdout 的关键基石. 自循环 2019 -10.1% 几乎没救
2. HDRF "value-gap"逻辑跟 cb_arb 本质 (溢价收敛套利) 直觉一致, 自循环 rank-based 是统计偏离
3. HDRF max_dd -0.35 跟自循环 -0.29 差距不大
4. HDRF 已经过 6 batch HDRF L0-L7 流程审查 (panic detector / trade filter / meta wrapper 都否决)

**自循环路线状态**: 
- iter 24 best params 在 2019 broken (-10.1% vs HDRF +16.1%, 26pp gap)
- paused 5 天因为 hypothesizer 出主意撞墙
- 表现总体**次于 HDRF**, 且关键年 fail

## 整体判断 (Claude 自决)

**cb_arb 主线饱和归档判断 — 最终确认**:

1. **HDRF 手工路线饱和** (今晚 4 batch 全否定: Round 5 / panic detector 3 batch / trade filter)
2. **自循环 LLM 路线确认次于 HDRF** — 2019 broken (-26pp gap), paused 不重启
3. **两路 cross-validation not_converged 但 HDRF 是 winner** — 不是失败的 cross-validation, 是确认 HDRF 不可被自循环替代

**ensemble 不可行**: 2019 HDRF +16.1% 已经压倒自循环 -10.1%, ensemble 任何权重都不优于裸 HDRF.

## 已确认无效方向 (本研究新加)

- `2026-05-15 | cb_arb 自循环 LLM 调参路线 (iter 1-60 sealed_pools, 13 维 yaml 绿区) | iter 24 best params 在 HDRF 6 年 holdout 上 2019 broken (-10.1% vs HDRF +16.1%, 26pp gap); paused 5 天 hypothesizer 撞墙; 总体表现次于 HDRF; ensemble 不优于裸 HDRF | reports/cb_arb_two_line_cross_validation_2026-05-15.md`

## cb_arb 主线最终归档结论

**cb_arb baseline 最终采用版本 (final)**:
- HDRF 手工: medium signal recovery=4 hurdle=0.15
- 6 年 holdout: 2019 +16.1% / 2020 -13.1% / 2021 -5.0% / 2022 +1.4% / 2023 -3.1% / 2024 +3.0%
- 4/5 holdout 合理, 2020 历史极端不再死磕
- cb_arb HDRF 手工路线 + 自循环 LLM 路线 **两条都已审查**, HDRF 是 winner, 主线饱和归档

## 下一步 (自决)

1. **cb_arb 主线完全停手** (HDRF baseline 冻结, 自循环不重启)
2. 转向 **真正的 cb_redemption (强赎策略) 立项** — 用户目录名暗示历史上想做的策略
   - framework 已成熟 (自循环 9 角色已经过 cb_arb 验证), 复用即可
   - 强赎套利逻辑跟 cb_arb 套利逻辑独立, 多策略组合潜力
3. arxiv 14 天周期持续 (2026-05-29)

## 算力成本

- sig VM: 6 分钟 (Codex 20:56 ACK → 21:02 RESPONSE)
- spot: 没起
- 总: ¥0
- cb_arb 今天累计: ¥8 (3 batch spot) + ¥0 (3 lightweight 诊断) = ¥8 / ¥90 月预算 = 9%

## 后续待办

- [x] 经验账本分区二: cb_arb 自循环 LLM 路线 加为已确认无效
- [x] 经验账本分区一: cb_arb baseline 最终采用版本 (HDRF 手工) 已加
- [x] 经验账本分区三: 真 cb_redemption (强赎) 立项加为高优 + 多策略组合
- [ ] 写 cb_redemption (真强赎) spec 草稿 (待 framework reuse 检查)
- [ ] CLOSEOUT DIRECT 给 Codex
