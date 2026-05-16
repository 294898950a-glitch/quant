# HDRF L1 实验设计模板 (spec.md)

每次新研究开始, Claude 必须先在 `data/<run-id>/spec.md` 写一份, 然后才能发 DIRECT 给 Codex.

## 模板

```markdown
# 研究 spec: <一句话标题>

日期: YYYY-MM-DD
研究 id: <run-id, 比如 cb_arb_csi_market_filter_2026-05-14>
策略: cb_arb / cb_redemption / ...

## L0 假设 (用户给, 一句话)

> "X 信号能改善 Y 年表现, 同时不伤 Z 年"

例: "沪深500 ETF 同日大跌过滤器能改善 cb_arb 在 2020 暴跌年的表现, 同时不伤 2021 正常年"

## 来源洞察

(用户为什么这么想的? 哪个先验观察?)

例: "用户的领域洞察: 暴跌一定是多市场的, A 股大盘必然同步大跌"

## 关键变量

新增参数 (会进 yaml 黄区):

| 参数名 | 范围 | 含义 |
| --- | --- | --- |
| `panic_market_filter_csi_threshold` | [-0.02, -0.005] | CSI500 同日跌幅门槛 |
| ... | ... | ... |

新增数据源:

- `data/csi500_grid/raw/510500_daily.parquet` — CSI500 ETF daily

## Grid 设计

- 维度 1: `<param-1>` ∈ {a, b, c, ...}
- 维度 2: `<param-2>` ∈ {x, y, z, ...}
- 总候选: N × M = ?

## 评估指标

主指标 (核心硬约束):

- `replay_<target-year>_excess ≥ <floor>` — e.g. replay_2020 ≥ -0.138588
- `replay_<other-year>_excess ≥ <floor>` — e.g. replay_2021 ≥ -0.033534

辅助指标:

- selection_avg_excess (跨非 holdout 年综合)
- max_drawdown
- trades 数量
- win rate

## leave-N-out 设计

- holdout 年: <e.g. 2019, 2020, 2021, 2022, 2023, 2024>
- 每年单独跑一个 search
- 真 CV 评估: 用 leave-Y 训练, 看 top1 在 Y 年表现 vs medium baseline (参考 [questioning_checklist Q3](./questioning_checklist.md))

## Stop conditions

- 如果第 1 轮 grid 0 个候选过 hard floor → 停, 不要进 Round 2
- 如果第 1 轮 > 50% 候选过 floor → 表面通过, 必须做 5 项质疑检查
- 如果真 CV 显示 < 50% holdout 年改善 → 信号不够泛化, 标记 "已确认无效"

## 算力估算 (按 [reference_spot_start_criteria.md](../../.claude/memory/reference_spot_start_criteria.md))

- 候选数 × 单次回测时长 = sig 估时 ~ <X> 分钟
- spot 估时 ~ <X/8> 分钟
- 是否需要起 spot: <是/否, 按 ≤30 / 30-2h / ≥2h 三档>

## 预算上限

- 总算力预算: 不超过 <¥X>
- 总时间预算: 不超过 <Y 小时>
- 预算计算器: `python3 scripts/estimate_compute_budget.py --spot-hours <X> --sig-hours <Y> --paid-data-yuan <Z>`
- 计算结果 ≤ ¥100 → 可自动继续
- 计算结果 > ¥100 或算不出来 → 暂停, 回到用户决策

## 必出产物 (L3 完成时必齐, Codex L3 RESPONSE 必含路径)

- `data/<run-id>/ranked.csv` — 候选排名
- `data/<run-id>/summary.csv` — baseline 行 + 候选汇总
- `data/<run-id>/trades.csv` — 每笔交易详情 (默认产出, 不可省)
- `data/<run-id>/daily_equity.csv` — 每天权益曲线 (默认产出, 不可省)
- `data/<run-id>/trigger_dates.csv` — 触发日列表 + 关联市场状态 (新机制时不可省)
- `reports/<run-id>.md` — L6 后写, 按 [report_template.md](./report_template.md)

## 已有脚本和函数 (供 Codex 复用)

- `evaluator`: <e.g. scripts/evaluate_cb_arb_value_gap_switch.py>
- `search 脚本`: <e.g. scripts/search_cb_arb_panic_leave_year_out.py>
- `daily replay`: <e.g. scripts/daily_replay_helper.py 或 inline>
- 可复用的 baseline 行 kind: <e.g. medium_opportunity / strong_opportunity / current_best_no_opportunity>

## 推荐执行命令模板

```
# sig 上 (2 核)
ssh root@100.91.245.108 'cd /root/projects/quant && python3 -u scripts/<search>.py --xxx'

