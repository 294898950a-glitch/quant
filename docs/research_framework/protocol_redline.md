# 协议红线 v1.4

每条 Claude ↔ Codex outbox 消息**顶部必须附本红线段**(16 行内). 双方收到消息时先检查版本号,版本不一致 → 暂停协作同步版本.

## 红线段(每条消息顶部复制粘贴)

```
<!-- protocol-redline-v1.4 -->
U1 数据/事实来源: 结论必须指向 CSV/parquet/报告/记录/VM 输出, 不准编
U2 执行位置: 重活只能在远端 VM 跑, local 只能编辑文档 / 读代码 / 轻量检查
U3 任务清单完整: 我发研究任务必含 8 字段 (假设/参数空间/底线/产物/算力/数据来源/真 CV 设计/停止条件)
U4 算力 ACK: Codex 收任务立刻报 sig 估时 + spot 协议判档
U5 质疑义务: Claude 收实验结果必跑标准疑点清单 (新机制时加 Q6/Q7); Codex 收论证请求必检 Claude 是否 ACK
U6 跳过留痕: 任何跳过的项必须明写"为什么跳过", 否则消息不被接受
U7 用户急单降级: 用户明说"急" 可临时跳过, 但报告必须记录跳过项 + 原因 + 补跑条件
U8 不重复不抢占: 发消息前查对方 outbox + state, 已 ACK/RUNNING 不重复处理; 模式 B 选研究前必查 tried_directions.jsonl
U9 健全性检查: 跑回测前 Codex 必跑 sanity_check, 参数和数据兼容性 fail → 拒跑
U10 审计员义务 (模式 B): 每轮完成 Claude 必写 audit_report; Codex 下一轮 DIRECT 前必检, 缺/否决 → 不开下一轮
U11 记录员义务: 研究流程关键节点必写 jsonl (设计完 / 跑完 / 报告完 / 切换模式 / 审计), 不能遗漏
U12 三轮收尾权: Claude↔Codex 辩论 ≤3 轮无共识, 听 Codex; Claude 不转用户除非触红线/预算/框架
U13 L0 硬化: L1 DIRECT 必含 l0-entry-id 标签 + 入口对应前置文档; arxiv 候选必含 passed_rule+evidence; 协作候选数 Claude 3-5/Codex 0-2; Codex 端跑 L0 查重 + 优先级算法 + U12 分歧计数
U14 进度心跳: Codex 处理任一任务总耗时 >10 分钟时必每 10 分钟在 outbox 写 PROGRESS (task/进度%/ETA/阻塞), 静默 >10 分钟 = 违规, Claude 端会主动催 HEARTBEAT-MISSING
U15 策略真值同步: RESPONSE 改策略真值 (新 baseline / status 切换 / 立项 / commit 关键文件) 必同 handoff 更新 docs/research_framework/CURRENT.md + data/research_framework/baseline_registry.md, 或显式说"为什么不更新"; 不触发 = 单纯 ACK/recon/schema/算力估算

模式 B 沙盒 (用户离场自动循环时, 双方都不准):
B1 改代码 (strategies/*.py / scripts/*.py / evaluator)
B2 加新数据来源
B3 加新参数到 yaml 黄区
B4 改协议红线本身
B5 碰经验账本里"已确认无效"区域
```

## 详细说明

### U1 数据/事实来源

- ❌ "我估计...""我认为...""可能..." → 不算结论
- ✅ "根据 panic_leave_2020 ranked.csv, 候选 top1 replay_2020 = X" → 合规
- ✅ "根据 git log, 上次 iter 是 11" → 合规

### U2 执行位置

详见 [reference_spot_start_criteria](../../.claude/memory/reference_spot_start_criteria.md):
- local WSL: 编辑 outbox / 改 reports / 改 memory / 写 spec / 读 CSV stat
- tencent-sig (2 vCPU 长期开): 轻分析 / 单点回测 / Codex 自身
- spot (16 vCPU 按需): 网格搜索 / 多 leave / 多回测

### U3 任务清单 8 字段

详见 [spec_template](./spec_template.md). 必含:
1. hypothesis (一句话假设)
2. parameter_space (维度 + 范围)
3. hard_floors (replay_year ≥ floor)
4. output_artifacts (ranked + trades + daily_equity + trigger_dates)
5. compute_estimate (sig X min / spot Y min)
6. data_sources (含 baseline 出处)
7. true_cv_design (用 leave-Y 训练, Y 评估)
8. stop_conditions (什么时候放弃 + 升级条件)

缺任一字段 → Codex 回 `HANDOFF/MISSING-SPEC`, 不开跑.

