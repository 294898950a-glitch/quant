# Quant project agent entry

This is the canonical agent bootstrap for the autonomous quantitative research
framework. Read it first, then load the machine-owned runtime entrypoints listed
in `data/research_framework/runtime_entrypoints.yaml`.

## Context loading

1. Read this file and `CLAUDE.md` when using Claude Code.
2. Load `data/research_framework/runtime_entrypoints.yaml`; its files are the
   runtime source of truth.
3. Read [`docs/QUANT_WORKFLOW.txt`](docs/QUANT_WORKFLOW.txt) for the detailed
   architecture, workflow rules, incident-derived constraints, current snapshot,
   and hard boundaries.
4. Do not treat Markdown as runtime state. When Markdown and YAML/JSON differ,
   the machine-owned runtime files win.

## Active framework nodes

The framework has five active nodes:

- `state_and_rules` — protocol rules, current state, and status codes.
- `ideation` — strategy ideation, proposal rewrite, and prompt contracts.
- `proposal_gate` — proposal schema, compilation, executor requirements, and
  handoff validation.
- `runner` — queue dispatch, remote execution, run recording, and result
  classification.
- `review_memory` — result review, recent-results digest, and queue memory.

Older Python entrypoints and alias shells stay deleted; helper modules must be
owned by one of these nodes.

## Non-negotiable boundaries

- Never promote a prototype, mark a strategy live, archive current strategy
  truth, or revive a rejected direction without explicit user approval.
- Truth changes go through `current.yaml` / `baseline_registry.yaml` or an
  explicit waiver under `data/research_framework/truth_sync_waivers/`.
- Use the registered data-quality judge, evidence toolkit, status codes, Hermes
  provider adapter, run recorder, and review pipeline; do not create parallel
  paths.
- Hermes is an executor-code handoff consumer, not a quant workflow entrypoint.
- Quant automation writes advance through the project-owned
  `QUANT_INTERNAL_CRON_TICK` running `scripts/quant_internal_tick.py`.

## Documentation map

| Path | Role |
|---|---|
| `AGENTS.md` | Short agent bootstrap and navigation entry. |
| `CLAUDE.md` | Claude Code compatibility pointer and runtime-loading reminder. |
| `docs/QUANT_WORKFLOW.txt` | Detailed architecture, workflow, incident rules, snapshot, and hard boundaries. |
| `docs/reviews/` | Review prompts, verdicts, and audit artifacts; not runtime input. |
| `docs/quant-distributed-network.html` | Generated architecture visualization; update its source/generator, not by hand unless requested. |

When changing autonomous framework entry behavior, update this entry or the
detailed workflow document as required by the repository's pre-commit policy.