# spot 上 (16 核, 需起机)
ssh -i ~/.ssh/quant_spot.pem ubuntu@<spot-ip> 'cd /home/ubuntu/projects/quant && python3 -u scripts/<search>.py --max-workers 16 --xxx'
```

## 数据可用性 (新数据源时必填)

- 数据路径: ...
- 日期覆盖: <起始 - 终止>
- 缺失率: <e.g. 0.2% (33/3000 天)>
- 价格字段对齐: <复权方式 / 拆分调整 / 分红处理>
- 时区对齐: <CST / UTC / etc>
- 是否含未来信息 (lookahead): <无 / 有, 说明类型 + 缓解>
- 数据预检脚本: <Codex 已跑 spec/L1.5 数据预检 RESPONSE 路径>

## 停止条件 (硬约束)

- 任一关键 holdout 年 0 候选过 hard floor → **直接停**, 不进 Round 2
- > 50% 候选过 floor → 表面通过, 必须做 5 项质疑检查
- 真 CV 显示 < 50% holdout 年改善 → 信号不够泛化, 标记 "已确认无效"
- 修复尝试 2 次仍 fail → 写 "已确认无效" 报告, 不再修
- 总算力预算超 ¥100 或预算算不出来 → 暂停, 回到用户决策

## 升级条件 (什么情况 Codex 必须回 Claude / 用户)

- 算力预估超出原 spec 1.5 倍 → 必须重新评估起 spot
- 跑出来某 baseline 不能复现 → 必须 stop + RESPONSE 报告差异
- 中途发现 spec 假设错误 (e.g. 数据来源不能用) → stop + RESPONSE
- 用户在 ≥30 分钟内无回应且 spot 在烧 → 触发 [auto-shutdown](../../.claude/memory/feedback_auto_shutdown_5loops.md)
```

## 8 必填字段 (硬约束)

Codex 收到 L1 DIRECT 时, 跑 schema 检查, 缺任一字段 → `HANDOFF/MISSING-SPEC`, 不开跑:

| 字段 | 名称 | 必含 |
| --- | --- | --- |
| 1 | 假设 (一句话) | 不能为空 |
| 2 | 参数空间 (维度 + 范围) | 至少一个维度 |
| 3 | 硬约束底线 (replay_X ≥ floor_X) | 必填, 每个 floor 标出处 |
| 4 | 数据来源 (含 baseline 来源) | 必填, 路径或脚本 |
| 5 | 算力预估 (sig X min / spot Y min) | 必填, 按 [spot 协议](../../.claude/memory/reference_spot_start_criteria.md) |
| 6 | 真 CV 设计 (用 leave-Y 训练, Y 评估) | 必填, 写明 leave 年份 + ranking metric |
| 7 | 输出物清单 (4 类 artifact) | 必填, 见上节 |
| 8 | 停止条件 + 升级条件 | 必填, 至少一组 |

## 必填要点 (v1 保留)

- ❌ 不能跳过假设和来源洞察 — 没有就不是 hypothesis-driven, 是盲扫
- ❌ 不能跳过 stop conditions — 否则研究会"自我膨胀"
- ❌ 不能跳过算力估算 — 否则无法按 spot 协议判断
- ❌ 不能跳过真 CV 设计 — 否则等到 Q3 才发现"6 年留一完全一样"已经太晚 (Codex review 改进 7)
- ❌ 不能跳过输出物清单 — 否则 trade-level / daily equity 在反向诊断时缺数据 (Codex review 改进 4)
- ✅ 弹性: grid 维度 / 评估指标 / leave 年份 因研究而异
