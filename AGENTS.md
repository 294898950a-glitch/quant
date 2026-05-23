# AGENTS

This is the only substantive Markdown bootstrap file kept for AI tools.
`CLAUDE.md` is allowed only as a minimal Claude Code auto-entry pointer back to
this file.

Machine-owned runtime files:

- `data/research_framework/runtime_entrypoints.yaml`
- `data/research_framework/current.yaml`
- `data/research_framework/experiments.yaml`
- `data/research_framework/research_insights.yaml`
- `data/research_framework/strategy_ideator.yaml`
- `data/research_framework/ai_prompt_contracts.yaml`
- `data/research_framework/status_code_maps.yaml`
- `data/research_framework/data_inventory.yaml`
- `data/research_framework/hermes_executor_handoffs.yaml`
- `data/research_framework/protocol_rules.yaml`
- `data/research_framework/research_queue.yaml`

Supporting registries such as `strategies.yaml`, `baseline_registry.yaml`,
`ai_providers.yaml`, and `framework_stability_todos.yaml`
are not default AI context. The specific component that needs them must load
them directly.

Global architecture rule:

The autonomous research framework has only five active nodes:
state_and_rules, ideation, proposal_gate, runner, and review_memory. Older
Python entrypoints and alias shells must stay deleted; helper modules are valid
only when owned by one of these five nodes.

Node-to-module mapping (logical node names are not 1:1 filenames; each node is
realized by a small group of helper modules):

- state_and_rules: `data/research_framework/protocol_rules.yaml`,
  `data/research_framework/current.yaml`, supporting registries
  (`strategies.yaml`, `baseline_registry.yaml`,
  `executor_registry.yaml`, `evidence_tool_registry.yaml`),
  `framework/autonomous/workflow_state.py`,
  `framework/autonomous/status_codes.py`.
- ideation: `framework/autonomous/queue_ideation.py`,
  `framework/autonomous/ideation_cycle.py`,
  `framework/autonomous/strategy_ideator.py`,
  `framework/autonomous/proposal_rewrite_loop.py`,
  `framework/autonomous/prompt_contracts.py`.
- proposal_gate: `framework/autonomous/proposal_schema.py`,
  `framework/autonomous/spec_compiler.py`,
  `framework/autonomous/executor_requirements.py`,
  `framework/autonomous/verification_tool.py`,
  `framework/autonomous/hermes_executor_handoff.py`.
- runner: `scripts/research_queue_runner.py`,
  `scripts/quant_internal_tick.py`,
  `framework/autonomous/queue_remote_execution.py`,
  `framework/autonomous/run_recorder.py`,
  `framework/autonomous/result_classification.py`.
- review_memory: `framework/autonomous/queue_review_memory.py`,
  `framework/autonomous/result_reviewer.py`,
  `framework/autonomous/recent_results_digest.py`,
  `scripts/review_result.py`.

Operational rule:

Load `runtime_entrypoints.yaml` first. It names the files that must be injected
into the AI context. Do not use Markdown maps or Markdown protocol files as
runtime sources.

Parallel-dispatch rule (2026-05-23):

`workflow_state.decide_scheduler_action()` is parallel-tolerant by default.
With Guangzhou spot and Singapore sig both in the pool, the scheduler will
keep ideating new directions while one experiment is already running, so
the next idle VM has work waiting. To force the older serial behavior on a
particular run, set `state["parallel_dispatch"] = false` in
`research_queue.yaml`.

Current snapshot as of 2026-05-22 13:00 Asia/Shanghai.
This snapshot was checked against `current.yaml`, `research_queue.yaml`,
`ai_providers.yaml`, `data_inventory.yaml`, the latest run reviews, and live
local processes before being written:

- Current main strategy remains `cb_arb_value_gap_switch`. It is still `wip`,
  not deployable, and not promoted beyond the user-approved current research
  track.
- Active provider is Hermes through `scripts/hermes_provider_adapter.py`; direct
  OpenAI-compatible API runtime providers have been removed from the active
  provider registry.
- The active scheduler is the project-owned WSL cron marker
  `QUANT_INTERNAL_CRON_TICK`, running `scripts/quant_internal_tick.py` every 10
  minutes. Hermes is not a queue runner.
- Core data inventory is machine-readable in
  `data/research_framework/data_inventory.yaml` and is core-database context
  only. It does not include experiment result artifacts.
- Recent completed result:
  `cb_arb_value_gap_switch_duration-adaptive-exit_2026-05-22` was accepted by
  review memory. Best parameters were `min_hold_days=5`,
  `initial_threshold_fraction=0.7`, `decay_period_factor=0.5`, and
  `effective_max_hold_days=45`. This is evidence for the research direction, not
  automatic promotion to current strategy truth.
- Recent rejected/negative result:
  `cb_arb_value_gap_switch_iv_percentile_exit_v1_1` improved the sealed test
  slice directionally but failed review because train-period excess was negative
  and the strategy remained deeply unprofitable in absolute PnL.
- Queue state at this snapshot: 33 complete, 22 failed, 4 blocked, 1 queued
  (60 total). The latest queue item recorded by `research_queue.yaml` is complete,
  while the live status file and local processes show the cron tick is generating
  the next research direction through Hermes. Always verify the live queue and
  `logs/research_queue_status.json` before acting.

