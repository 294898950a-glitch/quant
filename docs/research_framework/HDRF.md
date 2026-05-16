# HDRF — 假设驱动研究框架

**Hypothesis-Driven Research Framework for Quant Strategy Development**

日期: 2026-05-16
设计来源: 2026-05-14 cb_arb CSI500 市场过滤器研究 + Codex review
版本: v3.3 / 协议红线 v1.5 对齐版

## 解决什么问题

现有的 `strategies/cb_arb/orchestrator_main.py` 自动循环框架只能干**一类事**: 在 `tunable_space.yaml` 已声明的参数范围内自动微调 current 值. 它**不能**做的是:

- 加新参数 (yaml 黄区, 必须人工 PR)
- 加新策略机制 (py 红区, 永远人工)
- 引入新数据源
- 设计 leave-N-out 真 CV 评估方法学
- 跨多个 baseline 做横向对比
- trade-level 反向诊断
- 决定该不该把新功能纳入生产

HDRF 覆盖这些"研究新功能 + 新参数 + 决定该不该用"的完整流程, 沉淀今天 cb_arb 研究的实战经验, 让质量底线可被强制 / 双向对账.

**当前主干已经收缩**: 新会话不需要先读完整 HDRF. 先读 `CURRENT.md` 和 `baseline_registry.md`; 只有要发起新研究时, 才回到本文的 L0-L7 流程.

## 8 层 workflow

```
┌─────────────────────────────────────────────────────────┐
│ L0 想法来源     3 入口     见下方 L0 详细 (用户/AI内省/arxiv) │
├─────────────────────────────────────────────────────────┤
│ L1 实验设计    Claude     spec.md (8 必填字段, Codex 检查) │
├─────────────────────────────────────────────────────────┤
│ L1.5 数据预检   Codex      新数据源 schema 检查 (硬约束) │
├─────────────────────────────────────────────────────────┤
│ L2 实施        Codex (人工PR)   加 yaml + 改 evaluator + 写 search │
├─────────────────────────────────────────────────────────┤
│ L3 跑 grid     Codex      sig 或 spot (按 spot 协议) → 必出 ranked + trades + daily_equity + trigger_dates │
├─────────────────────────────────────────────────────────┤
│ L4 自动质疑    Claude     5 项必跑 + 2 项条件必跑 (Codex 监督) │
├─────────────────────────────────────────────────────────┤
│ L5 数据论证 + 反向诊断  Codex   trade-level diff / equity diff / 触发归因 / 路径依赖 │
├─────────────────────────────────────────────────────────┤
│ L6 决策 + 复盘报告  Claude    三个出口: 采用 / 拒绝归档 / 生成下一轮迷你 spec → L2 │
│                              (报告 6 必填字段, Codex 检查)│
├─────────────────────────────────────────────────────────┤
│ L7 沉淀和推广   Claude+Codex  采用→提参数到 yaml 绿区; 无效→retro; 报告 commit │
└─────────────────────────────────────────────────────────┘

         ┌─────────────────────────────────────┐
         │ L6 修复尝试失败 → 迷你 spec → L2 → ... │
         │ (循环直到三个出口之一终结)             │
         └─────────────────────────────────────┘
```

## L0 想法来源详细 (v3.3)

L0 是研究流程的起点 — 想法从哪儿来. 第一性原理简化为 **3 入口**:

| 入口 ID | 名称 | 来源 | 触发 | 关联文档 |
| ---: | --- | --- | --- | --- |
| 1 | 用户驱动 | 用户主动 / 用户给方向 | 用户说话 | l0_protocol §a |
| 2 | AI 内省 | 经验账本 + L5 副产品自动写入 | 模式 B 启动 / 用户问"下一个" | l0_protocol §b + experience_ledger.md |
| 3 | 外部学术 | arxiv | 用户主动 / 模式 B 启动前 / 每 14 天 | paper_ingestion_protocol.md |

