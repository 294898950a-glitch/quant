from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
from pathlib import Path
from typing import Any

try:
    from framework.autonomous.artifacts import ArtifactStore
    from framework.autonomous.evidence_tool_registry import EvidenceToolRegistry
    from framework.autonomous.framework_change_recorder import record_framework_change
    from framework.autonomous.paths import ResearchPaths
    from framework.autonomous.proposal_rewrite_loop import rewrite_until_valid
    from framework.autonomous.proposal_schema import validate_proposal
    from framework.autonomous.spec_compiler import compile as compile_proposal
    from framework.autonomous.strategy_ideator import propose
    from framework.autonomous.verification_tool import EvidenceToolkit
except ModuleNotFoundError:  # importlib-based tests may load files directly
    from artifacts import ArtifactStore  # type: ignore
    from evidence_tool_registry import EvidenceToolRegistry  # type: ignore
    from framework_change_recorder import record_framework_change  # type: ignore
    from paths import ResearchPaths  # type: ignore
    from proposal_rewrite_loop import rewrite_until_valid  # type: ignore
    from proposal_schema import validate_proposal  # type: ignore
    from spec_compiler import compile as compile_proposal  # type: ignore
    from strategy_ideator import propose  # type: ignore
    from verification_tool import EvidenceToolkit  # type: ignore


class ClaudeCommandAdapter:
    def __init__(self, config: dict[str, Any], repo_root: Path | str = Path(".")) -> None:
        self.config = config
        self.repo_root = Path(repo_root)

    def call_active_provider(self, prompt: str, schema: dict[str, Any]):
        provider = str(self.config.get("provider") or "claude")
        if provider != "claude":
            raise ValueError(f"only claude command provider is wired for this entrypoint, got {provider}")
        content = self._call_claude(prompt)
        return type(
            "ProviderResponse",
            (),
            {
                "content": content,
                "provider_id": provider,
                "response_hash": hashlib.sha256(content.encode("utf-8")).hexdigest()[:16],
                "retries_used": 0,
            },
        )()

    def _call_claude(self, prompt: str) -> str:
        command = str(self.config.get("command") or "claude")
        cmd = [
            command,
            "-p",
            "--output-format",
            "text",
            "--model",
            str(self.config.get("model") or "sonnet"),
            "--max-budget-usd",
            str(self.config.get("max_budget_usd") or 0.5),
            "--tools",
            "",
        ]
        timeout = int(self.config.get("timeout_seconds") or 240)
        result = subprocess.run(
            cmd,
            cwd=self.repo_root,
            input=prompt,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "claude command failed "
                f"rc={result.returncode}: {' '.join(shlex.quote(part) for part in cmd[:8])}\n"
                f"{result.stderr or result.stdout}"
            )
        return result.stdout.strip()