### U4 算力 ACK 格式

```
protocol-redline-v1.0 OK
spec check: pass (or missing: U3-field-X)
runtime estimate: sig ~N min, spot bucket ≤30/30-120/≥120
claim: running / waiting-user / no-run
```

### U5 质疑义务详见 [questioning_checklist](./questioning_checklist.md)

- Q1-Q5 每次 grid 都跑
- Q6-Q7 在新机制改变交易路径时加跑

### U6 跳过留痕格式

```
SKIPPED: <项 ID>
REASON: <为什么>
RISK: <跳过带来的风险>
COMPENSATE: <如何补 / 何时补>
```

### U7 急单降级条件

只有用户明确说 "急" / "立刻" / "马上" 才触发. 不可由 LLM 自行判定.

### U8 不重复不抢占

每条 outbox 消息有 hash (`<!-- msg-hash-XXX -->`), 接收方 cache 已处理 hash. 已处理 → 跳过.

---

## 模式 B 沙盒 (用户离场自动循环时)

进入模式 B 触发条件:
- 用户明说"自动跑" / "无人值守"
- 用户连续 5 次心跳无回应 + 当前算力账户余额 + 当前研究 batch 完成

模式 B 中 AI 只能:
- ✅ 调已经在 yaml 绿区的参数
- ✅ 跑标准 6 步流程
- ✅ 用经验账本里的"未完成线索 / 未来探索方向"挑下一个

模式 B 中 AI **绝对不能**(B1-B5):
- ❌ 改代码 (任何 .py)
- ❌ 加新数据来源
- ❌ 加新参数到 yaml 黄区
- ❌ 改协议红线本身
- ❌ 碰经验账本里"已确认无效"区域

模式 B 停止条件 (任一触发):
- 累计算力花费 ≥ ¥20
- 累计跑了 ≥ 5 轮
- 用户离场 ≥ 12 小时
- 经验账本"可探索"列表空

任一触发 → 停 + 关 spot + 写 [autonomous_summary](./autonomous_summary_template.md) 等用户回来.

---

## U12 三轮收尾权 (v1.1 新加)

适用范围: Claude ↔ Codex 之间关于 spec 细节 / 参数空间 / 信号选择 / 评估指标 / 实现路径等任务决策分歧.

机制:
- 一轮 = Claude 提点 + Codex 回应, 或 Codex 提点 + Claude 回应 (一来一回算一轮)
- ≤ 3 轮无共识 → Claude 在 outbox 写明 "round-3-deferral: 接受 Codex 方案 X, 不再讨论" 后接受 Codex 决定
- 不能转用户仲裁, 除非触发任一:
  - 红线 U1-U11 / B1-B5 任一条
  - 算力预算上限调整
  - 框架本身变更 (L0-L7 / 角色 / 模式 A/B 边界)
  - Codex 主动 ESCALATE

例外: 用户主动介入 (outbox 或直接消息) → 用户拍板, U12 当次失效.

## U13 L0 硬化 (v1.2 新加, 详见 l0_protocol.md)

7 项硬化分配 Claude 端 / Codex 端:

| # | 硬化项 | Claude 端动作 | Codex 端动作 |
| --- | --- | --- | --- |
| 1 | l0-entry-id 标签 | L1 DIRECT 顶部加 `<!-- l0-entry-id: 1\|2\|3 -->` | schema check, 缺 → HANDOFF/MISSING-L0-ENTRY |
| 2 | 协作候选数量 | Claude 提 3-5; 数组长度自检 | Codex 校验长度, 加 0-2, 越界 → reject |
| 3 | arxiv 筛选证据 | (n/a, Codex 侧产出) | 写候选 markdown 时每条必含 passed_rule + evidence, 缺 → 剔除 |
| 4 | L0 早期查重 | (n/a) | U9 sanity check 第 0 层: 查已确认无效 / 未完成线索 / yaml 黄绿区 |
| 5 | 优先级算法 | (n/a, AI 在场用户挑) | 模式 B 调 `scripts/research_select_next.py`: max priority / 同分取最旧 / dependency 排除 |
| 6 | 前置文档校验 | Claude 写 L1 前先建 `data/<run-id>/l0_<level>.md` | Codex 收 L1 DIRECT 时按 l0-entry-id 校验前置文档存在 |
| 7 | U12 分歧计数 | Claude 第 4 轮必含 "round-3-deferral" 字串 | Codex 维护 `disagreement_counter` per spec_id |

实施: 见 [l0_protocol.md §c-§g](./l0_protocol.md).

## U14 进度心跳 (v1.3 新加)

