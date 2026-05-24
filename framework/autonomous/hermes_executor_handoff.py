from __future__ import annotations

import argparse
import ast
import hashlib
import json
import py_compile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
HANDOFFS_PATH = Path("data/research_framework/hermes_executor_handoffs.yaml")
QUEUE_PATH = Path("data/research_framework/research_queue.yaml")
STATUS_PATH = Path("logs/research_queue_status.json")

FORBIDDEN_ACTIONS = [
    "Do not run scripts/quant_internal_tick.py.",
    "Do not run scripts/research_queue_runner.py.",
    "Do not launch VM or spot work.",
    "Do not modify data/research_framework/research_queue.yaml.",
    "Do not modify data/research_framework/current.yaml.",
    "Do not modify data/research_framework/baseline_registry.yaml.",
    "Do not promote, archive, or mark any strategy live.",
]
STALE_CLAIM_MINUTES = 10
# Statuses that hermes_executor_handoff is allowed to claim and process.
# Adding a new repair status (e.g. needs_generation_repair, needs_interface_repair)
# means extending this set, not patching open_tasks() at every call site.
# Keep this list in sync with the install / validate paths that flip tasks
# into these statuses.
HANDOFF_PICKABLE_STATUSES = {
    "open",
    "needs_compliance_repair",
    "needs_executor_regeneration",
}
REQUIRED_EXECUTOR_FUNCTIONS = {"main", "declare_data_requirements"}
REQUIRED_EXECUTOR_ARTIFACTS = {"summary.json", "report.yaml", "l4_ack.yaml", "diagnostic.yaml", "adoption_pass"}
FORBIDDEN_EXECUTOR_MARKERS = {"todo", "placeholder", "pseudocode", "demo-only"}
# GateKeeper compliance: every grid-style executor must import GateKeeper
# from scripts.gatekeeper. This is enforced by validate_gatekeeper_compliance.py
# at preflight; the handoff path must also enforce it so Hermes does not
# generate code that passes handoff-time checks but fails preflight later.
REQUIRED_GATEKEEPER_IMPORT = "from scripts.gatekeeper import GateKeeper"
COMPLETION_RECEIPT_NAME = "executor_completion.yaml"
REQUIRED_RECEIPT_CHECKS = {
    "compile_passed",
    "has_main",
    "has_declare_data_requirements",
    "writes_summary_json",
    "summary_has_adoption_pass",
    "writes_report_yaml",
    "writes_l4_ack_yaml",
    "writes_diagnostic_yaml",
    "no_forbidden_markers",
    "imports_gatekeeper",
}
ACTIVE_QUEUE_ITEM_STATUSES = {"queued", "running", "artifacts_synced", "review_pending"}
ACTIVE_RUNNER_STATUSES = {
    "queued_ideation_spec",
    "queued_vm_retry",
    "running_remote",
    "syncing_to_vm",
    "checking_remote_data_quality",
    "checking_data_quality",
    "waiting_remote_running",
    "waiting_review_memory",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _load_yaml(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return dict(default or {})
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} root must be a mapping")
    return loaded


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _load_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return dict(default or {})
    loaded = json.loads(path.read_text(encoding="utf-8") or "{}")
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} root must be a mapping")
    return loaded


def _handoffs_path(repo_root: Path) -> Path:
    return repo_root / HANDOFFS_PATH


def _queue_path(repo_root: Path) -> Path:
    return repo_root / QUEUE_PATH


def _status_path(repo_root: Path) -> Path:
    return repo_root / STATUS_PATH


def quant_workflow_active(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    state = _load_yaml(_queue_path(repo_root), default={})
    queue = state.get("queue") if isinstance(state, dict) else []
    if isinstance(queue, list):
        for item in queue:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "")
            if status in ACTIVE_QUEUE_ITEM_STATUSES:
                return {
                    "active": True,
                    "source": "research_queue",
                    "item_id": item.get("id"),
                    "item_status": status,
                }
    status_doc = _load_json(_status_path(repo_root), default={})
    runner_status = str(status_doc.get("status") or "")
    if runner_status in ACTIVE_RUNNER_STATUSES:
        return {
            "active": True,
            "source": "research_queue_status",
            "runner_status": runner_status,
            "item_id": status_doc.get("item_id"),
        }
    return {"active": False}