入口 1 = push (用户), 入口 2-3 = pull (AI).

**硬化机制** (协议红线 v1.5 U13-U16):
- 每条 L1 DIRECT 必含 `l0-entry-id` 标签, Codex schema check
- 入口 1.2 协作候选数量硬限制 (Claude 3-5, Codex 0-2)
- 入口 3 arxiv 候选必含 passed_rule + evidence 字段
- 进 L1 前 Codex 强制 L0 早期查重 (已确认无效 / 未完成线索 / yaml 黄绿区)
- 按入口 ID 检查对应前置文档 (灵感扩展 / 数据异常归因 / 复现报告) 存在
- 模式 B 选研究走 Codex 端确定性算法 (max priority / 同分取最旧)
- U12 三轮分歧计数器 Codex 端维护, 第 4 轮 Claude 必写 "round-3-deferral"

arxiv 筛选硬规则 (任何年份相同): **试盘 / 引用 ≥50 / 顶会 / 复现 stars ≥100** 任一即可. 2025+ 仅排序优先, 不是放水.

## GateKeeper 节点强制约定 (2026-05-16 加)

任何跑批 / 分析脚本启动时**必接 `scripts/gatekeeper.GateKeeper`**, 不能裸跑.

```python
from scripts.gatekeeper import GateKeeper
gate = GateKeeper()

# 节点 1: 启动 grid 前
gate.before_run_grid(spec_path)  # 不合规 → sys.exit(1), 不烧算力

# 节点 2: grid 跑完后
gate.after_run_grid(run_dir)  # 拦 manifest 不全 + 自动算 L4 数据

# 节点 3: 进入 L5 反向诊断前
gate.before_l5_diagnostic(run_dir)  # 拦 L4 ack 空填

# 节点 4: 改 CURRENT/baseline_registry 前
gate.before_commit_truth(run_dir)  # 完整 preflight
```

避免"跑完才知道 spec 不合规, 算力白烧". 不再靠 git commit 最后兜底.

不接 GateKeeper 的跑批脚本不许提交 git (pre-commit hook 检查 import 字符串).

## 硬约束矩阵 (v2 核心)

| 层 | 必填项 | 硬化方式 | 失败后果 |
| --- | --- | --- | --- |
| L1 | 假设 (一句话) | Codex 收到 DIRECT 检查 | 缺 → HANDOFF/MISSING-SPEC, 不开跑 |
| L1 | 参数空间 (维度 + 范围) | 同上 | 同上 |
| L1 | 硬约束底线 (replay_X ≥ floor_X) | 同上 | 同上 |
| L1 | 算力预估 (sig X 分钟 / spot Y 分钟) | 同上 | 同上 |
| L1 | 数据来源 (含 baseline floor 出处) | 同上 | 同上 |
| L1 | 停止条件 (什么时候放弃) | 同上 | 同上 |
| L1 | 真 CV 设计 (用 leave-Y 训练, Y 评估) | 同上 | 同上 |
| L1 | 输出物清单 (ranked + trades + daily_equity + trigger_dates) | 同上 | 同上 |
| L1.5 | 数据预检报告 (日期覆盖 / 缺失率 / 时区对齐 / 未来信息泄漏) | Codex 跑预检 + RESPONSE 内必含 | 缺 → 不进 L2 |
| L3 | 必出 4 类 artifact (上面 L1 输出物清单) | Codex 检查文件存在 | 缺 → 不进 L4 |
| L4 | Q1-Q5 必跑 | Claude 端 prompt 注入 + Codex 在 L5 RESPONSE 前检查 Claude ACK | Claude 漏 → Codex 拒答 L5, 退回 L4 |
| L4 | Q6 触发时点 / Q7 路径依赖 (有新机制时必跑) | 同上 | 同上 |
| L6 | 三个出口明确写 (采用 / 拒绝归档 / 迷你 spec) | Codex 看报告时检查 | 缺 → 拒绝 commit |
| L6 | 算力成本段 | git pre-commit hook + Codex 检查 | 缺 → 拒绝 commit |
| L6 | "已确认无效方向" 段 | 同上 | 同上 |
| L7 | retro 文档更新 (无效方向写入) | Codex 检查 retro mtime | 缺 → 拒绝 commit |
| L7 | 参数提升路径 (黄→绿 PR) | 人工 PR review | 标准 PR 流程 |

