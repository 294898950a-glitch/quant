from __future__ import annotations

import ast
import json
import re
import shutil
from pathlib import Path
from typing import Any

import yaml

try:
    from framework.autonomous.artifacts import ArtifactStore
    from framework.autonomous.ai_provider_adapter import RegisteredProviderAdapter
    from framework.autonomous.evidence_tool_registry import EvidenceToolRegistry
    from framework.autonomous.executor_registry import capability_catalog
    from framework.autonomous.executor_registry import validate_registry_schema
    from framework.autonomous.framework_change_recorder import record_framework_change
    from framework.autonomous.paths import ResearchPaths
    from framework.autonomous.spec_compiler import compile as compile_proposal
    from framework.autonomous.strategy_ideator import propose
    from framework.autonomous.verification_tool import EvidenceToolkit
except ModuleNotFoundError:  # importlib-based tests may load files directly
    from artifacts import ArtifactStore  # type: ignore
    from ai_provider_adapter import RegisteredProviderAdapter  # type: ignore
    from evidence_tool_registry import EvidenceToolRegistry  # type: ignore
    from executor_registry import capability_catalog  # type: ignore
    from executor_registry import validate_registry_schema  # type: ignore
    from framework_change_recorder import record_framework_change  # type: ignore
    from paths import ResearchPaths  # type: ignore
    from spec_compiler import compile as compile_proposal  # type: ignore
    from strategy_ideator import propose  # type: ignore
    from verification_tool import EvidenceToolkit  # type: ignore


class RegisteredAIProviderAdapter:
    def __init__(
        self,
        config: dict[str, Any],
        repo_root: Path | str = Path("."),
        providers_path: Path | str | None = None,
    ) -> None:
        self.config = config
        self.repo_root = Path(repo_root)
        registry_path = providers_path or self.repo_root / str(
            config.get("provider_registry") or "data/research_framework/ai_providers.yaml"
        )
        self.adapter = RegisteredProviderAdapter(
            Path(registry_path),
            repo_root=self.repo_root,
            entrypoint=str(config.get("allowed_entrypoint") or "scripts/run_strategy_ideation_once.py"),
        )

    def call_active_provider(self, prompt: str, schema: dict[str, Any]):
        return self.adapter.call_active_provider(prompt, schema)


