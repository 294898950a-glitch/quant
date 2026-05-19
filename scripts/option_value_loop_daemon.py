#!/usr/bin/env python3
"""Option-value research loop scheduler.

This daemon only schedules and monitors. It never runs cb_arb backtests locally.
Queued tasks must provide a READY spec.yaml; the daemon validates it locally,
syncs declared files to the VM, starts auto_research_pipeline.py remotely, and
starts the existing VM completion monitor.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import posixpath
import shlex
import socket
import subprocess
import sys
import time
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
from hermes_access_guard import require_ticket


STATE_PATH = REPO_ROOT / "data" / "research_framework" / "option_value_loop.yaml"
LOG_PATH = REPO_ROOT / "logs" / "option_value_loop.log"
STATUS_PATH = REPO_ROOT / "logs" / "option_value_loop_status.json"
PID_PATH = REPO_ROOT / ".option-value-loop.pid"
ONCE_LOCK_PATH = REPO_ROOT / "logs" / "option_value_loop_once.lock"
AUDIT_LOG_PATH = REPO_ROOT / "data" / "research_framework" / "orchestrator_log.jsonl"
RESTART_LOG_PATH = REPO_ROOT / "logs" / "option_value_loop_restarts.jsonl"
PAUSE_FLAG_PATH = REPO_ROOT / "data" / "research_framework" / "orchestrator_paused.flag"


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
        raise ValueError("option_value_loop.yaml root must be dict")
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


def start_background(cmd: list[str]) -> int:
    log_path = LOG_PATH.parent / "option_value_loop_child.log"
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        process = subprocess.Popen(
            cmd,
            cwd=REPO_ROOT,
            stdout=fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    return int(process.pid)


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
    budget_cap = float(spec.get("budget_cap_yuan", 0) or 0)
    if budget_cap > float(load_state().get("max_auto_budget_yuan", 100)):
        raise ValueError(f"budget_cap_yuan={budget_cap} exceeds option loop limit")
    if str(spec.get("status")) != "READY":
        raise ValueError(f"spec.status must be READY, got {spec.get('status')!r}")
    return spec


def spec_matches_discovery(state: dict[str, Any], spec_path: Path) -> bool:
    try:
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(spec, dict):
        return False
    discovery = state.get("discovery") or {}
    if not isinstance(discovery, dict):
        discovery = {}
    if str(spec.get("status")) in set(discovery.get("exclude_statuses") or []):
        return False
    if str(spec.get("status")) != "READY":
        return False
    required_strategy = str(discovery.get("strategy_id") or "")
    if required_strategy and str(spec.get("strategy_id") or "") != required_strategy:
        return False
    text_parts = [
        str(spec.get("run_id") or ""),
        str(spec.get("hypothesis") or ""),
        str(spec.get("source_insight") or ""),
    ]
    automation = spec.get("automation") or {}
    if isinstance(automation, dict):
        command = automation.get("command") or []
        text_parts.append(" ".join(str(x) for x in command) if isinstance(command, list) else str(command))
    haystack = "\n".join(text_parts).lower()
    tokens = [str(x).lower() for x in discovery.get("include_tokens") or []]
    return any(token in haystack for token in tokens)


def command_script_paths(spec: dict[str, Any]) -> list[str]:
    automation = spec.get("automation") or {}
    if not isinstance(automation, dict):
        return []
    paths = [
        str(item)
        for item in automation.get("sync_paths") or []
        if isinstance(item, str)
    ]
    command = automation.get("command") or []
    if not isinstance(command, list):
        return paths
    paths.extend(
        str(item)
        for item in command
        if isinstance(item, str) and item.startswith("scripts/") and item.endswith(".py")
    )
    return paths


def spec_family(spec: dict[str, Any]) -> str:
    ideation = spec.get("ideation") or {}
    if isinstance(ideation, dict) and ideation.get("family"):
        return str(ideation["family"])
    return str(spec.get("family") or "")


def discover_ready_specs(state: dict[str, Any]) -> list[dict[str, Any]]:
    if not state.get("auto_discover_ready_specs", False):
        return []
    known = {
        str(item.get("spec_path"))
        for item in (state.get("queue") or [])
        if isinstance(item, dict) and item.get("spec_path")
    }
    known.update(
        str(item.get("spec_path"))
        for item in (state.get("history") or [])
        if isinstance(item, dict) and item.get("spec_path")
    )
    discovered: list[dict[str, Any]] = []
    for spec_path in sorted((REPO_ROOT / "data").glob("*/spec.yaml")):
        rel_spec = rel(spec_path)
        if rel_spec in known:
            continue
        if not spec_matches_discovery(state, spec_path):
            continue
        try:
            spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(spec, dict):
            continue
        family = spec_family(spec)
        if family and family in set(state.get("forbidden_families") or []):
            continue
        run_id = str(spec.get("run_id") or spec_path.parent.name)
        discovered.append(
            {
                "id": run_id,
                "family": family or "auto_discovered_option_value",
                "status": "queued",
                "spec_path": rel_spec,
                "process_pattern": run_id,
                "sync_paths": command_script_paths(spec),
                "discovered_at": now_iso(),
            }
        )
    return discovered


def generate_next_spec_if_idle(state: dict[str, Any]) -> None:
    write_status(
        "needs_hermes_research_direction",
        {
            "note": (
                "No queued READY specs. Background LLM ideation is disabled; "
                "Hermes must generate or add a READY spec in its current turn."
            )
        },
    )
    audit("needs_hermes_research_direction", {"reason": "background_llm_ideation_disabled"})
    log("needs Hermes research direction; background LLM ideation disabled")


def ideation_blocked(state: dict[str, Any]) -> bool:
    ideation = state.get("ideation") or {}
    return isinstance(ideation, dict) and bool(ideation.get("blocked_reason"))


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

    task_name = str(item.get("id") or spec_path.parent.name)
    process_pattern = str(item.get("process_pattern") or Path(rel(spec_path)).parent.name)
    env_cmd = ["env", f"VM_HOST={vm_host}", f"REMOTE_REPO={remote_repo}"]
    if vm.get("proxy_host"):
        env_cmd.extend(
            [
                f"VM_PROXY_HOST={vm['proxy_host']}",
                f"VM_PROXY_REPO={proxy_stage_repo(state, vm)}",
                f"VM_IDENTITY_FILE_ON_PROXY={vm['identity_file_on_proxy']}",
            ]
        )
    monitor_pid = start_background(
        env_cmd
        + [
            "scripts/watch_quant_vm_task_completion.sh",
            "--task-name",
            task_name,
            "--remote-dir",
            rel(run_dir),
            "--local-dir",
            rel(run_dir),
            "--process-pattern",
            process_pattern,
            "--log-file",
            "auto_pipeline.log",
            "--poll-seconds",
            str(int(item.get("monitor_poll_seconds") or 60)),
        ]
    )
    log(f"started local monitor pid={monitor_pid} task={task_name} vm={vm_host}")
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
                item["status"] = "complete"
                item["completed_at"] = now_iso()
                mark_history(state, item, "complete", "remote run artifacts synced")
                audit("review", {"item_id": item.get("id"), "run_dir": rel(run_dir)})
                audit("digest", {"item_id": item.get("id"), "run_dir": rel(run_dir), "result": "complete"})
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


def tick(*, allow_ideation: bool = True) -> str:
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
        raise ValueError("option_value_loop.queue must be list")

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
        discovered = discover_ready_specs(state)
        if not discovered:
            generate_next_spec_if_idle(state)
            return "needs_hermes_research_direction"
        if discovered:
            queue.extend(discovered)
            state["queue"] = queue
            for item in discovered:
                mark_history(state, item, "queued", "auto-discovered READY option-related spec")
            save_state(state)

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
        vm = available_vms.pop(0)
        try:
            audit("compile", {"item_id": item.get("id"), "spec_path": rel(spec_path)})
            validate_spec(spec_path)
            write_status(
                "syncing_to_vm",
                {
                    "item_id": item.get("id"),
                    "vm_id": vm.get("id"),
                    "vm_host": vm.get("host"),
                },
            )
            sync_paths(state, item, spec_path, vm)
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
            item["status"] = "failed"
            item["failed_at"] = now_iso()
            item["failure_reason"] = f"{type(exc).__name__}: {exc}"
            mark_history(state, item, "failed", item["failure_reason"])
            save_state(state)
            write_status("failed", {"item_id": item.get("id"), "error": item["failure_reason"]})
            raise

        item["status"] = "running"
        item["started_at"] = now_iso()
        item["remote_pid"] = pid
        item["vm_id"] = vm["id"]
        item["vm_host"] = vm["host"]
        item["remote_repo"] = vm["remote_repo"]
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


def daemon_loop(poll_seconds: int) -> int:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    previous_pid = None
    if PID_PATH.exists():
        previous_pid = PID_PATH.read_text(encoding="utf-8").strip()
    if previous_pid and previous_pid != str(os.getpid()):
        RESTART_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "event": "restart",
            "previous_pid": previous_pid,
            "pid": os.getpid(),
            "reason": "daemon_loop_start",
            "ts": now_iso(),
        }
        with RESTART_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        log(f"restart detected previous pid={previous_pid} new pid={os.getpid()} reason=daemon_loop_start")
        audit("restart", {"previous_pid": previous_pid, "pid": os.getpid(), "reason": "daemon_loop_start"})
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    log(f"option value loop daemon started pid={os.getpid()}")
    try:
        while True:
            try:
                result = tick()
                log(f"tick: {result}")
            except Exception as exc:
                log(f"ERROR {type(exc).__name__}: {exc}")
                write_status("error", {"error": f"{type(exc).__name__}: {exc}"})
            time.sleep(poll_seconds)
    finally:
        if PID_PATH.exists():
            PID_PATH.unlink()


def tick_once_under_lock(*, allow_ideation: bool) -> str:
    ONCE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ONCE_LOCK_PATH.open("w", encoding="utf-8") as lock_fh:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            write_status(
                "skipped_locked",
                {"lock_path": rel(ONCE_LOCK_PATH), "note": "another option-value tick is already active"},
            )
            audit("skipped_locked", {"lock_path": rel(ONCE_LOCK_PATH)})
            return "skipped_locked"
        return tick(allow_ideation=allow_ideation)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--no-ideation",
        action="store_true",
        help="Compatibility flag; background LLM ideation is disabled unconditionally.",
    )
    parser.add_argument("--poll-seconds", type=int, default=None)
    args = parser.parse_args()

    if args.once:
        require_ticket("option_value_loop_once")
        print(tick_once_under_lock(allow_ideation=not args.no_ideation))
        return 0
    require_ticket("option_value_loop_daemon")
    state = load_state()
    poll_seconds = int(args.poll_seconds or state.get("poll_seconds") or 600)
    return daemon_loop(poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