## 双向对账协议 (软约束的硬化方式)

软约束 (无法用 schema 检查的, 如 "5 项质疑的内容是否充分"): **Claude 端 + Codex 端双向 prompt 注入对账**.

1. **Claude 必做 / Codex 监督**:
   - Claude 每次收到 Codex L3 RESPONSE, prompt 自动注入: "现在必须跑 Q1-Q5 (有新机制时跑 Q6-Q7)"
   - Codex 收到 Claude L5 DIRECT 时, 检查 "Claude 是否在 DIRECT 里 ACK 已跑完 Q1-Q5"
   - 没 ACK → Codex 拒答, RESPONSE 内容是 "请先 ACK Q1-Q5 各项结果"

2. **Codex 必做 / Claude 监督**:
   - Codex 收到 Claude L1 DIRECT 时, prompt 自动注入: "立刻检查 spec 8 必填字段 + 估算算力按协议判断 spot / sig"
   - Claude 收到 Codex L1 ACK 时, 检查 "ACK 是否含 8 字段验证结果 + 算力估算结果"
   - 没含 → Claude 不继续, 退回让 Codex 补

3. **每次研究开始, Claude 必跑 enforcement checklist** (记忆中固化):
   - [ ] L1 spec 8 字段齐全
   - [ ] L1.5 数据预检报告齐全 (新数据源时)
   - [ ] L3 artifact 4 类齐全
   - [ ] L4 Q1-Q5 跑完且记录
   - [ ] L6 三个出口 + 算力成本 + 已确认无效方向

## 强骨架 + 弹性内容 (v1 保留)

| 部分 | 性质 | 内容 |
| --- | --- | --- |
| L0 假设格式 | 强骨架 | 一句话写清楚 "什么信号 / 改善什么目标 / 在什么范围" |
| L1 spec 字段 | 强骨架硬约束 | 8 必填项 (上表) |
| L1.5 数据预检 | 强骨架硬约束 | 新数据进来必检 |
| L4 必跑项 | 强骨架硬约束 | Q1-Q5 + 条件 Q6-Q7 |
| L6 三出口 | 强骨架硬约束 | 必须写明 |
| L7 沉淀 | 强骨架硬约束 | retro / yaml 提升 / commit |
| L2 实施细节 | 弹性 | 加哪些参数、改哪些代码因假设而异 |
| L3 grid 规模 | 弹性 | 按 spot 协议估时 |
| L5 反向诊断角度 | 弹性 | 看 trade / equity / panic dates 视疑点而定 |

## 与已有自动循环框架的关系 + 灰区

```
┌──────────────────────────────────────────────────────────┐
│  HDRF: 加旋钮 + 测旋钮 + 决定该不该用                       │
│      (人工 + LLM 协作, 走 L0-L7)                            │
│                                ↓ (L7 推广)                  │
│      新参数提升到 yaml 绿区                                 │
│                                ↓                            │
│  自动循环框架 (cb_arb_orchestrator):                         │
│      绿区参数空间下自动微调找最优                            │
└──────────────────────────────────────────────────────────┘
```

灰区任务的归属:

- 只改 yaml `current` 值 / 已声明绿区内搜索 → **自动循环框架**
- 新增参数 / 新数据源 / 新 evaluator 逻辑 / 新 hard floor / 新 CV 方法 → **HDRF**
- 修 bug + 调一个参数:
  - 如果 bug 改变策略语义或历史结果 → HDRF (L2 实施 + L6 复盘)
  - 如果只是工程修复且参数已在绿区 → 自动循环
