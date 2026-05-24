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

Ideation evidence-injection rule (2026-05-24):

The strategy ideation prompt receives three signals that did not exist
before this rule:

1. `active_critical_insights` — every entry in
   `research_insights.yaml::key_insights` whose `priority` is `critical`
   is forwarded into the ideator's `insights` payload. The prompt
   instructs Hermes to read each one and reference any whose
   `decision_use` applies to the proposed family. This carries
   high-priority evidence findings (e.g.
   `value_gap_rank_is_anti_alpha_2026_05_24`) into the decision
   surface instead of leaving them as offline research memory.
2. `repeated_reject_families` — family tags with 2+ recent rejections
   are surfaced separately. When this list is non-empty, the prompt
   appends a hard rule requiring the new hypothesis to answer three
   questions before proposing the next neighbor-variant: is the
   underlying signal invalid, is the direction reversed, or is only the
   execution condition wrong. Hermes is not forced to switch direction;
   it is forced to choose and justify one explanation. The goal is to
   bring the reverse-direction hypothesis into the decision context,
   not to mandate the answer.
3. Probe-class evaluators that wrap the base strategy (currently
   `evaluate_cb_arb_reverse_probe.py` and
   `evaluate_cb_arb_full_flip_probe.py`) must call
   `scripts/probe_report_synth.py::write_probe_artifacts` after the
   wrapped `main()` returns so the run produces framework-schema
   `report.yaml` + `diagnostic.yaml` and enters review_memory normally.
   Previously such runs were mis-classified `failed` because the base
   evaluator does not write those files, which silently dropped probe
   evidence out of the recent-results digest that Hermes consumes.

DRAFT-pending-capability acceptance (2026-05-24):

`queue_ideation.py` distinguishes two kinds of DRAFT proposals:

* DRAFT + valid `executor_tool_package` (status ∈
  {`awaiting_hermes_executor_code`, `draft_tool_code`,
  `draft_tool_code_compile_not_ready`, `draft_tool_design_pending_code`})
  is treated as `accepted_pending_capability`. The run flows through
  `find_pending_tool_draft` →
  `scripts/hermes_executor_handoff_wakeup.sh` →
  `scripts/hermes_executor_handoff_tick.py` to receive the drafted
  executor code. Suppression is skipped. A persistent
  `ideation_accept_marker.yaml` is written in the run dir; subsequent
  ticks with the same `package_status` emit
  `ideation_pending_capability_noop` (idempotent across ticks).
* DRAFT without a valid pending package, REJECT, PROPOSAL_ONLY, or
  malformed proposals continue to go through
  `suppress_non_runnable_draft` and `ideation_non_runnable_suppressed`
  as before.

This split is enforced by tests in
`framework/tests/test_auto_research_pipeline.py`:
`test_draft_with_missing_capability_request_accepted_not_suppressed`,
`test_invalid_draft_still_suppressed`,
`test_runnable_ready_proposal_path_unchanged`,
`test_repeated_draft_dedupes_to_noop`,
`test_draft_with_terminal_package_status_is_suppressed`.

Spot idle auto-start (2026-05-24):

`scripts/spot_idle_start.py` is the symmetric counterpart to
`scripts/spot_idle_shutdown.py`. The shutdown guard powers the spot
off when idle for 15+ minutes with an empty queue; the start guard
powers it back on when queued work appears.

Trigger: spot Tencent state is `STOPPED` AND queue has at least one
item in `{queued, running}`. Decision is journaled to
`logs/spot_idle_start_state.json` on the sig VM so cooldowns and
counters survive across cron ticks.

Safety rails (all defaults, overridable via CLI flags):

* `--queued-stable-minutes 2.0` — only start after queue has been
  observed active for 2+ minutes; prevents racing the shutdown guard
  when work has just landed.
* `--stop-cooldown-minutes 10.0` — refuse to start if
  `spot_idle_shutdown_state.json::status == shutdown_sent` was
  written within 10 minutes; prevents yo-yo.
* `--failed-start-backoff-minutes 30.0` — after a `start_failed`
  attempt, refuse to retry for 30 minutes.
* `--daily-cap 6` — refuse if today's start counter (UTC day) is
  ≥ 6; prevents runaway cost.

