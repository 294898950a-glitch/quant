# HDRF 硬化机制 (enforcement protocol)

把 HDRF v1 的"软约束"升级成可强制 / 可双向对账的硬约束.

## 三层硬化

### 第一层: schema 检查 (机器可强制)

Codex 收到 DIRECT 时, 自动跑一遍 spec 完整性 schema:

```yaml
spec_schema:
  required_fields:
    - hypothesis            # 一句话假设
    - parameter_space       # 维度 + 范围
    - hard_floors           # replay_year ≥ value
    - compute_estimate      # sig X min / spot Y min
    - data_sources          # 含 baseline floor 出处
    - stop_conditions       # 什么时候放弃
    - true_cv_design        # 用 leave-Y 训练, Y 评估
    - output_artifacts      # ranked + trades + daily_equity + trigger_dates
```

任一字段缺失或为空 → Codex 立刻回 `HANDOFF/MISSING-SPEC`, 不开跑.

### 第二层: 双向对账 (LLM 软约束硬化)

无法 schema 检查的内容 (如 "Q1-Q5 的内容是否合理"), 用双向 prompt 注入对账:

**协议规则**:

| 谁必做 | 谁监督 | 触发点 | 监督方式 |
| --- | --- | --- | --- |
| Claude 必跑 Q1-Q5 (L4) | Codex 在 L5 检查 | Codex 收 Claude L5 DIRECT | DIRECT 必含 "Q1-Q5 各项结果 ACK", 缺 → 拒答 |
| Codex 必检 spec (L1) | Claude 在 L1 ACK 看 | Claude 收 Codex L1 ACK | ACK 必含 "8 字段验证结果 + 算力估算", 缺 → 退回 |
| Codex 必跑数据预检 (L1.5) | Claude 在 L2 前看 | Claude 收 Codex L1.5 RESPONSE | 必含 "日期覆盖 / 缺失率 / 时区 / 未来信息" 4 项, 缺 → 退回 |
| Codex 必出 4 类 artifact (L3) | Claude 在 L4 前看 | Claude 收 Codex L3 RESPONSE | 必含 "ranked / trades / daily_equity / trigger_dates" 4 个文件路径, 缺 → 退回 |
| Claude 必写三出口 (L6) | Codex 在 commit 前看 | Codex 收 Claude L6 commit 请求 | 报告必含 "采用 / 拒绝归档 / 迷你 spec" 三选一, 缺 → 拒绝 commit |
| Claude 必写算力成本 + 已确认无效 (L6) | Codex 在 commit 前看 | 同上 | 同上 |

### 第三层: 系统钩子 (git / repo 级)

- **git pre-commit hook**: 提交报告时检查必填段是否存在 (算力成本 / 已确认无效方向 / 三出口判断)
- **Codex 端流程包装**: Codex 接收 DIRECT 前必走 schema 验证, 不通过不进流程
- **Claude memory 强 reminder**: 关键节点 (L4 / L6) 触发 reminder, prompt Claude 自检

## 各角色的检查清单

### Claude 角色清单 (每次研究流程必走)

研究开始时:
- [ ] L0: 用户假设清晰记录
- [ ] L1: spec.md 8 必填字段齐全
- [ ] 发送 DIRECT 前自检: 上面 8 项

收到 Codex L1 ACK:
- [ ] ACK 含 8 字段验证结果? 否则退回
- [ ] ACK 含算力估算? 否则退回
- [ ] 算力档位决策 (sig / 起 spot / 必 spot)?

收到 Codex L1.5 数据预检 RESPONSE:
- [ ] 含 日期覆盖 / 缺失率 / 时区对齐 / 未来信息泄漏 4 项? 否则退回

收到 Codex L3 grid RESPONSE:
- [ ] 含 ranked.csv? trades.csv? daily_equity.csv? trigger_dates.csv? 缺一退回
- [ ] **接下来必跑 Q1-Q5** (有新机制时跑 Q6-Q7)

L4 跑质疑后, 发 L5 DIRECT 时:
- [ ] DIRECT 内必含 "Q1-Q5 各项 pass/warning/fail 结果"
- [ ] 任一 fail 项必含对应数据论证请求

L6 写报告时:
- [ ] 三个出口选一: 采用 / 拒绝归档 / 迷你 spec
- [ ] 算力成本段写
- [ ] "已确认无效方向" 段写
- [ ] "未来值得探索方向" 段写

L7 沉淀时:
- [ ] 采用 → 在 yaml 加新参数到绿区, 发 PR
- [ ] 拒绝归档 → 更新 retro 文档 (cb_arb_cross_eval_retro 或类似)
- [ ] 迷你 spec → 写 mini-spec 回到 L2

### Codex 角色清单 (每次接收 DIRECT 必走)

收 L1 DIRECT:
1. 跑 spec_schema 检查 8 字段
2. 缺 → 写 `HANDOFF/MISSING-SPEC`, 不开跑
3. 通过 → 估算算力, 按 spot 协议判断
4. 写 ACK: 含 8 字段验证 + 算力估算 + 建议 sig / 起 spot / 必 spot

收 L1.5 (数据预检):
1. 跑 4 项检查: 日期覆盖 / 缺失率 / 时区 / 未来信息
2. 写 RESPONSE: 4 项结果齐全

收 L3 (跑 grid):
1. 按 spec 跑
2. 出 4 类 artifact (ranked / trades / daily_equity / trigger_dates)
3. 写 RESPONSE: 4 类 artifact 路径齐

收 L5 DIRECT (Claude 跑完质疑请求论证):
1. 检查 DIRECT 是否含 Q1-Q5 各项 ACK
2. 缺 → 写 `HANDOFF/MISSING-Q1-Q5`, 拒答
3. 通过 → 用现有数据论证 fail 项

收 L6 commit 请求 (报告):
1. 检查报告含三出口 / 算力成本 / 已确认无效方向
2. 缺 → 拒绝 commit
3. 通过 → 进 L7 沉淀

## 例外条件

- 用户明确说"急, 跳过 X 检查" → 临时降级, 但报告里必须记录 "本次研究跳过 X 检查, 原因 ..."
- 用户说"先放着不管" → 暂停, 任何后续步骤不能跳过本协议

## 维护

- 每次发现新的"软约束应该硬化"模式, 加到对账协议表
- 每次发现某个硬约束太死 (合理研究被挡住) → 评估降级方式, 不能直接删