- 扩大已有参数 range:
  - 小范围校准 → 自动循环
  - 改变研究假设或风险边界 → 写 mini-spec 走 HDRF

## 子文件

**流程模板**:
- [spec_template.md](./spec_template.md) — L1 实验设计模板 (8 必填字段)
- [questioning_checklist.md](./questioning_checklist.md) — L4 质疑清单 (Q1-Q5 + Q6/Q7 条件)
- [report_template.md](./report_template.md) — L6 复盘报告模板 (三出口 + 6 必填段)

**协议机制**:
- [protocol_redline.md](./protocol_redline.md) — **协议红线** v1.5 (每条 outbox 必附顶部)
- [enforcement_protocol.md](./enforcement_protocol.md) — 硬化机制说明

**角色定义**:
- [auditor_role.md](./auditor_role.md) — 审计员: 看 trajectory, 4 verdict, 有否决权
- [memory_role.md](./memory_role.md) — 记录员: jsonl 持久化, 防重复
- [sanity_checker_role.md](./sanity_checker_role.md) — 健全性检查员: 跑前内容合法性

**自动循环**:
- [autonomous_loop_protocol.md](./autonomous_loop_protocol.md) — 模式 B 用户离场自动循环
- [autonomous_summary_template.md](./autonomous_summary_template.md) — 模式 B 结束后给用户的汇总报告

**经验积累**:
- [experience_ledger.md](./experience_ledger.md) — 经验账本 (4 分区, 人类可读)
- [paper_ingestion_protocol.md](./paper_ingestion_protocol.md) — L0 第 3 入口: arxiv 论文导入 (筛选规则 + 操作流程)

## 参考案例

- `reports/cb_arb_csi_market_filter_2026-05-14.md` — HDRF v1 第一个完整研究, 失败结论但诚实

## 版本变更日志

- **v1 (2026-05-14 23:00)**: 初版, 7 层 workflow, 软约束为主
- **v2 (2026-05-14 23:30)**: Codex review 后硬化版
  - 加 L1.5 数据预检
  - L5 改名 "数据论证 + 反向诊断"
  - L6 三出口 + 循环回头
  - L4 加 Q6/Q7 条件必跑
  - 加 L7 沉淀和推广
- **v3 (2026-05-15 00:00)**: 加角色 + 模式 B + 协议红线分层注入 + 经验账本
  - 加 L0 想法来源 (3 入口)
  - 加 模式 A / 模式 B 双模式
  - 加 审计员 / 记录员 / 健全性检查员 三个角色 (参考旧 cb_redemption 框架)
  - 协议红线 v1.0 (11 条 U + 5 条 B + 模式 B 上限)
  - 经验账本 (4 分区)
  - 模式 B 自动执行边界
  - 模式 B 停止上限 (¥100 / 5 轮 / 12 小时 / 探索空)
- **v3.1 (2026-05-15 01:15)**: L0 第 4 入口 — arxiv 论文导入
  - L0 从 3 入口扩展为 4 入口
  - 论文筛选硬规则: 2025+ 不限其他, 2020-2024 + 试盘/引用 50+/顶会/复现 stars 100+
  - 加 paper_ingestion_protocol.md
  - 默认 14 天检索一次, 模式 B 启动前必检一次
- **v3.3 / redline v1.5 (2026-05-16)**: 当前对齐版
  - L0 第一性原理简化为 3 入口: 用户驱动 / AI 内省 / arxiv
  - 论文筛选规则改为任何年份同一硬筛, 2025+ 只排序优先
  - 加 U14 心跳, U15 CURRENT/baseline 同步, U16 状态机 + run manifest
  - CURRENT.md 成为新会话 3 个入口之一 (当前真值层), baseline_registry 和 docs/INDEX 是另外两个