class CompileProxy:
    def __init__(
        self,
        *,
        status: str,
        reason: str,
        spec_path: str | None,
        implementation_plan_path: str | None,
        errors: list[str],
    ) -> None:
        self.status = status
        self.reason = reason
        self.spec_path = spec_path
        self.implementation_plan_path = implementation_plan_path
        self.errors = errors


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
        dry_run: bool = False,
    ) -> dict[str, Any]:
        config = self.store.read_yaml(config_path or self.paths.strategy_ideator_config)
        current = self.store.read_yaml(self.paths.current, default={})
        digest = self.store.read_yaml(digest_path or self.paths.recent_results_digest)
        registry = self.store.read_yaml(registry_path or self.paths.executor_registry)
        queue_state = self.store.read_yaml(self.paths.research_queue, default={})
        tool_registry_store = EvidenceToolRegistry(tool_registry_path or self.paths.evidence_tool_registry)
        tool_registry = tool_registry_store.load()
        pending = find_pending_tool_draft(Path(output_root or self.paths.data_root), self.store)
        if pending and not dry_run:
            proposal = normalize_proposal_shape(pending["proposal"])
            run_dir = Path(pending["run_dir"])
            ai_adapter = self.ai_adapter or RegisteredAIProviderAdapter(config, repo_root=self.paths.repo_root)
            package_path = Path(pending["package_path"]) if pending.get("package_path") else None
            if pending.get("package_status") == "draft_tool_code" and package_path:
                executor_tool_package = self.store.read_yaml(package_path)
                executor_tool_package["descriptor_path"] = str(package_path)
            else:
                executor_tool_package = request_executor_tool_code(
                    proposal=proposal,
                    compile_reason=str(pending.get("reason") or "no strict executor match"),
                    compile_errors=list(pending.get("errors") or []),
                    registry=registry,
                    ai_adapter=ai_adapter,
                    run_dir=run_dir,
                    store=self.store,
                )
            registration = None
            ready_result = None
            if executor_tool_package.get("status") == "draft_tool_code":
                registration = register_executor_tool_and_recompile(
                    proposal=proposal,
                    registry=registry,
                    registry_path=registry_path or self.paths.executor_registry,
                    closed_tags=closed_tags_from_runtime(digest=digest, config=config, queue_state=queue_state),
                    recent_proposals=recent_proposals_from_digest(digest),
                    run_dir=run_dir,
                    package_path=Path(executor_tool_package["descriptor_path"]),
                    config_defaults=config.get("defaults") if isinstance(config.get("defaults"), dict) else {},
                    store=self.store,
                )
                if registration.get("compile_status") == "READY":
                    ready_result = registration
            return {
                "proposal_path": str(run_dir / "proposal.yaml"),
                "proposal_id": proposal.get("proposal_id"),
                "capability_ids": proposal.get("capability_ids"),
                "mechanics": proposal.get("mechanics"),
                "status": "READY" if ready_result else "DRAFT",
                "reason": "auto-registered draft executor" if ready_result else "continued pending draft executor tooling",
                "spec_path": ready_result.get("spec_path") if ready_result else str(run_dir / "spec.yaml"),
                "implementation_plan_path": None if ready_result else str(run_dir / "implementation_plan.yaml"),
                "errors": pending.get("errors") or [],
                "executor_tool_package": executor_tool_package,
                "executor_registration": registration,
            }
        closed_tags = closed_tags_from_runtime(digest=digest, config=config, queue_state=queue_state)
        cap_menu = capability_menu(registry)
        pre_ideation_evidence = collect_pre_ideation_evidence(digest)

        instruction = proposal_instruction(closed_tags, digest, cap_menu, current)
        instruction["pre_ideation_evidence_from_tool"] = pre_ideation_evidence
        instruction["available_evidence_tools"] = tool_registry_store.manifest(tool_registry)
        ai_adapter = self.ai_adapter or RegisteredAIProviderAdapter(config, repo_root=self.paths.repo_root)
        proposal = propose(
            closed_tags=closed_tags,
            recent_digest=digest,
            insights=instruction,
            ai_adapter=ai_adapter,
        )
        proposal = normalize_proposal_shape(proposal)
        current_strategy_id = current_main_strategy_id(current) or str((config.get("defaults") or {}).get("strategy_id") or "")
        if current_strategy_id and str(proposal.get("strategy_id") or "") != current_strategy_id:
            proposal["strategy_id"] = current_strategy_id
            proposal["strategy_id_corrected_from_current_yaml"] = True
        proposal["rewrite_status"] = "disabled_two_step_flow"
        proposal["rewrite_rounds_used"] = 0
        proposal["rewrite_last_errors"] = []
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
                "capability_ids": proposal.get("capability_ids"),
                "mechanics": proposal.get("mechanics"),
                "rewrite_status": "disabled_two_step_flow",
                "rewrite_rounds_used": 0,
                "pre_ideation_evidence_count": len(pre_ideation_evidence),
            },
        )

        payload: dict[str, Any] = {
            "proposal_path": str(proposal_path),
            "proposal_id": proposal_id,
            "capability_ids": proposal.get("capability_ids"),
            "mechanics": proposal.get("mechanics"),
            "closed_tags_used": sorted(closed_tags),
            "rewrite_status": "disabled_two_step_flow",
            "rewrite_rounds_used": 0,
            "rewrite_last_errors": [],
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
            recent_proposals=recent_proposals_from_digest(digest),
            output_dir=run_dir,
        )
        executor_tool_package = None
        if result.status == "DRAFT" and result.reason in {
            "no strict executor match",
            "missing registered capability",
            "executor required data missing",
        }:
            executor_tool_package = request_executor_tool_code(
                proposal=proposal,
                compile_reason=result.reason,
                compile_errors=result.errors,
                registry=registry,
                ai_adapter=ai_adapter,
                run_dir=run_dir,
                store=self.store,
            )
            if executor_tool_package.get("status") == "draft_tool_code":
                registration = register_executor_tool_and_recompile(
                    proposal=proposal,
                    registry=registry,
                    registry_path=registry_path or self.paths.executor_registry,
                    closed_tags=closed_tags,
                    recent_proposals=recent_proposals_from_digest(digest),
                    run_dir=run_dir,
                    package_path=Path(executor_tool_package["descriptor_path"]),
                    config_defaults=config.get("defaults") if isinstance(config.get("defaults"), dict) else {},
                    store=self.store,
                )
                if registration.get("compile_status") == "READY":
                    result = CompileProxy(
                        status="READY",
                        reason="auto-registered draft executor",
                        spec_path=registration.get("spec_path"),
                        implementation_plan_path=None,
                        errors=[],
                    )
                    executor_tool_package["registration"] = registration
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
                "executor_tool_package": executor_tool_package,
            },
        )
        payload.update(
            {
                "status": result.status,
                "reason": result.reason,
                "spec_path": result.spec_path,
                "implementation_plan_path": result.implementation_plan_path,
                "errors": result.errors,
                "executor_tool_package": executor_tool_package,
                "compile_framework_change_event_hash": compile_event_hash,
            }
        )
        return payload


def normalize_proposal_shape(proposal: dict[str, Any]) -> dict[str, Any]:
    """Normalize common AI shape drift without asking the model to rewrite."""
    normalized = dict(proposal)
    test_design = normalized.get("test_design")
    if isinstance(test_design, str):
        normalized["test_design"] = {"description": test_design}
    success = normalized.get("success_criteria")
    if isinstance(success, str):
        normalized["success_criteria"] = {"description": success}
    elif isinstance(success, list):
        normalized["success_criteria"] = {"criteria": success}
    falsifiers = normalized.get("falsifiers")
    if isinstance(falsifiers, str):
        normalized["falsifiers"] = {
            "train": falsifiers,
            "validate": falsifiers,
            "test": falsifiers,
        }
    elif isinstance(falsifiers, list):
        normalized["falsifiers"] = {
            "train": falsifiers,
            "validate": falsifiers,
            "test": falsifiers,
        }
    return normalized