def _tasks(doc: dict[str, Any]) -> list[dict[str, Any]]:
    raw = doc.get("tasks")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _is_stale_claim(task: dict[str, Any], *, now_dt: datetime | None = None) -> bool:
    if str(task.get("status") or "") != "claimed":
        return False
    claimed_at = _parse_dt(task.get("claimed_at"))
    if not claimed_at:
        return True
    now_dt = now_dt or datetime.now()
    return now_dt - claimed_at >= timedelta(minutes=STALE_CLAIM_MINUTES)


def open_tasks(repo_root: Path = REPO_ROOT) -> list[dict[str, Any]]:
    """Return tasks that hermes_executor_handoff should process this tick.

    A task is pickable if either:
      * its status is in HANDOFF_PICKABLE_STATUSES (e.g. "open", or a
        repair status flipped by install_generated_executors), OR
      * it is a stale claim (a previous handoff was claimed but never
        finished within STALE_CLAIM_MINUTES).

    Terminal statuses (completed / installed / failed / etc.) are NOT
    pickable. Repair flow expects install to flip the task into one of
    the explicit repair statuses before this picker sees it.
    """
    doc = _load_yaml(_handoffs_path(repo_root), default={"tasks": []})
    now_dt = datetime.now()
    return [
        task
        for task in _tasks(doc)
        if str(task.get("status") or "") in HANDOFF_PICKABLE_STATUSES
        or _is_stale_claim(task, now_dt=now_dt)
    ]


def _boundary_for_task(task: dict[str, Any]) -> dict[str, Any]:
    descriptor_path = str(task.get("descriptor_path") or "")
    run_dir = str(task.get("run_dir") or "")
    target_script = str(task.get("target_script_path") or "")
    allowed_writes = [descriptor_path]
    if run_dir:
        allowed_writes.append(f"{run_dir}/generated_executor/")
    return {
        "actor": "hermes_executor_code_worker",
        "allowed_reads": [
            str(HANDOFFS_PATH),
            descriptor_path,
            run_dir,
        ],
        "allowed_writes": allowed_writes,
        "target_script_path": target_script,
        "required_completion_receipt": f"{run_dir}/generated_executor/{COMPLETION_RECEIPT_NAME}" if run_dir else COMPLETION_RECEIPT_NAME,
        "forbidden_actions": FORBIDDEN_ACTIONS,
        "completion_is_not_execution": (
            "After code and completion receipt are written, stop. The quant internal cron will validate and run the next step."
        ),
        "required_imports": [REQUIRED_GATEKEEPER_IMPORT],
        "required_imports_reason": (
            "framework_preflight.validate_gatekeeper_compliance requires every "
            "grid-style evaluator to import GateKeeper from scripts.gatekeeper "
            "and call its lifecycle methods (before_run_grid / after_run_grid / "
            "etc). Look at scripts/evaluate_cb_arb_exit_gap_closing_percentile.py "
            "or scripts/evaluate_cb_arb_iv_percentile_exit.py for the expected "
            "shape. Missing this import will block install — the script will be "
            "rejected as generated_noncompliant and you will be asked to repair "
            "it. Do not assume the executor will be added to the allowlist; that "
            "is a manual exception and not the default install path."
        ),
        "required_receipt_checks": sorted(REQUIRED_RECEIPT_CHECKS),
        **(
            {
                "regeneration_context": {
                    "reason": (
                        "This task's generated_executor source file was lost "
                        "after a previous successful install. The target in "
                        "scripts/ may not be the latest version. Regenerate "
                        "the executor from scratch based on the spec.yaml + "
                        "executor_tool_request.yaml in this run dir. The "
                        "previous install fingerprint is recorded on the task "
                        "as previous_installed_sha256 / previous_installed_source "
                        "/ previous_installed_at for audit. You do NOT need to "
                        "match the previous source byte-for-byte; you need to "
                        "produce an executor that satisfies the spec and the "
                        "current GateKeeper / receipt-check requirements."
                    ),
                    "request_marker": str(task.get("regeneration_request") or ""),
                    "source_lost_detected_at": task.get("source_lost_detected_at"),
                    "previous_installed_at": task.get("previous_installed_at"),
                    "previous_installed_sha256": task.get("previous_installed_sha256"),
                },
            }
            if str(task.get("status") or "") == "needs_executor_regeneration"
            else {}
        ),
    }


