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

Import reachability gate (2026-05-25 evening, post-incident):

The GateKeeper compliance check above is a *string* check on the source
text. It is necessary but not sufficient — a generated executor can
contain the right `from scripts.gatekeeper import GateKeeper` line and
still fail at runtime if the source lacks the boilerplate that prepends
`REPO_ROOT` to `sys.path` before that import. The install-time validator
ran inside the repo cwd where `sys.path` already had the repo root, so
the import resolved locally; spot ran the executor as a top-level
subprocess from `/home/ubuntu/...` where the bare `scripts.X` package
is unreachable. This produced 13 path-bug failures (production incident
2026-05-25) before the gate was added — see
`data/research_framework/incidents/2026-05-25_path_bug.yaml`.

`validate_executor()` now calls `_check_import_reachability(path)` after
the GateKeeper string check passes. The probe spawns a subprocess with
`-I` (isolated mode), `cwd="/tmp"`, and a stripped environment so that
`sys.path` no longer auto-resolves to the repo root. It re-imports the
source via `importlib.util.spec_from_file_location`. Any
`ModuleNotFoundError` (or other import-time error) is folded back as
`compliance_failed: import_unreachable: ...`, which routes the task
through the same compliance_failed → needs_compliance_repair → Hermes
regeneration flow as the missing-GateKeeper case. No new task state is
needed; the existing repair path absorbs the new failure category.

Tested by 2 additional cases in
`framework/tests/test_install_compliance_recompile.py`:
`test_compliance_fail_import_unreachable_when_sys_path_missing` (executor
has the GateKeeper import string but no `sys.path.insert` boilerplate →
must surface `import_unreachable` under `compliance_failed`) and
`test_compliance_pass_when_sys_path_fix_present` (boilerplate restored →
reachability probe passes).

Infra cluster detector + orchestrator pause flag (2026-05-25 evening):

