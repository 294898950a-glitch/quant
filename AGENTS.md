# AGENTS

This is the canonical Markdown entry point for AI tools working on the quant
project. `CLAUDE.md` is only a thin pointer back to this file.

## Project purpose

This repository is an autonomous quantitative strategy research framework. It
iteratively generates strategy hypotheses, compiles them into runnable specs,
executes experiments on spot VM infrastructure, and feeds results back into a
review/memory loop so that future ideation is grounded in evidence. The human
operator owns strategy truth, budget authority, and boundary decisions; the
framework owns queue state, execution mechanics, and incremental research
memory.

## How to load context

1. Read this file and `CLAUDE.md`.
2. Load `data/research_framework/runtime_entrypoints.yaml` first. It names the
   machine-readable files that must be injected into the AI context.
3. Do **not** use Markdown maps or Markdown protocol files as runtime sources.
4. Detailed rules are split across the docs below; follow the link that matches
   the task instead of re-deriving rules from this summary.

## Active framework nodes

The autonomous framework has exactly five active nodes. Older Python
entrypoints and alias shells must stay deleted; helper modules are valid only
when owned by one of these five nodes.

- **state_and_rules** — protocol rules, current state, status codes.
- **ideation** — strategy ideation, proposal rewrite, prompt contracts.
- **proposal_gate** — proposal schema, spec compilation, executor requirements,
  handoff validation.
- **runner** — queue runner, tick dispatch, remote execution, run recording,
  result classification.
- **review_memory** — result review, recent-results digest, queue review memory.

See `docs/ARCHITECTURE.md` for the full node-to-module mapping and the
machine-owned runtime files.

## Machine-owned runtime files

These YAML/JSON files are runtime state, not documentation. Treat them as the
source of truth whenever they conflict with any Markdown summary:

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

## Hard boundaries summary

The full boundaries live in `docs/HARD_BOUNDARIES.md`. In short:

- Do not promote a prototype to current strategy truth, mark a strategy live,
  archive the current strategy, or revive a rejected direction without explicit
  user approval.
- Truth changes must update `current.yaml` and/or `baseline_registry.yaml`, or
  add a waiver under `data/research_framework/truth_sync_waivers/`.
- Use the registered AI data-quality judge, evidence toolkit, status codes,
  Hermes provider adapter, run recorder, and review pipeline rather than
  inventing parallel paths.
- Hermes is not a quant workflow entrypoint; it only receives executor-code
  handoffs and returns code plus a completion receipt.
- Quant automation write entrypoints advance through the project-owned WSL
  crontab `QUANT_INTERNAL_CRON_TICK` running `scripts/quant_internal_tick.py`.

## Detailed rule index

- `docs/ARCHITECTURE.md` — five nodes, node-to-module mapping, runtime files,
  supporting registries.
- `docs/WORKFLOW.md` — runtime_entrypoints loading, parallel dispatch,
  ideation evidence injection, DRAFT-pending-capability acceptance, current
  snapshot.
- `docs/SPOT_OPERATIONS.md` — spot idle auto-start, cluster detector + pause
  flag, recovery awareness, pause-flag propagation, requeue freshness guards.
- `docs/HERMES_HANDOFF.md` — install/recompile, GateKeeper compliance gate,
  import reachability gate, pickable statuses, compliance-repair overwrite,
  executor regeneration.
- `docs/DELTA_GATE.md` — Research Delta Gate, reject-chain defence, ideation
  skip-evidence pre-injection + cooldown.
- `docs/HARD_BOUNDARIES.md` — complete hard-boundaries section.
