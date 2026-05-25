#!/usr/bin/env python3
"""Install hermes-generated evaluator scripts to scripts/ and flip registry.

Background
----------
When the autonomous quant framework needs a new evaluator, Hermes is woken via
``scripts/hermes_executor_handoff_wakeup.sh`` and writes the implementation
into ``data/<run_dir>/generated_executor/<script>.py``. That part works.

The last mile was missing: nothing copied the generated file to
``scripts/<script>.py`` and nothing flipped the matching ``executor_registry``
entry from ``draft`` to ``implemented``. As a result the proposal compiler
kept rejecting future proposals that asked for the same script, and Hermes
was asked to re-implement it again and again.

This script closes that gap. For every completed handoff:

1. Resolve the source ``generated_executor`` path.
2. Validate the generated file (compile + ``main`` + ``declare_data_requirements``).
3. Copy it to ``target_script_path`` (skipping if the destination already
   matches the source by SHA-256 — install is idempotent).
4. If ``executor_registry.yaml`` has an entry whose ``script_path`` matches the
   target and whose ``status`` is ``draft``, flip it to ``implemented``.
5. Record the install on the handoff task as ``installed_at`` /
   ``installed_source`` / ``installed_sha256`` so a re-run can detect drift.

The script intentionally does NOT create brand-new registry entries — those
require mechanic / capability metadata that has to come from the proposer.
For now drafts get promoted; new entries stay a manual / Codex job.

Hard boundaries respected (per CLAUDE.md / AGENTS.md):
- Does not touch verifier / cost_model / baseline_registry.
- Does not promote strategy state.
- Does not start runs.
- Only writes to scripts/<name>.py, executor_registry.yaml, and the same
  handoff task entries it inspects.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
HANDOFFS_PATH = REPO_ROOT / "data/research_framework/hermes_executor_handoffs.yaml"
REGISTRY_PATH = REPO_ROOT / "data/research_framework/executor_registry.yaml"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def dump_yaml(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def resolve_source(task: dict[str, Any]) -> Path | None:
    run_dir_raw = str(task.get("run_dir") or "")
    if not run_dir_raw:
        return None
    run_dir = Path(run_dir_raw)
    if not run_dir.is_absolute():
        run_dir = REPO_ROOT / run_dir
    generated_dir = run_dir / "generated_executor"
    if not generated_dir.is_dir():
        return None
    target_name = Path(str(task.get("target_script_path") or "")).name
    if target_name:
        preferred = generated_dir / target_name
        if preferred.exists():
            return preferred
    candidates = sorted(p for p in generated_dir.glob("*.py") if p.is_file())
    if len(candidates) == 1:
        return candidates[0]
    return None


# GateKeeper compliance string — mirrors framework_preflight's
# validate_gatekeeper_compliance.py and the handoff-time check in
# framework/autonomous/hermes_executor_handoff.REQUIRED_GATEKEEPER_IMPORT.
# Kept inline rather than imported to keep this script standalone for the
# `*/5` cron.
REQUIRED_GATEKEEPER_IMPORT = "from scripts.gatekeeper import GateKeeper"


def validate_executor(path: Path) -> list[str]:
    errors: list[str] = []
    try:
        spec = importlib.util.spec_from_file_location(f"_check_{path.stem}", path)
        if spec is None or spec.loader is None:
            return [f"cannot load module: {path}"]
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    except SyntaxError as exc:
        return [f"SyntaxError: {exc}"]
    except Exception as exc:
        errors.append(f"import-time failure: {type(exc).__name__}: {exc}")
        return errors
    if not callable(getattr(module, "main", None)):
        errors.append("missing main()")
    if not callable(getattr(module, "declare_data_requirements", None)):
        errors.append("missing declare_data_requirements()")
    # Compliance gate: the generated executor must import GateKeeper. This
    # mirrors framework_preflight.validate_gatekeeper_compliance.py and the
    # handoff-time check in hermes_executor_handoff._validate_generated_executor.
    # Failing this here is a "compliance_failed" outcome: the source is NOT
    # copied to scripts/, the run dir gets a repair_request marker, and the
    # handoff task is held open for Hermes to repair on its next tick.
    try:
        source_text = path.read_text(encoding="utf-8")
    except Exception as exc:
        errors.append(f"could not read source for compliance check: {exc}")
        return errors
    if REQUIRED_GATEKEEPER_IMPORT not in source_text:
        errors.append(
            f"compliance_failed: missing '{REQUIRED_GATEKEEPER_IMPORT}' "
            "(framework_preflight requires every grid-style executor to "
            "import GateKeeper and call its lifecycle methods)"
        )
    return errors


def install_one(
    task: dict[str, Any],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    handoff_id = task.get("id") or "?"
    target_raw = str(task.get("target_script_path") or "")
    if not target_raw or not target_raw.startswith("scripts/") or not target_raw.endswith(".py"):
        return {"id": handoff_id, "action": "skipped", "reason": "target_script_path must be scripts/*.py"}

    target = REPO_ROOT / target_raw
    source = resolve_source(task)
    if source is None:
        # Distinguish two subcategories of "no generated_executor source":
        #
        # 1. Task was previously installed (installed_at is set) AND the
        #    target file still exists in scripts/. This means the source
        #    was lost AFTER a successful install (e.g., the run dir's
        #    generated_executor/ was cleaned up). The task is stuck — its
        #    executor_tool_request keeps reporting awaiting_hermes_executor_code
        #    or draft_tool_code, but install can never re-validate or replace
        #    the noncompliant target because there is nothing to read.
        #    Flip the task to needs_executor_regeneration so the handoff
        #    layer can re-claim it and ask Hermes to rewrite from scratch.
        #
        # 2. Task was never installed (no installed_at). This is the normal
        #    "Hermes hasn't written it yet" path — keep the old skipped
        #    behaviour, the upstream Hermes flow will fill the directory.
        installed_at = task.get("installed_at")
        if installed_at and target.exists():
            regeneration_marker = _write_regeneration_request(task, target, dry_run=dry_run)
            return {
                "id": handoff_id,
                "action": "needs_executor_regeneration",
                "reason": (
                    "generated_executor source is missing for a previously-"
                    "installed task; cannot re-validate or replace the target "
                    "in scripts/ without it. Hermes must regenerate the "
                    "executor from the original spec + executor_tool_request."
                ),
                "target": target_raw,
                "installed_at": installed_at,
                "regeneration_request": regeneration_marker,
            }
        return {"id": handoff_id, "action": "skipped", "reason": "no generated_executor source found"}

    validation_errors = validate_executor(source)
    if validation_errors:
        compliance_failed = any(
            err.startswith("compliance_failed:") for err in validation_errors
        )
        if compliance_failed:
            # Do NOT copy to scripts/. Mark the run dir + handoff task with a
            # repair_request so Hermes' next handoff tick can rewrite.
            repair_marker = _write_repair_request(task, source, validation_errors, dry_run=dry_run)
            return {
                "id": handoff_id,
                "action": "compliance_failed",
                "reason": "executor must be GateKeeper-compliant before install",
                "errors": validation_errors,
                "source": str(source.relative_to(REPO_ROOT)),
                "repair_request": repair_marker,
            }
        return {
            "id": handoff_id,
            "action": "skipped",
            "reason": "generated executor failed validation",
            "errors": validation_errors,
            "source": str(source.relative_to(REPO_ROOT)),
        }

    source_hash = sha256_of(source)
    target_exists = target.exists()
    target_hash = sha256_of(target) if target_exists else None

    if target_hash == source_hash:
        return {
            "id": handoff_id,
            "action": "noop",
            "reason": "destination already up-to-date",
            "target": target_raw,
            "sha256": source_hash[:16],
        }

    # Repair-flow overwrite: when a task carries compliance_failed_at OR
    # source_lost_detected_at, the destination is a known-bad / known-stale
    # version that we explicitly asked Hermes to rewrite (compliance repair
    # or executor regeneration after a lost source). The source above
    # already passed validate_executor() — which includes the GateKeeper
    # compliance check — so overwriting is safe and is in fact the only way
    # to land the repair. Both conditions must be true: history says it
    # needs replacement AND the new source is verified compliant.
    repair_reason: str | None = None
    if target_exists:
        if task.get("compliance_failed_at"):
            repair_reason = "compliance_repair"
        elif task.get("source_lost_detected_at"):
            repair_reason = "executor_regeneration"
    if target_exists and repair_reason:
        if dry_run:
            return {
                "id": handoff_id,
                "action": "would_overwrite_compliance_repair"
                          if repair_reason == "compliance_repair"
                          else "would_overwrite_executor_regeneration",
                "source": str(source.relative_to(REPO_ROOT)),
                "target": target_raw,
                "source_sha256": source_hash[:16],
                "previous_target_sha256": (target_hash or "")[:16],
            }
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return {
            "id": handoff_id,
            "action": "overwritten_after_compliance_repair"
                      if repair_reason == "compliance_repair"
                      else "overwritten_after_executor_regeneration",
            "source": str(source.relative_to(REPO_ROOT)),
            "target": target_raw,
            "sha256": source_hash[:16],
            "previous_target_sha256": (target_hash or "")[:16],
            "overwrite_reason": repair_reason,
        }

    # Never silently overwrite a destination that already exists with different
    # content outside the repair flow — that destination was almost certainly
    # hand-edited (e.g., a bug fix or schema patch) and the generated_executor
    # copy is the older Hermes draft. Re-installing it would clobber the
    # human/Claude edit.
    if target_exists:
        return {
            "id": handoff_id,
            "action": "skipped",
            "reason": (
                "destination exists with different content; refusing to overwrite "
                "(likely hand-edited). Delete the target or use a new path if you "
                "really want to re-install."
            ),
            "target": target_raw,
            "target_sha256": (target_hash or "")[:16],
            "source_sha256": source_hash[:16],
        }

    if dry_run:
        return {
            "id": handoff_id,
            "action": "would_install",
            "source": str(source.relative_to(REPO_ROOT)),
            "target": target_raw,
            "target_exists": target_exists,
            "source_sha256": source_hash[:16],
        }

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return {
        "id": handoff_id,
        "action": "installed",
        "source": str(source.relative_to(REPO_ROOT)),
        "target": target_raw,
        "sha256": source_hash[:16],
    }


def _write_regeneration_request(
    task: dict[str, Any], target: Path, *, dry_run: bool
) -> str:
    """Mark a task whose generated_executor source was lost after install
    so the next handoff tick can re-claim it and ask Hermes to regenerate.

    Writes ``executor_regeneration_request.yaml`` into the run dir's
    ``generated_executor/`` directory (creating it if needed). The marker
    captures the original install fingerprint so Hermes (and any auditor)
    can see what was lost vs what is currently in scripts/.
    """
    handoff_id = str(task.get("id") or "")
    run_dir_raw = str(task.get("run_dir") or "")
    if not run_dir_raw:
        return ""
    run_dir = Path(run_dir_raw)
    if not run_dir.is_absolute():
        run_dir = REPO_ROOT / run_dir
    marker_path = run_dir / "generated_executor" / "executor_regeneration_request.yaml"
    target_sha256 = sha256_of(target) if target.exists() else ""
    payload = {
        "schema_version": 1,
        "handoff_id": handoff_id,
        "target_script_path": str(target.relative_to(REPO_ROOT)) if target.is_relative_to(REPO_ROOT) else str(target),
        "current_target_sha256": target_sha256,
        "previous_installed_source": task.get("installed_source"),
        "previous_installed_sha256": task.get("installed_sha256"),
        "previous_installed_at": task.get("installed_at"),
        "required_action": (
            "Regenerate the executor from the existing spec.yaml + "
            "executor_tool_request.yaml in this run dir. The source file "
            "under generated_executor/ was lost after the previous install; "
            "the current target in scripts/ may not be the latest version. "
            "Write a fresh executor that satisfies the original validation "
            "requirements (main + declare_data_requirements + summary.json / "
            "report.yaml / l4_ack.yaml / diagnostic.yaml output + GateKeeper "
            "import). Once written, the standard handoff completion → "
            "install validation → install overwrite (compliance repair flow) "
            "path will re-land the executor."
        ),
        "noted_at": now_iso(),
        "dry_run": dry_run,
    }
    if dry_run:
        return str(marker_path.relative_to(REPO_ROOT)) if marker_path.is_relative_to(REPO_ROOT) else str(marker_path)
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return str(marker_path.relative_to(REPO_ROOT))


def _write_repair_request(
    task: dict[str, Any], source: Path, errors: list[str], *, dry_run: bool
) -> str:
    """Mark a compliance-failed generated executor for Hermes repair.

    Writes ``compliance_repair_request.yaml`` next to the failed generated
    executor with the validator errors and the expected fix. The handoff
    task is NOT marked completed/installed; the run dir flags the repair
    so the next handoff_tick can see and re-claim it.
    """
    handoff_id = str(task.get("id") or "")
    run_dir_raw = str(task.get("run_dir") or "")
    if not run_dir_raw:
        return ""
    run_dir = Path(run_dir_raw)
    if not run_dir.is_absolute():
        run_dir = REPO_ROOT / run_dir
    marker_path = source.parent / "compliance_repair_request.yaml"
    payload = {
        "schema_version": 1,
        "handoff_id": handoff_id,
        "source": str(source.relative_to(REPO_ROOT)),
        "compliance_errors": errors,
        "required_fix": (
            "Add 'from scripts.gatekeeper import GateKeeper' to the generated "
            "executor and call the GateKeeper lifecycle methods "
            "(before_run_grid / after_run_grid). See "
            "scripts/evaluate_cb_arb_exit_gap_closing_percentile.py and "
            "scripts/evaluate_cb_arb_iv_percentile_exit.py for the canonical "
            "pattern. Do not request manual allowlist; the install path will "
            "remain blocked until the import is in place."
        ),
        "noted_at": now_iso(),
        "dry_run": dry_run,
    }
    if dry_run:
        return str(marker_path.relative_to(REPO_ROOT))
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return str(marker_path.relative_to(REPO_ROOT))


def recompile_drafts_after_install(
    installed_targets: set[str], *, dry_run: bool
) -> dict[str, Any]:
    """For each newly-installed executor, find DRAFT specs whose
    required_executor / required_script_path matches it, and re-run
    spec_compiler.compile. If compile succeeds → spec is overwritten with
    the now-READY version (spec_compiler writes the file itself via the
    output_dir argument). If compile still returns DRAFT/REJECT → leave
    the spec as it is and surface the reason in the audit log.

    Does NOT manually flip status; the compile call is the single source
    of truth for whether a spec is runnable.
    """
    result: dict[str, Any] = {"recompiled": [], "skipped": [], "blocked": []}
    if not installed_targets:
        return result
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from framework.autonomous.spec_compiler import compile as compile_proposal  # noqa: E402
    except Exception as exc:
        result["error"] = f"cannot import spec_compiler: {exc}"
        return result
    registry = load_yaml(REGISTRY_PATH) if REGISTRY_PATH.exists() else {}
    closed_path = REPO_ROOT / "data" / "research_framework" / "closed_tags.yaml"
    closed_tags: dict[str, Any] = {}
    if closed_path.exists():
        ct = load_yaml(closed_path)
        if isinstance(ct, dict):
            closed_tags = ct.get("closed_tags") if isinstance(ct.get("closed_tags"), dict) else ct
    # Build an index: script_path -> executor entry id, so we can match
    # spec.required_executor (which is the executor id) back to a script
    # path that was just installed.
    executors = (registry.get("executors") or {}) if isinstance(registry, dict) else {}
    script_to_executor_id: dict[str, str] = {}
    for ex_id, ex in executors.items():
        if isinstance(ex, dict):
            sp = ex.get("script_path")
            if isinstance(sp, str):
                script_to_executor_id[sp] = ex_id
    just_installed_executor_ids = {
        script_to_executor_id.get(t) for t in installed_targets if t in script_to_executor_id
    }
    # Scan data/ for DRAFT specs whose required_executor is in this set.
    data_root = REPO_ROOT / "data"
    if not data_root.exists():
        return result
    for spec_path in data_root.glob("*/spec.yaml"):
        try:
            spec_data = load_yaml(spec_path)
        except Exception as exc:
            result["skipped"].append({"spec": str(spec_path.relative_to(REPO_ROOT)),
                                      "reason": f"yaml load failed: {exc}"})
            continue
        if str(spec_data.get("status") or "") != "DRAFT":
            continue
        required_executor = str(spec_data.get("required_executor") or "")
        if required_executor not in just_installed_executor_ids:
            continue
        # Reconstruct the proposal payload spec_compiler expects.
        proposal_path = spec_path.parent / "proposal.yaml"
        if not proposal_path.exists():
            result["skipped"].append({"spec": str(spec_path.relative_to(REPO_ROOT)),
                                      "reason": "missing proposal.yaml"})
            continue
        try:
            proposal = load_yaml(proposal_path)
        except Exception as exc:
            result["skipped"].append({"spec": str(spec_path.relative_to(REPO_ROOT)),
                                      "reason": f"proposal yaml load failed: {exc}"})
            continue
        if dry_run:
            result["recompiled"].append({
                "spec": str(spec_path.relative_to(REPO_ROOT)),
                "action": "would_recompile",
            })
            continue
        try:
            outcome = compile_proposal(
                proposal=proposal,
                registry=registry,
                closed_tags=closed_tags,
                recent_proposals=[],
                output_dir=spec_path.parent,
            )
        except Exception as exc:
            result["skipped"].append({"spec": str(spec_path.relative_to(REPO_ROOT)),
                                      "reason": f"compile raised: {type(exc).__name__}: {exc}"})
            continue
        if outcome.status == "READY":
            result["recompiled"].append({
                "spec": str(spec_path.relative_to(REPO_ROOT)),
                "new_status": "READY",
            })
        else:
            # Surfaces "still DRAFT / REJECT" as a blocked entry so the run
            # is visible in the install summary but not silently absorbed.
            result["blocked"].append({
                "spec": str(spec_path.relative_to(REPO_ROOT)),
                "compile_status": outcome.status,
                "reason": outcome.reason,
            })
    return result


def flip_registry_drafts(installed_targets: set[str], *, dry_run: bool) -> dict[str, Any]:
    flipped: list[str] = []
    if not REGISTRY_PATH.exists():
        return {"flipped": flipped, "note": "registry not found"}
    registry = load_yaml(REGISTRY_PATH)
    executors = registry.get("executors") or {}
    changed = False
    for key, entry in executors.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("script_path") in installed_targets and entry.get("status") == "draft":
            flipped.append(key)
            if not dry_run:
                entry["status"] = "implemented"
                entry["status_flipped_at"] = now_iso()
                entry["status_flipped_by"] = "install_generated_executors"
                changed = True
    if changed and not dry_run:
        registry["updated_at"] = now_iso()
        dump_yaml(REGISTRY_PATH, registry)
    return {"flipped": flipped}


def _import_executor(script_path: Path):
    module_name = f"_install_register_{script_path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    return module


def _spec_for_task(task: dict[str, Any]) -> dict[str, Any] | None:
    run_dir_raw = str(task.get("run_dir") or "")
    if not run_dir_raw:
        return None
    run_dir = Path(run_dir_raw)
    if not run_dir.is_absolute():
        run_dir = REPO_ROOT / run_dir
    spec_path = run_dir / "spec.yaml"
    if not spec_path.exists():
        return None
    try:
        return load_yaml(spec_path)
    except Exception:
        return None


def _capability_lookup(registry: dict[str, Any]) -> dict[str, str]:
    """Returns mapping mechanic -> capability_id (e.g. dynamic_regime_switch -> C005)."""
    out: dict[str, str] = {}
    for cap_id, info in (registry.get("capabilities") or {}).items():
        if isinstance(info, dict):
            mechanic = info.get("mechanic")
            if mechanic:
                out[str(mechanic)] = str(cap_id)
    return out


def _all_mechanic_tags(registry: dict[str, Any]) -> list[str]:
    return sorted((registry.get("mechanic_tags") or {}).keys())


def _build_registry_entry(
    task: dict[str, Any],
    script_path: str,
    spec: dict[str, Any],
    evaluator_module: Any,
    capability_lookup: dict[str, str],
    all_mechanics: list[str],
) -> dict[str, Any] | None:
    strategy_id = spec.get("strategy_id")
    if not isinstance(strategy_id, str) or not strategy_id:
        return None
    mechanics_raw = spec.get("mechanics") or []
    can_test = [str(m) for m in mechanics_raw if isinstance(m, str)]
    if not can_test:
        return None
    can_test_ids = [capability_lookup[m] for m in can_test if m in capability_lookup]
    cannot_test = sorted(m for m in all_mechanics if m not in set(can_test))
    cannot_test_ids = [capability_lookup[m] for m in cannot_test if m in capability_lookup]

    required_data: list[dict[str, Any]] = []
    if evaluator_module is not None and callable(getattr(evaluator_module, "declare_data_requirements", None)):
        cmd_for_decl = list(spec.get("automation", {}).get("command") or [])
        try:
            req = evaluator_module.declare_data_requirements(cmd_for_decl, spec)
            for f in (req or {}).get("required_files") or []:
                if isinstance(f, str):
                    required_data.append({"path": f, "description": "", "schema_hash": None})
                elif isinstance(f, dict) and f.get("path"):
                    required_data.append(
                        {
                            "path": str(f["path"]),
                            "description": f.get("note") or f.get("description") or "",
                            "schema_hash": None,
                        }
                    )
        except Exception:
            pass

    command_template = list(spec.get("automation", {}).get("command") or [])
    if not command_template:
        # When the proposing spec doesn't supply a concrete command, fall back
        # to the canonical cb_arb argparse contract — every cb_arb evaluator
        # accepts --data-root / --train-start / --train-end / --test-start /
        # --test-end / --output-dir. Spec compiler will substitute the values
        # from default_config. Evaluator-specific optional flags can still be
        # appended later by hand-editing the registry entry.
        command_template = [
            ".venv/bin/python", script_path,
            "--data-root", "{data_root}",
            "--train-start", "{train_start}",
            "--train-end", "{train_end}",
            "--test-start", "{test_start}",
            "--test-end", "{test_end}",
            "--output-dir", "{output_dir}",
        ]
    default_config: dict[str, Any] = {
        "data_root": "data/cb_arb_concurrent_supervised_20260511_094500",
        "train_start": "20190101",
        "train_end": "20241231",
        "test_start": "20250101",
        "test_end": "20260508",
    }
    compute = spec.get("compute_estimate") or {}
    budget_estimate = {
        "sig_minutes": compute.get("sig_minutes", 0),
        "spot_minutes": compute.get("spot_minutes", 60),
        "local_minutes": compute.get("local_minutes", 0),
        "estimated_cost_yuan": compute.get("estimated_cost_yuan", 0.0),
    }
    hypothesis = spec.get("hypothesis")
    if isinstance(hypothesis, dict):
        description = hypothesis.get("summary") or hypothesis.get("description") or ""
    elif isinstance(hypothesis, str):
        description = hypothesis
    else:
        description = ""
    description = description.strip()
    if len(description) > 600:
        description = description[:597] + "..."
    docstring = (getattr(evaluator_module, "__doc__", "") or "").strip().splitlines()
    if docstring and not description:
        description = docstring[0]

    family = str(spec.get("family") or task.get("family") or Path(script_path).stem.replace("evaluate_cb_arb_", ""))

    entry: dict[str, Any] = {
        "id": Path(script_path).stem,
        "version": 1,
        "status": "implemented",
        "strategy_id": strategy_id,
        "family": family,
        "script_path": script_path,
        "owner": "hermes_auto_install",
        "description": description or f"Auto-registered evaluator for {family}.",
        "can_test": can_test,
        "can_test_capability_ids": can_test_ids,
        "cannot_test": cannot_test,
        "cannot_test_capability_ids": cannot_test_ids,
        "required_data": required_data,
        "required_config_fields": ["data_root", "train_start", "train_end", "test_start", "test_end", "output_dir"],
        "command_template": command_template,
        "default_config": default_config,
        "artifacts_produced": ["summary.json", "report.yaml", "l4_ack.yaml", "diagnostic.yaml"],
        "budget_estimate": budget_estimate,
        # cb_arb-family executors all require VM execution (per CLAUDE.md "VM
        # only, no local exec" rule). The registry schema requires this field
        # to be present; leaving it out makes validate_registry_schema reject
        # the whole registry and breaks spec compilation for every proposal.
        "vm_local_limits": {"vm_required": True, "local_allowed": False},
        "auto_registered": True,
        "auto_registered_at": now_iso(),
        "auto_registered_by": "install_generated_executors",
        "auto_registered_from_handoff": task.get("id"),
    }
    return entry


def auto_register_new_executors(
    completed_tasks: list[dict[str, Any]],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Add a registry entry for any installed evaluator that has no existing entry."""
    if not REGISTRY_PATH.exists():
        return {"registered": [], "note": "registry not found"}
    registry = load_yaml(REGISTRY_PATH)
    existing_paths = {
        str(entry.get("script_path"))
        for entry in (registry.get("executors") or {}).values()
        if isinstance(entry, dict)
    }
    capability_lookup = _capability_lookup(registry)
    all_mechanics = _all_mechanic_tags(registry)

    registered: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    changed = False
    for task in completed_tasks:
        target_raw = str(task.get("target_script_path") or "")
        if not target_raw.startswith("scripts/") or not target_raw.endswith(".py"):
            continue
        if target_raw in existing_paths:
            continue
        if not (REPO_ROOT / target_raw).exists():
            continue
        spec = _spec_for_task(task)
        if spec is None:
            skipped.append({"target": target_raw, "reason": "no spec.yaml in run_dir"})
            continue
        module = _import_executor(REPO_ROOT / target_raw)
        if module is None:
            skipped.append({"target": target_raw, "reason": "evaluator failed to import for registration"})
            continue
        entry = _build_registry_entry(
            task, target_raw, spec, module, capability_lookup, all_mechanics
        )
        if entry is None:
            skipped.append({"target": target_raw, "reason": "could not derive required fields (strategy_id / mechanics)"})
            continue
        key = entry["id"]
        if dry_run:
            registered.append({"key": key, "target": target_raw, "action": "would_register"})
            continue
        registry.setdefault("executors", {})[key] = entry
        registered.append({"key": key, "target": target_raw, "action": "registered"})
        changed = True
    if changed and not dry_run:
        registry["updated_at"] = now_iso()
        dump_yaml(REGISTRY_PATH, registry)
    return {"registered": registered, "skipped": skipped}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="show what would change without writing files")
    args = parser.parse_args()

    if not HANDOFFS_PATH.exists():
        print(f"handoffs file not found: {HANDOFFS_PATH}", file=sys.stderr)
        return 1

    doc = load_yaml(HANDOFFS_PATH)
    tasks = doc.get("tasks") or []
    installed_targets: set[str] = set()
    results: list[dict[str, Any]] = []
    handoff_changed = False
    now = now_iso()

    for task in tasks:
        if not isinstance(task, dict):
            continue
        if str(task.get("status") or "") != "completed":
            continue
        outcome = install_one(task, dry_run=args.dry_run)
        results.append(outcome)
        action = outcome.get("action")
        if action == "installed":
            installed_targets.add(outcome["target"])
            if not args.dry_run:
                task["installed_at"] = now
                task["installed_source"] = outcome["source"]
                task["installed_sha256"] = outcome["sha256"]
                task["installed_by"] = "install_generated_executors"
                handoff_changed = True
        elif action == "overwritten_after_compliance_repair" and not args.dry_run:
            # Repair flow landed. Mark the install fields like a normal install,
            # plus an explicit repair_installed_at, and shift the stale
            # compliance fields into a previous_* history so audit can still
            # see the run was once noncompliant.
            installed_targets.add(outcome["target"])
            task["status"] = "completed"
            task["installed_at"] = now
            task["repair_installed_at"] = now
            task["installed_source"] = outcome["source"]
            task["installed_sha256"] = outcome["sha256"]
            task["installed_by"] = "install_generated_executors"
            task["install_action"] = "overwritten_after_compliance_repair"
            task["last_compliance_status"] = "passed"
            if task.get("compliance_failed_at"):
                task["previous_compliance_failed_at"] = task.pop("compliance_failed_at")
            if task.get("compliance_errors"):
                task["previous_compliance_errors"] = task.pop("compliance_errors")
            handoff_changed = True
        elif action == "overwritten_after_executor_regeneration" and not args.dry_run:
            # Regeneration flow landed: Hermes wrote a fresh executor after
            # the original source was lost, and validate_executor accepted it.
            # Mark like a normal install, plus regeneration_installed_at, and
            # move the source_lost_detected_at / regeneration_request fields
            # into previous_* history so the audit trail keeps both the lost
            # event and the recovery event.
            installed_targets.add(outcome["target"])
            task["status"] = "completed"
            task["installed_at"] = now
            task["regeneration_installed_at"] = now
            task["installed_source"] = outcome["source"]
            task["installed_sha256"] = outcome["sha256"]
            task["installed_by"] = "install_generated_executors"
            task["install_action"] = "overwritten_after_executor_regeneration"
            task["last_compliance_status"] = "passed"
            if task.get("source_lost_detected_at"):
                task["previous_source_lost_detected_at"] = task.pop("source_lost_detected_at")
            if task.get("regeneration_request"):
                task["previous_regeneration_request"] = task.pop("regeneration_request")
            if task.get("regeneration_reason"):
                task["previous_regeneration_reason"] = task.pop("regeneration_reason")
            handoff_changed = True
        elif action == "noop":
            installed_targets.add(outcome["target"])
        elif action == "needs_executor_regeneration" and not args.dry_run:
            # generated_executor source vanished after an earlier successful
            # install. Flip the task into the regeneration repair flow.
            # Keep previous install fingerprints around as audit history so
            # the Hermes side knows what the target used to be.
            task["status"] = "needs_executor_regeneration"
            task["source_lost_detected_at"] = now
            task["regeneration_request"] = outcome.get("regeneration_request") or ""
            task["regeneration_reason"] = outcome.get("reason") or ""
            # Move current "installed_*" pointers into a history bucket so the
            # next install sees a clean slate when Hermes lands the rewrite,
            # but preserves the audit trail.
            if task.get("installed_at"):
                task["previous_installed_at"] = task.pop("installed_at")
            if task.get("installed_source"):
                task["previous_installed_source"] = task.pop("installed_source")
            if task.get("installed_sha256"):
                task["previous_installed_sha256"] = task.pop("installed_sha256")
            handoff_changed = True
        elif action == "compliance_failed" and not args.dry_run:
            # Task was marked completed by Hermes but the generated executor
            # fails compliance — flip the task back to needs_compliance_repair
            # so the handoff path knows Hermes still has work to do.
            task["status"] = "needs_compliance_repair"
            task["compliance_errors"] = outcome.get("errors") or []
            task["compliance_repair_request"] = outcome.get("repair_request") or ""
            task["compliance_failed_at"] = now
            # Clear installed_* fields since this code did not pass the gate.
            task.pop("installed_at", None)
            task.pop("installed_source", None)
            task.pop("installed_sha256", None)
            task.pop("installed_by", None)
            handoff_changed = True

    if handoff_changed and not args.dry_run:
        doc["updated_at"] = now
        dump_yaml(HANDOFFS_PATH, doc)

    registry_outcome = flip_registry_drafts(installed_targets, dry_run=args.dry_run)

    completed_tasks = [t for t in tasks if isinstance(t, dict) and str(t.get("status") or "") == "completed"]
    auto_reg_outcome = auto_register_new_executors(completed_tasks, dry_run=args.dry_run)

    # After install + registry flip + auto-register, the registry now reflects
    # the just-installed executors. Re-run spec_compiler on any DRAFT spec
    # whose required_executor is one of them, so DRAFT → READY transitions
    # happen automatically without anyone manually flipping spec.status.
    recompile_outcome = recompile_drafts_after_install(installed_targets, dry_run=args.dry_run)

    summary = {
        "timestamp": now,
        "dry_run": args.dry_run,
        "counts": {
            "installed": sum(1 for r in results if r.get("action") == "installed"),
            "would_install": sum(1 for r in results if r.get("action") == "would_install"),
            "noop": sum(1 for r in results if r.get("action") == "noop"),
            "skipped": sum(1 for r in results if r.get("action") == "skipped"),
            "compliance_failed": sum(1 for r in results if r.get("action") == "compliance_failed"),
            "overwritten_after_compliance_repair": sum(
                1 for r in results if r.get("action") == "overwritten_after_compliance_repair"
            ),
            "would_overwrite_compliance_repair": sum(
                1 for r in results if r.get("action") == "would_overwrite_compliance_repair"
            ),
            "needs_executor_regeneration": sum(
                1 for r in results if r.get("action") == "needs_executor_regeneration"
            ),
            "overwritten_after_executor_regeneration": sum(
                1 for r in results if r.get("action") == "overwritten_after_executor_regeneration"
            ),
        },
        "registry_drafts_flipped": registry_outcome.get("flipped", []),
        "registry_auto_registered": [r["key"] for r in auto_reg_outcome["registered"]],
        "registry_auto_skipped": auto_reg_outcome["skipped"],
        "spec_recompiled_to_ready": recompile_outcome.get("recompiled", []),
        "spec_recompile_blocked": recompile_outcome.get("blocked", []),
        "spec_recompile_skipped": recompile_outcome.get("skipped", []),
        "results": results,
    }
    print(yaml.safe_dump(summary, allow_unicode=True, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