Crontab on sig (mirrors the shutdown line's cadence):

```
*/5 * * * * cd /root/projects/quant && source /root/.tencent_secrets/cvm.env \
  && /usr/bin/python3 scripts/spot_idle_start.py \
  >> logs/spot_idle_start.cron.log 2>&1 # QUANT_SPOT_IDLE_START
```

Tested in `framework/tests/test_spot_idle_start.py` (8 cases
covering the no-queue path, unstable-window stamp, stop cooldown,
failed-start backoff, daily cap, running-spot skip, all-gates-pass
start, and probe-failure error path).

This module does NOT queue experiments, ask an AI, or change
strategy state — same boundary as the shutdown guard.

Install-and-recompile + Hermes executor compliance gate (2026-05-25):

`scripts/install_generated_executors.py` is the cron path that copies
Hermes-generated executor code from each run dir's
`generated_executor/<name>.py` into `scripts/<name>.py`. Two changes
turn that path from "copy + register" into "copy + register +
re-compile + compliance-gate":

1. **Compliance gate (pre-install)** — `validate_executor()` now also
   requires the generated source to contain
   `from scripts.gatekeeper import GateKeeper`. Without it, the
   install path returns `action: compliance_failed`, writes a
   `compliance_repair_request.yaml` next to the generated executor,
   and flips the handoff task from `status: completed` back to
   `status: needs_compliance_repair`. The destination in `scripts/`
   is NOT touched; the path will not be allowlisted by default. This
   keeps "Hermes wrote bad code" inside the handoff repair loop
   instead of leaking past install and tripping
   `validate_gatekeeper_compliance.py` at preflight.

2. **DRAFT → READY recompile (post-install)** —
   `recompile_drafts_after_install()` runs after the install loop and
   after `auto_register_new_executors`. For every just-installed
   executor, it scans `data/*/spec.yaml` for any `status: DRAFT` spec
   whose `required_executor` matches and re-runs
   `spec_compiler.compile()` on the corresponding `proposal.yaml`.
   The compiler is the single source of truth for the new status —
   nothing manually rewrites `spec.status`. If compile returns READY,
   the compiler rewrites `spec.yaml`. If it still returns DRAFT or
   REJECT (e.g., closed-tag intersection), the result is surfaced as
   `spec_recompile_blocked` in the install summary.

The matching contract changes on the Hermes side
(`framework/autonomous/hermes_executor_handoff.py`):

* `REQUIRED_GATEKEEPER_IMPORT` constant added; the same import string
  is also checked by `_validate_generated_executor()` at handoff
  finalize time so receipt validation matches install-time validation.
* `REQUIRED_RECEIPT_CHECKS` adds `imports_gatekeeper`. Hermes must
  self-report this check passes; missing it fails finalize.
* `_boundary_for_task()` now passes `required_imports` plus a
  `required_imports_reason` paragraph and the full
  `required_receipt_checks` list into Hermes's task envelope so the
  contract is visible at write time, not discovered at install time.

Tested in `framework/tests/test_install_compliance_recompile.py` (6
cases: compliance pass returns clean errors, missing-GateKeeper is
flagged as `compliance_failed`, repair_request marker is written,
DRAFT spec routes through compile correctly, READY specs are not
touched, compliant install completes).

Handoff pickable statuses (2026-05-25):

`hermes_executor_handoff.HANDOFF_PICKABLE_STATUSES` is the explicit
set of task statuses that `open_tasks()`, `wake_once()`, and
`claim_task()` will surface as work. Today it contains:

```
{"open", "needs_compliance_repair"}
```

When install_generated_executors flips a noncompliant task from
`completed` to `needs_compliance_repair`, the handoff layer must
re-pick it so Hermes can rewrite the executor with the missing
`GateKeeper` import. Hardcoding `status == "open"` at every call
site was the original bug — repair tasks were generated but never
consumed, breaking the closed loop. New repair statuses (e.g.
`needs_generation_repair`, `needs_interface_repair`) should be added
to this constant, not patched at each call site.

`claim_task` flips `needs_compliance_repair → claimed` exactly like
`open → claimed`; `finalize_task_if_valid` then accepts the claimed
task on completion. The state machine flow is:

```
needs_compliance_repair  ──claim_task──▶  claimed
                                                ──complete_task──▶  finalize ──▶ completed
                                                                                    │
                                                                                    ▼
                                                                       install_generated_executors
                                                                          (re-runs compliance check)
                                                                                    │
                                              ┌─────────────────────────────────────┘
                                              ▼
                          if still noncompliant: back to needs_compliance_repair
                          if compliant:          spec_compiler recompile → READY → queued
```

Pinned by `framework/tests/test_hermes_executor_handoff.py` (5 new
cases plus the existing stale-claim recovery test): needs_compliance_repair
is pickable by open_tasks + wake_once + claim_task; completed and
installed are NOT pickable; failed is NOT pickable; stale-claim
recovery is unchanged; the constant set is asserted explicitly so
adding a new status forces a code-review change.

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