规则:
- Codex 收到任一 DIRECT 后, 如果**总处理耗时超过 10 分钟**, 必须每 10 分钟在 outbox 追加一条 PROGRESS 消息
- PROGRESS 消息格式:
  ```
  ### YYYY-MM-DD HH:MM CST - Codex - PROGRESS/<task-id>

  <!-- protocol-redline-v1.3 -->
  Project: quant
  Task: <task 名>
  Elapsed: X min / 起算从 ACK 时间
  Progress: <0-100>%
  Current step: <一句话当前在做什么>
  ETA to next milestone: <Y min> (or "unknown")
  Blockers: <none / 列表>
  ```
- 即使无进展也要写: "Progress: 同上, 阻塞 = <具体原因>"

Claude 端配套机制:
- Claude 监控 outbox, 任一 active DIRECT 在 ACK 后超过 11 分钟无 PROGRESS → 写 HEARTBEAT-MISSING 消息催 Codex
- HEARTBEAT-MISSING 是 escalation 信号, 不算分歧 (不进 U12 三轮计数)

例外:
- 任务总耗时 < 10 分钟 → 不需 PROGRESS, 直接出 RESPONSE
- Codex 正在跑 spot 回测 (Python 进程占据) → 用旁路写 PROGRESS (sig VM 写 outbox), 不能用"我在跑代码所以没法回" 当借口
- 网络断 / outbox 文件锁住 → 算违规, 但优先恢复, 用 ESCALATE 描述

为什么要这条:
用户 2026-05-15 03:25 CST 明确: "Codex 每次都静默" 不可接受. 静默 ≥10 分钟意味着用户无法判断状态 (跑着 / 卡住 / 退出 / 关闭), 直接破坏协作信任.

## U15 策略真值同步 (v1.4 新加)

**铁律**: 任何 Claude/Codex RESPONSE 改变**策略真值**时, 必须**同 handoff** 更新 `docs/research_framework/CURRENT.md` 和 `data/research_framework/baseline_registry.md`, 或在 RESPONSE 里显式说"为什么不更新".

**触发更新事件** (策略真值变化):
- 跑出新 baseline 数字 (跨年 / 单年 / OOS 全段 / 任何 fresh metric)
- 策略状态切换 (adopted ↔ rejected ↔ WIP ↔ archived)
- 新策略立项 / 撤销立项
- 关键文件 commit / promote (research → main) / quarantine

**不触发** (避免 CURRENT.md 被噪音淹没):
- 单纯 lightweight reconnaissance (ls / cat / grep)
- 单纯 schema check (健全性验证, 不出 metric)
- 单纯算力估算 (估时间不跑回测)
- 单纯 idle ACK / PROGRESS / heartbeat

**强制内容**:
- baseline_registry.md 加新行 (历史不可删, 只 supersede)
- CURRENT.md 对应策略段更新 (累计 excess / status / 下一步 owner / artifact 链接)

**违反后果**:
- 下次新会话 Claude 必反复读错策略状态 → 浪费用户时间纠错 (今天 cb_arb 主策略 vs value-gap switch 混淆已暴露)
- "做完研究 → 写到 report → 不更新 CURRENT" 是 framework 核心病灶, 必须红线

**Claude 端配套**:
- 每次写 RESPONSE 前自查: 这次有没有改策略真值? 有 → 同 handoff 改两个文件
- 经验账本仍是历史, autonomous_summary 仍是回顾 — 不能替代 CURRENT.md

**为什么要这条**:
2026-05-15 framework debate 暴露 — Claude 反复读错 "哪个是主策略 / 当前 baseline 是多少", 因为信息散在 sig saved_best / local 6 个 report / 经验账本 / git modified-untracked, 没有权威入口. 用户原话 "整个研究框架对研究结果保存不够完整, 所以你无法读取". 这条规则是根因解.

## 版本管理

- 当前版本: **v1.4**
- 升级规则:
  - 一个研究 batch 完成 → 复盘 → 如发现新铁律, minor +1 (v1.0 → v1.1)
  - 用户对协议本身提方向性要求 → major +1 (v1 → v2)
  - 单次研究中途绝对不改
- 升级流程:
  - 一方写 `PROTOCOL/REDLINE-CHANGE` 消息, 含 old/new version + diff
  - 另一方 ACK 或反对
  - 用户拍板
  - 下一个 batch 起生效

## 例外条件

- 用户明说"急" → 临时降级 U3-U8 任一,但报告必记录跳过项
- 用户说"暂停" / "停" → 所有 outbox 流程冻结, 模式 B 立即退出
- 阻断层 (U1, U2) 不允许跳过, 用户说急也只能选"不做这件事"
