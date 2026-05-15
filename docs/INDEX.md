# 文档地图 (INDEX)

**最后更新**: 2026-05-16
**用途**: 新会话 / 新人进项目时, 从这里看清所有文档的功能分类. 不要在主目录乱翻.

---

## 入口顺序 (新会话先看这 4 个, 跟 README 保持一致)

1. **`docs/research_framework/CURRENT.md`** — **当前真值**, 每个策略状态 / 当前成绩 / 下一步等谁
2. **`docs/INDEX.md`** — 本文件, 文档地图
3. **`data/research_framework/baseline_registry.md`** — 成绩单档案, 历史每次回测出的数字 (immutable-ish)
4. **`docs/research_framework/experience_ledger.md`** — 经验账本 (4 分区: 已采用 / 已无效 / 未完成 / 未来)

看完这 4 个文件就知道 "现在该做什么". 其他文件 (协议 / 流程 / 角色 / 模板 / 报告 / 工具) 按需翻, 见下面分类.

---

## 协议层 (硬约束规则, 双方必须遵守)

| 文件 | 内容 |
|---|---|
| `docs/research_framework/protocol_redline.md` | **主协议 v1.5**, 17 条规则 (U1-U16), 红黄绿区, 模式 B 沙盒 |
| `docs/research_framework/enforcement_protocol.md` | 协议违规的判断和处理 |
| `docs/research_framework/l0_protocol.md` | 立项 (L0) 阶段的硬化规则 (3 个 idea source + 7 项 schema check) |
| `docs/research_framework/paper_ingestion_protocol.md` | arxiv 论文检索 / 候选 / 立项的流程 |
| `docs/research_framework/autonomous_loop_protocol.md` | 模式 B (用户离场) 自动循环协议 |

---

## 流程层 (怎么做研究)

| 文件 | 内容 |
|---|---|
| `docs/research_framework/HDRF.md` | **手工研究流程** L0-L7 (立项 → 跑回测 → 复盘 → 沉淀) |
| `docs/research_framework/questioning_checklist.md` | Claude 收到结果时的标准疑点清单 (Q1-Q7) |

---

## 角色定义 (每个 AI 的责任)

| 文件 | 角色 |
|---|---|
| `docs/research_framework/auditor_role.md` | 审计员 (自动调参时判过拟合) |
| `docs/research_framework/memory_role.md` | 记忆员 (每轮存档 + 防重复) |
| `docs/research_framework/sanity_checker_role.md` | 健全性检查员 (跑前校验) |

---

## 模板 (具体格式)

| 文件 | 用途 |
|---|---|
| `docs/research_framework/spec_template.md` | 立 spec 时的格式 (假设 / 参数 / 数据 / 算力 / 停止条件) |
| `docs/research_framework/report_template.md` | 复盘报告的格式 |
| `docs/research_framework/autonomous_summary_template.md` | 每日自治总结的格式 |
| `docs/research_framework/run_manifest_schema.md` | **跑批档案 YAML schema** (跑回测必同 handoff 写) |

---

## 真值层 (当前状态 / 历史经验)

| 文件 | 内容 |
|---|---|
| `docs/research_framework/CURRENT.md` | **当前真值** (每个策略一段) |
| `docs/research_framework/experience_ledger.md` | 经验账本 (4 分区: 已采用 / 已无效 / 未完成 / 未来探索) |
| `data/research_framework/baseline_registry.md` | 成绩单档案 (immutable-ish, 每次跑出新成绩加一行) |
| `data/research_framework/strategies.yaml` | 策略 ID 和假设 ID 单源 |
| `data/research_framework/data_schema_expectations.yaml` | 数据字段预期 (跑前 validate_data_schema 校验) |

---

## 计划 / 路线图

| 文件 | 内容 |
|---|---|
| `docs/plans/` | 各种计划文件 (按日期命名) |

---

## 复盘报告

| 位置 | 索引 |
|---|---|
| `reports/INDEX.md` | **按策略 + 日期分类的报告索引** |

`reports/` 目录里 20+ 篇报告平铺, 实际按策略找请看 `reports/INDEX.md`.

---

## 自动检查工具 (跑前 / commit 前)

跑前 / commit 前自动跑, 不通过会 block:
- `scripts/framework_preflight.py` — 聚合所有 validator + dirty-file inventory
- `scripts/validate_current_md.py` — CURRENT.md schema 校验
- `scripts/validate_run_manifest.py` — 跑批档案 schema 校验
- `scripts/validate_data_schema.py` — 数据字段校验
- `scripts/install_pre_commit_hook.sh` — 装 git 提交钩子

查询工具 (按需跑):
- `scripts/get_baseline.py --strategy <id>` — 查 baseline_registry
- `scripts/search_ledger.py "hypothesis text"` — 立项前查重经验账本
- `scripts/snapshot_current_state.py` — 把 CURRENT.md 总览段导出到 memory

工具脚本 (用户 / Codex 触发):
- `scripts/backfill_run_manifests.py` — 自动补历史跑批档案
- `scripts/process_quant_claude_outbox.py` — Codex 用的 Claude 信箱处理器 (P1.0)

---

## stable IDs (路径迁移用)

未来如需移文件 (e.g. 把 reports 按策略分子目录), 用 stable ID 引用而不是路径:

- strategy: 按 `data/research_framework/strategies.yaml` 的 `id` 字段引用
- baseline: 按 `baseline_registry.md` 的 `pk` 字段引用 (e.g. `cb_arb-main-yaml-current-20260515`)
- hypothesis: 按 `strategies.yaml` 的 `hypotheses.id` 引用
- 报告: 按"策略 + 日期 + slug" 引用 (e.g. `cb_arb/2026-05-15/panic-diagnostic`)

新引用尽量用 stable ID, 旧引用 (`reports/foo.md` 等具体路径) 保留兼容.

---

## 维护规则

- 加新文件 → 同时更新本 INDEX.md 相应分类
- 加新策略 / 新协议条 / 新模板 → 必须列在这里
- INDEX 自己别写细节, 只是地图. 细节在各个文件里.
- INDEX 加内容时不要破坏排版 (大类目顺序: 入口 → 协议 → 流程 → 角色 → 模板 → 真值 → 计划 → 报告 → 工具 → stable IDs → 维护)