Hard boundaries:

- Do not promote a prototype to current strategy truth without user approval.
- Do not mark a strategy live without user approval.
- Do not archive the current strategy without user approval.
- Do not revive a rejected direction without user approval.
- Compute estimates are record-only execution metadata. There is no active
  budget approval gate or standalone budget module.
- Truth changes must update `data/research_framework/current.yaml` and/or
  `data/research_framework/baseline_registry.yaml`, or add a waiver under
  `data/research_framework/truth_sync_waivers/`.
- Data quality must pass the registered AI data-quality judge before a run can
  start. VM-side summaries provide evidence only; final pass, repair_candidate,
  or fail decisions come from the AI judge. Old missing data-root pointers may
  be rewritten to the current warehouse when the AI judge returns a repair plan,
  but unfixable missing/unreadable/broken required data blocks the flow.
- Evidence tools for strategy ideation and review must go through
  `framework/autonomous/verification_tool.py::EvidenceToolkit` and be registered
  in `data/research_framework/evidence_tool_registry.yaml` before use.
- LLM-facing status-like outputs must use numeric codes from
  `data/research_framework/status_code_maps.yaml`; prompts must not ask models
  to invent or emit free-form status labels. Code may translate codes back to
  internal labels only after validation.
- Data-quality AI checks may only use `scripts/validate_data_quality.py` with a
  quant automation ticket for `data_quality_judge`; VM-side data is summarized
  first, then the registered local provider judges run/block.
- `data_inventory.yaml` is descriptive core-database context only. Ideation may
  read its compact view to choose realistic data paths, but it must not include
  experiment result artifacts or treat the inventory as data-quality approval,
  repair authority, or execution permission.
- The active AI provider is the registered Hermes command adapter. Direct
  OpenAI-compatible API providers are not configured for runtime calls; using
  Hermes as a provider does not make Hermes a quant workflow entrypoint.
- Data-quality `repair_candidate` failures must go through
  `scripts/repair_data_quality.py` with a quant automation ticket for
  `data_quality_repair`; the repairer uses the registered provider to generate
  run-local repair code, writes only `prepared_data`, and never overwrites raw
  warehouse files. The data validator must re-check repaired inputs before
  execution.
- Run records must enter through `framework/autonomous/run_recorder.py`. A
  newly executed run must use the executed-run entry with command, start/end
  time, exit code, compute metadata, and a passing data-quality decision.
  Historical artifact classification must use the backfill entry and must not
  trigger the next research step by itself.
- Review-memory AI calls may only use `scripts/review_result.py` through the
  registered provider. The script extracts facts deterministically first, then
  requires the provider to return fixed-schema raw YAML only; invalid prose,
  Markdown fences, or unknown fields must fail the review step.
- `scripts/research_queue_runner.py` is only the queue decision entrypoint.
  Strategy ideation must be delegated to `framework/autonomous/queue_ideation.py`;
  VM launch, data-quality gating, result sync, repair requeue, and completion
  settlement must be delegated to `framework/autonomous/queue_remote_execution.py`.
  Synced artifacts become `review_pending`; review and digest updates must be
  delegated to `framework/autonomous/queue_review_memory.py`.
- Before any AI proposes or registers a new evidence tool, inject the existing
  tool manifest with ids, paths, callables, descriptions, and manifest hash.
  New tool registration must include why existing tools are insufficient.
- Quant automation write entrypoints are advanced by the project-owned WSL
  crontab entry marked `QUANT_INTERNAL_CRON_TICK`, which runs
  `scripts/quant_internal_tick.py` every 10 minutes. Hermes is not a quant
  workflow entrypoint; Hermes may receive executor-code implementation handoffs
  in `data/research_framework/hermes_executor_handoffs.yaml` and must return
  completed code plus `generated_executor/executor_completion.yaml` instead of
  launching runs. The project-owned handoff script reads that YAML receipt and
  updates the executable descriptor; Hermes must not be trusted to synchronize
  descriptor state by hand.
  The Hermes one-minute wake path is `scripts/hermes_executor_handoff_wakeup.sh`,
  which first calls `scripts/hermes_executor_handoff_tick.py`; it may only
  expose, claim, or complete executor-code handoffs and must not advance the
  research queue, launch VMs, or change strategy truth.
  Direct calls to the research queue or next-spec generator require a
  short-lived quant automation ticket in environment variables; calls without
  that ticket must be rejected and audited.
- If ideation produces a non-runnable draft, the same tick must mark that draft
  skipped for automation and try another direction up to the configured attempt
  limit. When a READY spec is produced, the same tick should enqueue and start it
  instead of waiting for the next cron wake-up.
- Data-quality blocks must record the local decision-input signature. If later
  ticks detect that the spec, executor, validator, status maps, or required data
  file presence/metadata changed, the blocked item is automatically requeued for
  a fresh data-quality gate instead of requiring manual status edits.
- Proposal compilation must not mark a spec runnable when the matched executor
  script is absent; with the registry script-existence rule enabled it must remain
  DRAFT until executable code is registered.
- Commits that change autonomous framework entry behavior must also stage a
  changed `AGENTS.md` or `CLAUDE.md`; the pre-commit hook blocks unchanged
  bootstrap entrypoints for those framework changes.