def wake_once(repo_root: Path = REPO_ROOT, *, limit: int = 1) -> dict[str, Any]:
    """Prepare at most one open handoff for Hermes cron wake-gating."""
    active = quant_workflow_active(repo_root)
    if active.get("active") is True:
        return {
            "wakeAgent": False,
            "reason": "quant_workflow_active",
            "active": active,
        }
    path = _handoffs_path(repo_root)
    doc = _load_yaml(path, default={"schema_version": 1, "tasks": []})
    tasks = _tasks(doc)
    selected: list[dict[str, Any]] = []
    now = _now()
    now_dt = datetime.now()
    changed = False
    for task in tasks:
        status = str(task.get("status") or "")
        if status == "claimed" and _is_stale_claim(task, now_dt=now_dt):
            task["status"] = "open"
            task["stale_claim_reopened_at"] = now
            task["retry_count"] = int(task.get("retry_count") or 0) + 1
            task.setdefault("previous_errors", [])
            if isinstance(task["previous_errors"], list):
                task["previous_errors"].append(
                    "Hermes claimed the handoff but did not complete it before the stale-claim timeout."
                )
            status = "open"
        if status not in HANDOFF_PICKABLE_STATUSES:
            continue
        task["last_wakeup_at"] = now
        task["wakeup_count"] = int(task.get("wakeup_count") or 0) + 1
        task["boundary"] = _boundary_for_task(task)
        selected.append(task)
        changed = True
        if len(selected) >= limit:
            break
    if changed:
        doc["updated_at"] = now
        doc["tasks"] = tasks
        _write_yaml(path, doc)
    if not selected:
        return {"wakeAgent": False, "reason": "no_open_hermes_executor_handoff"}
    return {
        "wakeAgent": True,
        "reason": "open_hermes_executor_handoff",
        "handoffs_path": str(path),
        "tasks": selected,
    }


def claim_task(task_id: str, repo_root: Path = REPO_ROOT, *, actor: str = "hermes") -> dict[str, Any]:
    path = _handoffs_path(repo_root)
    doc = _load_yaml(path, default={"schema_version": 1, "tasks": []})
    tasks = _tasks(doc)
    now = _now()
    for task in tasks:
        if str(task.get("id") or "") != task_id:
            continue
        status = str(task.get("status") or "")
        claimable = HANDOFF_PICKABLE_STATUSES | {"claimed"}
        if status not in claimable:
            return {"status": "not_claimable", "task_id": task_id, "current_status": status}
        if status in HANDOFF_PICKABLE_STATUSES:
            task["status"] = "claimed"
            task["claimed_at"] = now
            task["claimed_by"] = actor
            task["boundary"] = _boundary_for_task(task)
            doc["updated_at"] = now
            doc["tasks"] = tasks
            _write_yaml(path, doc)
        return {"status": "claimed", "task_id": task_id, "claimed_by": task.get("claimed_by")}
    return {"status": "missing", "task_id": task_id}


