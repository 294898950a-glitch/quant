# cb_arb 研究完整复盘

**日期**: 2026-05-17
**作者**: Claude (Codex 审计)
**触发**: 跑研究方向 H 时 search_ledger 抓到 STRONG MATCH, ledger 已穷举多角度, 触发整条 cb_arb 研究路 retro
**目的**: 给未来 (用户 / AI) 进入这个 repo 时一份单页清单, 不用再爬 11 条 ledger + 9 个 reports

---

## 一句话结论

cb_arb 主策略 (横截面排名套利) 在 **当前 universe (中国可转债全集 ~700 只) + 当前时段 (2019-2024) + 当前 cost 模型** 下, 累计 excess 触底:

- **cost-off baseline**: -12.7% (6 年 OOS, 复利)
- **cost-on baseline**: -19.46% (cost realism wall 退化 -6.76pp)

**不是参数没调对, 是 strategy + universe + 时段组合的 fundamental ceiling = 负 excess**. 多个独立子方向穷举后都 reject, 单维度改良已无希望.

---

## 已穷举的子方向 (全 ledger reject)

| 方向 | 数据结果 | 失败根因 | 报告 |
|---|---|---|---|
| CSI500 同日大跌过滤 (2026-05-14) | 2 胜 2 平 2 负 | 2022 反向, dating 修复失败 | `cb_arb_csi_market_filter_2026-05-14.md` |
| medium_recovery3_hurdle0p10 无条件全年 (2026-05-15) | 2/4 通过 | 跨 regime 不鲁棒 | `cb_arb_round5_retro_2026-05-15.md` |
| 自身 PnL lookback regime classifier | 0/108 过 4 floor | lookback 滞后 60 日历日, 2020 第一次触发 04-03 距 panic 02-03 已晚 | `cb_arb_regime_switch_retro_2026-05-15.md` |
| 转债池跌幅截面 panic detector | 0/162 过 4 floor | path-sensitive, 2024 -16062 PnL gap 污染 | `cb_arb_market_breadth_panic_retro_2026-05-15.md` |
| breadth+pool-mean ensemble AND-trigger | 0/108 过 4 floor | 两 signal 高相关 (-0.71), AND 退化为单 signal | `cb_arb_breadth_confirm_ensemble_retro_2026-05-15.md` |
| **panic detector 整条子方向** | dead end | (1) cross-sectional 无法分真假 panic; (2) 真 panic 后 baseline 自己输 benchmark -3.8%, action 救不了; (3) baseline 已隐含 panic 行为, overlay 在假 panic 上反而误伤 | `cb_arb_panic_diagnostic_2026-05-15.md` |
| **单一 trade filter (entry/exit/holding)** | dead end | 2020 cross-trade broad weakness: 74% trades 负, median -4.32pp, worst10 只占 23%, 散在 5 个 entry month + 4 类 exit reason. 单 filter 救不了 | `cb_arb_baseline_trade_diagnostic_2026-05-15.md` |
| **年份选择性 meta wrapper (B 路径)** | dead end | meta 依赖死了的 detector + 2020 broad weakness 不是 panic 短窗口, meta 没什么可减仓 | 同上 |
| **自循环 LLM 调参 (iter 1-60)** | iter 24 < HDRF 26pp | 2019 HDRF +16.1% vs 自循环 -10.1%, 8 池只用 2 池但已次于 HDRF, ensemble 任何权重都不优于裸 HDRF | `cb_arb_two_line_cross_validation_2026-05-15.md` |
| **arxiv XVA paper 候选 (1608.02690)** | priority=低 | 弱关联 cb_arb (XVA = 衍生品估值调整, 不给策略层信号) | `paper_candidates/2026-05-15.md` |
| **cb_redemption 强赎策略** | data_mining verdict | 工作树 deleted, factor lookahead pollution (remaining_size 只有最新值), stock_momentum 名实不符 | `cb_redemption_state_assessment_2026-05-15.md` |

