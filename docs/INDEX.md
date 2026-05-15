# 文档地图 (INDEX)

**最后生成**: 2026-05-16 04:08 (由 `scripts/generate_indexes.py` 自动扫描生成, 不要手工编辑)
**触发**: 加新文件后跑 `python3 scripts/generate_indexes.py` 重新生成

---

## 入口顺序 (新会话先看这 4 个)

1. **`docs/research_framework/CURRENT.md`** — 当前真值, 每个策略状态 / 当前成绩 / 下一步等谁
2. **`docs/INDEX.md`** — 本文件, 文档地图
3. **`data/research_framework/baseline_registry.md`** — 成绩单档案, 历史每次回测出的数字 (immutable-ish)
4. **`docs/research_framework/experience_ledger.md`** — 经验账本 (4 分区: 已采用 / 已无效 / 未完成 / 未来)

看完这 4 个文件就知道 "现在该做什么". 其他文件按需翻.

---

## 协议层

- `docs/research_framework/autonomous_loop_protocol.md`
- `docs/research_framework/enforcement_protocol.md`
- `docs/research_framework/l0_protocol.md`
- `docs/research_framework/paper_ingestion_protocol.md`
- `docs/research_framework/protocol_redline.md`

## 流程层

- `docs/research_framework/HDRF.md`
- `docs/research_framework/questioning_checklist.md`

## 角色定义

- `docs/research_framework/auditor_role.md`
- `docs/research_framework/memory_role.md`
- `docs/research_framework/sanity_checker_role.md`

## 模板

- `docs/research_framework/autonomous_summary_template.md`
- `docs/research_framework/report_template.md`
- `docs/research_framework/run_manifest_schema.md`
- `docs/research_framework/spec_template.md`

## 真值层

- `docs/research_framework/CURRENT.md`
- `docs/research_framework/experience_ledger.md`

## 真值数据 (data/research_framework/)

- `data/research_framework/baseline_registry.md`
- `data/research_framework/data_schema_expectations.yaml`
- `data/research_framework/paper_interest_keywords.txt`
- `data/research_framework/strategies.yaml`

## 自动校验工具 (commit 前自动跑)

- `scripts/framework_preflight.py`
- `scripts/install_pre_commit_hook.sh`
- `scripts/validate_current_md.py`
- `scripts/validate_data_schema.py`
- `scripts/validate_run_manifest.py`

## 查询工具

- `scripts/generate_indexes.py`
- `scripts/get_baseline.py`
- `scripts/search_ledger.py`
- `scripts/snapshot_current_state.py`

## 自动化脚本

- `scripts/backfill_run_manifests.py`
- `scripts/process_quant_claude_outbox.py`

## 报告

- `reports/INDEX.md` — 按策略 + 日期分类的报告索引 (也由 generate_indexes.py 自动生成)

## 计划

- `docs/plans/2026-04-24-automated-optimization-pipeline.md`
- `docs/plans/2026-04-24-convertible-bond-strong-redemption-strategy.md`
- `docs/plans/2026-04-25-cb-backtest-temporal-fix.md`
- `docs/plans/2026-05-07-holdout-pool-design.md`
- `docs/plans/2026-05-07-self-loop-roadmap.md`
- `docs/plans/2026-05-07-verifier-audit.md`
- `docs/plans/2026-05-09-cb-option-arb-strategy.md`
- `docs/plans/2026-05-10-evaluation-framework.md`

---

## stable IDs (路径迁移用)

未来若移文件位置, 用 stable ID 引用而不是具体路径:

- strategy: 按 `data/research_framework/strategies.yaml` 的 `id` 字段
- baseline: 按 `data/research_framework/baseline_registry.md` 的 `pk` 字段 (e.g. `cb_arb-main-yaml-current-20260515`)
- hypothesis: 按 `strategies.yaml` 的 `hypotheses.id`
- 报告: 按 "策略 + 日期 + slug" (e.g. `cb_arb/2026-05-15/panic-diagnostic`)

---

## 自动生成规则

本文件由 `scripts/generate_indexes.py` 扫描以下位置自动生成:
- `docs/research_framework/*.md` — 按文件名分类
- `data/research_framework/{*.md, *.yaml, *.txt}` — 真值/配置数据
- `scripts/{validate_*, framework_preflight, get_baseline, ...}.py` — 工具脚本
- `docs/plans/*.md` — 计划

分类规则在脚本里的 `classify_doc()` 函数. 改分类规则时改脚本, 不改本文件.
