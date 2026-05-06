# Quant — 可转债策略实验仓

可转债（Convertible Bond）量化研究 + 策略实现。

> Wiki / 知识库那部分已拆出去：`~/projects/quant-wiki/`

## 当前两条策略线

### 1. CB-PEAD — 下修公告事件后漂移
`strategies/cb_pead/` · `data/cb_pead/`

研究下修公告后是否存在 PEAD（Post-Event Announcement Drift）。

**核心发现**（112 次事件，2023.01–2025.10）：

| 窗口 | 大幅下修 (n=51) | 小幅下修 (n=61) |
|------|:---:|:---:|
| T+1 | +2.46%*** | -0.32% |
| T+20 | +6.24%*** | +0.27% |
| T+60 | +9.15%*** | +0.59% |

大幅下修（after/before ≤ 0.75）存在显著 PEAD，60 天约 9% 超额收益。研究笔记见 `data/cb_pead_research.md`。

### 2. CB-Redemption — 强赎策略
`strategies/cb_redemption/` · `data/cb_redemption/`

强赎事件相关的 ML 流水线：信号工程 → 训练 → 回测 → 优化 → 预测 → Notion 入库。
见 `strategies/cb_redemption/CLAUDE.md`。

### 统一回测引擎
`strategies/unified_engine.py` + `run_unified.py`

## 数据布局

```
data/
├── cb_pead/         # PEAD 研究数据 (raw/processed/backtest/docs)
├── cb_redemption/   # 强赎策略状态/快照/缓存
└── cb_warehouse/    # 跨策略 parquet 仓库 (cb_basic/cb_daily/holder_*…)
```

## 仓库结构

```
quant/
├── data/         # 数据层（见上）
├── strategies/   # 策略代码
├── scripts/      # 仓库构建/数据拉取/策略 review
├── docs/         # plans + 数据源调研
├── reports/      # 策略 review 输出
├── logs/
└── .venv/
```

## 数据源
- 集思录 JSL (`bond_cb_adj_logs_jsl`) — 下修事件
- akshare / 东方财富 (`bond_zh_hs_cov_daily`) — 转债日线
- Tushare — 正股日线（前复权）
