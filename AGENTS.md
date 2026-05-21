# AGENTS

This is the only Markdown bootstrap file kept for AI tools.

Machine-owned runtime files:

- `data/research_framework/runtime_entrypoints.yaml`
- `data/research_framework/current.yaml`
- `data/research_framework/experiments.yaml`
- `data/research_framework/research_insights.yaml`
- `data/research_framework/strategy_ideator.yaml`
- `data/research_framework/ai_prompt_contracts.yaml`
- `data/research_framework/status_code_maps.yaml`
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

Operational rule:

Load `runtime_entrypoints.yaml` first. It names the files that must be injected
into the AI context. Do not use Markdown maps or Markdown protocol files as
runtime sources.

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
- Commits that change autonomous framework entry behavior must also stage a
  changed `AGENTS.md` or `CLAUDE.md`; the pre-commit hook blocks unchanged
  bootstrap entrypoints for those framework changes.