def complete_task(task_id: str, repo_root: Path = REPO_ROOT, *, actor: str = "hermes") -> dict[str, Any]:
    finalized = finalize_task_if_valid(task_id, repo_root, actor=f"{actor}_complete")
    if finalized.get("status") == "completed":
        return {"status": "completed", "task_id": task_id, "completed_by": actor, "finalized": finalized}
    path = _handoffs_path(repo_root)
    doc = _load_yaml(path, default={"schema_version": 1, "tasks": []})
    tasks = _tasks(doc)
    now = _now()
    for task in tasks:
        if str(task.get("id") or "") != task_id:
            continue
        descriptor = Path(str(task.get("descriptor_path") or ""))
        if not descriptor.is_absolute():
            descriptor = repo_root / descriptor
        package = _load_yaml(descriptor, default={})
        tool_code_response = package.get("tool_code_response") if isinstance(package.get("tool_code_response"), dict) else {}
        if package.get("status") != "draft_tool_code" or not tool_code_response.get("completion_receipt"):
            return {
                "status": "descriptor_not_ready",
                "task_id": task_id,
                "descriptor_status": package.get("status"),
                "completion_receipt": tool_code_response.get("completion_receipt"),
                "finalize_status": finalized.get("status"),
                "finalize_errors": finalized.get("errors"),
            }
        task["status"] = "completed"
        task["completed_at"] = now
        task["completed_by"] = actor
        doc["updated_at"] = now
        doc["tasks"] = tasks
        _write_yaml(path, doc)
        return {"status": "completed", "task_id": task_id, "completed_by": actor}
    return {"status": "missing", "task_id": task_id}


def cancel_task(
    task_id: str,
    repo_root: Path = REPO_ROOT,
    *,
    actor: str = "codex",
    reason: str = "cancelled",
) -> dict[str, Any]:
    path = _handoffs_path(repo_root)
    doc = _load_yaml(path, default={"schema_version": 1, "tasks": []})
    tasks = _tasks(doc)
    now = _now()
    for task in tasks:
        if str(task.get("id") or "") != task_id:
            continue
        status = str(task.get("status") or "")
        if status in {"completed", "cancelled"}:
            return {"status": status, "task_id": task_id, "reason": task.get("cancel_reason")}
        task["status"] = "cancelled"
        task["cancelled_at"] = now
        task["cancelled_by"] = actor
        task["cancel_reason"] = reason
        doc["updated_at"] = now
        doc["tasks"] = tasks
        _write_yaml(path, doc)
        return {"status": "cancelled", "task_id": task_id, "reason": reason}
    return {"status": "missing", "task_id": task_id}


def _generated_executor_path(task: dict[str, Any], repo_root: Path) -> Path | None:
    run_dir = Path(str(task.get("run_dir") or ""))
    if not run_dir.is_absolute():
        run_dir = repo_root / run_dir
    target_name = Path(str(task.get("target_script_path") or "")).name
    generated_dir = run_dir / "generated_executor"
    if target_name:
        preferred = generated_dir / target_name
        if preferred.exists():
            return preferred
    candidates = sorted(path for path in generated_dir.glob("*.py") if path.is_file())
    if len(candidates) == 1:
        return candidates[0]
    return None


def _completion_receipt_path(script_path: Path) -> Path:
    return script_path.parent / COMPLETION_RECEIPT_NAME


def _validate_completion_receipt(task: dict[str, Any], script_path: Path, repo_root: Path) -> list[str]:
    receipt_path = _completion_receipt_path(script_path)
    if not receipt_path.exists():
        return [f"missing completion receipt: {receipt_path}"]
    try:
        receipt = _load_yaml(receipt_path)
    except Exception as exc:
        return [f"completion receipt invalid yaml: {type(exc).__name__}: {exc}"]
    errors: list[str] = []
    if receipt.get("schema_version") != 1:
        errors.append(f"completion receipt schema_version must be 1, got {receipt.get('schema_version')}")
    expected_handoff_id = str(task.get("id") or "")
    if str(receipt.get("handoff_id") or "") != expected_handoff_id:
        errors.append("completion receipt handoff_id does not match task id")
    run_dir = Path(str(task.get("run_dir") or repo_root))
    if not run_dir.is_absolute():
        run_dir = repo_root / run_dir
    expected_script = _repo_relative(script_path, run_dir)
    if expected_script.startswith(".."):
        expected_script = _repo_relative(script_path, repo_root)
    if str(receipt.get("generated_executor") or "") != expected_script:
        errors.append("completion receipt generated_executor does not match generated script")
    checks = receipt.get("checks")
    if not isinstance(checks, dict):
        errors.append("completion receipt checks must be a mapping")
        return errors
    missing = sorted(REQUIRED_RECEIPT_CHECKS - set(checks))
    if missing:
        errors.append(f"completion receipt missing checks: {missing}")
    for key in sorted(REQUIRED_RECEIPT_CHECKS & set(checks)):
        if checks.get(key) is not True:
            errors.append(f"completion receipt check {key} must be true")
    return errors