`framework/autonomous/infra_cluster_detector.py` runs once per
`scripts/quant_internal_tick.py` invocation, before
`research_queue_runner.py` is dispatched. It inspects the most recent
five failed/infra_failed tasks (sorted by `failed_at` or
`infra_reclassified_at`), normalizes each to a root-cause signature
(`infra_failure_type` tag if present, otherwise the last `<Error>:
<message>` line from the run's `auto_pipeline.log`, otherwise the
queue item's `failure_reason`), and if two or more consecutive tasks
share the same signature it touches
`data/research_framework/orchestrator_paused.flag`.

`research_queue_runner.py` already short-circuits the entire tick when
that flag exists (it has done so since the file was introduced). The
cluster detector therefore acts as an automatic-stop layer between
"first repeat infra failure detected" and "next dispatch tick" — at
most one extra task can land on spot before the queue freezes.

Threshold is two consecutive same-signature failures (user mandate
2026-05-25 post-incident); window is the most recent five. Both are
module-level constants in `infra_cluster_detector` and can be tuned
without changing the hook in `quant_internal_tick.main()`.

`orchestrator_paused.flag` is gitignored (it is runtime state). To
unpause: confirm the underlying defect is fixed and the fixes are
deployed on every host that can read this repo (sig in particular,
since `spot_idle_start.py` runs there), then delete the flag.

Tested in `framework/tests/test_infra_cluster_detector.py` (10 cases
covering empty/single/two-same/two-different signature inputs,
window truncation, infra_failure_type vs traceback precedence,
non-failed status exclusion, and the three flag-touch branches:
write/skip-existing/skip-when-should-not-pause).

Cluster detector recovery awareness (2026-05-26):

The first cluster detector design (one day prior) counted all
historical failed/infra_failed tasks in the queue against the
consecutive-signature threshold. The first unpause after the
2026-05-25 path-bug incident immediately re-tripped the detector
because the two reclassified historical infra_failed tasks
(`moneyness_entry_filter_v1_7` + `credit_adjusted_valuation_v1`)
still carried the same `infra_failure_type` signature. Protection
mechanism worked, but the judgment criterion was too coarse.

`infra_cluster_detector.evaluate()` now accepts
`recovery_armed_at` (ISO-8601 string). Tasks whose failed_at /
infra_reclassified_at predates that cutoff are filtered out before
consecutive counting. A default rolling
`DEFAULT_LOOKBACK_MINUTES = 60` provides a fallback when no
`recovery_armed_at` is set, so even a freshly deployed detector
will never wake up on ancient corpses. The effective cutoff is
`max(recovery_armed_at, now - lookback)`, surfaced as
`cutoff_source` in the decision and in the pause flag body.

State is tracked in
`data/research_framework/cluster_detector_state.json`:

```json
{
  "recovery_armed_at": "2026-05-26T00:05:21+00:00",
  "armed_by": "<who_unpaused>",
  "notes": "..."
}
```

`mark_recovery_armed()` writes the stamp; `load_recovery_armed_at()`
reads it; `scripts/quant_internal_tick.py` passes the loaded value
into `evaluate()`. When unpausing after an incident, the operator
should both delete `orchestrator_paused.flag` AND call
`mark_recovery_armed()` so the detector sees a fresh starting
point. (A future helper script can bundle the two actions; for now
both are manual.)

Tested in `framework/tests/test_infra_cluster_detector.py` with
8 additional cases on top of the original 10:
- historical infra_failed pre-recovery do not trigger pause
- new post-recovery failures with same signature do trigger pause
- different post-recovery signatures do not merge into a cluster
- rolling lookback window excludes ancient corpses without recovery
- pause flag body surfaces "post-recovery" / "cutoff_source"
- state file round-trip (write, read missing, read malformed)

requeue_stale_pipeline_failures freshness guards (2026-05-26):

`framework/autonomous/queue_remote_execution.QueueRemoteExecutionService.
requeue_stale_pipeline_failures()` is the auto-requeue path that
resurrects failed tasks when a tracked framework file SHA changes
(executor source, pipeline driver, gatekeeper, run_recorder, etc.).
The signal is "the code that produced the failure is no longer the
code that exists on disk, so it deserves a fresh try". This is too
loose by itself — any commit that touches one of the tracked files,
including the install-time gates added during the 2026-05-25 incident
response, also changes the SHA, which flooded the queue with 4
historical path-bug tasks on the first post-unpause tick of
2026-05-26. Two guards now sit in front of the requeue:

1. **infrastructure classification skip** — if the task carries
   `failure_category: "infrastructure"` OR `infra_failure_type: ...`,
   requeue refuses to touch it. The path-bug class specifically
   requires Hermes to regenerate the executor through the new
   `import_reachability` gate; that route runs through
   `install_generated_executors`, not through requeue.

2. **age cutoff** — `REQUEUE_FRESHNESS_HOURS = 24` (class constant).
   Failed tasks older than the cutoff are not auto-resurrected even
   when the SHA-change check fires. Older corpses require explicit
   operator action, which keeps unrelated framework commits from
   reviving long-dead tasks. Unparseable `failed_at` is treated
   conservatively (skipped).

Both guards apply BEFORE the existing first-time-signature stamping,
so a task that fails the guards never enters the signature ledger
and cannot accumulate state that would later make it appear "newly
deserving of a rerun". Tested in
`framework/tests/test_auto_research_pipeline.py` with 3 new cases
(`test_requeue_skips_task_tagged_infrastructure_failure`,
`test_requeue_skips_task_with_infra_failure_type`,
`test_requeue_skips_task_older_than_freshness_window`).

spot_idle_start respects orchestrator pause flag (2026-05-26):

`scripts/spot_idle_start.py` runs on the Singapore sig VM and used to
auto-start spot whenever the queue had `queued`/`running` tasks. The
wsl-side pause flag (`data/research_framework/orchestrator_paused.flag`)
only stopped the dispatcher — it did not propagate to sig. During the
2026-05-26 incident response this produced the "queue frozen but spot
empty-spinning" deadlock: pause flag held on wsl, queue still had
queued tasks, sig saw active queue and started spot, but wsl refused
to dispatch.

Two corresponding changes close the loop:

1. `scripts/sync_queue_to_sig.sh` (cron `*/15` on wsl) now also
   synchronizes the pause flag. If the flag exists locally it is
   rsync'd to sig next to the queue file; if it is absent locally
   the sync explicitly removes any stale copy on sig so a fresh
   recovery propagates correctly.

2. `scripts/spot_idle_start.py` checks for the synced pause flag
   alongside the queue file as gate 0 — before the queue-active,
   stable-window, cooldown, backoff, daily-cap, and probe gates.
   When the flag is present, the script writes
   `last_status: orchestrator_paused`, resets `queued_since` so the
   stable-window timer starts fresh on the next unpause, and exits
   0 without touching the spot.

Tested in `framework/tests/test_spot_idle_start.py` with one new
case (`test_pause_flag_blocks_start_even_with_active_queue`) on top
of the existing 8 cases.

Recovery operator obligation update: when unpausing, the operator
must (1) delete `orchestrator_paused.flag` locally, (2) call
`mark_recovery_armed()` to write `cluster_detector_state.json`, and
(3) trigger `scripts/sync_queue_to_sig.sh` so the flag deletion
propagates to sig and the next spot_idle_start tick sees no pause.

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

Compliance-repair overwrite (2026-05-25):

`install_one()`'s "refuse to overwrite different content" guard now
splits into two cases instead of always refusing:

* **Normal flow** — target exists, hash differs, task has no
  `compliance_failed_at` history. Continues to refuse (the destination
  is presumed hand-edited; the generated_executor copy is a stale
  Hermes draft).
* **Repair flow** — target exists, hash differs, AND the task carries
  `compliance_failed_at` (set earlier when install rejected the
  noncompliant source), AND the new source has already passed
  `validate_executor()` (which includes the GateKeeper compliance
  check). The new source is therefore verified compliant, and the
  destination is a known-bad noncompliant version we explicitly
  asked Hermes to rewrite. Overwrite is allowed and returns
  `action: overwritten_after_compliance_repair`.

After a successful compliance-repair overwrite, `main()` writes:

```
installed_at: <now>
repair_installed_at: <now>
installed_sha256: <new>
install_action: overwritten_after_compliance_repair
last_compliance_status: passed
previous_compliance_failed_at: <old>     ← moved from compliance_failed_at
previous_compliance_errors: <old>        ← moved from compliance_errors
```

The current-state fields `compliance_failed_at` / `compliance_errors`
are removed; their values are preserved in the `previous_*` history
so audit can still tell the run went through a repair cycle.

Pinned by `framework/tests/test_install_compliance_recompile.py`
(4 new cases on top of the existing 6): normal flow still refuses
on hash mismatch; repair flow + compliant source overwrites; repair
flow + still-noncompliant source is blocked at the validate_executor
gate (not the overwrite branch); main() cleans the stale compliance
fields and writes the repair_installed_at audit trail.

Executor regeneration (2026-05-25):

A second subcategory of "install has no source to read" needs the
same self-healing treatment as the compliance repair flow above:
when a previously-installed task loses its
``generated_executor/<name>.py`` source on disk (e.g., the run dir
was cleaned up but the install fingerprint and the target in
``scripts/`` are still present), install gets stuck in
"skipped: no generated_executor source found" forever and the
upstream ideation cycle keeps reporting
``ideation_pending_capability_noop`` against the same task.

`install_one()` now distinguishes:

* **New task, no source yet** (no ``installed_at``): keep returning
  ``skipped`` as before; Hermes hasn't written code yet.
* **Previously-installed task whose source was lost**
  (``installed_at`` is set AND the target file in ``scripts/``
  still exists): return
  ``action: needs_executor_regeneration``, write
  ``generated_executor/executor_regeneration_request.yaml``
  describing what was lost (previous install fingerprint), and let
  `main()` flip the task to ``status: needs_executor_regeneration``.

The flip also moves ``installed_at`` / ``installed_source`` /
``installed_sha256`` into ``previous_installed_*`` history fields,
so the next install of the regenerated source lands as a fresh
install (rather than tripping the "destination exists" guard).

`hermes_executor_handoff.HANDOFF_PICKABLE_STATUSES` extends to
``{"open", "needs_compliance_repair", "needs_executor_regeneration"}``,
and `_boundary_for_task()` adds a ``regeneration_context`` block
when the task is in this status so Hermes sees the source-loss
reason, the previous install fingerprint, and the requirement to
satisfy the spec + GateKeeper rules without trying to match the
old source byte-for-byte.

If the regenerated source still fails compliance, it flows into the
existing ``compliance_failed → needs_compliance_repair`` loop
already documented above. Hermes does the rewrite; install does not.

Pinned by `framework/tests/test_install_compliance_recompile.py`
(3 new cases) and `framework/tests/test_hermes_executor_handoff.py`
(4 new cases): new task without source still returns plain skipped;
previously-installed task with missing source returns
needs_executor_regeneration + marker; main() flips status and
preserves audit history; handoff layer picks up the new status,
exposes the regeneration_context in the boundary, and allows
claim_task to transition needs_executor_regeneration → claimed;
normal open tasks do NOT pick up the regeneration_context.

Research Delta Gate (Phase 1 V1, 2026-05-25):

After the auto-research execution chain was confirmed closed, the
next risk was Hermes being able to propose anything it wants
without judgement. The Research Delta Gate is a small pure-function
module that runs between proposal generation and spec compilation
and decides whether the new proposal carries real research delta
over history.

`framework/autonomous/research_delta_gate.evaluate(proposal, *,
recent_digest, research_insights, queue_state)` returns a
`DeltaDecision(action, reason, evidence)` where `action` is exactly
one of:

* `advance` — flow continues into compile + queue as before.
* `skip`    — duplicate of a settled prior run with the same family
              and capability_ids (and no new critical-insight cited),
              OR family marked closed in suggested_closed_families
              (with no new critical-insight cited). The proposal is
              kept on disk for audit, but the cycle returns
              `SKIPPED_BY_DELTA_GATE` and the spec compiler is NOT
              invoked.
* `watch`   — the shape matches a prior run but the proposal cites a
              new critical insight (hold for review instead of
              silently re-running), OR the queue has saturated and
              new advances are throttled. Returns
              `WATCHED_BY_DELTA_GATE`; the spec compiler is NOT
              invoked.

The merge (`B_merge`) and pivot (`C_pivot`) actions are explicitly
out of scope for V1.

Tuning knobs live as module-level constants:

* `CLOSED_FAMILY_REJECT_THRESHOLD` (default 2): a family must have
  this many rejects in suggested_closed_families before the gate
  treats it as closed.
* `QUEUE_SATURATION_DEPTH` (default 5): when the live queue holds
  this many queued/running items, brand-new advances are downgraded
  to `watch` to keep the queue bounded.

The decision is written back to the proposal artifact under the
`research_delta_gate` key so downstream readers (review_memory,
Hermes' next ideation context, audits) can see why a proposal was
advanced / watched / skipped without re-deriving the call.

`queue_ideation.generate_next_spec_if_idle` now recognises the two
new terminal statuses and routes them through dedicated audit /
status events (`ideation_skipped_by_delta_gate` /
`ideation_watched_by_delta_gate`). Crucially these statuses do NOT
go through `suppress_non_runnable_draft` — a delta-gate decision is
a legitimate research-judgement output, not malformed garbage. They
are also NOT in `RETRYABLE_IDEATION_RESULTS`; the loop does not
retry on them.

Pinned by `framework/tests/test_research_delta_gate.py` (18 cases
covering the contract, the five user-level acceptance criteria
including human-readable reasons, edge cases for adopted prior runs
and below-threshold closed families, and robustness against missing
inputs).

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