class IdeationCycle:
    def __init__(
        self,
        paths: ResearchPaths | None = None,
        store: ArtifactStore | None = None,
        ai_adapter: Any | None = None,
    ) -> None:
        self.paths = paths or ResearchPaths.from_repo_root(Path("."))
        self.store = store or ArtifactStore()
        self.ai_adapter = ai_adapter

    def run_once(
        self,
        config_path: Path | str | None = None,
        digest_path: Path | str | None = None,
        registry_path: Path | str | None = None,
        mechanics_vocab_path: Path | str | None = None,
        tool_registry_path: Path | str | None = None,
        output_root: Path | str | None = None,
        budget_cap_yuan: float | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        config = self.store.read_yaml(config_path or self.paths.strategy_ideator_config)
        digest = self.store.read_yaml(digest_path or self.paths.recent_results_digest)
        registry = self.store.read_yaml(registry_path or self.paths.executor_registry)
        tool_registry_store = EvidenceToolRegistry(tool_registry_path or self.paths.evidence_tool_registry)
        tool_registry = tool_registry_store.load()
        closed_tags = closed_tags_from_digest(digest)
        vocab = mechanics_vocab(registry, Path(mechanics_vocab_path or self.paths.mechanics_vocab), self.store)
        budget_cap = float(
            budget_cap_yuan
            if budget_cap_yuan is not None
            else config.get("max_auto_budget_yuan") or config.get("budget_cap_yuan") or 100
        )
        pre_ideation_evidence = collect_pre_ideation_evidence(digest)

        instruction = proposal_instruction(closed_tags, digest)
        instruction["pre_ideation_evidence_from_tool"] = pre_ideation_evidence
        instruction["available_evidence_tools"] = tool_registry_store.manifest(tool_registry)
        ai_adapter = self.ai_adapter or ClaudeCommandAdapter(config, repo_root=self.paths.repo_root)
        proposal = propose(
            closed_tags=closed_tags,
            recent_digest=digest,
            insights=instruction,
            budget_cap=budget_cap,
            ai_adapter=ai_adapter,
        )
        rewrite = rewrite_until_valid(
            initial_proposal=proposal,
            validator=lambda candidate: validate_proposal(candidate, vocab),
            ai_adapter=ai_adapter,
            max_rounds=3,
            context={
                "allowed_mechanics": sorted(vocab),
                "closed_tags": sorted(closed_tags),
                "required_strategy_id": "cb_arb_value_gap_switch",
            },
        )
        proposal = rewrite.final_proposal
        if rewrite.provenance:
            proposal["rewrite_provenance"] = rewrite.provenance
        proposal["rewrite_status"] = rewrite.status
        proposal["rewrite_rounds_used"] = rewrite.rounds_used
        proposal["rewrite_last_errors"] = rewrite.last_errors
        proposal_id = str(proposal.get("proposal_id") or "auto_strategy_proposal").replace(" ", "_")
        run_dir = Path(output_root or self.paths.data_root) / proposal_id
        proposal_path = run_dir / "proposal.yaml"
        proposal["proposal_path"] = str(proposal_path)
        self.store.write_yaml(proposal_path, proposal)
        proposal_event_hash = record_framework_change(
            change_type="strategy_proposal_generated",
            summary=f"Generated strategy proposal {proposal_id}",
            changed_paths=[str(proposal_path)],
            actor=str(proposal.get("ai_provider") or "ai"),
            reason=str(proposal.get("source_insight") or "strategy ideation cycle"),
            impact="Proposal is input to spec compiler; it does not run on VM by itself.",
            evidence={
                "proposal_id": proposal_id,
                "mechanics": proposal.get("mechanics"),
                "rewrite_status": rewrite.status,
                "rewrite_rounds_used": rewrite.rounds_used,
                "pre_ideation_evidence_count": len(pre_ideation_evidence),
            },
        )

        payload: dict[str, Any] = {
            "proposal_path": str(proposal_path),
            "proposal_id": proposal_id,
            "mechanics": proposal.get("mechanics"),
            "closed_tags_used": sorted(closed_tags),
            "rewrite_status": rewrite.status,
            "rewrite_rounds_used": rewrite.rounds_used,
            "rewrite_last_errors": rewrite.last_errors,
            "pre_ideation_evidence_count": len(pre_ideation_evidence),
            "proposal_framework_change_event_hash": proposal_event_hash,
        }
        if dry_run:
            payload["status"] = "PROPOSAL_ONLY"
            return payload

        result = compile_proposal(
            proposal=proposal,
            registry=registry,
            closed_tags=closed_tags,
            budget_cap=budget_cap,
            recent_proposals=[],
            output_dir=run_dir,
        )
        compile_event_hash = record_framework_change(
            change_type="strategy_spec_compiled",
            summary=f"Compiled proposal {proposal_id} to {result.status}",
            changed_paths=[
                str(path)
                for path in (proposal_path, result.spec_path, result.implementation_plan_path)
                if path
            ],
            actor="codex",
            reason=result.reason,
            impact="READY specs may be queued by runner; DRAFT specs are implementation backlog only.",
            evidence={
                "proposal_id": proposal_id,
                "status": result.status,
                "errors": result.errors,
                "spec_path": result.spec_path,
                "implementation_plan_path": result.implementation_plan_path,
            },
        )
        payload.update(
            {
                "status": result.status,
                "reason": result.reason,
                "spec_path": result.spec_path,
                "implementation_plan_path": result.implementation_plan_path,
                "errors": result.errors,
                "compile_framework_change_event_hash": compile_event_hash,
            }
        )
        return payload


def closed_tags_from_digest(digest: dict[str, Any]) -> dict[str, Any]:
    closed: dict[str, Any] = {}
    for run in digest.get("runs", []) or []:
        if not isinstance(run, dict):
            continue
        for tag in run.get("closed_direction_tags", []) or []:
            closed[str(tag)] = {
                "source_run_id": run.get("run_id"),
                "verdict": run.get("verdict"),
                "one_line_summary": run.get("one_line_summary"),
            }
    return closed


def mechanics_vocab(
    registry: dict[str, Any],
    vocab_path: Path,
    store: ArtifactStore | None = None,
) -> set[str]:
    artifact_store = store or ArtifactStore()
    vocab: set[str] = set()
    if Path(vocab_path).exists():
        loaded = artifact_store.read_yaml(vocab_path)
        vocab.update(str(item) for item in loaded.get("mechanics", []) or [])
    executors = registry.get("executors", {})
    items = executors.values() if isinstance(executors, dict) else executors
    for executor in items or []:
        if not isinstance(executor, dict):
            continue
        vocab.update(str(item) for item in executor.get("can_test", []) or [])
        vocab.update(str(item) for item in executor.get("cannot_test", []) or [])
    return vocab


def proposal_instruction(closed_tags: dict[str, Any], digest: dict[str, Any]) -> dict[str, Any]:
    return {
        "task": "Generate exactly one new strategy proposal as a YAML or JSON object.",
        "hard_rules": [
            "Return only the object. No markdown fences, no explanation.",
            "Do not use any mechanics listed in closed_tags.",
            "If the idea requires an unimplemented executor, still propose it; compiler will mark DRAFT.",
            "If you need a new evidence tool, first review available_evidence_tools and explain why existing tools are insufficient. New tools must be registered before use.",
            "Do not write files. Do not run tests. Do not change current strategy.",
        ],
        "required_fields": [
            "proposal_id",
            "strategy_id",
            "family",
            "hypothesis",
            "source_insight",
            "expected_improvement",
            "mechanics",
            "required_executor",
            "required_data",
            "test_design",
            "success_criteria",
            "falsifiers",
            "risk",
            "why_not_repeated_failure",
            "related_prior_runs",
            "implementation_assumption",
        ],
        "allowed_strategy_id": "cb_arb_value_gap_switch",
        "closed_tags": closed_tags,
        "recent_digest": digest,
    }


def collect_pre_ideation_evidence(digest: dict[str, Any]) -> list[dict[str, Any]]:
    runs = digest.get("runs", []) or []
    if not runs or not isinstance(runs[0], dict):
        return []
    latest = runs[0]
    review_path = latest.get("review_path")
    if not review_path:
        return []
    review_dir = Path(str(review_path)).parent
    review_id = str(latest.get("run_id") or review_dir.name)
    return EvidenceToolkit(review_dir=review_dir).collect_for_role(
        role="ideator",
        review_id=review_id,
        purpose="pre_ideation_evidence",
    )