def _validate_generated_executor(script_path: Path) -> list[str]:
    errors: list[str] = []
    if not script_path.exists():
        return [f"missing generated executor: {script_path}"]
    source = script_path.read_text(encoding="utf-8")
    lowered = source.lower()
    for marker in FORBIDDEN_EXECUTOR_MARKERS:
        if marker in lowered:
            errors.append(f"forbidden marker present: {marker}")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"syntax error: {exc}"]
    function_names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    missing_functions = sorted(REQUIRED_EXECUTOR_FUNCTIONS - function_names)
    if missing_functions:
        errors.append(f"missing required functions: {', '.join(missing_functions)}")
    missing_artifacts = sorted(item for item in REQUIRED_EXECUTOR_ARTIFACTS if item not in source)
    if missing_artifacts:
        errors.append(f"missing required artifact markers: {', '.join(missing_artifacts)}")
    # GateKeeper compliance: framework_preflight requires every grid-style
    # executor to either import scripts.gatekeeper.GateKeeper or be listed in
    # gatekeeper_allowlist.yaml. Handoff-time validation enforces the import
    # so we never install code that the global preflight will reject.
    if REQUIRED_GATEKEEPER_IMPORT not in source:
        errors.append(
            "missing GateKeeper import "
            f"(handoff requires '{REQUIRED_GATEKEEPER_IMPORT}' — see "
            "scripts/gatekeeper.py and other evaluate_cb_arb_* scripts for the "
            "expected before_run_grid / after_run_grid pattern)"
        )
    try:
        py_compile.compile(str(script_path), doraise=True)
    except py_compile.PyCompileError as exc:
        errors.append(f"compile failed: {exc.msg}")
    return errors


