# CB Down-Revision Events — Full Scan Summary

> Generated 2026-05-07 from `all_down_events.parquet` (April 27 scan, 1014 unique bonds covering live + delisted/redeemed universe).
> Output: `cb_down_events_full.csv`

## Sample Size

|  | 总样本 | 深修 (ratio ≤ 0.75) | 小修 |
|---|---:|---:|---:|
| 旧基线 `cb_down_events_with_returns.csv` | 112 | 51 | 61 |
| **本次全扫描** | **500** | **223** | **254** |
| 新增 | +388 | +172 | +193 |

旧 112 条 100% 包含在新 500 内（overlap=112, old-only=0）— 是干净的超集。

## 数据质量

- 重复（bond_id × meeting_date）：0
- meeting_date 缺失：0
- before/after_price 缺失：23 条（建议下游分析过滤掉）

## 年度分布

| 年 | 事件数 |
|---|---:|
| 2017 | 1 |
| 2018 | 25 |
| 2019 | 20 |
| 2020 | 10 |
| 2021 | 33 |
| 2022 | 44 |
| 2023 | 62 |
| 2024 | 206 |
| 2025 | 78 |
| 2026 | 20 |

2024 年占 41%，是下修井喷年（与 CB 市场普遍承压一致）。

## Schema

```
bond_id, name,
board_announce_date, meeting_date, effective_date,   # board_announce_date 留空: JSL 不暴露
before_price, after_price, down_floor_price,
ratio, is_deep, year, change_reason
```

## 已知缺口

1. **board_announce_date 留空**：JSL 的 `bond_cb_adj_logs_jsl` 接口不返回董事会提议日。要拿首次公告日需要走交易所公告抓取（另立任务）。
2. **价格回填未做**：本次只输出事件元数据；T+0..T+60 价格序列要单独跑一次 `ak.bond_zh_hs_cov_daily` 脚本回填，写入 `cb_pead_series_full.csv`。
3. **null 价格 23 条**：可能是早期上交所记账格式问题，下游需 dropna。

## 下一步

- 补价格回填（写新脚本，用 `cb_daily.parquet` 仓库优先，仓库没有再走 ak）
- 用此 500 事件重跑 PEAD 验证（深修 N=223 vs 原 51，统计自由度大幅改善）
- 等"首次公告日"补全后，做 board_announce vs meeting_date 的事件窗对比（CLAUDE.md 改进项 #4）

## 相关文件

- 来源：`all_down_events.parquet`（500 事件，April 27 scan）
- 全量扫描脚本：`scripts/fetch_full_cb_events.py`（已写好，未重跑；如要刷新数据再调）
- 旧基线（保留对比）：`cb_down_events_with_returns.csv`、`cb_pead_series.csv`
