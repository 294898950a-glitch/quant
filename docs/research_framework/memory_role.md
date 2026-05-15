# 记录员角色

来源: 参考旧框架 `strategies/cb_redemption/memory.py` 设计.

## 职责定位

记录员**纯持久化层**, 不判断、不决策, 只把每个研究 batch 的产物完整记下来, 让后续 (审计员、模式 B 选研究、用户回溯) 能查.

## 三个日志文件

存放位置: `data/research_framework/` (跟旧框架的 `data/cb_redemption/` 类比)

### 1. `runs.jsonl` — 每个研究 batch 一行

```json
{
  "run_id": "cb_arb_csi_market_filter_2026-05-14",
  "started_at": "2026-05-14T11:42:00+08:00",
  "ended_at": "2026-05-14T20:14:00+08:00",
  "strategy": "cb_arb",
  "hypothesis": "沪深500同日大跌过滤能改善 2020...",
  "mode": "A",          // A (用户在场) | B (自动循环)
  "parameter_space": { "panic_market_filter_csi_threshold": [-0.02, -0.005, ...] },
  "hard_floors": { "replay_2020": -0.138588, "replay_2021": -0.033534 },
  "outcome": "rejected", // adopted | rejected | reroute
  "is_metric_best": 0.033013,
  "oos_metrics_by_year": { "2019": 0.170, "2020": -0.133, ... },
  "compute_cost_yuan": 6.0,
  "report_path": "reports/cb_arb_csi_market_filter_2026-05-14.md",
  "audit_report_path": "data/research_framework/audits/2026-05-14T20:14:00.json"
}
```

### 2. `tried_directions.jsonl` — 已试方向 (防重复)

```json
{
  "direction_key": "cb_arb::csi_market_filter::raw_date::threshold_-0.5%",
  "first_tried": "2026-05-14",
  "outcome": "rejected",
  "outcome_reason": "2 胜 2 平 2 负, 2022 反向",
  "report_path": "reports/cb_arb_csi_market_filter_2026-05-14.md"
}
```

direction_key 的生成规则:
```
<strategy>::<feature>::<key_param_1>::<key_param_2>::...
```

模式 B 选下一个研究前必查 — 如果同 direction_key 已 outcome=rejected → **跳过**(对应协议红线 B5).

### 3. `decisions.jsonl` — 决策日志

每次"切换模式 / 升级协议 / 否决 / 用户拍板" 都记一行:

```json
{
  "timestamp": "2026-05-14T22:24:00+08:00",
  "actor": "user | claude | codex",
  "action": "enter_mode_b | exit_mode_b_vetoed | adopt_research | reject_research | escalate_to_user",
  "context": { ... }
}
```

## 与 [experience_ledger.md](./experience_ledger.md) 的关系

经验账本是**给人看的概要**(用 Markdown, 优先级排序). 记录员是**给机器查的明细**(用 JSONL, 完整产物).

- 写入顺序: 研究完成 → 记录员先写 JSONL (机器可读) → 经验账本同步摘要 (人可读)
- 查询顺序:
  - 模式 B 选下一个研究: 先查 `tried_directions.jsonl` 跳过已 rejected; 再读 experience_ledger 优先级列表
  - 审计员审计: 读 `runs.jsonl` 的时间序列指标
  - 用户审查: 读经验账本概要 + 必要时点开 `runs.jsonl` 细节

## 接口约束

- **append-only**: 永不覆盖, 永不删除. 任何修改 → 追加新行 (标记 supersedes: <old-run-id>)
- **文件锁**: 多进程并发写时, 用 fcntl.flock (POSIX) 串行化, 防止损坏
- **commit 到 git**: 每次研究完成后必 commit 到 git, 保持可回溯

## 写入触发点

| 时机 | 谁写 | 内容 |
| --- | --- | --- |
| L1 实验设计完成 | Claude | `runs.jsonl` append (status=designed, 等等 outcome) |
| L3 实验跑完 | Codex | 同 run_id 更新 (status=ran, oos_metrics) |
| L6 报告写完 | Claude | 同 run_id 更新 (outcome=adopted/rejected/reroute, compute_cost), 并写 `tried_directions.jsonl` |
| 模式切换 | 触发方 | 写 `decisions.jsonl` |
| 审计员产出 | Claude | 写 `audits/<timestamp>.json` |

## 查询接口 (Codex 端 + Claude 端共用)

约定的查询函数 (Codex 端实现, Claude 端调用):

```
list_runs(strategy=None, mode=None, outcome=None, since=ISO) → [RunRecord]
has_been_tried(direction_key) → bool
get_recent_oos(strategy, last_n=3) → [{ year: ..., metric: ... }]
record_decision(actor, action, context) → None
```

## 例外

- 用户说"忽略记录员" → 临时跳过, 但**事后必须补**, 否则下次有重复研究风险
- JSONL 文件损坏 → 暂停所有 outbox 流程, 等用户从 git 历史恢复

## 与协议红线的关系

协议红线 U8 (不重复不抢占) 的具体实现就是查 `tried_directions.jsonl` + outbox msg-hash cache.
