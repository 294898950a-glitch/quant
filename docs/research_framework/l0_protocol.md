# L0 想法管理协议 (v3.3)

来源: 2026-05-15 用户在架构审查中(1) 第一性原理简化 5 → 3 入口, (2) 把软约束硬化为 Codex schema check.

L0 是研究流程的起点 — 想法从哪儿来、怎么入库、怎么排队、怎么挑下一个做.

## 3 个入口 (v3.3 简化)

| 入口 ID | 名称 | 来源 | 触发 | 关联文档 |
| ---: | --- | --- | --- | --- |
| 1 | 用户驱动 | 用户主动 / 用户给方向 | 用户说话 | 本文档 §a |
| 2 | AI 内省 | 经验账本 + L5 副产品自动写入 | 模式 B 启动 / 用户问"下一个" | 本文档 §b + experience_ledger.md |
| 3 | 外部学术 | arxiv | 用户主动 / 模式 B 启动前 / 每 14 天 | [paper_ingestion_protocol](./paper_ingestion_protocol.md) |

入口 1 = push (用户驱动); 入口 2, 3 = pull (AI 主动).

每条 L1 DIRECT **必含** `<!-- l0-entry-id: 1|2|3 -->` 标签(协议红线 U13 硬化, Codex 不到 → reject).

---

## §a. 入口 1 (用户驱动) — 两种子流程

### 1.1 用户主动 (具体)

用户直接说"验证 X 信号" → Claude 写规格 → 进 L1.

### 1.2 用户给方向 (模糊) — 协作出候选 (硬化数量)

1. **Claude 主菜**: 看用户描述, 提 **3-5 个**候选 (硬: ≥3 ≤5, Codex schema check, 超出范围 reject)
   - 每个含: 一句话假设 / 关联经验账本条目 / 工程难度 / 数据可用性
2. **Codex 副菜**: review + 可加 **1-2 个**候选 (硬: ≥0 ≤2, 不能 ≥3 抢主菜)
3. **Claude 排序**: 汇总 4-7 候选, 按 工程难度 / 数据可用性 / 收益估计 / 跨年泛化预期 排序
4. **用户挑 1 个** → 进 L1

时间预算: ≤ 1 小时. 超出 → 用户拍板"先做哪个".

消息格式 (Codex schema 校验):
```yaml
collaborative-ideation:
  candidates:    # Claude 提
    - {hypothesis: "...", ledger_ref: "...", difficulty: "易|中|难", data_avail: "已有|新数据"}
    - ... (至少 3, 至多 5)
  codex_additions:  # Codex 加
    - ... (至少 0, 至多 2)
```

## §b. 入口 2 (AI 内省) — 两种来源

### 2.1 经验账本未完成线索

模式 B 自动选研究. 算法 (Codex 端硬化代码, 不是文档):

```
1. 读 experience_ledger 分区三, 解析 priority 字段 (整数 0-100)
2. 排除 dependency 未满足的条目
3. 取 max(priority)
4. 同 priority 取 min(last_updated) (最旧)
5. 输出 next_research_entry
6. 校验:命中"已确认无效" → 弃, 取 top 2
```

模式 A: 用户挑. AI 不动 (除非用户问"推荐", 给基于 priority 的 top 3).

### 2.2 L5 副产品自动写入

Codex 在 L5 RESPONSE 必含 `### L5_side_findings` 段(可空但段必存, Codex schema 校验).

- 每条 side_finding 含: 现象一句话 / 数据 artifact 路径 / 是否跟原 batch 目标正交 (yes/no)
- Claude 收到后做 sanity check (3 步):
  - 跟"已确认无效" 冲突? 冲突 → 弃
  - 跟"未完成线索" 重叠? 重叠 → 合并
  - 可证伪? 不可 → 弃
- 通过 sanity → 自动写入 experience_ledger 分区三 (priority=50, source=2.2)
- **不在当前 batch 追**, 留作下一轮新 L0

## §c. 入口 3 (外部学术) — arxiv

详见 [paper_ingestion_protocol](./paper_ingestion_protocol.md). 关键点:

- 任何年份硬筛规则相同: **试盘 / 引用 ≥50 / 顶会 / 复现 stars ≥100** 任一即可
- 2025+ 仅排序优先 (高), 2020-2024 中, 2020 前低
- 每条候选必含 `passed_rule + evidence` 字段 (Codex schema 校验, 缺失自动剔除)

---

## §d. L0 早期查重 (硬化, 通用前置)

任何入口的 L0 进 L1 前必查 3 步, **Codex 端 U9 健全性检查第 0 层强制**:

| 步骤 | 查什么 | 命中后果 (Codex 自动) |
| --- | --- | --- |
| 1 | experience_ledger 分区二 (已确认无效) | HANDOFF/L0-DUPLICATE-REJECTED, 记录"曾被重提"+1 |
| 2 | experience_ledger 分区三 (未完成线索) | HANDOFF/L0-DUPLICATE-MERGE, 合并不另起 |
| 3 | strategies/cb_arb/tunable_space.yaml 黄绿区 | HANDOFF/L0-DUPLICATE-AUTOLOOP, 改成自动循环路径 |

Codex 在所有 L1 DIRECT 收到时, 第 0 层先跑查重, 通过才进入 U9 sanity check 主流程.

---

## §e. 想法成熟度分级 (硬化, 前置文档检查)

L0 成熟度不同, 进 L1 前必须有对应前置文档. Codex 端 schema 校验:

| 入口 ID | 成熟度 | 前置文档 (路径) | Codex 校验 |
| ---: | --- | --- | --- |
| 1.1 | 灵感 | `data/<run-id>/l0_intuition.md` | 文件存在 + 含强中弱假设 ladder |
| 1.2 | 协作候选 | 协作流程消息已存档 outbox | 无独立前置文档 |
| 2.1 | 已成熟线索 | 无 (账本本身即文档) | 不校验 |
| 2.2 | 数据异常 (副产品) | `data/<run-id>/l0_anomaly.md` | 文件存在 + 含统计异常 + 假设原因 |
| 3 | 文献方法 | `data/<run-id>/l0_reproduction.md` | 文件存在 + 含作者数据 vs 我方数据对齐 |

缺前置文档 → Codex 回 `HANDOFF/MISSING-L0-PRECONDITION`, 不开跑.

灵感扩展示例:
```markdown
原话: "我觉得 panic detector 现在过敏感"
强假设: panic detector 在 2022 触发 ≥5 次且每次都不对应实际下跌
中假设: panic detector 在 2022 触发 ≥3 次, 至少 50% 不对应
弱假设: panic detector 在 2022 触发频率 > 2020
能验证: 中假设
```

数据异常示例:
```markdown
异常: Round 5 selection_avg 4 年全部小幅改进 (+0.003 到 +0.007)
统计上是什么: 跨 4 年同方向 + 同量级 (非随机)
假设原因: r3_h010 改善挑选环节, 跟原 batch (修退出) 正交
是否值得验证: 是
```

复现报告示例:
```markdown
arxiv id: 2024.xxxxx
作者 Sharpe (原文): 1.8 (CB 2018-2023 US 数据)
我方数据 Sharpe (复现): 1.6 (CB 2018-2024 CN 数据)
gap 来源: 市场差异 / 标的不同 / 时间窗口
可移植性: 中 (Sharpe gap 0.2 在容忍内)
```

---

## §f. 优先级机制 (硬化算法)

experience_ledger 分区三 schema:

```yaml
未完成线索:
  - id: <auto>
    description: "<一句话>"
    priority: 0-100  # 整数
    level: 高|中|低  # 派生 (≥70=高, 30-69=中, <30=低)
    created_at: YYYY-MM-DD
    last_updated: YYYY-MM-DD
    source: l0-entry-id (1.1/1.2/2.1/2.2/3)
    dependency: [<id>, ...]  # 依赖
```

Codex 端硬化代码 `scripts/research_select_next.py`:

```python
def select_next_research(ledger_path) -> entry_id:
    entries = parse_ledger(ledger_path, section="未完成线索")
    eligible = [e for e in entries if all_deps_done(e)]
    if not eligible: return None  # 触发模式 B 退出
    max_p = max(e.priority for e in eligible)
    top = [e for e in eligible if e.priority == max_p]
    if len(top) > 1:
        top.sort(key=lambda e: e.last_updated)  # 同分取最旧
    selected = top[0]
    # 反查"已确认无效"
    if duplicate_in_rejected(selected, ledger_path):
        eligible.remove(selected)
        return select_next_research_recurse(eligible)
    return selected.id
```

模式 A 中, AI 调用此函数仅生成"推荐 top 3" 给用户, 不自动执行.

模式 B 中, AI 自动用 top 1, 跳过用户.

priority 动态调整:
- 跑完 1 轮 verdict=adopted → 移到分区一, priority 不再生效
- 跑完 1 轮 verdict=rejected → 移到分区二, priority 不再生效
- 跑完 1 轮 verdict=needs_followup → priority -10

---

## §g. U12 三轮收尾权 (硬化分歧计数)

Codex 端硬化 `disagreement_counter` per `spec_id`:

```python
class DisagreementTracker:
    def __init__(self): self.counters = {}  # spec_id → int

    def increment(self, spec_id, by_claude=False, by_codex=False):
        # 一来一回算一轮
        if spec_id not in self.counters: self.counters[spec_id] = 0
        if by_claude and self.counters[spec_id] % 2 == 1:
            self.counters[spec_id] += 1
        elif by_codex and self.counters[spec_id] % 2 == 0:
            self.counters[spec_id] += 1

    def check(self, spec_id, claude_msg):
        round_num = self.counters.get(spec_id, 0) // 2
        if round_num >= 3 and "round-3-deferral" not in claude_msg:
            return "REJECT: must write round-3-deferral after 3 rounds"
        return "OK"
```

Codex 收到 Claude 消息 → 调 `check`, 不通过 → 拒答, 让 Claude 改.

---

## 跟其他文档的关系

- HDRF.md: 总览, 提到 3 入口 + 链向本文档
- experience_ledger.md: priority schema 落地
- paper_ingestion_protocol.md: 入口 3 (arxiv) 专属子协议
- sanity_checker_role.md: 第 0 层 (L0 早期查重) 由它执行
- protocol_redline.md: U13 L0 硬化总入口

## 版本变更

- v3.0: L0 一句话, 无入口拆分
- v3.1 (2026-05-15 01:15): L0 拆 4 入口, 加 arxiv
- v3.2 (2026-05-15 01:30): 5 入口 + 协作流程 + 副产品 + 早期查重 + 优先级 + 成熟度
- v3.3 (2026-05-15 02:10): **第一性原理简化 5 → 3 入口** + arxiv 筛选修正 + 7 项软→硬约束
