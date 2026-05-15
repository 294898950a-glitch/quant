# cb_arb 主策略 vs prototype 校准 (今日判断重审)

日期: 2026-05-15 (22:49 数据)
触发: 用户 22:35 质疑超额收益不稳定 → Claude 复盘代码暴露混淆 → Codex 主策略真实 baseline

## 今日错误前提

整个 2026-05-15 下午+晚上的研究 (panic detector 3 batch / trade-level 归因 / cross-validation / cb_redemption recon) 全部建立在**错误前提**:

**误判**: cb_arb baseline = "medium signal recovery=4 hurdle=0.15" 表现 (6 年 holdout: 2019 +16.1% / 2020 -13.1% / 2021 -5.0% / 2022 +1.4% / 2023 -3.1% / 2024 +3.0%)

**真实**: 这是 `scripts/evaluate_cb_arb_value_gap_switch.py` (**evaluation prototype**) 的表现, 不是主策略 `strategies/cb_arb/verifier.py` + yaml 13 维参数. 脚本第 9 行明说: "It is an evaluation harness only; **it does not replace the default strategy**."

## 两套东西真实对比 (Codex 22:49 主策略真实跑出)

| 年 | 主策略 (yaml current 13 维) | prototype (medium signal recovery=4 hurdle=0.15) | 差 |
|---|---:|---:|---:|
| 2019 | **-9.85%** | +16.1% | -26pp (prototype 大幅赢) |
| 2020 | -12.09% | -13.1% | +1pp |
| 2021 | +1.47% | -5.0% | +6.5pp |
| 2022 | +3.95% | +1.4% | +2.5pp |
| 2023 | -4.05% | -3.1% | -1pp |
| 2024 | +8.85% | +3.0% | +5.9pp |

**累计**:

| 指标 | 主策略 | prototype |
|---|---:|---:|
| 简单加和 | **-11.72%** | -0.70% |
| 复利累计 | **-12.70%** | -3.00% |
| 6 年全段 OOS 总收益 | +9.69% | n/a |
| 6 年全段 OOS benchmark | +27.23% | n/a |
| **6 年全段 OOS excess** | **-17.54%** | n/a |
| Sharpe | 0.20 | n/a |
| Max drawdown | -30.7% | n/a |
| Total trades | 834 | n/a |

## 真实判断

**主策略 yaml current 13 维参数版本**:
- 简单加 -11.72% / 复利 -12.70% / 全段 OOS excess **-17.54%**
- sharpe 0.20 极低, max_dd -30.7% 实盘不能要
- **不可上实盘**

**Prototype (medium signal recovery+hurdle)**:
- 简单加 -0.7% / 复利 -3.0%
- 比主策略好, 但仍负
- **2019 prototype +16.1% vs 主策略 -9.85%** — 26pp 反差是关键
- 暗示 "绝对价值差额排名 + panic detector" 比 "百分比偏离率排名" 有方向上优势

## 哪些今日判断需要撤回 / 重审

### 撤回 (因为基于错误前提)

1. **"cb_arb 主线 final 归档"** — 主策略真实 baseline 没看过就归档不成立
2. **"HDRF 是 winner / 自循环路线已次"** — 这俩都是 prototype 内部不同 hyperparameter 比较, 跟主策略无关
3. **"4/5 holdout 合理"** — prototype 的 6 年里只有 3/6 年正 (2019/2022/2024), 主策略只有 2/6 正 (2021/2022/2024); 算不上 4/5
4. **经验账本"已采用"条目** — 全部撤回, 主策略和 prototype 都没达"已采用"绝对门槛

### 仍成立 (跟前提无关的发现)

1. panic detector 整子方向无效 — 仍成立, 是 prototype 内部的研究, 跨 batch 同 pattern 是有效观察
2. cb_redemption 复活否决 — 跟主策略无关, 仍成立
3. 网格策略 EXPERIMENT_LOG 封档 — 跟主策略无关, 仍成立

### 重审 (要基于主策略真实数据)

1. **cb_arb 整体方向是否值得继续投入** — 用主策略真实数据判断 (而不是 prototype)
2. **prototype 思路 → 主策略改造**: 2019 prototype +16% 主策略 -10% 反差 26pp, 这是 prototype 思路 ("绝对价值差额排名 + panic detector") 比主策略思路 ("百分比偏离率排名") 有 +25% 差距的强信号. 是否值得正式把 prototype 思路提升到主策略 yaml 绿区?

## 教训沉淀 (memory)

新加 `feedback_baseline_must_pass_absolute_threshold.md`:
- 判断 baseline 可用必须看绝对水平 (累计 excess + 单年 dd), 不是相对水平 ("穷举完没 better")
- 红线: 累计 excess ≤ 0% 或 单年最大 dd ≤ -10% 直接归无效

**新教训 (本研究新发现)**: 
- **主策略 vs evaluation harness 必须分清** — 评估脚本 (scripts/evaluate_*.py) 是 prototype, **不替换主策略**. 报告策略表现时, 必须先 grep 主策略 verifier.py / tunable_space.yaml 看真实参数, 不能用 evaluation harness 的输出当 "baseline".

## 下一步 (待用户白天定)

**真正值得讨论的方向 (基于真实数据)**:

A. **prototype 思路提升主策略** — 把"绝对价值差额排名 + panic detector" 思路落到 yaml 绿区, 替换或叠加现有"百分比偏离率排名". 2019 +25% 反差是真信号
B. **cb_arb 整体撤离** — 主策略 -17.5% excess + prototype -3% 都没找到 stable 整套, 也许 cb 套利在中国市场结构性不行
C. **更多 idea 来源** — arxiv 22:32 0 通过, 改 keywords 到 concrete tradable domain, 24h 后 S2 cooldown 再跑

不今晚自决, 等你白天判断.

## 算力成本

- 本次 Codex 主策略真实 baseline: 9 min sig 上, ¥0
- 今天总: ¥8 (3 batch panic spot) + ¥0 (5 lightweight 诊断/recon/baseline) = ¥8 / ¥90 月预算 = 9%
