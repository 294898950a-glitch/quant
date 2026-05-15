# cb_arb panic 诊断报告 (2024 真假 panic 数据驱动反思)

日期: 2026-05-15
对象: 2020-01-23 真 panic + 2024 5 个 false positive panic dates

## 研究问题

3 batch panic detector 研究 (regime switch / market breadth / breadth+confirm) 都在 2019/2020/2024 fail 同 pattern. 这次不开新 batch, 直接看历史数据找 fail 根因.

核心 question: cb_arb 2024 fail 是 (a) signal 选不出真假, 还是 (b) action 即使识别准也救不了?

## 数据

来源: `data/cb_arb_panic_calendar_2024_diagnostic_2026-05-15/panic_calendar.csv`
- 6 个 panic 日期: 2020-01-23 (真) + 2024-01-22/02-05/02-28/06-24/10-09 (5 个 false positive)
- 每个日期 ~ 20 个 metrics: 当日 breadth / pool mean / pre-5d excess / post-30d excess / 开仓数

## 关键发现

### 1. Same-day cross-sectional 不能区分真假 panic

| Metric | 2020-01-23 (真) | 2024 假 avg | 假比真 |
| --- | ---: | ---: | ---: |
| pool mean | -0.0189 | -0.0252 | 假**更剧烈** |
| drop3 share | 0.201 | 0.364 | 假**更剧烈** |
| drop5 share | 0.063 | 0.113 | 假**更剧烈** |
| drop7 share | 0.013 | 0.034 | 假**更剧烈** |

最极端: 2024-10-09 比 2020-01-23
- pool mean abs 比 = **2.27 倍**
- drop3 比 = 3.60 倍
- drop5 比 = 5.95 倍
- drop7 比 = **9.63 倍**

**也就是说**, 2024-10-09 当日数据看起来比 2020-01-23 还像 "panic", 但实际 cb_arb 之后并没有像 2020 那样持续下跌. **Cross-sectional metrics 是个不可靠的 panic discriminator**.

### 2. Pre-5 day excess return 部分 distinctive (小样本)

- 真 panic 2020-01-23 pre5 = -0.013 (panic 前 5 日 cb_arb 已落后 benchmark 1.3%)
- 2024 假 panic pre5 大多 flat/正 (除 2024-06-24 = -0.024)

5 个样本太小, 不能强结论. 但暗示: 真 panic 通常**有前期累积弱化** (vs 突发型假 panic).

### 3. **最深刻发现 — 真 panic 之后 baseline 自己也输 benchmark**

- 2020-01-23 真 panic post30 baseline excess = **-0.038** (panic 之后 30 天 baseline 还输 benchmark 3.8%)
- 2024 假 panic post30 = -0.029 to +0.019 (mixed)

**这翻转了整个 panic detector 研究的假设**:

之前所有 panic detector 研究都假设"识别 panic + 收手 → 改善后续表现". 但数据显示:

> 2020-01-23 真 panic 之后, 即使完美收手, 30 天后 baseline 已经输 benchmark 3.8%. 不是 detector 没识别, 是 cb_arb 策略本身在 panic+反弹组合期跑不过 benchmark.

### 4. 假 panic 触发后的 cb_arb 实际开仓

2020-01-23 真 panic 后 5 日: 0 个新仓 (baseline 自动停手)
2024 假 panic 后 5 日: 平均 4 个新仓 (baseline 正常开仓)

→ baseline 在真 panic 后已经"自动收手", panic detector + 主动收手 不增加保护. 在假 panic 后 baseline 正常开新仓, panic detector 主动收手反而打这些仓的 path.

**说明 baseline 本身已隐含 panic 行为**, panic detector overlay 在真 panic 上无效果, 在假 panic 上**有害**.

## 整体判断 (架构层)

**3 batch 同 pattern + 本诊断 = panic detector 这条路 fundamentally dead**:

1. signal 选不出真假 (same-day cross-sectional 不可靠, pre-period 小样本不够泛化)
2. baseline 在真 panic 之后 30 天自己也输, **detector 即使完美也救不了**
3. baseline 已隐含 panic 行为, detector overlay 反而误伤 (假 panic 收手 → path 污染)

**该停止 panic detector 子方向研究**.

**真正问题**: cb_arb 在 2020/2024 这种 "panic 后反弹混合年" 跨不过 benchmark. 这是策略**结构性弱点**, 不是 panic 模块能解的.

**架构层 2 个候选方向 (升级用户拍板)**:

A. **重审 cb_arb 基础策略** — 直接看 2020/2024 baseline 在哪些 trade / 哪些时段亏钱, 看是否能改基础策略 (不只调 panic 模块)
B. **接受 cb_arb 跨年泛化局限** — 把 cb_arb 当作"非反弹年友好策略", 配套**年份选择性运行** (e.g. 一个 meta-detector 判断当年是否反弹年, 反弹年自动减仓 cb_arb 切换到别策略)

A 和 B 都是大方向变化, 需要用户 architecture-level 决策.

## 已确认无效方向 (本研究新加)

- 所有 panic detector 子方向 (regime switch / market breadth / ensemble / 任何 same-day cross-sectional signal): 即使完美识别也救不了 cb_arb 跨年泛化, 因为 baseline 在真 panic 后 30 天自己也输 benchmark

## 算力成本

- 本地 lightweight 数据分析, 3 分钟跑完
- 实际花费 ¥0
- 累计今天: ¥8 (3 batch spot) + ¥0 (本诊断) = ¥8

## 后续待办

- [x] 经验账本: panic detector 子方向整体已确认无效 (升级到根因, 不只是单个 batch)
- [ ] **用户拍板**: A (重审 cb_arb 基础) vs B (年份选择性运行 meta)
