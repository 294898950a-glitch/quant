# 当前研究状态 (CURRENT)

**最后更新**: 2026-05-15 23:50 CST
**触发更新事件**: framework debate Round 3 共识 — 首版建立

这页是每次新会话或新研究 batch 的**唯一权威入口**。读完这页就知道当前在做什么、谁是 owner、下一步等谁。

经验账本 = 历史。autonomous_summary = 回顾。**CURRENT.md = 当前真值**。

---

## 总览

| 策略 | status | 累计 excess (复利) | max_dd (口径) | 下一步 owner |
|---|---|---:|---|---|
| cb_arb 主策略 (verifier yaml-current) | **WIP, 不达门槛** | -12.7% | -30.7% (全段 OOS 2019-2024) | 用户 (决定是否继续) |
| cb_arb value-gap switch (评估分支) | **WIP 加强版, 不达门槛** | -3.0% | -13.1% (单年 2020) | 用户 (是否升级到主策略) |
| cb_redemption (强赎) | **archived (framework data_mining verdict)** | n/a | n/a | 不复活 |
| 网格策略 6 标的 (sp500/csi500/yzm/工行/神华/长电) | **archived (EXPERIMENT_LOG 封档)** | n/a (全负) | n/a | 不复跑 |

注: 上面 max_dd 列两个口径不直接可比 (一个全段, 一个单年最大). 重要 baseline 的全段 max_dd 见 baseline_registry.md.

**当前实际可上实盘的策略**: **0 个**。

---

## cb_arb 主策略 (verifier yaml-current)

- **实现**: `strategies/cb_arb/verifier.py` `run_backtest()`
- **配置**: `strategies/cb_arb/tunable_space.yaml` current 值 (13 维 + 3 rules)
- **算法**: 横截面排名套利 — 算理论价 (BS + 债底) → (市场价 / 理论价) **百分比偏离率**排名 → 买前 10% (`rank_buy_pct=0.1`) → 持仓跌出 50% 内 (`rank_sell_pct=0.5`) 卖
- **过滤**: 剩余规模 ≥ 7290 万, 20 日均成交 ≥ 100 万, 评级 ≥ AA-, 单只 ≤ 3%, 同时 ≤ 30 只, 最长 90 天, -8% 止损
- **baseline (Codex 22:49 sig 跑)**:
  - 简单加和 -11.72% / 复利 -12.70%
  - 全段 OOS 总收益 +9.69% / benchmark +27.23% / **excess -17.54%**
  - sharpe **0.20**, max_dd **-30.7%**, 834 trades
- **artifact**: `data/cb_arb_main_strategy_baseline_2026-05-15/{holdout_yearly.csv, daily_equity.csv, summary.json}`
- **status**: **不达实盘门槛** (accumulated excess 负 + max_dd > 10%)
- **当前 gate**: 等用户白天决定 (A. 升级 value-gap switch 思路 / B. cb_arb 整体撤离 / C. arxiv 新方向)

## cb_arb value-gap switch (评估分支)

- **实现**: `scripts/evaluate_cb_arb_value_gap_switch.py` (untracked WIP)
- **算法**: 把主策略"百分比偏离率排名"换成"**绝对价值差额排名** `(理论价 - 市场价) × 买入量`" + 加 panic detector + switch_hurdle_pct
- **lineage**: 从主策略 verifier 派生, 复用底层 (`_load_cb_daily` / `_build_call_index` 等), 但有独立 backtest 主循环
- **依赖**: 被 16+ 研究脚本 import (panic detector / cross-validation / breadth ensemble 等都用)
- **baseline (今天讨论的 "medium signal recovery=4 hurdle=0.15")**:
  - 6 年 holdout: 2019 +16.1% / 2020 -13.1% / 2021 -5.0% / 2022 +1.4% / 2023 -3.1% / 2024 +3.0%
  - 简单加和 -0.7% / **复利 -3.0%**
  - max_dd 单年 -13.1% (2020)
- **artifact**: `reports/cb_arb_baseline_trade_diagnostic_2026-05-15.md` + `reports/cb_arb_two_line_cross_validation_2026-05-15.md`
- **关键发现**: 2019 +16.1% vs 主策略 -9.85% → **26pp 反差**, 说明 "绝对价值差额 + panic" 思路在主策略 "百分比偏离率" 上有方向优势
- **status**: **WIP 加强版, 不达实盘门槛, 但思路有空间**
- **设计意图** (脚本第 9 行注释): "evaluation harness only, does not replace default strategy" — Codex 确认这是有意为之, 而非工程债
- **当前 gate**: 等用户白天决定是否把思路 promote 到主策略 yaml 绿区

## cb_redemption (强赎策略)

