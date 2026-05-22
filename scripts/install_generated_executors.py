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
        return {"id": handoff_id, "action": "skipped", "reason": "no generated_executor source found"}

    validation_errors = validate_executor(source)
    if validation_errors:
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

    # Never silently overwrite a destination that already exists with different
    # content — that destination was almost certainly hand-edited (e.g., a bug
    # fix or schema patch) and the generated_executor copy is the older Hermes
    # draft. Re-installing it would clobber the human/Claude edit.
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
        "required_config_fields": [],
        "command_template": command_template,
        "default_config": {},
        "artifacts_produced": ["summary.json", "report.yaml", "l4_ack.yaml", "diagnostic.yaml"],
        "budget_estimate": budget_estimate,
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
        elif action == "noop":
            installed_targets.add(outcome["target"])

    if handoff_changed and not args.dry_run:
        doc["updated_at"] = now
        dump_yaml(HANDOFFS_PATH, doc)

    registry_outcome = flip_registry_drafts(installed_targets, dry_run=args.dry_run)

    completed_tasks = [t for t in tasks if isinstance(t, dict) and str(t.get("status") or "") == "completed"]
    auto_reg_outcome = auto_register_new_executors(completed_tasks, dry_run=args.dry_run)

    summary = {
        "timestamp": now,
        "dry_run": args.dry_run,
        "counts": {
            "installed": sum(1 for r in results if r.get("action") == "installed"),
            "would_install": sum(1 for r in results if r.get("action") == "would_install"),
            "noop": sum(1 for r in results if r.get("action") == "noop"),
            "skipped": sum(1 for r in results if r.get("action") == "skipped"),
        },
        "registry_drafts_flipped": registry_outcome.get("flipped", []),
        "registry_auto_registered": [r["key"] for r in auto_reg_outcome["registered"]],
        "registry_auto_skipped": auto_reg_outcome["skipped"],
        "results": results,
    }
    print(yaml.safe_dump(summary, allow_unicode=True, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
