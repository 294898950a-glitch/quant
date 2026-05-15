# baseline registry (历史 baseline 权威表)

**最后更新**: 2026-05-15 23:50 CST
**首版建立**: framework debate Round 3 共识

## 规则

- 一行一个 baseline (一次回测出一个数字)
- 字段顺序: 日期 / 策略 / 实现路径 / config / period / protocol / 主指标 / artifact / status
- **immutable-ish**: 历史行不可改 (改了等于撒谎). 当前活跃 baseline 可以新加一行 supersede 旧行
- baseline 改变就加新行, 不覆盖
- 任何 RESPONSE 跑出新 baseline 必须**同 handoff** 加一行

## 列定义

- **日期**: baseline 产出日期
- **策略**: cb_arb / cb_redemption / grid-sp500 / 等
- **实现路径**: 具体跑哪个文件 (verifier.py 还是 evaluate_*.py)
- **config**: yaml current / commit hash / params 摘要
- **period**: 回测时间窗
- **protocol**: leave-one-year-out / 8-pool-sealed / 全段 / 等
- **主指标**: 累计 excess (复利) / sharpe / max_dd / total_trades
- **artifact**: 数据 / 报告链接
- **status**: WIP / adopted / rejected / archived

---

## 历史 baselines

### cb_arb 主策略

| 日期 | 实现 | config | period | protocol | 累计 excess (复利) | sharpe | max_dd | trades | artifact | status |
|---|---|---|---|---|---:|---:|---:|---:|---|---|
| 2026-05-15 | `strategies/cb_arb/verifier.py` `run_backtest()` | yaml current 13 维 + 3 rules (vol_window=60 / rank_buy=0.1 / rank_sell=0.5 / max_holdings=30 等) | 2019-01-02 ~ 2024-12-31 | leave-one-year-out 6 年 holdout | **-12.70%** (简单加 -11.72%) | 0.1957 | -30.70% | 834 | `data/cb_arb_main_strategy_baseline_2026-05-15/` | **WIP, 不达门槛** |

#### cb_arb 主策略年度细分 (2026-05-15)

| 年 | excess |
|---|---:|
| 2019 | -9.85% |
| 2020 | -12.09% |
| 2021 | +1.47% |
| 2022 | +3.95% |
| 2023 | -4.05% |
| 2024 | +8.85% |

### cb_arb value-gap switch (评估分支)

| 日期 | 实现 | config | period | protocol | 累计 excess (复利) | max_dd 单年 | artifact | status |
|---|---|---|---|---|---:|---:|---|---|
| 2026-05-15 | `scripts/evaluate_cb_arb_value_gap_switch.py` | medium signal recovery=4 hurdle=0.15 | 2019-2024 | leave-one-year-out 6 年 holdout | **-3.00%** (简单加 -0.70%) | -13.1% (2020) | `reports/cb_arb_baseline_trade_diagnostic_2026-05-15.md` | **WIP 加强版, 不达门槛** |
| 2026-05-10 (Codex 自循环 iter 24) | `strategies/cb_arb/verifier.py` (用 main strategy 的 yaml 13 维 + LLM 调参) | iter 24 best params (rank_sell=0.505 / max_position=0.0315 等微调) | 2022-01-01 ~ 2026-05-08 | 8 sealed pools, 只用 Pool 0/1 | **+0.313 (3 年累计 OOS)** | -0.292 | `sig:/root/projects/quant/data/cb_arb_rerun_fixed_20260510_124155/` | 已次于 HDRF 路线 (2019 broken -10.1% vs HDRF +16.1%, 26pp 反差) |

#### cb_arb value-gap switch 年度细分 (2026-05-15)

| 年 | excess |
|---|---:|
| 2019 | +16.1% |
| 2020 | -13.1% |
| 2021 | -5.0% |
| 2022 | +1.4% |
| 2023 | -3.1% |
| 2024 | +3.0% |

### cb_redemption (强赎)

| 日期 | 实现 | config | period | protocol | excess | holdout_compliance | audit verdict | artifact | status |
|---|---|---|---|---|---:|---|---|---|---|
| 2026-05-06 (HEAD deleted) | `strategies/cb_redemption/{data,backtest,optimizer}.py` (HEAD, working tree deleted) | 5 weight CMA-ES iter 5 | n/a (audit failed) | n/a | n/a | **False** | **data_mining** | `sig: data/cb_redemption/runs.jsonl` (HEAD deleted) | **archived (framework verdict)** |

### 网格策略 (6 标的, 2026-05-09 EXPERIMENT_LOG 封档)

| 日期 | 标的 | 平均得分 | 历史最高 | 4 年全段 vs 持有不动 | artifact | status |
|---|---|---:|---:|---|---|---|
| 2026-05-09 封档 | sp500-grid (513500) | +0.22 | +4.84 | 平均不行, 偶尔好运 | `EXPERIMENT_LOG.md` | archived |
| 2026-05-09 封档 | csi500-grid (510500) | +1.90 | +4.18 | **审计员判过拟合, 不可信** | `EXPERIMENT_LOG.md` | archived |
| 2026-05-09 封档 | yzm-grid (300415) | +0.42 | +2.17 | 单股波动太大被打穿 | `EXPERIMENT_LOG.md` | archived |
| 2026-05-09 封档 | 工行 (601398) | -1.02 | -0.43 | LLM 调到几乎不持仓, 错过整段牛市 | `EXPERIMENT_LOG.md` | archived |
| 2026-05-09 封档 | 神华 (601088) | -0.70 | n/a | 全段输持有 70 点 | `EXPERIMENT_LOG.md` | archived |
| 2026-05-09 封档 | 长电 (600900) | n/a | n/a | 输持有 | `EXPERIMENT_LOG.md` | archived |

---

## 当前活跃 baseline 一句话总结

- **0 个达上线门槛**.
- cb_arb 两个 baseline (主策略 -12.7% / value-gap switch -3.0%) 都是 WIP, 累计 excess 都负.
- 其他策略 (cb_redemption / 网格) 都 archived.

## 维护规则

- 任何 baseline-changing RESPONSE 必须同 handoff 加一行
- 历史行不删, 新结果 superseded 时加注释 "supersedes row N (date)"
- 实际产生 ranked.csv / metrics.json 时务必把 artifact 路径填这里, 不能只放在 report