---

## 关键跨 batch 模式 (这是复盘核心)

### 1. 2020 是 fundamental dead zone

不是 tail event, **是 cross-trade broad weakness**:
- 74% trades 负 excess
- median -4.32pp
- worst10 trades 只占 23% 损失 → 不是少数 outlier 拖累, 是普遍弱
- 损失散在 5 个 entry month + 4 类 exit reason → 没有单一 trade-type 可救
- **任何 single filter / single signal 救不了 2020**

### 2. panic detector 整条死

**真 panic 后 baseline 自己也输 benchmark -3.8%** (2020-01-23 post-30 day).
- detector 即使完美识别也救不了 (action 在 panic 后没 alpha)
- baseline 已隐含 panic 行为 (横截面 rank 自动避开太弱的 CB)
- detector overlay 在假 panic 上反而误伤

**2024 5 个假 panic 比 2020 真 panic 还剧烈** (cross-sectional 看):
- 2024-10-09 pool mean abs 是 2020-01-23 的 2.27 倍
- drop3/5/7 share 是 3.60 / 5.95 / 9.63 倍

cross-sectional metrics **不可靠 panic discriminator**.

### 3. 2024 baseline 本身 +0.77pp OK

**问题不在 2024**, 是之前 panic detector overlay (recovery=1 hurdle=0.05 收手) 把 2024 -16062 PnL gap 打出来. detector 已被否决后, baseline naked 跑 2024 就 OK.

### 4. HDRF > 自循环 (跨路线 cross-validation)

- 2019 HDRF +16.1% vs 自循环 -10.1% (26pp gap)
- 自循环 8 池只用 2 池, 已次于 HDRF 6 年 leave-one-year-out
- ensemble 任何权重都不优于裸 HDRF

**研究方法论结论**: `leave-one-year-out` + 用户领域洞察 (HDRF) 比 `sealed_pool + LLM 调参` 强很多. 自循环死.

### 5. cost realism wall (2026-05-16 后)

实盘 cost 退化:
- cb_arb 主策略: -6.76pp (cost-off -12.7% → cost-on -19.46%)
- cb_arb value-gap switch: -7.47pp (cost-off -3.0% → cost-on -10.47%)

**远超 5pp fatal 阈值**. 这是 2026-05-16 之后新加的硬墙, 之前的 cost-off 数字都不算实盘 valid.

---

## 根因总评

cb_arb 主策略 dead end 不是 "参数差" 或 "信号选错", 是 **strategy + universe + 时段组合 fundamental ceiling**:

1. 横截面排名策略需要 "便宜的能均值回归" — cb 池在 2020 普遍弱, 没有便宜→贵的均值回归
2. 中国可转债池 ~700 只里, 高 quality 子集太小, 流动性参差 → cost 自然高
3. 2019-2024 时段含 2020 broad weakness 这种不可救的年份
4. cost 模型 (slippage 0.15% + sqrt impact + fee 0.03%) 是 fair, 但这市场的实盘 cost 就是这么高

---

## 没穷尽的方向 (重要)

按 ledger / reports 看, 我们没尝试:

1. **不同 universe**:
   - 更长历史 (含 2015 牛市 / 2018 熊市) → 不同 regime mix
   - 更高 quality 子池 (e.g. AAA only / top-100 流动性 only)
   - 跳过 2020 / 跳过 2024 → 单独看 work 不 work
   - 港股转债 / 美股可转债 → 不同市场

2. **不同 strategy family**:
   - 不是横截面 rank, 改 mean reversion (value-gap 已试, 还有别的 mean reversion 方式)
   - momentum (动量) — cb 池里有没?
   - event-driven (强赎 / 下修 / 转股价调整) — cb_redemption 死了但 event-driven 子集没穷尽
   - market neutral (cb_arb 已经是 neutral, 但相对什么 neutral 可改)

