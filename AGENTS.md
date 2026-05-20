# AGENTS

This is the only Markdown bootstrap file kept for AI tools.

Machine-owned runtime files:

- `data/research_framework/runtime_entrypoints.yaml`
- `data/research_framework/current.yaml`
- `data/research_framework/experiments.yaml`
- `data/research_framework/research_insights.yaml`
- `data/research_framework/strategy_ideator.yaml`
- `data/research_framework/protocol_rules.yaml`

Supporting registries such as `strategies.yaml`, `baseline_registry.yaml`,
`ai_providers.yaml`, `research_queue.yaml`, and `framework_stability_todos.yaml`
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
- Evidence tools for strategy ideation and review must go through
  `framework/autonomous/verification_tool.py::EvidenceToolkit` and be registered
  in `data/research_framework/evidence_tool_registry.yaml` before use.
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
- Before any AI proposes or registers a new evidence tool, inject the existing
  tool manifest with ids, paths, callables, descriptions, and manifest hash.
  New tool registration must include why existing tools are insufficient.
- Quant automation write entrypoints are advanced by the project-owned WSL
  crontab entry marked `QUANT_INTERNAL_CRON_TICK`, which runs
  `scripts/quant_internal_tick.py` every 10 minutes. Hermes is not a quant
  workflow entrypoint. Direct calls to the research queue or next-spec generator
  require a short-lived quant automation ticket in environment variables; calls
  without that ticket must be rejected and audited.
- Commits that change autonomous framework entry behavior must also stage a
  changed `AGENTS.md` or `CLAUDE.md`; the pre-commit hook blocks unchanged
  bootstrap entrypoints for those framework changes.
