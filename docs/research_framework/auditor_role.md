# 审计员角色

> **本文件: 描述性参考, 非机器约束源**.
> 真正的机器强制规则在 `scripts/validate_*.py` + `data/research_framework/*.yaml`.
> 本文件解释"为什么这么做" + 历史背景, 不写"必须如何".

来源: 参考旧框架 `strategies/cb_redemption/auditor.py` 设计.

## 职责定位

审计员**不看单次研究**, 只看**整个研究历史** (多次 batch 累计的 trajectory).

判断: 整个研究循环是在"学习"(OOS 持续改进 / IS 改进且 OOS 跟上), 还是在"数据挖掘"(IS 涨但 OOS 不动 / 反向).

**有否决权** — 任一非健康判定 → 强制暂停, 等用户审.

## 何时触发审计

- **模式 A (用户在场)**: 每完成 1 个研究 batch (即 1 次完整 6 步流程), 用户可以主动 trigger 审计员
- **模式 B (用户离场)**: 每完成 1 轮后**强制**触发审计员, 非健康 → 立刻退出模式 B, 不进下一轮

## 判定 verdict (4 种)

读 records (经验账本"已采用方向"和"已确认无效"的累计) 后, 审计员输出 1 个 verdict:

| Verdict | 触发条件 | 行动 |
| --- | --- | --- |
| **健康 (healthy)** | OOS 持续改进或稳定, 没有警告信号 | 允许继续 (模式 B 可进下一轮) |
| **停滞 (stagnant)** | 最近 ≥ 3 轮 OOS 改进幅度 < 阈值 (e.g. < 0.5%) | **否决** — 退出模式 B, 写汇总, 等用户 |
| **数据挖掘 (data_mining)** | IS 持续改进但 OOS 不动或反向; 或 OOS 单调下降 ≥ 3 轮 | **否决** — 退出, 标记"该方向疑似过拟合", 等用户 |
| **分歧 (diverging)** | OOS 单步下降幅度 ≥ 阈值 (e.g. -1%) | **否决** — 立刻退出, 关 spot, 等用户 |

## 阈值 (可调)

```
冷启动最小轮数: 3 (累计研究少于 3 轮时, 永远 healthy 不下负面判定)
近期窗口: 3 (最近 3 轮算"近期")
停滞阈值: 0.005 (|OOS delta| < 0.5% 视为停滞)
分歧阈值: -0.01 (OOS 单步降 ≥ 1% 视为分歧)
healthy 最低改进: 0.005 (近期窗口内 OOS 改进 ≥ 0.5% 才算 healthy)
```

## 审计员的输入

读经验账本的"已采用方向"和"已确认无效":
- 时间序列的 in-sample 指标 (e.g. selection_avg_excess)
- 时间序列的 out-of-sample 指标 (e.g. holdout-year replay)
- 假设和参数的 family (用于判断"是否在同一类方向上反复 try")

## 审计员的输出

格式:
```yaml
audit_report:
  timestamp: ISO8601
  verdict: healthy | stagnant | data_mining | diverging
  veto: true | false
  recent_runs_summary:
    - run_id: ...
      is_metric: ...
      oos_metric: ...
      delta: ...
  reasoning: <一段话解释为什么这个 verdict>
  recommendation:
    - 如果 stagnant: "暂停, 考虑换研究方向"
    - 如果 data_mining: "暂停, 此方向疑似过拟合, 标记 family X 为'需用户重新审视'"
    - 如果 diverging: "立刻停, OOS 急剧恶化, 可能引入了 lookahead 或数据 bug"
```

## 模式 B 中的特殊行为

模式 B 中, 审计员每轮都强制触发. 否决 → 立刻退出, **不进下一轮**.

例外: 第 1 轮 (冷启动 < 3 轮) 永远 healthy. 第 4 轮起开始能下负面判定.

## 审计员与 Claude 角色的关系

- **审计员逻辑由 Claude 实施** (Claude 是质疑层 + 文档层, 看历史)
- 审计员产出 audit_report 后, 必须写入 [experience_ledger.md](./experience_ledger.md) 的"审计日志"分区
- audit_report 是公开的, Codex 也读, 用于校验 Claude 没有"自己审计自己说健康"

## 双向对账 (硬化机制)

- Claude 在模式 B 中, 每轮完成必须写 audit_report
- Codex 在下一轮 DIRECT 收到前, 必须检查上一轮的 audit_report:
  - 缺 audit_report → 拒答下一轮 DIRECT
  - audit_report 是 veto (否决) → 不开跑下一轮, 写 HANDOFF/MODE-B-VETOED
- 这样防止 Claude 偷偷跳过审计员

## 例外

- 用户明说"忽略审计" → 临时跳过, 但报告必记录"用户主动跳过审计员, 风险:..."
- 审计员自身代码 bug → 默认输出"unknown" verdict + 暂停模式 B, 等人修