- **实现**: `strategies/cb_redemption/` 但 `data.py` / `backtest.py` / `config.py` / `optimizer.py` **工作树 deleted** (HEAD git 里有)
- **历史**: 5 iter 跑过, framework 审计员 **verdict=`data_mining`**, holdout_compliance=**False**
- **factor 缺陷**: `remaining_size` lookahead pollution (只有最新值 cb_basic), `stock_momentum` 名实不符 (实际是转债 pct_chg 不是正股)
- **数据基础**: `data/cb_warehouse/cb_call.parquet` (997 强赎事件 2008-2026) 有
- **artifact**: `reports/cb_redemption_state_assessment_2026-05-15.md`
- **status**: **archived (历史 audit data_mining + factor 设计缺陷)**, 不复活
- **不复活理由**: 3-5 天工程恢复 + 历史已判过拟合 + 同时间 arxiv idea source 更高 leverage

## 网格策略 6 标的

- **实现**: 散在 `strategies/cb_redemption/` framework 通用代码 + 各标的 systemd 服务 (已 stop+disable)
- **标的**: sp500-grid (513500) / csi500-grid (510500) / yzm-grid (300415) / 工行 (601398) / 神华 (601088) / 长电 (600900)
- **结论 (`EXPERIMENT_LOG.md` 2026-05-09 封档)**: 网格在 2022-2026 中美股票市场对蓝筹股**全跑不赢直接持有 + 股息**
- **status**: **archived**, 6 service stop+disable, 不再跑
- **复活条件**: 找到非蓝筹股 / 非牛市环境的目标

---

## WIP 文件清单 (untracked / modified 关键文件)

| 文件 | git status | 描述 | 下一步 |
|---|---|---|---|
| `scripts/evaluate_cb_arb_value_gap_switch.py` | **untracked** | value-gap switch 评估分支, 16+ 脚本依赖 | quarantine: 写明"evaluation prototype, not authoritative", 不强制 commit |
| `strategies/cb_arb/verifier.py` | modified 82 行 | 加 `run_backtest_dynamic` 函数支持 per-date cfg 切换 | 工程债, 待用户决定是否合并 |
| `strategies/cb_arb/tunable_space.yaml` | modified | current 值改动 | 跟 verifier 同步, 待合并 |
| `strategies/cb_arb/orchestrator_main.py` | modified | 暂未 inspection | 待 inspection |
| `strategies/cb_redemption/*` 多个 .py | modified | 网格 framework 通用代码改动 | 已 archived, 不需要 commit |
| `EXPERIMENT_LOG.md` / `README.md` | modified | 内容更新 | 自动随研究 commit |

---

## 策略关系图 (lineage 标签)

```
cb_arb 策略族:
    主策略 (verifier.py + yaml) ─── 默认实现, framework 自循环跑这个
         │
         ├── (派生) value-gap switch (evaluate_cb_arb_value_gap_switch.py)
         │       │
         │       ├── panic detector — regime switch / market breadth / breadth-confirm ensemble (3 batch, 全 reject)
         │       ├── trade-level 归因诊断 (5 min lightweight)
         │       └── 两路线 cross-validation (vs 自循环 LLM)
         │
         └── (派生) verifier dynamic mode (verifier.py 中 run_backtest_dynamic)
                 └── per-date cfg 切换 (panic detector 基础设施, 未独立测试)

cb_redemption (强赎): archived
网格 6 标的: archived (EXPERIMENT_LOG)
```

---

## 协议触发 CURRENT.md 更新事件 (U15, see protocol_redline.md)

**必更新**:
- 跑出新 baseline 数字 (跨年 / 单年 / OOS 全段)
- 策略状态切换 (adopted ↔ rejected ↔ WIP ↔ archived)
- 新策略立项 / 撤销立项
- 关键文件 commit / promote / quarantine

**不触发** (避免文件被噪音淹没):
- 单纯 lightweight reconnaissance
- 单纯 schema check
- 单纯算力估算
- 单纯 idle ACK

---

## 下一步 owner (当前等谁)

- **用户白天决定**: cb_arb 整体方向 (A. value-gap switch 思路 promote 到 yaml / B. cb_arb 撤离 / C. arxiv 新方向 + keywords 调整)
- **arxiv S2 cooldown**: 24h 后可重跑 (但 keywords 需先重审, 否则可能仍 0 通过)
- **Codex**: idle / standby, 等用户白天决定后再触发下一个 task

---

## 维护规则

- 每次研究 batch 完成时, 必须**同 handoff** 更新本文件
- 不允许"做完研究 → 写到 report → 不更新 CURRENT" (这是 framework debate 暴露的核心病灶)
- baseline 数字以 baseline_registry.md 为权威源, 这页只取 latest
- 改 status 必须在经验账本同步