3. **入口 3 (arxiv) 找新 idea**:
   - 当前 ledger 只 reject 了 XVA 一篇, 还没系统拉 arxiv 论文做关联筛
   - 应该建 arxiv 关键词监控 (cb / convertible bond / fixed income arbitrage)

4. **跟外部对照**:
   - 我们只有 internal baseline, 没跟商业 cb 策略 (公募基金可转债 fund) 对照
   - 商业 fund 跑得是负 excess 还是正 excess? 不知道

---

## 建议下一步 (留给用户决定)

**A 路径 (短期)**: archive cb_arb 整个 strategy, 转新方向:
- 入口 3 arxiv 系统拉 paper 做关联筛 (有 paper_ingestion_protocol.md 框架)
- 新 strategy family 立项 (momentum / event-driven / cross-market)

**B 路径 (中期)**: 不放弃 cb, 改 universe / 改时段重试:
- universe: AAA only / 高流动性 only
- 时段: 跳过 2020 单看 (但泛化担忧)

**C 路径 (重投资)**: 破"不动 verifier 红区"规则, 改 verifier 核心 logic (不是参数, 是 exit/entry decision). 风险大 + 工作量大 + ledger 已经穷举 13 维 grid 调参提示 logic 本身可能不是最优.

**D 路径 (休息)**: 停 cb_arb research 一段, 用户跑别的, AI 模式 B 自动 arxiv 监控找新 idea, 真有新洞察再重启.

---

## 工具效能 (framework retro 一并)

这次研究 batch 里, framework 工具显示价值:

- **search_ledger.py + new_research.py** 两次抓 STRONG MATCH:
  - 第一次: 我和 Codex 都漏看 ledger "panic detector 整条已 reject", 工具拦下, 重新 debate
  - 第二次: 我提议 X3 universe filter, 工具又抓 "单一 trade filter 改造" 已 reject
  - **没工具我俩可能直接开 batch 浪费算力 + 重复证伪**
- **GateKeeper 5 道闸** 对 spec.yaml DRAFT 全过, 防 spec 不合规跑批
- **L5 diagnostic validator** 当前没现存 retry/reject batch, 但故意构造能抓

framework 加固完整完成 (P0+P1 共 4 commit + Q5 Q2-C 早完成), 下面要不要追加 backlog (E/F/G) 由用户决定.

---

## 文件清单 (复盘引用源)

历史 reports/cb_arb*:
- `cb_arb_evaluation_2026-05-10.md`
- `cb_arb_csi_market_filter_2026-05-14.md`
- `cb_arb_round5_retro_2026-05-15.md`
- `cb_arb_regime_switch_retro_2026-05-15.md`
- `cb_arb_market_breadth_panic_2026-05-15.md`
- `cb_arb_market_breadth_panic_retro_2026-05-15.md`
- `cb_arb_breadth_confirm_ensemble_2026-05-15.md`
- `cb_arb_breadth_confirm_ensemble_retro_2026-05-15.md`
- `cb_arb_panic_diagnostic_2026-05-15.md`
- `cb_arb_baseline_trade_diagnostic_2026-05-15.md`
- `cb_arb_main_vs_prototype_recalibration_2026-05-15.md`
- `cb_arb_two_line_cross_validation_2026-05-15.md`
- `cb_redemption_state_assessment_2026-05-15.md`

ledger:
- `docs/research_framework/experience_ledger.md` (11 条 reject)
- `data/research_framework/baseline_registry.yaml` (5 baseline, 全 cost-on 负)

framework 工具:
- `scripts/search_ledger.py` (这次救了俩)
- `scripts/new_research.py` (上面包装)
- `scripts/research_sanity_checker.py` (Q2-A, 跑回测前 spec 检)
- `scripts/validate_l5_diagnostic.py` (Q1 P1)
- `scripts/validate_baseline_registry.py` (Q2-D)
- `scripts/gatekeeper.py` (5 道闸入口)