def _repo_relative(path: Path, repo_root: Path) -> str:
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def finalize_task_if_valid(
    task_id: str,
    repo_root: Path = REPO_ROOT,
    *,
    actor: str = "hermes_wakeup",
) -> dict[str, Any]:
    path = _handoffs_path(repo_root)
    doc = _load_yaml(path, default={"schema_version": 1, "tasks": []})
    tasks = _tasks(doc)
    now = _now()
    for task in tasks:
        if str(task.get("id") or "") != task_id:
            continue
        if str(task.get("status") or "") not in {"open", "claimed", "completed"}:
            return {"status": "not_finalizable", "task_id": task_id, "current_status": task.get("status")}
        script_path = _generated_executor_path(task, repo_root)
        if script_path is None:
            task["status"] = "open"
            task["stale_claim_reopened_at"] = now
            task["retry_count"] = int(task.get("retry_count") or 0) + 1
            task.setdefault("previous_errors", [])
            if isinstance(task["previous_errors"], list):
                task["previous_errors"].append("Hermes did not write a generated executor file.")
            doc["updated_at"] = now
            _write_yaml(path, doc)
            return {"status": "missing_generated_executor", "task_id": task_id}
        validation_errors = _validate_generated_executor(script_path)
        receipt_errors = _validate_completion_receipt(task, script_path, repo_root)
        validation_errors.extend(receipt_errors)
        if validation_errors:
            task["status"] = "open"
            task["stale_claim_reopened_at"] = now
            task["retry_count"] = int(task.get("retry_count") or 0) + 1
            task["last_validation_errors"] = validation_errors
            task.setdefault("previous_errors", [])
            if isinstance(task["previous_errors"], list):
                task["previous_errors"].append("Generated executor failed deterministic validation.")
            doc["updated_at"] = now
            _write_yaml(path, doc)
            return {"status": "validation_failed", "task_id": task_id, "errors": validation_errors}

        descriptor = Path(str(task.get("descriptor_path") or ""))
        if not descriptor.is_absolute():
            descriptor = repo_root / descriptor
        package = _load_yaml(descriptor, default={})
        rel_script = _repo_relative(script_path, Path(str(task.get("run_dir") or repo_root)))
        if rel_script.startswith(".."):
            rel_script = _repo_relative(script_path, repo_root)
        code_hash = hashlib.sha256(script_path.read_bytes()).hexdigest()[:16]
        package["status"] = "draft_tool_code"
        package["code_response_hash"] = code_hash
        package["validation_errors"] = []
        package["files"] = [
            {
                "path": rel_script,
                "purpose": "Complete evaluator implementation.",
                "content": script_path.read_text(encoding="utf-8"),
            }
        ]
        package["written_files"] = [rel_script]
        package["tool_code_response"] = {
            "status": "draft_tool_code",
            "handoff_id": task_id,
            "generated_executor": rel_script,
            "completion_receipt": _repo_relative(_completion_receipt_path(script_path), repo_root),
            "finalized_by": actor,
            "finalized_at": now,
            "validation": "passed",
        }
        _write_yaml(descriptor, package)

        task["status"] = "completed"
        task["completed_at"] = now
        task["completed_by"] = actor
        task["generated_executor"] = _repo_relative(script_path, repo_root)
        doc["updated_at"] = now
        doc["tasks"] = tasks
        _write_yaml(path, doc)
        return {
            "status": "completed",
            "task_id": task_id,
            "completed_by": actor,
            "generated_executor": _repo_relative(script_path, repo_root),
        }
    return {"status": "missing", "task_id": task_id}


def finalize_claimed_tasks(repo_root: Path = REPO_ROOT, *, actor: str = "hermes_wakeup") -> dict[str, Any]:
    doc = _load_yaml(_handoffs_path(repo_root), default={"schema_version": 1, "tasks": []})
    results = []
    for task in _tasks(doc):
        if str(task.get("status") or "") == "claimed":
            results.append(finalize_task_if_valid(str(task.get("id") or ""), repo_root, actor=actor))
    return {"status": "checked", "results": results}


def format_wake_output(payload: dict[str, Any]) -> str:
    if payload.get("wakeAgent") is False:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)
    body = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).strip()
    gate = json.dumps({"wakeAgent": True, "handoff_count": len(payload.get("tasks") or [])}, ensure_ascii=False)
    return "\n".join(["HERMES_QUANT_EXECUTOR_HANDOFF", body, gate])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument("--claim")
    parser.add_argument("--complete")
    parser.add_argument("--cancel")
    parser.add_argument("--reason", default="cancelled")
    parser.add_argument("--finalize")
    parser.add_argument("--finalize-claimed", action="store_true")
    parser.add_argument("--actor", default="hermes")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    if args.claim:
        print(json.dumps(claim_task(args.claim, repo_root, actor=args.actor), ensure_ascii=False, sort_keys=True))
        return 0
    if args.complete:
        print(json.dumps(complete_task(args.complete, repo_root, actor=args.actor), ensure_ascii=False, sort_keys=True))
        return 0
    if args.cancel:
        print(
            json.dumps(
                cancel_task(args.cancel, repo_root, actor=args.actor, reason=args.reason),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.finalize:
        print(
            json.dumps(
                finalize_task_if_valid(args.finalize, repo_root, actor=args.actor),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.finalize_claimed:
        print(json.dumps(finalize_claimed_tasks(repo_root, actor=args.actor), ensure_ascii=False, sort_keys=True))
        return 0
    print(format_wake_output(wake_once(repo_root)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
