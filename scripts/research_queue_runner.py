#!/usr/bin/env python3
"""Research queue tick runner.

This runner only schedules and monitors. It never runs cb_arb backtests locally.
Queued tasks must provide a READY spec.yaml. The runner validates the spec,
syncs declared files to a VM, and starts auto_research_pipeline.py remotely.
Later ticks settle running tasks from remote process and result-file evidence.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import posixpath
import shlex
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml required", file=sys.stderr)
    sys.exit(2)

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
for path in (REPO_ROOT, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from framework.autonomous.workflow_state import count_status, decide_scheduler_action
from quant_access_guard import INTERNAL_CRON_ISSUER, issue_ticket, require_ticket


STATE_PATH = REPO_ROOT / "data" / "research_framework" / "research_queue.yaml"
LOG_PATH = REPO_ROOT / "logs" / "research_queue_runner.log"
STATUS_PATH = REPO_ROOT / "logs" / "research_queue_status.json"
ONCE_LOCK_PATH = REPO_ROOT / "logs" / "research_queue_runner_once.lock"
AUDIT_LOG_PATH = REPO_ROOT / "data" / "research_framework" / "orchestrator_log.jsonl"
PAUSE_FLAG_PATH = REPO_ROOT / "data" / "research_framework" / "orchestrator_paused.flag"
EXCLUDE_STATUSES = {"DRAFT", "REJECT", "PROPOSAL_ONLY"}


class DataQualityBlocked(RuntimeError):
    pass


class DataQualityRepairCandidate(RuntimeError):
    def __init__(self, decision_text: str, summary_json: str):
        super().__init__(decision_text[-4000:])
        self.decision_text = decision_text
        self.summary_json = summary_json


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def log(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(f"[{now_iso()}] {message}\n")


def audit(action: str, payload: dict[str, Any] | None = None) -> None:
    AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {"action": action, "payload": payload or {}, "ts": now_iso()}
    with AUDIT_LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def is_paused() -> bool:
    return PAUSE_FLAG_PATH.exists()


def _previous_status_payload() -> dict[str, Any] | None:
    if not STATUS_PATH.exists():
        return None
    try:
        data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def write_status(status: str, extra: dict[str, Any] | None = None) -> None:
    previous_status = _previous_status_payload()
    status_changed = previous_status is None or previous_status.get("status") != status
    payload: dict[str, Any] = {
        "status": status,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "updated_at": now_iso(),
        "state_path": rel(STATE_PATH),
        "status_changed": status_changed,
    }
    if extra:
        payload.update(extra)
    if not status_changed:
        payload["previous_status"] = previous_status.get("status") if previous_status else status
        payload["status_unchanged"] = True
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATUS_PATH)


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        raise FileNotFoundError(f"missing state file: {STATE_PATH}")
    data = yaml.safe_load(STATE_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("research_queue.yaml root must be dict")
    return data


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    tmp = STATE_PATH.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(state, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp.replace(STATE_PATH)


def run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        check=False,
    )
    if check and result.returncode != 0:
        output = result.stdout or ""
        raise RuntimeError(f"command failed rc={result.returncode}: {' '.join(map(shlex.quote, cmd))}\n{output}")
    return result


def ssh(vm_host: str, command: str, *, check: bool = True) -> str:
    result = run(
        [
            "ssh",
            "-n",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            vm_host,
            command,
        ],
        check=check,
    )
    return result.stdout or ""


def ssh_vm(vm: dict[str, Any], command: str, *, check: bool = True) -> str:
    proxy_host = str(vm.get("proxy_host") or "").strip()
    if not proxy_host:
        return ssh(str(vm["host"]), command, check=check)
    identity = str(vm.get("identity_file_on_proxy") or "").strip()
    if not identity:
        raise ValueError(f"proxy vm {vm.get('id')} missing identity_file_on_proxy")
    nested = (
        "ssh -n -i "
        f"{shlex.quote(identity)} "
        "-o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new "
        "-o ConnectTimeout=8 "
        f"{shlex.quote(str(vm['host']))} {shlex.quote(command)}"
    )
    return ssh(proxy_host, nested, check=check)


def proxy_ssh_cmd(vm: dict[str, Any]) -> str:
    identity = str(vm.get("identity_file_on_proxy") or "").strip()
    if not identity:
        raise ValueError(f"proxy vm {vm.get('id')} missing identity_file_on_proxy")
    return (
        "ssh -i "
        f"{shlex.quote(identity)} "
        "-o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new "
        "-o ConnectTimeout=8"
    )


def proxy_stage_repo(state: dict[str, Any], vm: dict[str, Any]) -> str:
    return str(vm.get("proxy_repo") or state.get("remote_repo") or "/root/projects/quant")


def vm_configs(state: dict[str, Any]) -> list[dict[str, Any]]:
    raw = state.get("vm_hosts")
    configs: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for idx, item in enumerate(raw):
            if not isinstance(item, dict) or item.get("enabled") is False:
                continue
            host = str(item.get("host") or item.get("vm_host") or "").strip()
            if not host:
                continue
            config: dict[str, Any] = {
                "id": str(item.get("id") or item.get("name") or host),
                "host": host,
                "remote_repo": str(item.get("remote_repo") or state.get("remote_repo") or "/root/projects/quant"),
            }
            for key in ("proxy_host", "proxy_repo", "identity_file_on_proxy"):
                if item.get(key):
                    config[key] = str(item[key])
            configs.append(config)
    if configs:
        return configs
    return [
        {
            "id": str(state.get("vm_id") or state.get("vm_host") or "default"),
            "host": str(state["vm_host"]),
            "remote_repo": str(state["remote_repo"]),
        }
    ]


def item_vm_config(state: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    known = vm_configs(state)
    item_id = str(item.get("vm_id") or "")
    host = str(item.get("vm_host") or "")
    for vm in known:
        if (item_id and str(vm.get("id")) == item_id) or (host and str(vm.get("host")) == host):
            merged = dict(vm)
            if item.get("remote_repo"):
                merged["remote_repo"] = str(item["remote_repo"])
            return merged
    if host:
        remote_repo = str(item.get("remote_repo") or state.get("remote_repo") or "/root/projects/quant")
        return {"id": str(item.get("vm_id") or host), "host": host, "remote_repo": remote_repo}
    return known[0]


def validate_spec(spec_path: Path) -> dict[str, Any]:
    run(["python3", "scripts/validate_spec.py", rel(spec_path)])
    run(["python3", "scripts/research_sanity_checker.py", "--spec", rel(spec_path)])
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    if not isinstance(spec, dict):
        raise ValueError(f"{rel(spec_path)} root must be dict")
    compute = spec.get("compute_estimate") or {}
    if not isinstance(compute, dict):
        raise ValueError("spec.compute_estimate must be dict")
    spot_minutes = float(compute.get("spot_minutes", 0) or 0)
    local_minutes = float(compute.get("local_minutes", 0) or 0)
    if spot_minutes <= 0 or local_minutes > 0:
        raise ValueError("option loop requires spot_minutes > 0 and local_minutes = 0")
    if str(spec.get("status")) != "READY":
        raise ValueError(f"spec.status must be READY, got {spec.get('status')!r}")
    return spec


def spec_family(spec: dict[str, Any]) -> str:
    ideation = spec.get("ideation") or {}
    if isinstance(ideation, dict) and ideation.get("family"):
        return str(ideation["family"])
    return str(spec.get("family") or "")


def _ideation_env() -> dict[str, str]:
    os.environ["QUANT_AUTOMATION_ISSUER"] = INTERNAL_CRON_ISSUER
    ticket = issue_ticket("strategy_ideation_once")
    env = dict(**os.environ)
    env["QUANT_AUTOMATION_ACTOR"] = "quant_internal_cron"
    env["QUANT_AUTOMATION_TICKET_PATH"] = ticket["path"]
    env["QUANT_AUTOMATION_TICKET_TOKEN"] = ticket["token"]
    return env


def _enqueue_ready_spec(state: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    spec_raw = payload.get("spec_path")
    if not spec_raw:
        raise ValueError("ideation returned READY without spec_path")
    spec_path = Path(str(spec_raw))
    if not spec_path.is_absolute():
        spec_path = REPO_ROOT / spec_path
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
    if not isinstance(spec, dict):
        raise ValueError(f"{rel(spec_path)} root must be dict")
    run_id = str(spec.get("run_id") or spec_path.parent.name)
    queue = state.get("queue")
    if not isinstance(queue, list):
        raise ValueError("research_queue.queue must be list")
    for item in queue:
        if isinstance(item, dict) and (item.get("id") == run_id or item.get("spec_path") == rel(spec_path)):
            return {"item_id": run_id, "spec_path": rel(spec_path), "already_present": True}
    automation = spec.get("automation") if isinstance(spec.get("automation"), dict) else {}
    item = {
        "id": run_id,
        "family": str(spec.get("family") or (spec.get("proposal") or {}).get("family") or "auto_deepseek_ideation"),
        "status": "queued",
        "spec_path": rel(spec_path),
        "process_pattern": run_id,
        "sync_paths": automation.get("sync_paths") if isinstance(automation.get("sync_paths"), list) else [],
        "discovered_at": now_iso(),
        "source": "project_owned_ideation",
    }
    queue.append(item)
    state["queue"] = queue
    escalation = state.get("escalation")
    if isinstance(escalation, dict) and escalation.get("status") == "blocked_awaiting_user":
        escalation["status"] = "auto_resolved"
        escalation["resolved_at"] = now_iso()
        escalation["resolution_reason"] = "old directions exhausted; DeepSeek provider available for new project-owned ideation"
    save_state(state)
    mark_history(state, item, "queued", "queued READY spec generated by project-owned ideation")
    save_state(state)
    return {"item_id": run_id, "spec_path": rel(spec_path), "already_present": False}


def generate_next_spec_if_idle(state: dict[str, Any]) -> str:
    write_status(
        "generating_research_direction",
        {
            "note": "No queued READY specs. Project-owned ideation is generating one candidate through the registered provider.",
        },
    )
    try:
        result = subprocess.run(
            [sys.executable, "scripts/run_strategy_ideation_once.py"],
            cwd=REPO_ROOT,
            env=_ideation_env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=360,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        write_status(
            "ideation_timeout",
            {
                "timeout_seconds": 360,
                "output": output[-4000:],
                "note": "Provider or rewrite loop timed out; next tick may try again.",
            },
        )
        audit("ideation_timeout", {"timeout_seconds": 360, "output": output[-4000:]})
        log("ideation timed out after 360 seconds")
        return "ideation_timeout"
    output = result.stdout or ""
    if result.returncode != 0:
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            payload = {}
        payload_status = str(payload.get("status") or "")
        if payload_status in EXCLUDE_STATUSES:
            audit(
                "ideate",
                {
                    "status": payload_status,
                    "proposal_id": payload.get("proposal_id"),
                    "spec_path": payload.get("spec_path"),
                    "reason": payload.get("reason"),
                },
            )
            write_status(
                "ideation_not_runnable",
                {
                    "proposal_status": payload_status,
                    "proposal_id": payload.get("proposal_id"),
                    "spec_path": payload.get("spec_path"),
                    "implementation_plan_path": payload.get("implementation_plan_path"),
                    "reason": payload.get("reason"),
                    "errors": payload.get("errors"),
                },
            )
            return "ideation_not_runnable"
        if "executor_tool_code" in output or "returned empty content" in output:
            write_status(
                "executor_tool_response_invalid",
                {
                    "returncode": result.returncode,
                    "reason": "tool-code AI response was empty or invalid",
                    "output": output[-4000:],
                },
            )
            audit("executor_tool_response_invalid", {"returncode": result.returncode, "output": output[-4000:]})
            return "executor_tool_response_invalid"
        write_status("ideation_failed", {"returncode": result.returncode, "output": output[-4000:]})
        audit("ideation_failed", {"returncode": result.returncode, "output": output[-4000:]})
        log(f"ideation failed rc={result.returncode}: {output[-1000:]}")
        return "ideation_failed"
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        write_status("ideation_failed", {"error": f"JSONDecodeError: {exc}", "output": output[-4000:]})
        audit("ideation_failed", {"error": f"JSONDecodeError: {exc}"})
        return "ideation_failed"
    status = str(payload.get("status") or "")
    audit("ideate", {"status": status, "proposal_id": payload.get("proposal_id"), "spec_path": payload.get("spec_path")})
    if status == "READY":
        item = _enqueue_ready_spec(state, payload)
        write_status("queued_ideation_spec", item)
        return "queued_ideation_spec"
    write_status(
        "ideation_not_runnable",
        {
            "proposal_status": status,
            "proposal_id": payload.get("proposal_id"),
            "spec_path": payload.get("spec_path"),
            "implementation_plan_path": payload.get("implementation_plan_path"),
            "reason": payload.get("reason"),
            "errors": payload.get("errors"),
        },
    )
    return "ideation_not_runnable"


def escalation_block(state: dict[str, Any]) -> dict[str, Any] | None:
    escalation = state.get("escalation") if isinstance(state, dict) else None
    if not isinstance(escalation, dict):
        return None
    status = str(escalation.get("status") or "")
    if status != "blocked_awaiting_user":
        return None
    if escalation.get("requires_user_decision") is not True and escalation.get("protected_action_required") is not True:
        return None
    return {
        "reason": str(escalation.get("reason") or "awaiting user decision"),
        "since": escalation.get("since"),
        "last_escalation": escalation.get("last_escalation"),
        "escalation_count": escalation.get("escalation_count"),
        "user_options": escalation.get("user_options"),
    }


def stop_for_user_block(state: dict[str, Any]) -> str:
    payload = escalation_block(state) or {"reason": "awaiting user decision"}
    write_status(
        "blocked_awaiting_user",
        {
            "note": "Research queue is blocked by explicit escalation; no new direction may be generated.",
            **payload,
        },
    )
    audit("blocked_awaiting_user", payload)
    log(f"blocked awaiting user: {payload.get('reason')}")
    return "blocked_awaiting_user"


def remote_running_on_vm(vm: dict[str, Any], process_pattern: str | None = None) -> bool:
    pattern = "auto_research_pipeline.py|evaluate_cb_arb_.*option|option-value|option_value"
    if process_pattern:
        pattern = process_pattern
    out = ssh_vm(vm, f"pgrep -af {shlex.quote(pattern)} | grep -v pgrep || true", check=False)
    return bool(out.strip())


def vm_available(vm: dict[str, Any]) -> bool:
    try:
        ssh_vm(vm, "echo ok >/dev/null", check=True)
    except Exception as exc:
        log(f"vm unavailable {vm.get('id')}: {type(exc).__name__}: {exc}")
        return False
    return not remote_running_on_vm(vm)


def is_transient_vm_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}"
    markers = (
        "rsync error",
        "Connection reset",
        "kex_exchange_identification",
        "Connection timed out",
        "No route to host",
        "Connection refused",
        "Permission denied (publickey)",
        "rc=255",
    )
    return any(marker in text for marker in markers)


def choose_vm_for_item(available_vms: list[dict[str, Any]], item: dict[str, Any]) -> dict[str, Any] | None:
    avoid = set(str(value) for value in item.get("avoid_vm_ids", []) or [])
    for index, vm in enumerate(available_vms):
        if str(vm.get("id")) not in avoid:
            return available_vms.pop(index)
    return None


def sync_one_path(state: dict[str, Any], path: Path, rel_path: str, vm: dict[str, Any]) -> None:
    vm_host = str(vm["host"])
    remote_repo = str(vm["remote_repo"])
    proxy_host = str(vm.get("proxy_host") or "").strip()

    if not proxy_host:
        dest = f"{vm_host}:{remote_repo}/{rel_path}"
        if path.is_dir():
            run(["rsync", "-av", "--delete", f"{rel_path}/", f"{dest}/"])
        else:
            ssh_vm(vm, f"mkdir -p {shlex.quote(posixpath.dirname(posixpath.join(remote_repo, rel_path)))}")
            run(["rsync", "-av", rel_path, dest])
        return

    proxy_repo = proxy_stage_repo(state, vm)
    proxy_dest = f"{proxy_host}:{proxy_repo}/{rel_path}"
    ssh(proxy_host, f"mkdir -p {shlex.quote(posixpath.dirname(posixpath.join(proxy_repo, rel_path)))}")
    if path.is_dir():
        run(["rsync", "-av", "--delete", f"{rel_path}/", f"{proxy_dest}/"])
    else:
        run(["rsync", "-av", rel_path, proxy_dest])

    ssh_cmd = proxy_ssh_cmd(vm)
    remote_abs = posixpath.join(remote_repo, rel_path)
    proxy_abs = posixpath.join(proxy_repo, rel_path)
    if path.is_dir():
        ssh(
            proxy_host,
            " && ".join(
                [
                    f"mkdir -p {shlex.quote(remote_abs)}",
                    (
                        "rsync -av --delete "
                        f"-e {shlex.quote(ssh_cmd)} "
                        f"{shlex.quote(proxy_abs + '/')} "
                        f"{shlex.quote(vm_host + ':' + remote_abs + '/')}"
                    ),
                ]
            ),
        )
    else:
        ssh(
            proxy_host,
            " && ".join(
                [
                    f"ssh -i {shlex.quote(str(vm['identity_file_on_proxy']))} "
                    "-n -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new "
                    "-o ConnectTimeout=8 "
                    f"{shlex.quote(vm_host)} mkdir -p {shlex.quote(posixpath.dirname(remote_abs))}",
                    (
                        "rsync -av "
                        f"-e {shlex.quote(ssh_cmd)} "
                        f"{shlex.quote(proxy_abs)} "
                        f"{shlex.quote(vm_host + ':' + remote_abs)}"
                    ),
                ]
            ),
        )


def sync_paths(state: dict[str, Any], item: dict[str, Any], spec_path: Path, vm: dict[str, Any]) -> None:
    paths = list(state.get("default_sync_paths") or [])
    paths.extend(item.get("sync_paths") or [])
    paths.append(rel(spec_path))
    run_dir = spec_path.parent
    paths.append(rel(run_dir))
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
    if isinstance(spec, dict):
        automation = spec.get("automation") if isinstance(spec.get("automation"), dict) else {}
        automation_sync_paths = automation.get("sync_paths") if isinstance(automation.get("sync_paths"), list) else []
        paths.extend(automation_sync_paths)
    seen: set[str] = set()
    for path_raw in paths:
        path = Path(path_raw)
        if not path.is_absolute():
            path = REPO_ROOT / path
        if not path.exists():
            raise FileNotFoundError(f"sync path missing: {rel(path)}")
        rel_path = rel(path)
        if rel_path in seen:
            continue
        seen.add(rel_path)
        sync_one_path(state, path, rel_path, vm)


def remote_data_quality_summary(spec_path: Path, vm: dict[str, Any]) -> str:
    remote_repo = str(vm["remote_repo"])
    spec_rel = rel(spec_path)
    cmd = (
        f"cd {shlex.quote(remote_repo)} && "
        "PY=.venv/bin/python; "
        "[ -x \"$PY\" ] || PY=python3; "
        f"$PY scripts/validate_data_quality.py --spec {shlex.quote(spec_rel)} --summary-only"
    )
    output = ssh_vm(vm, cmd)
    try:
        json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"remote data quality summary was not JSON: {exc}\n{output[-2000:]}")
    return output


def judge_data_quality(summary_json: str) -> str:
    ticket = issue_ticket("data_quality_judge")
    env = dict(**os.environ)
    env["QUANT_AUTOMATION_ACTOR"] = "research_queue_runner"
    env["QUANT_AUTOMATION_TICKET_PATH"] = ticket["path"]
    env["QUANT_AUTOMATION_TICKET_TOKEN"] = ticket["token"]
    result = subprocess.run(
        [sys.executable, "scripts/validate_data_quality.py", "--judge-summary-stdin"],
        cwd=REPO_ROOT,
        env=env,
        input=summary_json,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    output = result.stdout or ""
    parsed = yaml.safe_load(output) if output.strip() else {}
    if isinstance(parsed, dict) and str(parsed.get("status") or "").lower() in {"repair_candidate", "fixable"}:
        raise DataQualityRepairCandidate(output, summary_json)
    if result.returncode != 0:
        raise DataQualityBlocked(output[-4000:] or f"data quality judge failed rc={result.returncode}")
    return output


def run_data_repair(spec_path: Path, decision_path: Path) -> dict[str, Any]:
    ticket = issue_ticket("data_quality_repair")
    env = dict(**os.environ)
    env["QUANT_AUTOMATION_ACTOR"] = "research_queue_runner"
    env["QUANT_AUTOMATION_TICKET_PATH"] = ticket["path"]
    env["QUANT_AUTOMATION_TICKET_TOKEN"] = ticket["token"]
    result = subprocess.run(
        [sys.executable, "scripts/repair_data_quality.py", "--spec", rel(spec_path), "--decision", rel(decision_path)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    output = result.stdout or ""
    if result.returncode != 0:
        raise DataQualityBlocked(output[-4000:] or f"data repair failed rc={result.returncode}")
    payload = json.loads(output)
    return payload if isinstance(payload, dict) else {"repair_output": output}


def write_data_quality_decision(spec_path: Path, decision_text: str, summary_json: str) -> Path:
    decision = yaml.safe_load(decision_text)
    if not isinstance(decision, dict):
        raise RuntimeError("data quality decision must be YAML mapping")
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
    if not isinstance(spec, dict):
        spec = {}
    payload = {
        "schema_version": 1,
        "run_id": spec.get("run_id") or spec_path.parent.name,
        "spec_path": rel(spec_path),
        "validated_at": now_iso(),
        "summary_sha256": hashlib.sha256(summary_json.encode("utf-8")).hexdigest(),
        **decision,
    }
    path = spec_path.parent / "data_quality_decision.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def validate_or_repair_data_quality(state: dict[str, Any], item: dict[str, Any], spec_path: Path, vm: dict[str, Any]) -> Path:
    quality_summary = remote_data_quality_summary(spec_path, vm)
    try:
        quality_decision = judge_data_quality(quality_summary)
    except DataQualityRepairCandidate as exc:
        decision_path = write_data_quality_decision(spec_path, exc.decision_text, exc.summary_json)
        repair_report = run_data_repair(spec_path, decision_path)
        sync_paths(state, item, spec_path, vm)
        repaired_summary = remote_data_quality_summary(spec_path, vm)
        repaired_decision = judge_data_quality(repaired_summary)
        final_path = write_data_quality_decision(spec_path, repaired_decision, repaired_summary)
        final_decision = yaml.safe_load(repaired_decision) or {}
        if not isinstance(final_decision, dict) or str(final_decision.get("status") or "").lower() != "pass":
            raise DataQualityBlocked(f"repaired data did not pass validation: {repaired_decision[-3000:]}")
        audit(
            "data_quality_repair",
            {
                "item_id": item.get("id"),
                "spec_path": rel(spec_path),
                "decision_path": rel(decision_path),
                "final_decision_path": rel(final_path),
                "repair": repair_report,
            },
        )
        return final_path
    decision_path = write_data_quality_decision(spec_path, quality_decision, quality_summary)
    return decision_path


def start_remote_pipeline(state: dict[str, Any], item: dict[str, Any], spec_path: Path, vm: dict[str, Any]) -> str:
    vm_host = str(vm["host"])
    remote_repo = str(vm["remote_repo"])
    run_dir = spec_path.parent
    remote_run_dir = f"{remote_repo}/{rel(run_dir)}"
    remote_spec = f"{remote_repo}/{rel(spec_path)}"
    cmd = (
        f"cd {shlex.quote(remote_repo)} && "
        f"mkdir -p {shlex.quote(remote_run_dir)} && "
        f"(nohup python3 scripts/auto_research_pipeline.py {shlex.quote(rel(spec_path))} --quiet "
        f"> {shlex.quote(remote_run_dir + '/vm_pipeline_stdout.log')} 2>&1 < /dev/null & "
        f"echo $!)"
    )
    pid = ssh_vm(vm, cmd).strip().splitlines()[-1].strip()
    if not pid:
        raise RuntimeError(f"remote pipeline did not return pid for {remote_spec}")
    log(f"started remote pipeline pid={pid} task={item.get('id') or spec_path.parent.name} vm={vm_host}")
    return pid


def mark_history(state: dict[str, Any], item: dict[str, Any], status: str, message: str) -> None:
    history = state.setdefault("history", [])
    if isinstance(history, list):
        history.append(
            {
                "id": item.get("id"),
                "spec_path": item.get("spec_path"),
                "status": status,
                "message": message,
                "at": now_iso(),
                "vm_id": item.get("vm_id"),
                "vm_host": item.get("vm_host"),
            }
        )


def review_and_digest_run(run_dir: Path) -> dict[str, Any]:
    review_result = run([sys.executable, "scripts/review_result.py", rel(run_dir)], check=True)
    digest_result = run(
        [
            sys.executable,
            "scripts/build_recent_results_digest.py",
            "--limit",
            "10",
            "--update-current",
        ],
        check=True,
    )
    review_path = run_dir / "review.yaml"
    digest_path = REPO_ROOT / "data" / "research_framework" / "recent_results_digest.yaml"
    current_path = REPO_ROOT / "data" / "research_framework" / "current.yaml"
    return {
        "review_path": rel(review_path),
        "digest_path": rel(digest_path),
        "current_path": rel(current_path),
        "review_output": (review_result.stdout or "")[-1000:],
        "digest_output": (digest_result.stdout or "")[-1000:],
    }


def required_artifacts_present(spec: dict[str, Any], run_dir: Path) -> bool:
    artifacts = spec.get("artifacts_required") or []
    if not isinstance(artifacts, list):
        return False
    return all((run_dir / str(name)).exists() for name in artifacts)


def sync_remote_run_dir(state: dict[str, Any], item: dict[str, Any], run_dir: Path) -> None:
    vm = item_vm_config(state, item)
    vm_host = str(vm["host"])
    remote_repo = str(vm["remote_repo"])
    rel_dir = rel(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    result_filter = [
        "--include=*/",
        "--include=*.yaml",
        "--include=*.json",
        "--include=*.csv",
        "--include=*.log",
        "--include=*.txt",
        "--exclude=*",
    ]
    proxy_host = str(vm.get("proxy_host") or "").strip()
    if not proxy_host:
        run(
            ["rsync", "-av", *result_filter, f"{vm_host}:{remote_repo}/{rel_dir}/", f"{rel_dir}/"],
            check=False,
        )
        return
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in rel_dir)
    stage = f"/tmp/quant_proxy_pull_{safe}"
    ssh_cmd = proxy_ssh_cmd(vm)
    ssh(
        proxy_host,
        " && ".join(
            [
                f"rm -rf {shlex.quote(stage)}",
                f"mkdir -p {shlex.quote(stage)}",
                (
                    "rsync -av "
                    "--include='*/' --include='*.yaml' --include='*.json' "
                    "--include='*.csv' --include='*.log' --include='*.txt' --exclude='*' "
                    f"-e {shlex.quote(ssh_cmd)} "
                    f"{shlex.quote(vm_host + ':' + posixpath.join(remote_repo, rel_dir) + '/')} "
                    f"{shlex.quote(stage + '/')}"
                ),
            ]
        ),
        check=False,
    )
    run(["rsync", "-av", *result_filter, f"{proxy_host}:{stage}/", f"{rel_dir}/"], check=False)


def settle_running_items(state: dict[str, Any], queue: list[Any]) -> int:
    running_items = [
        item
        for item in queue
        if isinstance(item, dict) and item.get("status") == "running"
    ]
    if not running_items:
        return 0
    changed = 0
    for item in running_items:
        spec_raw = item.get("spec_path")
        if not isinstance(spec_raw, str) or not spec_raw:
            item["status"] = "failed"
            item["failed_at"] = now_iso()
            item["failure_reason"] = "running item missing spec_path"
            mark_history(state, item, "failed", item["failure_reason"])
            changed += 1
            continue
        spec_path = Path(spec_raw)
        if not spec_path.is_absolute():
            spec_path = REPO_ROOT / spec_path
        run_dir = spec_path.parent
        try:
            vm = item_vm_config(state, item)
            pattern = str(item.get("process_pattern") or spec_path.parent.name)
            if remote_running_on_vm(vm, pattern):
                continue
            sync_remote_run_dir(state, item, run_dir)
            spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
            if not isinstance(spec, dict):
                raise ValueError("spec root must be dict")
            if required_artifacts_present(spec, run_dir):
                item["workflow_stage"] = "artifacts_synced"
                mark_history(state, item, "artifacts_synced", "remote run artifacts synced")
                review_payload = review_and_digest_run(run_dir)
                item["workflow_stage"] = "digested"
                item["review_path"] = review_payload["review_path"]
                item["digest_path"] = review_payload["digest_path"]
                item["status"] = "complete"
                item["completed_at"] = now_iso()
                mark_history(state, item, "complete", "remote run reviewed and digest updated")
                audit("review", {"item_id": item.get("id"), "run_dir": rel(run_dir), **review_payload})
                audit("digest", {"item_id": item.get("id"), "run_dir": rel(run_dir), "result": "complete", **review_payload})
                changed += 1
            else:
                item["status"] = "failed"
                item["failed_at"] = now_iso()
                item["failure_reason"] = "remote process exited but required artifacts are missing"
                mark_history(state, item, "failed", item["failure_reason"])
                changed += 1
        except Exception as exc:
            item["status"] = "failed"
            item["failed_at"] = now_iso()
            item["failure_reason"] = f"{type(exc).__name__}: {exc}"
            mark_history(state, item, "failed", item["failure_reason"])
            changed += 1
    if changed:
        state["queue"] = queue
        save_state(state)
    return changed


def tick() -> str:
    if is_paused():
        write_status("paused", {"pause_flag": rel(PAUSE_FLAG_PATH)})
        audit("paused", {"pause_flag": rel(PAUSE_FLAG_PATH)})
        return "paused"

    state = load_state()
    if not state.get("enabled", False):
        write_status("disabled")
        return "disabled"
    queue = state.get("queue") or []
    if not isinstance(queue, list):
        raise ValueError("research_queue.queue must be list")

    settled_count = settle_running_items(state, queue)
    decision = decide_scheduler_action(state)
    running_count = count_status(state, "running")

    if decision.action != "start_or_continue_queued":
        if decision.action == "monitor_running":
            write_status(
                "waiting_remote_running",
                {
                    "running_count": running_count,
                    "settled_count": settled_count,
                    "note": "Remote work is still running; no new direction may be generated.",
                },
            )
            return "waiting_remote_running"
        if escalation_block(state):
            return stop_for_user_block(state)
        return generate_next_spec_if_idle(state)

    available_vms = [vm for vm in vm_configs(state) if vm_available(vm)]
    if not available_vms:
        running_count = sum(1 for item in queue if isinstance(item, dict) and item.get("status") == "running")
        status = "waiting_remote_running" if running_count else "idle_no_available_vm"
        write_status(
            status,
            {
                "running_count": running_count,
                "settled_count": settled_count,
                "vm_count": len(vm_configs(state)),
            },
        )
        return status

    started: list[dict[str, str]] = []
    for item in queue:
        if not isinstance(item, dict) or item.get("status") != "queued":
            continue
        if not available_vms:
            break
        family = str(item.get("family") or "")
        forbidden = set(state.get("forbidden_families") or [])
        spec_raw = item.get("spec_path")
        spec_family_name = ""
        if isinstance(spec_raw, str) and spec_raw:
            spec_path_for_family = Path(spec_raw)
            if not spec_path_for_family.is_absolute():
                spec_path_for_family = REPO_ROOT / spec_path_for_family
            try:
                spec_data = yaml.safe_load(spec_path_for_family.read_text(encoding="utf-8")) or {}
                if isinstance(spec_data, dict):
                    spec_family_name = spec_family(spec_data)
            except Exception:
                spec_family_name = ""
        if family in forbidden or spec_family_name in forbidden:
            item["status"] = "blocked"
            item["blocked_at"] = now_iso()
            item["block_reason"] = f"family {spec_family_name or family} is forbidden"
            mark_history(state, item, "blocked", item["block_reason"])
            save_state(state)
            write_status("blocked_forbidden_family", {"item_id": item.get("id")})
            return "blocked_forbidden_family"

        if not isinstance(spec_raw, str) or not spec_raw:
            item["status"] = "failed"
            item["failed_at"] = now_iso()
            item["failure_reason"] = "missing spec_path"
            mark_history(state, item, "failed", "missing spec_path")
            save_state(state)
            return "failed_missing_spec_path"

        spec_path = Path(spec_raw)
        if not spec_path.is_absolute():
            spec_path = REPO_ROOT / spec_path
        vm = choose_vm_for_item(available_vms, item)
        if vm is None:
            write_status(
                "waiting_unavoided_vm",
                {
                    "item_id": item.get("id"),
                    "avoid_vm_ids": item.get("avoid_vm_ids", []),
                    "available_vm_ids": [vm.get("id") for vm in available_vms],
                },
            )
            return "waiting_unavoided_vm"
        try:
            audit("compile", {"item_id": item.get("id"), "spec_path": rel(spec_path)})
            validate_spec(spec_path)
            item["workflow_stage"] = "spec_validated"
            write_status(
                "syncing_to_vm",
                {
                    "item_id": item.get("id"),
                    "vm_id": vm.get("id"),
                    "vm_host": vm.get("host"),
                },
            )
            sync_paths(state, item, spec_path, vm)
            item["workflow_stage"] = "synced_to_vm"
            write_status(
                "checking_remote_data_quality",
                {
                    "item_id": item.get("id"),
                    "vm_id": vm.get("id"),
                    "vm_host": vm.get("host"),
                },
            )
            decision_path = validate_or_repair_data_quality(state, item, spec_path, vm)
            item["workflow_stage"] = "data_checked"
            sync_one_path(state, decision_path, rel(decision_path), vm)
            audit(
                "data_quality_pass",
                {
                    "item_id": item.get("id"),
                    "spec_path": rel(spec_path),
                    "vm_id": vm.get("id"),
                    "decision_path": rel(decision_path),
                },
            )
            write_status(
                "starting_remote_pipeline",
                {
                    "item_id": item.get("id"),
                    "vm_id": vm.get("id"),
                    "vm_host": vm.get("host"),
                },
            )
            pid = start_remote_pipeline(state, item, spec_path, vm)
            audit("run", {"item_id": item.get("id"), "spec_path": rel(spec_path), "vm_id": vm.get("id"), "remote_pid": pid})
        except Exception as exc:
            if isinstance(exc, DataQualityBlocked):
                item["status"] = "blocked"
                item["blocked_at"] = now_iso()
                item["block_reason"] = f"data_quality_blocked: {exc}"
                mark_history(state, item, "blocked", item["block_reason"])
                save_state(state)
                write_status(
                    "blocked_data_quality",
                    {
                        "item_id": item.get("id"),
                        "vm_id": vm.get("id"),
                        "reason": str(exc)[-4000:],
                    },
                )
                return "blocked_data_quality"
            if is_transient_vm_error(exc):
                avoid = list(item.get("avoid_vm_ids", []) or [])
                if vm.get("id") not in avoid:
                    avoid.append(vm.get("id"))
                item["avoid_vm_ids"] = avoid
                item["status"] = "queued"
                item["last_start_error_at"] = now_iso()
                item["last_start_error"] = f"{type(exc).__name__}: {exc}"
                item["start_retry_count"] = int(item.get("start_retry_count") or 0) + 1
                mark_history(state, item, "queued", f"transient VM start error on {vm.get('id')}; requeued")
                save_state(state)
                write_status(
                    "queued_vm_retry",
                    {
                        "item_id": item.get("id"),
                        "vm_id": vm.get("id"),
                        "error": item["last_start_error"],
                        "avoid_vm_ids": avoid,
                    },
                )
                return "queued_vm_retry"
            item["status"] = "failed"
            item["failed_at"] = now_iso()
            item["failure_reason"] = f"{type(exc).__name__}: {exc}"
            mark_history(state, item, "failed", item["failure_reason"])
            save_state(state)
            write_status("failed", {"item_id": item.get("id"), "error": item["failure_reason"]})
            raise

        item["status"] = "running"
        item["workflow_stage"] = "running_remote"
        item["started_at"] = now_iso()
        item["remote_pid"] = pid
        item["vm_id"] = vm["id"]
        item["vm_host"] = vm["host"]
        item["remote_repo"] = vm["remote_repo"]
        item.pop("avoid_vm_ids", None)
        item.pop("last_start_error", None)
        item.pop("last_start_error_at", None)
        mark_history(state, item, "running", f"started remote pid {pid} on {vm['id']}")
        save_state(state)
        started.append({"item_id": str(item.get("id")), "remote_pid": pid, "vm_id": vm["id"], "vm_host": vm["host"]})

    if started:
        write_status("running_remote", {"started": started, "started_count": len(started)})
        return f"started_{len(started)}"
    if settled_count:
        write_status("settled_running_items", {"settled_count": settled_count})
        return "settled_running_items"
    write_status("idle_no_queued_specs")
    return "idle_no_queued_specs"


def tick_once_under_lock() -> str:
    ONCE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ONCE_LOCK_PATH.open("w", encoding="utf-8") as lock_fh:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            write_status(
                "skipped_locked",
                {"lock_path": rel(ONCE_LOCK_PATH), "note": "another research queue tick is already active"},
            )
            audit("skipped_locked", {"lock_path": rel(ONCE_LOCK_PATH)})
            return "skipped_locked"
        return tick()


def main() -> int:
    require_ticket("research_queue_runner_once")
    print(tick_once_under_lock())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
