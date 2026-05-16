# 文档地图 (INDEX)

**最后生成**: 2026-05-16 11:36 (由 `scripts/generate_indexes.py` 自动扫描生成, 不要手工编辑)
**触发**: 加新文件后跑 `python3 scripts/generate_indexes.py` 重新生成

---

## 入口顺序 (新会话只看这 3 个)

1. **`docs/research_framework/CURRENT.md`** — 当前真值, 每个策略状态 / 当前成绩 / 下一步等谁
2. **`data/research_framework/baseline_registry.md`** — 成绩单档案, 历史每次回测出的数字
3. **`docs/INDEX.md`** — 本文件, 找其他文档和工具

`docs/research_framework/experience_ledger.md` 只在查原因或选下一方向时读.
除上面 3 个入口文件外, 其他协议、角色、模板、报告都是非入口文件, 不作为当前状态判断依据.

---

## 当前主干

- `docs/research_framework/CURRENT.md` — 当前真值
- `data/research_framework/baseline_registry.md` — 历史 baseline
- `data/research_framework/run_manifests/` — 每次回测的来源、配置、成本和结果
- `docs/research_framework/protocol_redline.md` — 最小硬约束和详细边界
- `C:/Users/陈教授/Desktop/ai/projects/quant/{claude,codex}/outbox.md` — 双方通信

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

## docs 根目录 (其他)

- `docs/INDEX.md`
- `docs/data_source_summary.md`

## 真值数据 (data/research_framework/)

- `data/research_framework/baseline_registry.md`
- `data/research_framework/data_schema_expectations.yaml`
- `data/research_framework/decisions.jsonl`
- `data/research_framework/paper_interest_keywords.txt`
- `data/research_framework/processed_claude_messages.jsonl`
- `data/research_framework/runs.jsonl`
- `data/research_framework/strategies.yaml`
- `data/research_framework/tried_directions.jsonl`
- `data/research_framework/paper_candidates/` (子目录)
- `data/research_framework/run_manifests/` (子目录)

## 自动校验工具 (commit 前自动跑)

- `scripts/framework_preflight.py`
- `scripts/install_pre_commit_hook.sh`
- `scripts/validate_compute_budget.py`
- `scripts/validate_current_md.py`
- `scripts/validate_data_schema.py`
- `scripts/validate_entrypoints.py`
- `scripts/validate_l4_ack.py`
- `scripts/validate_run_manifest.py`
- `scripts/validate_spec.py`

## 查询工具

- `scripts/generate_indexes.py`
- `scripts/get_baseline.py`
- `scripts/search_ledger.py`
- `scripts/snapshot_current_state.py`

## 自动化脚本

- `scripts/backfill_run_manifests.py`
- `scripts/process_quant_claude_outbox.py`

## 研究脚本 — 评估实验 (evaluate_*)

共 17 个. 每个对应一次研究假设的回测.
- `scripts/evaluate_cb_arb_breadth_confirm_ensemble.py`
- `scripts/evaluate_cb_arb_cross_pools.py`
- `scripts/evaluate_cb_arb_daily_regime_switch.py`
- `scripts/evaluate_cb_arb_legacy.py`
- `scripts/evaluate_cb_arb_market_breadth_panic.py`
- `scripts/evaluate_cb_arb_normal_vol.py`
- `scripts/evaluate_cb_arb_panic_bond_anchor.py`
- `scripts/evaluate_cb_arb_panic_option_stop.py`
- `scripts/evaluate_cb_arb_panic_option_weight.py`
- `scripts/evaluate_cb_arb_regime_switch.py`
- `scripts/evaluate_cb_arb_selfpnl_regime_switch.py`
- `scripts/evaluate_cb_arb_stop_revaluation.py`
- `scripts/evaluate_cb_arb_stop_source_stress.py`
- `scripts/evaluate_cb_arb_stop_value_retention.py`
- `scripts/evaluate_cb_arb_three_value_gate.py`
- `scripts/evaluate_cb_arb_valuation_switch.py`
- `scripts/evaluate_cb_arb_value_gap_switch.py`

## 研究脚本 — 网格搜索 (search_*)

- `scripts/search_cb_arb_behavior_grid.py`
- `scripts/search_cb_arb_behavior_regimes.py`
- `scripts/search_cb_arb_panic_leave_year_out.py`
- `scripts/search_cb_arb_panic_mid_signal.py`
- `scripts/search_cb_arb_time_split_grid.py`

## 研究脚本 — 分析诊断 (analyze_*)

- `scripts/analyze_cb_arb_baseline_trade_attribution.py`
- `scripts/analyze_cb_arb_panic_calendar_diagnostic.py`
- `scripts/analyze_cb_arb_panic_yearly_decomposition.py`
- `scripts/analyze_cb_arb_repair_times.py`
- `scripts/analyze_cb_arb_stop_source_breakdown.py`
- `scripts/analyze_cb_panic_detector.py`
- `scripts/analyze_cb_panic_execution_feasibility.py`

## 研究脚本 — 数据加工 (build/enrich/fix/fetch/verify/recover)

- `scripts/build_cb_warehouse.py`
- `scripts/enrich_cb_conv_price.py`
- `scripts/fetch_csi500_etf.py`
- `scripts/fetch_icbc_stock.py`
- `scripts/fetch_shenhua_stock.py`
- `scripts/fetch_sp500_etf.py`
- `scripts/fetch_stk_daily.py`
- `scripts/fetch_yangtze_stock.py`
- `scripts/fetch_yzm_stock.py`
- `scripts/fix_delisted_conv_price.py`
- `scripts/recover_cb_arb_reflog.py`
- `scripts/verify_cb_data_independent.py`

## 研究脚本 — 跑批 / 监控 (run/monitor)

- `scripts/monitor_cb_arb_concurrent.py`
- `scripts/monitor_cb_arb_holdout_progress.py`
- `scripts/run_cb_arb_concurrent.py`
- `scripts/run_cb_arb_cost_realism.py`
- `scripts/run_cb_arb_two_line_cross_validation.py`

## 研究脚本 — 协作通道 (outbox/watch/check)

- `scripts/check_quant_outbox_misroute.py`
- `scripts/outbox_protocol_preflight.py`
- `scripts/outbox_to_telegram.py`
- `scripts/watch_quant_claude_processor.sh`
- `scripts/watch_quant_vm_task_completion.sh`

## 研究脚本 — 研究流程 (research_*, train_*, sanity)

- `scripts/cb_pricer_sanity.py`
- `scripts/research_arxiv_first_run.py`
- `scripts/research_memory.py`
- `scripts/research_sanity_checker.py`
- `scripts/research_select_next.py`
- `scripts/train_cb_panic_detector.py`

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
- `docs/*.md` — 根目录文档
- `docs/research_framework/*.md` — 按文件名分类 (协议/流程/角色/模板/真值)
- `data/research_framework/{*.md, *.yaml, *.txt, *.jsonl}` + 子目录 — 真值/配置数据/账本
- `scripts/*.py + *.sh` — 全部脚本, 按 prefix 分组 (validate/get_/search_ledger/snapshot/generate/backfill/process/evaluate/search/analyze/build|fetch|enrich|fix|verify|recover/run|monitor/outbox|watch|check/research_|train_|cb_pricer_sanity)
- `docs/plans/*.md` — 计划

分类规则在脚本里的 `classify_doc()` + 各 group 的 glob. 改分类规则时改脚本, 不改本文件.