def _strip_markdown_fence(text: str) -> str:
    stripped = text.strip()
    match = re.fullmatch(r"```(?:yaml|yml|json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else stripped


def _parse_mapping_response(content: str) -> dict[str, Any]:
    cleaned = _strip_markdown_fence(content)
    try:
        loaded = json.loads(cleaned)
    except json.JSONDecodeError:
        try:
            loaded = yaml.safe_load(cleaned)
        except yaml.YAMLError:
            loaded = None
    return loaded if isinstance(loaded, dict) else {"status": "invalid_tool_response"}


def _safe_generated_path(run_dir: Path, raw_path: str, index: int) -> Path:
    name = raw_path.strip() or f"generated_tool_{index}.py"
    candidate = Path(name)
    if candidate.is_absolute() or ".." in candidate.parts:
        candidate = Path(candidate.name or f"generated_tool_{index}.py")
    if candidate.parts and candidate.parts[0] == "generated_executor":
        rel_path = candidate
    else:
        rel_path = Path("generated_executor") / candidate.name
    return run_dir / rel_path


def find_pending_tool_draft(output_root: Path, store: ArtifactStore) -> dict[str, Any] | None:
    candidates: list[tuple[int, float, Path, dict[str, Any], str]] = []
    if not output_root.exists():
        return None
    for spec_path in output_root.glob("*/spec.yaml"):
        try:
            spec = store.read_yaml(spec_path)
        except Exception:
            continue
        if spec.get("status") != "DRAFT":
            continue
        if spec.get("notes") not in {"no strict executor match", "missing registered capability"}:
            continue
        request_path = spec_path.parent / "executor_tool_request.yaml"
        request_status = ""
        if request_path.exists():
            try:
                request = store.read_yaml(request_path)
            except Exception:
                continue
            request_status = str(request.get("status") or "")
            attempts = int(request.get("tool_code_attempts") or 0)
            if request_status not in {"draft_tool_design_pending_code", "invalid_tool_code_response", "draft_tool_code"}:
                continue
            if request_status == "invalid_tool_code_response" and attempts >= 3:
                continue
        priority = {"draft_tool_code": 3, "draft_tool_design_pending_code": 2, "invalid_tool_code_response": 1}.get(
            request_status,
            0,
        )
        candidates.append((priority, spec_path.stat().st_mtime, spec_path, spec, request_status))
    if not candidates:
        return None
    _, _, spec_path, spec, request_status = max(candidates, key=lambda item: (item[0], item[1]))
    proposal = spec.get("proposal") if isinstance(spec.get("proposal"), dict) else {}
    if not proposal:
        proposal_path = spec_path.parent / "proposal.yaml"
        proposal = store.read_yaml(proposal_path, default={})
    return {
        "run_dir": spec_path.parent,
        "proposal": proposal,
        "reason": spec.get("notes"),
        "errors": spec.get("errors") or [],
        "package_path": spec_path.parent / "executor_tool_request.yaml",
        "package_status": request_status,
    }


def _executor_registry_items(registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = registry.get("executors") or {}
    if isinstance(raw, dict):
        return {str(key): dict(value) for key, value in raw.items() if isinstance(value, dict)}
    if isinstance(raw, list):
        return {
            str(item.get("id") or index): dict(item)
            for index, item in enumerate(raw)
            if isinstance(item, dict)
        }
    return {}


def _fill_executor_defaults(entry: dict[str, Any], config_defaults: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(entry)
    defaults = dict(enriched.get("default_config") or {})
    fallback_defaults = {
        "lookback_days": 20,
        "multiplier_min": 0.5,
        "multiplier_max": 1.5,
        "floor_factor": 0.5,
        "ceiling_factor": 1.5,
        "fixed_source": "2",
        "rule": "score_4state",
        "drawdown_thresholds": "0.05,0.10,0.15",
        "exit_multipliers": "0.25,0.50,0.75",
        "base_max_hold": "120",
        "model_config": "{}",
    }
    for key in enriched.get("required_config_fields", []) or []:
        if key == "output_dir":
            continue
        if key not in defaults and key in config_defaults:
            defaults[key] = config_defaults[key]
        if key not in defaults and key in fallback_defaults:
            defaults[key] = fallback_defaults[key]
    if "cost_model_enabled" in enriched.get("required_config_fields", []) and "cost_model_enabled" not in defaults:
        defaults["cost_model_enabled"] = True
    enriched["default_config"] = defaults
    return enriched


def _command_unresolved_placeholders(entry: dict[str, Any]) -> list[str]:
    config = dict(entry.get("default_config") or {})
    config.setdefault("output_dir", "data/_registration_check")
    command = []
    for item in entry.get("command_template", []) or []:
        text = str(item)
        for key, value in config.items():
            text = text.replace("{" + str(key) + "}", str(value))
        command.append(text)
    joined = " ".join(command)
    return sorted(set(re.findall(r"\{[^{}]+\}", joined)))


def _copy_generated_files_to_registry_paths(
    *,
    run_dir: Path,
    package: dict[str, Any],
    registry_entry: dict[str, Any],
) -> list[str]:
    written: list[str] = []
    script_path = registry_entry.get("script_path")
    if not script_path:
        raise ValueError("registry_entry.script_path missing")
    source_files = package.get("written_files") or []
    if not source_files:
        raise ValueError("no generated files available for registration")
    source = Path(str(source_files[0]))
    if not source.is_absolute():
        source = run_dir / source
    if not source.exists():
        raise FileNotFoundError(f"generated tool file missing: {source}")
    target = Path(str(script_path))
    if target.is_absolute() or ".." in target.parts:
        raise ValueError(f"unsafe registry script_path: {script_path}")
    target = run_dir.parents[1] / target if len(run_dir.parents) > 1 else Path(".") / target
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    written.append(str(target))
    return written


def register_executor_tool_and_recompile(
    *,
    proposal: dict[str, Any],
    registry: dict[str, Any],
    registry_path: Path | str,
    closed_tags: dict[str, Any],
    recent_proposals: list[dict[str, Any]],
    run_dir: Path,
    package_path: Path,
    config_defaults: dict[str, Any],
    store: ArtifactStore,
) -> dict[str, Any]:
    package = store.read_yaml(package_path)
    if package.get("status") != "draft_tool_code":
        return {"status": "skipped", "reason": "tool package is not draft_tool_code"}
    package_errors = validate_executor_tool_package(package)
    if package_errors:
        package["status"] = "invalid_tool_code_response"
        package["validation_errors"] = package_errors
        package["tool_code_attempts"] = int(package.get("tool_code_attempts") or 0) + 1
        store.write_yaml(package_path, package, no_aliases=True)
        return {"status": "failed", "reason": "draft executor code failed validation", "errors": package_errors}
    entry = package.get("registry_entry")
    if not isinstance(entry, dict):
        return {"status": "failed", "reason": "registry_entry missing"}
    entry = _fill_executor_defaults(entry, config_defaults)
    unresolved = _command_unresolved_placeholders(entry)
    if unresolved:
        return {"status": "failed", "reason": "registry command has unresolved placeholders", "unresolved": unresolved}
    executors = _executor_registry_items(registry)
    executor_id = str(entry.get("id") or "")
    if not executor_id:
        return {"status": "failed", "reason": "registry_entry.id missing"}
    executors[executor_id] = entry
    updated_registry = dict(registry)
    updated_registry["executors"] = executors
    registry_errors = validate_registry_schema(updated_registry)
    if registry_errors:
        return {"status": "failed", "reason": "registered executor failed registry schema", "errors": registry_errors}
    proposal_for_compile = dict(proposal)
    proposal_data = list(proposal_for_compile.get("required_data") or [])
    proposal_data_paths = {str(item.get("path") if isinstance(item, dict) else item) for item in proposal_data}
    for data_item in entry.get("required_data") or []:
        data_path = str(data_item.get("path") if isinstance(data_item, dict) else data_item)
        if data_path and data_path not in proposal_data_paths:
            proposal_data.append(data_item)
            proposal_data_paths.add(data_path)
    proposal_for_compile["required_data"] = proposal_data
    result = compile_proposal(
        proposal=proposal_for_compile,
        registry=updated_registry,
        closed_tags=closed_tags,
        recent_proposals=recent_proposals,
        output_dir=run_dir,
    )
    if result.status != "READY":
        package["status"] = "draft_tool_code_compile_not_ready"
        package["post_registration_compile_status"] = result.status
        package["post_registration_compile_reason"] = result.reason
        store.write_yaml(package_path, package, no_aliases=True)
        return {
            "status": package["status"],
            "executor_id": executor_id,
            "registered_files": [],
            "registry_path": None,
            "compile_status": result.status,
            "compile_reason": result.reason,
            "spec_path": result.spec_path,
            "errors": result.errors,
        }
    copied_files = _copy_generated_files_to_registry_paths(run_dir=run_dir, package=package, registry_entry=entry)
    registry_written = store.write_yaml(registry_path, updated_registry, no_aliases=True)
    record_framework_change(
        change_type="executor_tool_registered",
        summary=f"Registered executor tool {executor_id}",
        changed_paths=[str(registry_written), *copied_files, str(run_dir / "spec.yaml")],
        actor=str(package.get("ai_provider") or "ai"),
        reason="draft executor code passed validation and was auto-registered",
        impact="Registered executor may make its DRAFT proposal compile READY and enter the queue.",
        evidence={
            "proposal_id": proposal.get("proposal_id"),
            "executor_id": executor_id,
            "compile_status": result.status,
            "compile_reason": result.reason,
            "copied_files": copied_files,
        },
    )
    package["status"] = "registered_tool_code" if result.status == "READY" else "registered_tool_code_compile_not_ready"
    package["registered_executor_id"] = executor_id
    package["registered_files"] = copied_files
    package["registered_registry_path"] = str(registry_written)
    package["post_registration_compile_status"] = result.status
    package["post_registration_compile_reason"] = result.reason
    store.write_yaml(package_path, package, no_aliases=True)
    return {
        "status": package["status"],
        "executor_id": executor_id,
        "registered_files": copied_files,
        "registry_path": str(registry_written),
        "compile_status": result.status,
        "compile_reason": result.reason,
        "spec_path": result.spec_path,
        "errors": result.errors,
    }


def validate_executor_tool_package(package: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    placeholder_tokens = [
        "placeholder",
        "demonstration",
        "actual logic would",
        "detailed simulation code",
        "trade_records = []",
        "...",
        "TODO",
    ]
    for field in (
        "tool_request_id",
        "why_existing_tools_insufficient",
        "reviewed_existing_executor_ids",
        "registry_entry",
        "files",
        "validation_plan",
    ):
        if field not in package:
            errors.append(f"missing required tool package field {field}")
    if "reviewed_existing_executor_ids" in package and not isinstance(package.get("reviewed_existing_executor_ids"), list):
        errors.append("reviewed_existing_executor_ids must be a list")
    if "registry_entry" in package and not isinstance(package.get("registry_entry"), dict):
        errors.append("registry_entry must be a mapping")
    files = package.get("files")
    if not isinstance(files, list) or not files:
        errors.append("files must be a non-empty list")
    else:
        for index, item in enumerate(files, start=1):
            if not isinstance(item, dict):
                errors.append(f"files[{index}] must be a mapping")
                continue
            if not isinstance(item.get("path"), str) or not item.get("path"):
                errors.append(f"files[{index}].path must be non-empty")
            if not isinstance(item.get("content"), str) or not item.get("content"):
                errors.append(f"files[{index}].content must be non-empty")
            else:
                lowered = item["content"].lower()
                for token in placeholder_tokens:
                    if token.lower() in lowered:
                        errors.append(f"files[{index}].content contains placeholder marker {token!r}")
                        break
                if str(item.get("path") or "").endswith(".py"):
                    try:
                        tree = ast.parse(item["content"])
                    except SyntaxError as exc:
                        errors.append(f"files[{index}].content is not valid Python: {exc.msg}")
                        continue
                    function_names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
                    if "main" not in function_names:
                        errors.append(f"files[{index}].content must define main()")
                    if "declare_data_requirements" not in function_names:
                        errors.append(f"files[{index}].content must define declare_data_requirements(command, spec)")
                    if "__name__" not in item["content"] or "__main__" not in item["content"]:
                        errors.append(f"files[{index}].content must have a __main__ entrypoint")
                    if "adoption_pass" not in item["content"]:
                        errors.append(f"files[{index}].content must compute and write adoption_pass")
                    for artifact_name in ("summary.json", "report.yaml", "l4_ack.yaml", "diagnostic.yaml"):
                        if artifact_name not in item["content"]:
                            errors.append(f"files[{index}].content must write {artifact_name}")
    return errors


def validate_executor_tool_design(design: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in (
        "tool_request_id",
        "why_existing_tools_insufficient",
        "reviewed_existing_executor_ids",
        "registry_entry",
        "implementation_outline",
        "validation_plan",
    ):
        if field not in design:
            errors.append(f"missing required tool design field {field}")
    if "reviewed_existing_executor_ids" in design and not isinstance(design.get("reviewed_existing_executor_ids"), list):
        errors.append("reviewed_existing_executor_ids must be a list")
    registry_entry = design.get("registry_entry")
    if not isinstance(registry_entry, dict):
        errors.append("registry_entry must be a mapping")
    else:
        for field in ("id", "script_path", "strategy_id", "family", "command_template", "artifacts_produced"):
            if field not in registry_entry:
                errors.append(f"registry_entry missing required field {field}")
    if "implementation_outline" in design and not isinstance(design.get("implementation_outline"), dict):
        errors.append("implementation_outline must be a mapping")
    validation_plan = design.get("validation_plan")
    if not isinstance(validation_plan, list) or not validation_plan:
        errors.append("validation_plan must be a non-empty list")
    return errors


def validate_executor_tool_code_response(response: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    files = response.get("files")
    if not isinstance(files, list) or not files:
        errors.append("files must be a non-empty list")
        return errors
    package = {
        "tool_request_id": "code_response",
        "why_existing_tools_insufficient": "validated in design step",
        "reviewed_existing_executor_ids": [],
        "registry_entry": {},
        "files": files,
        "validation_plan": ["syntax check"],
    }
    return validate_executor_tool_package(package)


def executor_tool_design_required_yaml_template() -> str:
    return """tool_request_id: dynamic_exit_gap_decay_executor_v1
why_existing_tools_insufficient: >-
  Explain why each reviewed executor cannot run this proposal.
reviewed_existing_executor_ids:
  - option_position_sizing
  - option_source_pnl_feedback_scaler
registry_entry:
  id: dynamic_exit_gap_decay_executor
  version: 1
  status: draft
  strategy_id: cb_arb_value_gap_switch
  family: dynamic_exit_gap_decay
  script_path: scripts/evaluate_cb_arb_dynamic_exit_gap_decay.py
  owner: ai_draft
  description: Evaluate the proposal with a real backtest implementation.
  can_test:
    - dynamic_exit_gap_decay
  can_test_capability_ids: []
  cannot_test:
    - rolling_pnl_feedback
    - new_valuation_formula
    - new_data_source
  cannot_test_capability_ids:
    - C004
    - C006
    - C008
  required_data:
    - path: data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/daily_value_gap_amounts.parquet
      description: Base daily value-gap ranks.
      schema_hash: null
  required_config_fields:
    - data_root
    - train_start
    - train_end
    - test_start
    - test_end
    - output_dir
  command_template:
    - .venv/bin/python
    - scripts/evaluate_cb_arb_dynamic_exit_gap_decay.py
    - --data-root
    - "{data_root}"
    - --output-dir
    - "{output_dir}"
  default_config: {}
  artifacts_produced:
    - summary.json
    - report.yaml
    - l4_ack.yaml
    - diagnostic.yaml
  budget_estimate:
    sig_minutes: 0
    spot_minutes: 90
    local_minutes: 0
    estimated_cost_yuan: 0.0
  vm_local_limits:
    vm_required: true
    local_allowed: false
  obsolescence_date: null
implementation_outline:
  script_path: scripts/evaluate_cb_arb_dynamic_exit_gap_decay.py
  main_inputs:
    - data_root
    - output_dir
  core_steps:
    - load existing base ranks
    - apply proposal-specific rule
    - produce required artifacts
  code_constraints:
    - use existing project data files
    - do not change current strategy truth
validation_plan:
  - python3 -m py_compile generated_executor/evaluate_cb_arb_dynamic_exit_gap_decay.py
  - run a small data smoke test
  - verify all required artifacts are produced
"""


def executor_tool_code_required_yaml_template() -> str:
    return """files:
  - path: generated_executor/evaluate_cb_arb_dynamic_exit_gap_decay.py
    purpose: Complete draft evaluator implementation.
    content: |
      #!/usr/bin/env python3
      # Full Python source code goes here.
      # Must define declare_data_requirements(command, spec) returning {"required_files": [{"path": "..."}]}.
      # Must compute and write adoption_pass into summary.json using baseline-aligned success criteria.
      # Do not return placeholder, TODO, pseudocode, or demo-only code.
"""


def executor_tool_required_yaml_template() -> str:
    return """tool_request_id: dynamic_exit_gap_decay_executor_v1
why_existing_tools_insufficient: >-
  Explain why every reviewed executor cannot run this proposal.
reviewed_existing_executor_ids:
  - option_position_sizing
  - option_source_pnl_feedback_scaler
registry_entry:
  id: dynamic_exit_gap_decay_executor
  version: 1
  status: draft
  strategy_id: cb_arb_value_gap_switch
  family: dynamic_exit_gap_decay
  script_path: scripts/evaluate_cb_arb_dynamic_exit_gap_decay.py
  owner: ai_draft
  description: Evaluate the proposal with a real backtest implementation.
  can_test:
    - dynamic_exit_gap_decay
  can_test_capability_ids: []
  cannot_test:
    - rolling_pnl_feedback
    - new_valuation_formula
    - new_data_source
  cannot_test_capability_ids:
    - C004
    - C006
    - C008
  required_data:
    - path: data/cb_arb_value_gap_switch_regime-option-entry-gate_2026-05-17/daily_value_gap_amounts.parquet
      description: Base daily value-gap ranks.
      schema_hash: null
  required_config_fields:
    - data_root
    - train_start
    - train_end
    - test_start
    - test_end
    - output_dir
  command_template:
    - .venv/bin/python
    - scripts/evaluate_cb_arb_dynamic_exit_gap_decay.py
    - --data-root
    - "{data_root}"
    - --output-dir
    - "{output_dir}"
  default_config: {}
  artifacts_produced:
    - summary.json
    - report.yaml
    - l4_ack.yaml
    - diagnostic.yaml
  budget_estimate:
    sig_minutes: 0
    spot_minutes: 90
    local_minutes: 0
    estimated_cost_yuan: 0.0
  vm_local_limits:
    vm_required: true
    local_allowed: false
  obsolescence_date: null
validation_plan:
  - python3 -m py_compile generated_executor/evaluate_cb_arb_dynamic_exit_gap_decay.py
  - run a small data smoke test
  - verify all required artifacts are produced
files:
  - path: generated_executor/evaluate_cb_arb_dynamic_exit_gap_decay.py
    purpose: Complete draft evaluator implementation.
    content: |
      #!/usr/bin/env python3
      # Full Python source code goes here.
      # Do not return placeholder, TODO, pseudocode, or demo-only code.
"""


def request_executor_tool_design(
    *,
    proposal: dict[str, Any],
    compile_reason: str,
    compile_errors: list[str],
    registry: dict[str, Any],
    ai_adapter,
) -> dict[str, Any]:
    executors = registry.get("executors") or {}
    existing = executors if isinstance(executors, dict) else {str(i): item for i, item in enumerate(executors or [])}
    prompt = yaml.safe_dump(
        {
            "task": "The proposal cannot run because no existing executor/tool matches it. Return only the missing evaluator design and registry metadata in YAML. Do not write code yet.",
            "rules": [
                "Return YAML only.",
                "Do not include markdown fences or explanation.",
                "Do not modify current strategy truth.",
                "Do not claim the tool is registered or runnable yet.",
                "Use the exact top-level YAML keys shown in required_output_yaml_template.",
                "Do not return Python code in this step.",
                "Do not return JSON.",
            ],
            "required_output_yaml_template": executor_tool_design_required_yaml_template(),
            "proposal": proposal,
            "compile_reason": compile_reason,
            "compile_errors": compile_errors,
            "existing_executors": existing,
        },
        allow_unicode=True,
        sort_keys=False,
    )
    response = ai_adapter.call_active_provider(prompt, schema={"type": "executor_tool_design"})
    design = _parse_mapping_response(response.content)
    design["ai_provider"] = getattr(response, "provider_id", None)
    design["response_hash"] = getattr(response, "response_hash", None)
    design["compile_reason"] = compile_reason
    validation_errors = validate_executor_tool_design(design)
    design["validation_errors"] = validation_errors
    design["status"] = "draft_tool_design" if not validation_errors else "invalid_tool_design_response"
    return design


def request_executor_tool_code_from_design(
    *,
    proposal: dict[str, Any],
    design: dict[str, Any],
    ai_adapter,
    previous_errors: list[str] | None = None,
) -> dict[str, Any]:
    prompt = yaml.safe_dump(
        {
            "task": "Return only the Python file content needed by this already-approved draft evaluator design. Do not repeat registry metadata.",
            "rules": [
                "Return YAML only.",
                "Do not include markdown fences or explanation.",
                "Use the exact top-level YAML keys shown in required_output_yaml_template.",
                "Put Python source in files[*].content using a YAML literal block scalar: content: |",
                "Do not return JSON.",
                "Do not return placeholder, TODO, pseudocode, or demo-only code.",
                "Keep the implementation as small as possible while still producing the registered artifacts.",
            ],
            "required_output_yaml_template": executor_tool_code_required_yaml_template(),
            "proposal": proposal,
            "approved_tool_design": design,
            "previous_code_response_errors": previous_errors or [],
        },
        allow_unicode=True,
        sort_keys=False,
    )
    response = ai_adapter.call_active_provider(prompt, schema={"type": "executor_tool_code"})
    code_response = _parse_mapping_response(response.content)
    code_response["ai_provider"] = getattr(response, "provider_id", None)
    code_response["response_hash"] = getattr(response, "response_hash", None)
    validation_errors = validate_executor_tool_code_response(code_response)
    code_response["validation_errors"] = validation_errors
    code_response["status"] = "draft_tool_code" if not validation_errors else "invalid_tool_code_response"
    return code_response


def request_executor_tool_code(
    *,
    proposal: dict[str, Any],
    compile_reason: str,
    compile_errors: list[str],
    registry: dict[str, Any],
    ai_adapter,
    run_dir: Path,
    store: ArtifactStore,
) -> dict[str, Any]:
    descriptor_path = run_dir / "executor_tool_request.yaml"
    existing_descriptor = store.read_yaml(descriptor_path, default={}) if descriptor_path.exists() else {}
    existing_design = existing_descriptor.get("tool_design") if isinstance(existing_descriptor.get("tool_design"), dict) else None
    existing_status = str(existing_descriptor.get("status") or "")
    prior_code_attempts = int(existing_descriptor.get("tool_code_attempts") or 0)
    if existing_status == "invalid_tool_code_response" and prior_code_attempts == 0:
        prior_code_attempts = 1
    if existing_status in {"draft_tool_design_pending_code", "invalid_tool_code_response"} and existing_design:
        design = existing_design
    else:
        design = request_executor_tool_design(
            proposal=proposal,
            compile_reason=compile_reason,
            compile_errors=compile_errors,
            registry=registry,
            ai_adapter=ai_adapter,
        )
    code_response: dict[str, Any] | None = None
    if design["status"] == "draft_tool_design":
        pending_package = {
            "tool_request_id": design.get("tool_request_id"),
            "why_existing_tools_insufficient": design.get("why_existing_tools_insufficient"),
            "reviewed_existing_executor_ids": design.get("reviewed_existing_executor_ids"),
            "registry_entry": design.get("registry_entry"),
            "implementation_outline": design.get("implementation_outline"),
            "validation_plan": design.get("validation_plan"),
            "ai_provider": design.get("ai_provider"),
            "design_response_hash": design.get("response_hash"),
            "code_response_hash": None,
            "compile_reason": compile_reason,
            "tool_design": design,
            "tool_code_response": None,
            "files": [],
            "written_files": [],
            "tool_code_attempts": prior_code_attempts,
            "validation_errors": [],
            "status": "draft_tool_design_pending_code",
        }
        store.write_yaml(descriptor_path, pending_package, no_aliases=True)
        try:
            code_response = request_executor_tool_code_from_design(
                proposal=proposal,
                design=design,
                ai_adapter=ai_adapter,
                previous_errors=list(existing_descriptor.get("validation_errors") or []),
            )
        except Exception as exc:
            attempts = prior_code_attempts + 1
            validation_errors = [f"provider_error: {exc}"]
            package = {
                "tool_request_id": design.get("tool_request_id"),
                "why_existing_tools_insufficient": design.get("why_existing_tools_insufficient"),
                "reviewed_existing_executor_ids": design.get("reviewed_existing_executor_ids"),
                "registry_entry": design.get("registry_entry"),
                "implementation_outline": design.get("implementation_outline"),
                "validation_plan": design.get("validation_plan"),
                "ai_provider": design.get("ai_provider"),
                "design_response_hash": design.get("response_hash"),
                "code_response_hash": None,
                "compile_reason": compile_reason,
                "tool_design": design,
                "tool_code_response": {
                    "status": "provider_error",
                    "validation_errors": validation_errors,
                },
                "files": [],
                "written_files": [],
                "tool_code_attempts": attempts,
                "validation_errors": validation_errors,
                "status": "abandoned_tool_code_response" if attempts >= 3 else "invalid_tool_code_response",
            }
            written_descriptor_path = store.write_yaml(descriptor_path, package, no_aliases=True)
            package["descriptor_path"] = str(written_descriptor_path)
            record_framework_change(
                change_type="executor_tool_code_request_failed",
                summary=f"Executor tool code request failed for {proposal.get('proposal_id')}",
                changed_paths=[str(written_descriptor_path)],
                actor=str(package.get("ai_provider") or "ai"),
                reason=compile_reason,
                impact="Provider failure is recorded on the draft so automation can retry or skip it instead of stalling.",
                evidence={
                    "proposal_id": proposal.get("proposal_id"),
                    "tool_request_id": package.get("tool_request_id"),
                    "design_response_hash": package.get("design_response_hash"),
                    "tool_code_attempts": attempts,
                    "status": package["status"],
                    "validation_errors": validation_errors,
                },
            )
            return {
                "descriptor_path": str(written_descriptor_path),
                "written_files": [],
                "tool_request_id": package.get("tool_request_id"),
                "design_response_hash": package.get("design_response_hash"),
                "code_response_hash": None,
                "status": package["status"],
                "validation_errors": validation_errors,
            }

    package = {
        "tool_request_id": design.get("tool_request_id"),
        "why_existing_tools_insufficient": design.get("why_existing_tools_insufficient"),
        "reviewed_existing_executor_ids": design.get("reviewed_existing_executor_ids"),
        "registry_entry": design.get("registry_entry"),
        "implementation_outline": design.get("implementation_outline"),
        "validation_plan": design.get("validation_plan"),
        "ai_provider": design.get("ai_provider"),
        "design_response_hash": design.get("response_hash"),
        "code_response_hash": code_response.get("response_hash") if code_response else None,
        "compile_reason": compile_reason,
        "tool_design": design,
        "tool_code_response": code_response,
        "files": code_response.get("files") if code_response else [],
        "tool_code_attempts": prior_code_attempts + (1 if code_response is not None else 0),
    }
    validation_errors = []
    validation_errors.extend([f"design: {error}" for error in design.get("validation_errors", [])])
    if code_response is None:
        validation_errors.append("code: skipped because tool design was invalid")
    else:
        validation_errors.extend([f"code: {error}" for error in code_response.get("validation_errors", [])])
    package["validation_errors"] = validation_errors

    written_files: list[str] = []
    if not validation_errors:
        for index, item in enumerate(package.get("files", []) or [], start=1):
            content = item["content"]
            target = _safe_generated_path(run_dir, str(item["path"]), index)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            written_files.append(str(target))
    package["written_files"] = written_files
    package["status"] = "draft_tool_code" if not validation_errors else "invalid_tool_code_response"
    written_descriptor_path = store.write_yaml(descriptor_path, package, no_aliases=True)
    package["descriptor_path"] = str(written_descriptor_path)
    record_framework_change(
        change_type="executor_tool_code_requested",
        summary=f"Requested executor tool code for {proposal.get('proposal_id')}",
        changed_paths=[str(written_descriptor_path), *written_files],
        actor=str(package.get("ai_provider") or "ai"),
        reason=compile_reason,
        impact="Draft evaluator code is stored for review and registration; it is not runnable until registered.",
        evidence={
            "proposal_id": proposal.get("proposal_id"),
            "tool_request_id": package.get("tool_request_id"),
            "design_response_hash": package.get("design_response_hash"),
            "code_response_hash": package.get("code_response_hash"),
            "written_files": written_files,
        },
    )
    return {
        "descriptor_path": str(descriptor_path),
        "written_files": written_files,
        "tool_request_id": package.get("tool_request_id"),
        "design_response_hash": package.get("design_response_hash"),
        "code_response_hash": package.get("code_response_hash"),
        "status": package["status"],
        "validation_errors": validation_errors,
    }


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


def closed_tags_from_runtime(
    *,
    digest: dict[str, Any],
    config: dict[str, Any] | None = None,
    queue_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    closed = closed_tags_from_digest(digest)
    for source_name, source in (("strategy_ideator", config or {}), ("research_queue", queue_state or {})):
        for family in source.get("forbidden_families", []) or []:
            tag = str(family)
            closed.setdefault(
                tag,
                {
                    "source": source_name,
                    "verdict": "forbidden_family",
                    "one_line_summary": "Direction is closed by runtime forbidden_families.",
                },
            )
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


def capability_vocab(registry: dict[str, Any]) -> set[str]:
    return set(capability_catalog(registry))


def recent_proposals_from_digest(digest: dict[str, Any]) -> list[dict[str, Any]]:
    proposals: list[dict[str, Any]] = []
    runs = digest.get("runs") or digest.get("recent_runs") or []
    if not isinstance(runs, list):
        return proposals
    for run in runs:
        if not isinstance(run, dict):
            continue
        proposal = run.get("proposal")
        if isinstance(proposal, dict):
            proposals.append(proposal)
            continue
        item: dict[str, Any] = {}
        for key in ("proposal_id", "hypothesis", "capability_ids", "mechanics"):
            if key in run:
                item[key] = run[key]
        if item:
            proposals.append(item)
    return proposals


def capability_menu(registry: dict[str, Any]) -> dict[str, Any]:
    raw_capabilities = registry.get("capabilities") or {}
    capabilities = raw_capabilities if isinstance(raw_capabilities, dict) else {}
    executors = registry.get("executors") or {}
    executor_items = executors.values() if isinstance(executors, dict) else executors
    menu: dict[str, Any] = {}
    for cid, item in capabilities.items():
        if not isinstance(item, dict):
            continue
        cid_str = str(cid)
        mechanic = str(item.get("mechanic") or "")
        entry = {
            "mechanic": mechanic,
            "label": item.get("label"),
            "available_executors": [],
            "blocked_by_executors": [],
            "required_data": [],
        }
        for executor in executor_items or []:
            if not isinstance(executor, dict):
                continue
            executor_id = executor.get("id")
            if cid_str in {str(x) for x in executor.get("can_test_capability_ids", []) or []}:
                entry["available_executors"].append({
                    "id": executor_id,
                    "family": executor.get("family"),
                    "description": executor.get("description"),
                })
                for data_item in executor.get("required_data", []) or []:
                    path = data_item.get("path") if isinstance(data_item, dict) else data_item
                    if path and path not in entry["required_data"]:
                        entry["required_data"].append(path)
            if cid_str in {str(x) for x in executor.get("cannot_test_capability_ids", []) or []}:
                entry["blocked_by_executors"].append(executor_id)
        menu[cid_str] = entry
    return menu


def proposal_instruction(
    closed_tags: dict[str, Any],
    digest: dict[str, Any],
    cap_menu: dict[str, Any],
    current: dict[str, Any] | None = None,
) -> dict[str, Any]:
    active_strategy_id = current_main_strategy_id(current or {}) or "unknown"
    return {
        "task": "Generate exactly one new strategy proposal as a YAML or JSON object.",
        "hard_rules": [
            "Return only the object. No markdown fences, no explanation.",
            "Select capability_ids only from capability_menu. Do not hand-write capability names.",
            "Use capability_menu to understand each id, available executors, and required data.",
            "If no capability id fits the idea, do not invent one; include missing_capability_request and expect DRAFT.",
            "Do not use any capability whose resolved mechanic is listed in closed_tags.",
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
            "capability_ids",
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
        "current_state": current or {},
        "allowed_strategy_id": active_strategy_id,
        "closed_tags": closed_tags,
        "allowed_capability_ids": sorted(cap_menu),
        "capability_menu": cap_menu,
        "recent_digest": digest,
    }


def current_main_strategy_id(current: dict[str, Any]) -> str | None:
    summary = current.get("summary") if isinstance(current, dict) else {}
    if isinstance(summary, dict) and summary.get("current_main_strategy_id"):
        return str(summary["current_main_strategy_id"])
    strategies = current.get("strategies") if isinstance(current, dict) else []
    if isinstance(strategies, list):
        for item in strategies:
            if isinstance(item, dict) and item.get("relationship") == "current_main" and item.get("strategy_id"):
                return str(item["strategy_id"])
    return None


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
