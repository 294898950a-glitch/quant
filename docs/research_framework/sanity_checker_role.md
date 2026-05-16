# 健全性检查员角色

> **本文件: 描述性参考, 非机器约束源**.
> 真正的机器强制规则在 `scripts/validate_*.py` + `data/research_framework/*.yaml`.
> 本文件解释"为什么这么做" + 历史背景, 不写"必须如何".

来源: 参考旧框架 `strategies/cb_redemption/sanity_checker.py` 设计.

## 职责定位

在 Codex 跑实验**之前**, 先快速检查"参数和数据是不是合法"——拒掉机械上不可能跑通的组合, 不浪费算力.

跟 schema 检查的区别:
- **schema 检查** (协议红线 U3): 检查 spec 有没有 8 必填字段 — 形式合规
- **健全性检查** (本文档): 检查 8 必填字段的**内容是不是合法 + 自洽** — 语义合规

## 两层检查

### 第 1 层: 硬规则 (纯代码, 零成本, 每次必跑)

针对已知失败模式的确定性规则. 当前规则:

| 规则 | 失败示例 |
| --- | --- |
| 时间窗口不能比数据范围长 | window=200 + 数据 101 天 → 回测无法稳定 |
| 维度内部不能自相矛盾 | trend_short_window >= trend_long_window |
| 硬约束底线必须在合理范围 | replay_2020 ≥ 999 (不可能达到) |
| 候选数量必须 ≥ 1 | grid 维度交叉空集 |
| 数据来源必须存在 | path 文件不存在 / parquet 列缺失 |
| baseline 必须可读 | summary CSV 缺 baseline 行 |
| 真 CV 设计自洽 | leave year 不在 replay_years 里 |

新规则添加: 每次研究发现一个"早该拦下的失败模式", 把它写进硬规则.

### 第 2 层: LLM 评估 (DeepSeek 调用, 事件驱动, 不每次跑)

事件触发:
- 研究启动时
- 数据来源切换后
- 池子轮换后
- 审计员标记 "数据挖掘" 后
- 每 20 轮一次心跳

LLM 评估接收 spec 完整内容, 输出 sanity report (验证内容合理性).

## 输出格式

```yaml
sanity_report:
  verdict: pass | warning | fatal
  hard_rules:
    passed: [rule_id, rule_id, ...]
    failed: [{rule_id, fail_reason}, ...]
  llm_evaluation: (if triggered)
    verdict: pass | warning | fatal
    reasoning: <一段话>
  recommendation:
    - 如果 fatal: "spec 有内部矛盾, 不可开跑; Codex 必须回 HANDOFF/SANITY-FATAL"
    - 如果 warning: "可以开跑, 但 Claude 应该补充澄清"
    - 如果 pass: "通过, 可开跑"
```

## 触发时机

**每次 Codex 收到 L1 DIRECT 后**, 协议红线 U9 强制 Codex 跑 sanity_check, **晚于** schema 检查 (U3) 但**早于**算力估算 + 开跑.

流程:
```
L1 DIRECT 到达 Codex
  ↓
schema check (U3): 缺字段? → HANDOFF/MISSING-SPEC
  ↓
sanity check (U9): 内容合法?
  ├─ fatal → HANDOFF/SANITY-FATAL, 不开跑
  ├─ warning → ACK 含 warning, 等 Claude 澄清
  └─ pass → 继续算力估算 + 开跑
```

## 与其他角色的关系

- **vs 审计员**: 审计员看 trajectory (多轮历史), sanity checker 看单轮 input
- **vs 记录员**: sanity report 写入 `data/research_framework/sanity_reports/<run-id>.json`, 后续审计员可读

## 例外

- 用户明说"急, 跳过 sanity" → 临时跳过, 但报告记录 (per 协议红线 U7)
- 硬规则误报 → 调整硬规则, 不能单次跳过

## 实施位置

- 硬规则代码: `scripts/sanity_checker.py` (Codex 端, sig VM 上)
- LLM 评估: Codex 调 DeepSeek
- 触发: Codex 接收 L1 DIRECT 处理流程里硬编码

(初版可只实现硬规则, LLM 评估后补)
