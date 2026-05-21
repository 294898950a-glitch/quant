"""Remote execution service for autonomous research queue items."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shlex
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import yaml


STALE_VM_AVOID_AFTER = timedelta(minutes=30)
SYNC_COMMAND_TIMEOUT_SECONDS = 180
SSH_COMMAND_TIMEOUT_SECONDS = 120
LARGE_DATA_SUFFIXES = {".parquet", ".feather", ".h5", ".hdf", ".pkl"}


class DataQualityBlocked(RuntimeError):
    pass


class DataQualityRepairCandidate(RuntimeError):
    def __init__(self, decision_text: str, summary_json: str):
        super().__init__(decision_text[-4000:])
        self.decision_text = decision_text
        self.summary_json = summary_json


def _rel(repo_root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo_root))
    except ValueError:
        return str(path)


def should_sync_path_for_run(path: Path, rel_path: str, spec_path: Path, repo_root: Path | None = None) -> bool:
    root = repo_root or Path.cwd()
    run_rel = _rel(root, spec_path.parent).rstrip("/")
    prepared_rel = f"{run_rel}/prepared_data"
    if rel_path == prepared_rel or rel_path.startswith(f"{prepared_rel}/"):
        return True
    if rel_path.startswith("data/") and path.suffix.lower() in LARGE_DATA_SUFFIXES:
        return False
    if path.is_dir() and rel_path.startswith("data/") and not rel_path.startswith("data/research_framework/"):
        return False
    return True


def pipeline_execution_failed(run_dir: Path) -> str | None:
    for name in ("vm_pipeline_stdout.log", "auto_pipeline_stdout.log"):
        path = run_dir / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"^exit_code:\s*([1-9]\d*)\s*$", text, flags=re.MULTILINE)
        if match:
            return f"remote pipeline exit_code={match.group(1)}"
    return None


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


class QueueRemoteExecutionService:
    def __init__(
        self,
        *,
        repo_root: Path,
        save_state: Callable[[dict[str, Any]], None],
        write_status: Callable[[str, dict[str, Any] | None], None],
        audit: Callable[[str, dict[str, Any] | None], None],
        log: Callable[[str], None],
        mark_history: Callable[[dict[str, Any], dict[str, Any], str, str], None],
        rel: Callable[[Path], str],
        now_iso: Callable[[], str],
        issue_ticket: Callable[[str], dict[str, str]],
        data_quality_gate_passes_locally: Callable[[Path], bool] | None = None,
        data_quality_repair_signature: Callable[[Path, dict[str, Any]], str] | None = None,
        pipeline_execution_failed: Callable[[Path], str | None] | None = None,
        remote_running_on_vm: Callable[[dict[str, Any], str | None], bool] | None = None,
        sync_remote_run_dir: Callable[[dict[str, Any], dict[str, Any], Path], None] | None = None,
        item_vm_config: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    ):
        self.repo_root = repo_root
        self.save_state = save_state
        self.write_status = write_status
        self.audit = audit
        self.log = log
        self.mark_history = mark_history
        self.rel = rel
        self.now_iso = now_iso
        self.issue_ticket = issue_ticket
        self._data_quality_gate_passes_locally_cb = data_quality_gate_passes_locally
        self._data_quality_repair_signature_cb = data_quality_repair_signature
        self._pipeline_execution_failed_cb = pipeline_execution_failed
        self._remote_running_on_vm_cb = remote_running_on_vm
        self._sync_remote_run_dir_cb = sync_remote_run_dir
        self._item_vm_config_cb = item_vm_config

    def run(
        self,
        cmd: list[str],
        *,
        check: bool = True,
        capture: bool = True,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                cmd,
                cwd=self.repo_root,
                env=env,
                text=True,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.STDOUT if capture else None,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            output = exc.stdout if isinstance(exc.stdout, str) else ""
            raise RuntimeError(
                f"command timed out after {timeout}s: {' '.join(map(shlex.quote, cmd))}\n{output[-2000:]}"
            ) from exc
        if check and result.returncode != 0:
            output = result.stdout or ""
            raise RuntimeError(f"command failed rc={result.returncode}: {' '.join(map(shlex.quote, cmd))}\n{output}")
        return result

    def ssh(self, vm_host: str, command: str, *, check: bool = True, timeout: int | None = SSH_COMMAND_TIMEOUT_SECONDS) -> str:
        result = self.run(
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
            timeout=timeout,
        )
        return result.stdout or ""

    def ssh_vm(self, vm: dict[str, Any], command: str, *, check: bool = True) -> str:
        proxy_host = str(vm.get("proxy_host") or "").strip()
        if not proxy_host:
            return self.ssh(str(vm["host"]), command, check=check)
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
        return self.ssh(proxy_host, nested, check=check)

    def proxy_ssh_cmd(self, vm: dict[str, Any]) -> str:
        identity = str(vm.get("identity_file_on_proxy") or "").strip()
        if not identity:
            raise ValueError(f"proxy vm {vm.get('id')} missing identity_file_on_proxy")
        return (
            "ssh -i "
            f"{shlex.quote(identity)} "
            "-o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new "
            "-o ConnectTimeout=8"
        )

    def proxy_stage_repo(self, state: dict[str, Any], vm: dict[str, Any]) -> str:
        return str(vm.get("proxy_repo") or state.get("remote_repo") or "/root/projects/quant")

    def vm_configs(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        raw = state.get("vm_hosts")
        configs: list[dict[str, Any]] = []
        if isinstance(raw, list):
            for item in raw:
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

    def item_vm_config(self, state: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        if self._item_vm_config_cb is not None:
            return self._item_vm_config_cb(state, item)
        known = self.vm_configs(state)
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

    def validate_spec(self, spec_path: Path) -> dict[str, Any]:
        self.run(["python3", "scripts/validate_spec.py", self.rel(spec_path)])
        self.run(["python3", "scripts/research_sanity_checker.py", "--spec", self.rel(spec_path)])
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        if not isinstance(spec, dict):
            raise ValueError(f"{self.rel(spec_path)} root must be dict")
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

    def spec_family(self, spec: dict[str, Any]) -> str:
        ideation = spec.get("ideation") or {}
        if isinstance(ideation, dict) and ideation.get("family"):
            return str(ideation["family"])
        return str(spec.get("family") or "")

    def remote_running_on_vm(self, vm: dict[str, Any], process_pattern: str | None = None) -> bool:
        if self._remote_running_on_vm_cb is not None:
            return self._remote_running_on_vm_cb(vm, process_pattern)
        pattern = "auto_research_pipeline.py|evaluate_cb_arb_.*option|option-value|option_value"
        if process_pattern:
            pattern = process_pattern
        out = self.ssh_vm(vm, f"pgrep -af {shlex.quote(pattern)} | grep -v pgrep || true", check=False)
        return bool(out.strip())

    def vm_available(self, vm: dict[str, Any]) -> bool:
        try:
            self.ssh_vm(vm, "echo ok >/dev/null", check=True)
        except Exception as exc:
            self.log(f"vm unavailable {vm.get('id')}: {type(exc).__name__}: {exc}")
            return False
        return not self.remote_running_on_vm(vm)

    def choose_vm_for_item(self, available_vms: list[dict[str, Any]], item: dict[str, Any]) -> dict[str, Any] | None:
        avoid = set(str(value) for value in item.get("avoid_vm_ids", []) or [])
        for index, vm in enumerate(available_vms):
            if str(vm.get("id")) not in avoid:
                return available_vms.pop(index)
        return None

    @staticmethod
    def _parse_local_datetime(raw: Any) -> datetime | None:
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            return datetime.fromisoformat(raw.strip())
        except ValueError:
            return None

    def clear_stale_vm_avoidances(self, state: dict[str, Any], queue: list[Any]) -> int:
        changed = 0
        cutoff = datetime.now() - STALE_VM_AVOID_AFTER
        for item in queue:
            if not isinstance(item, dict) or item.get("status") != "queued" or not item.get("avoid_vm_ids"):
                continue
            failed_at = self._parse_local_datetime(item.get("last_start_error_at"))
            if failed_at is not None and failed_at > cutoff:
                continue
            old_avoid = list(item.get("avoid_vm_ids") or [])
            item.pop("avoid_vm_ids", None)
            item["workflow_stage"] = "queued_after_stale_vm_avoidance_reset"
            item["stale_vm_avoidance_reset_at"] = self.now_iso()
            self.mark_history(state, item, "queued", f"cleared stale transient VM avoid list: {old_avoid}")
            self.audit(
                "stale_vm_avoidance_reset",
                {
                    "item_id": item.get("id"),
                    "old_avoid_vm_ids": old_avoid,
                    "last_start_error_at": item.get("last_start_error_at"),
                },
            )
            changed += 1
        if changed:
            state["queue"] = queue
            self.save_state(state)
        return changed

    def sync_one_path(self, state: dict[str, Any], path: Path, rel_path: str, vm: dict[str, Any]) -> None:
        vm_host = str(vm["host"])
        remote_repo = str(vm["remote_repo"])
        proxy_host = str(vm.get("proxy_host") or "").strip()

        if not proxy_host:
            dest = f"{vm_host}:{remote_repo}/{rel_path}"
            if path.is_dir():
                self.run(["rsync", "-av", "--delete", f"{rel_path}/", f"{dest}/"], timeout=SYNC_COMMAND_TIMEOUT_SECONDS)
            else:
                self.ssh_vm(vm, f"mkdir -p {shlex.quote(posixpath.dirname(posixpath.join(remote_repo, rel_path)))}")
                self.run(["rsync", "-av", rel_path, dest], timeout=SYNC_COMMAND_TIMEOUT_SECONDS)
            return

        proxy_repo = self.proxy_stage_repo(state, vm)
        proxy_dest = f"{proxy_host}:{proxy_repo}/{rel_path}"
        self.ssh(proxy_host, f"mkdir -p {shlex.quote(posixpath.dirname(posixpath.join(proxy_repo, rel_path)))}")
        if path.is_dir():
            self.run(["rsync", "-av", "--delete", f"{rel_path}/", f"{proxy_dest}/"], timeout=SYNC_COMMAND_TIMEOUT_SECONDS)
        else:
            self.run(["rsync", "-av", rel_path, proxy_dest], timeout=SYNC_COMMAND_TIMEOUT_SECONDS)

        ssh_cmd = self.proxy_ssh_cmd(vm)
        remote_abs = posixpath.join(remote_repo, rel_path)
        proxy_abs = posixpath.join(proxy_repo, rel_path)
        if path.is_dir():
            self.ssh(
                proxy_host,
                " && ".join(
                    [
                        f"ssh -i {shlex.quote(str(vm['identity_file_on_proxy']))} "
                        "-n -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new "
                        "-o ConnectTimeout=8 "
                        f"{shlex.quote(vm_host)} mkdir -p {shlex.quote(remote_abs)}",
                        (
                            "rsync -av --delete "
                            f"-e {shlex.quote(ssh_cmd)} "
                            f"{shlex.quote(proxy_abs + '/')} "
                            f"{shlex.quote(vm_host + ':' + remote_abs + '/')}"
                        ),
                    ]
                ),
                timeout=SYNC_COMMAND_TIMEOUT_SECONDS,
            )
        else:
            self.ssh(
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
                timeout=SYNC_COMMAND_TIMEOUT_SECONDS,
            )

    def should_sync_path_for_run(self, path: Path, rel_path: str, spec_path: Path) -> bool:
        return should_sync_path_for_run(path, rel_path, spec_path, self.repo_root)

    def sync_paths(self, state: dict[str, Any], item: dict[str, Any], spec_path: Path, vm: dict[str, Any]) -> None:
        paths = [
            "framework/autonomous",
            "scripts/quant_access_guard.py",
        ]
        paths.extend(state.get("default_sync_paths") or [])
        paths.extend(item.get("sync_paths") or [])
        paths.append(self.rel(spec_path))
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
        if isinstance(spec, dict):
            automation = spec.get("automation") if isinstance(spec.get("automation"), dict) else {}
            automation_sync_paths = automation.get("sync_paths") if isinstance(automation.get("sync_paths"), list) else []
            paths.extend(automation_sync_paths)
        prepared_data = spec_path.parent / "prepared_data"
        if prepared_data.exists():
            paths.append(self.rel(prepared_data))
        seen: set[str] = set()
        for path_raw in paths:
            path = Path(path_raw)
            if not path.is_absolute():
                path = self.repo_root / path
            if not path.exists():
                raise FileNotFoundError(f"sync path missing: {self.rel(path)}")
            rel_path = self.rel(path)
            if rel_path in seen:
                continue
            seen.add(rel_path)
            if not self.should_sync_path_for_run(path, rel_path, spec_path):
                self.log(f"skipped large or historical data sync path: {rel_path}")
                self.audit("sync_path_skipped", {"item_id": item.get("id"), "path": rel_path, "reason": "large_or_historical_data"})
                continue
            self.sync_one_path(state, path, rel_path, vm)

    def remote_data_quality_summary(self, spec_path: Path, vm: dict[str, Any]) -> str:
        remote_repo = str(vm["remote_repo"])
        spec_rel = self.rel(spec_path)
        cmd = (
            f"cd {shlex.quote(remote_repo)} && "
            "PY=.venv/bin/python; "
            "[ -x \"$PY\" ] || PY=python3; "
            f"$PY scripts/validate_data_quality.py --spec {shlex.quote(spec_rel)} --summary-only"
        )
        output = self.ssh_vm(vm, cmd)
        try:
            json.loads(output)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"remote data quality summary was not JSON: {exc}\n{output[-2000:]}")
        return output

    def judge_data_quality(self, summary_json: str) -> str:
        ticket = self.issue_ticket("data_quality_judge")
        env = dict(**os.environ)
        env["QUANT_AUTOMATION_ACTOR"] = "research_queue_runner"
        env["QUANT_AUTOMATION_TICKET_PATH"] = ticket["path"]
        env["QUANT_AUTOMATION_TICKET_TOKEN"] = ticket["token"]
        result = subprocess.run(
            [sys.executable, "scripts/validate_data_quality.py", "--judge-summary-stdin"],
            cwd=self.repo_root,
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

    def run_data_repair(self, spec_path: Path, decision_path: Path) -> dict[str, Any]:
        ticket = self.issue_ticket("data_quality_repair")
        env = dict(**os.environ)
        env["QUANT_AUTOMATION_ACTOR"] = "research_queue_runner"
        env["QUANT_AUTOMATION_TICKET_PATH"] = ticket["path"]
        env["QUANT_AUTOMATION_TICKET_TOKEN"] = ticket["token"]
        result = subprocess.run(
            [sys.executable, "scripts/repair_data_quality.py", "--spec", self.rel(spec_path), "--decision", self.rel(decision_path)],
            cwd=self.repo_root,
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

    def write_data_quality_decision(self, spec_path: Path, decision_text: str, summary_json: str) -> Path:
        decision = yaml.safe_load(decision_text)
        if not isinstance(decision, dict):
            raise RuntimeError("data quality decision must be YAML mapping")
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
        if not isinstance(spec, dict):
            spec = {}
        payload = {
            "schema_version": 1,
            "run_id": spec.get("run_id") or spec_path.parent.name,
            "spec_path": self.rel(spec_path),
            "validated_at": self.now_iso(),
            "summary_sha256": hashlib.sha256(summary_json.encode("utf-8")).hexdigest(),
            **decision,
        }
        path = spec_path.parent / "data_quality_decision.yaml"
        path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return path

    def validate_or_repair_data_quality(self, state: dict[str, Any], item: dict[str, Any], spec_path: Path, vm: dict[str, Any]) -> Path:
        quality_summary = self.remote_data_quality_summary(spec_path, vm)
        try:
            quality_decision = self.judge_data_quality(quality_summary)
        except DataQualityRepairCandidate as exc:
            decision_path = self.write_data_quality_decision(spec_path, exc.decision_text, exc.summary_json)
            repair_report = self.run_data_repair(spec_path, decision_path)
            self.sync_paths(state, item, spec_path, vm)
            repaired_summary = self.remote_data_quality_summary(spec_path, vm)
            repaired_decision = self.judge_data_quality(repaired_summary)
            final_path = self.write_data_quality_decision(spec_path, repaired_decision, repaired_summary)
            final_decision = yaml.safe_load(repaired_decision) or {}
            if not isinstance(final_decision, dict) or str(final_decision.get("status") or "").lower() != "pass":
                raise DataQualityBlocked(f"repaired data did not pass validation: {repaired_decision[-3000:]}")
            self.audit(
                "data_quality_repair",
                {
                    "item_id": item.get("id"),
                    "spec_path": self.rel(spec_path),
                    "decision_path": self.rel(decision_path),
                    "final_decision_path": self.rel(final_path),
                    "repair": repair_report,
                },
            )
            return final_path
        decision_path = self.write_data_quality_decision(spec_path, quality_decision, quality_summary)
        return decision_path

    def start_remote_pipeline(self, state: dict[str, Any], item: dict[str, Any], spec_path: Path, vm: dict[str, Any]) -> str:
        vm_host = str(vm["host"])
        remote_repo = str(vm["remote_repo"])
        run_dir = spec_path.parent
        remote_run_dir = f"{remote_repo}/{self.rel(run_dir)}"
        remote_spec = f"{remote_repo}/{self.rel(spec_path)}"
        cmd = (
            f"cd {shlex.quote(remote_repo)} && "
            f"mkdir -p {shlex.quote(remote_run_dir)} && "
            f"(nohup python3 scripts/auto_research_pipeline.py {shlex.quote(self.rel(spec_path))} --quiet "
            f"> {shlex.quote(remote_run_dir + '/vm_pipeline_stdout.log')} 2>&1 < /dev/null & "
            f"echo $!)"
        )
        pid = self.ssh_vm(vm, cmd).strip().splitlines()[-1].strip()
        if not pid:
            raise RuntimeError(f"remote pipeline did not return pid for {remote_spec}")
        self.log(f"started remote pipeline pid={pid} task={item.get('id') or spec_path.parent.name} vm={vm_host}")
        return pid

    def data_quality_gate_passes_locally(self, spec_path: Path) -> bool:
        if self._data_quality_gate_passes_locally_cb is not None:
            return self._data_quality_gate_passes_locally_cb(spec_path)
        from scripts.validate_data_quality import deterministic_decision, summarize_data_quality

        decision = deterministic_decision(summarize_data_quality(spec_path))
        return str(decision.get("status") or "").lower() == "pass"

    def data_quality_repair_signature(self, spec_path: Path, repair: dict[str, Any]) -> str:
        if self._data_quality_repair_signature_cb is not None:
            return self._data_quality_repair_signature_cb(spec_path, repair)
        report_raw = repair.get("data_fix_report")
        if isinstance(report_raw, str) and report_raw:
            report_path = Path(report_raw)
            if not report_path.is_absolute():
                report_path = self.repo_root / report_path
            if report_path.exists():
                return hashlib.sha256(report_path.read_bytes()).hexdigest()
        return hashlib.sha256(yaml.safe_dump(repair, sort_keys=True).encode("utf-8")).hexdigest()

    def requeue_repaired_data_items(self, state: dict[str, Any], queue: list[Any]) -> int:
        changed = 0
        for item in queue:
            if not isinstance(item, dict) or str(item.get("status") or "") not in {"failed", "blocked"}:
                continue
            spec_raw = item.get("spec_path")
            if not isinstance(spec_raw, str) or not spec_raw:
                continue
            spec_path = Path(spec_raw)
            if not spec_path.is_absolute():
                spec_path = self.repo_root / spec_path
            if not spec_path.exists():
                continue
            spec = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
            if not isinstance(spec, dict):
                continue
            repair = spec.get("data_quality_repair") if isinstance(spec.get("data_quality_repair"), dict) else {}
            if repair.get("status") != "prepared":
                continue
            repair_sig = self.data_quality_repair_signature(spec_path, repair)
            prior_requeues = int(item.get("data_quality_repair_requeue_attempts") or 0)
            if item.get("data_quality_repair_signature") != repair_sig:
                prior_requeues = 0
            if prior_requeues >= 1:
                item["status"] = "failed"
                item["failed_at"] = self.now_iso()
                item["failure_reason"] = "data repair rerun failed after prepared repair; requires system fix before rerun"
                self.mark_history(state, item, "failed", item["failure_reason"])
                changed += 1
                continue
            spec_status = str(spec.get("status") or "")
            retrying_same_repaired_item = bool(item.get("data_quality_repair_rerun")) and bool(
                self.pipeline_execution_failed(spec_path.parent)
            )
            if spec_status == "DRAFT" or (spec_status == "COMPLETE" and not retrying_same_repaired_item):
                continue
            if not self.data_quality_gate_passes_locally(spec_path):
                continue

            original_status = spec_status
            spec["status"] = "READY"
            repair["requeue_status"] = "queued_after_data_repair"
            repair["requeued_at"] = self.now_iso()
            spec["data_quality_repair"] = repair
            spec_path.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")

            previous_status = str(item.get("status") or "")
            item["status"] = "queued"
            item["requeued_at"] = self.now_iso()
            item["requeue_reason"] = "data quality repair prepared run-local data and deterministic recheck passed"
            item["data_quality_repair_rerun"] = True
            item["data_quality_repair_requeue_attempts"] = prior_requeues + 1
            item["data_quality_repair_signature"] = repair_sig
            item["previous_status_before_data_requeue"] = previous_status
            item["previous_spec_status_before_data_requeue"] = original_status
            for key in ("failed_at", "failure_reason", "blocked_at", "block_reason", "remote_pid", "avoid_vm_ids"):
                item.pop(key, None)
            self.mark_history(state, item, "queued", item["requeue_reason"])
            self.audit(
                "data_quality_requeue",
                {
                    "item_id": item.get("id"),
                    "spec_path": self.rel(spec_path),
                    "previous_status": previous_status,
                    "previous_spec_status": original_status,
                },
            )
            changed += 1
        if changed:
            state["queue"] = queue
            self.save_state(state)
        return changed

    @staticmethod
    def required_artifacts_present(spec: dict[str, Any], run_dir: Path) -> bool:
        artifacts = spec.get("artifacts_required") or []
        if not isinstance(artifacts, list):
            return False
        return all((run_dir / str(name)).exists() for name in artifacts)

    def pipeline_execution_failed(self, run_dir: Path) -> str | None:
        if self._pipeline_execution_failed_cb is not None:
            return self._pipeline_execution_failed_cb(run_dir)
        return pipeline_execution_failed(run_dir)

    def sync_remote_run_dir(self, state: dict[str, Any], item: dict[str, Any], run_dir: Path) -> None:
        if self._sync_remote_run_dir_cb is not None:
            self._sync_remote_run_dir_cb(state, item, run_dir)
            return
        vm = self.item_vm_config(state, item)
        vm_host = str(vm["host"])
        remote_repo = str(vm["remote_repo"])
        rel_dir = self.rel(run_dir)
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
            self.run(
                ["rsync", "-av", *result_filter, f"{vm_host}:{remote_repo}/{rel_dir}/", f"{rel_dir}/"],
                check=False,
                timeout=SYNC_COMMAND_TIMEOUT_SECONDS,
            )
            return
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in rel_dir)
        stage = f"/tmp/quant_proxy_pull_{safe}"
        ssh_cmd = self.proxy_ssh_cmd(vm)
        self.ssh(
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
            timeout=SYNC_COMMAND_TIMEOUT_SECONDS,
        )
        self.run(
            ["rsync", "-av", *result_filter, f"{proxy_host}:{stage}/", f"{rel_dir}/"],
            check=False,
            timeout=SYNC_COMMAND_TIMEOUT_SECONDS,
        )

    def settle_running_items(self, state: dict[str, Any], queue: list[Any]) -> int:
        running_items = [item for item in queue if isinstance(item, dict) and item.get("status") == "running"]
        if not running_items:
            return 0
        changed = 0
        for item in running_items:
            spec_raw = item.get("spec_path")
            if not isinstance(spec_raw, str) or not spec_raw:
                item["status"] = "failed"
                item["failed_at"] = self.now_iso()
                item["failure_reason"] = "running item missing spec_path"
                self.mark_history(state, item, "failed", item["failure_reason"])
                changed += 1
                continue
            spec_path = Path(spec_raw)
            if not spec_path.is_absolute():
                spec_path = self.repo_root / spec_path
            run_dir = spec_path.parent
            try:
                vm = self.item_vm_config(state, item)
                pattern = str(item.get("process_pattern") or spec_path.parent.name)
                if self.remote_running_on_vm(vm, pattern):
                    continue
                self.sync_remote_run_dir(state, item, run_dir)
                spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
                if not isinstance(spec, dict):
                    raise ValueError("spec root must be dict")
                execution_failure = self.pipeline_execution_failed(run_dir)
                if execution_failure:
                    item["status"] = "failed"
                    item["failed_at"] = self.now_iso()
                    item["failure_reason"] = execution_failure
                    self.mark_history(state, item, "failed", item["failure_reason"])
                    changed += 1
                    continue
                if self.required_artifacts_present(spec, run_dir):
                    item["workflow_stage"] = "artifacts_synced"
                    self.mark_history(state, item, "artifacts_synced", "remote run artifacts synced")
                    item["status"] = "review_pending"
                    item["review_pending_at"] = self.now_iso()
                    self.mark_history(state, item, "review_pending", "remote artifacts synced; waiting for review_memory")
                    changed += 1
                else:
                    item["status"] = "failed"
                    item["failed_at"] = self.now_iso()
                    item["failure_reason"] = "remote process exited but required artifacts are missing"
                    self.mark_history(state, item, "failed", item["failure_reason"])
                    changed += 1
            except Exception as exc:
                item["status"] = "failed"
                item["failed_at"] = self.now_iso()
                item["failure_reason"] = f"{type(exc).__name__}: {exc}"
                self.mark_history(state, item, "failed", item["failure_reason"])
                changed += 1
        if changed:
            state["queue"] = queue
            self.save_state(state)
        return changed

    def start_queued_items(
        self,
        state: dict[str, Any],
        queue: list[Any],
        *,
        settled_count: int,
        requeued_repaired_count: int,
        stale_vm_avoidance_reset_count: int,
    ) -> str:
        vm_config_list = self.vm_configs(state)
        available_vms = [vm for vm in vm_config_list if self.vm_available(vm)]
        if not available_vms:
            running_count = sum(1 for item in queue if isinstance(item, dict) and item.get("status") == "running")
            status = "waiting_remote_running" if running_count else "idle_no_available_vm"
            self.write_status(
                status,
                {
                    "running_count": running_count,
                    "settled_count": settled_count,
                    "requeued_repaired_count": requeued_repaired_count,
                    "stale_vm_avoidance_reset_count": stale_vm_avoidance_reset_count,
                    "vm_count": len(vm_config_list),
                },
            )
            return status

        started: list[dict[str, str]] = []
        skipped_unavoided: list[dict[str, Any]] = []
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
                    spec_path_for_family = self.repo_root / spec_path_for_family
                try:
                    spec_data = yaml.safe_load(spec_path_for_family.read_text(encoding="utf-8")) or {}
                    if isinstance(spec_data, dict):
                        spec_family_name = self.spec_family(spec_data)
                except Exception:
                    spec_family_name = ""
            forbidden_family = family in forbidden or spec_family_name in forbidden
            if forbidden_family and not item.get("data_quality_repair_rerun"):
                item["status"] = "blocked"
                item["blocked_at"] = self.now_iso()
                item["block_reason"] = f"family {spec_family_name or family} is forbidden"
                self.mark_history(state, item, "blocked", item["block_reason"])
                self.save_state(state)
                self.write_status("blocked_forbidden_family", {"item_id": item.get("id")})
                return "blocked_forbidden_family"
            if forbidden_family and item.get("data_quality_repair_rerun"):
                item["forbidden_family_bypass_reason"] = "same item rerun after data repair; not a new research-family proposal"
                self.audit(
                    "forbidden_family_data_repair_rerun_bypass",
                    {
                        "item_id": item.get("id"),
                        "family": family,
                        "spec_family": spec_family_name,
                    },
                )

            if not isinstance(spec_raw, str) or not spec_raw:
                item["status"] = "failed"
                item["failed_at"] = self.now_iso()
                item["failure_reason"] = "missing spec_path"
                self.mark_history(state, item, "failed", "missing spec_path")
                self.save_state(state)
                return "failed_missing_spec_path"

            spec_path = Path(spec_raw)
            if not spec_path.is_absolute():
                spec_path = self.repo_root / spec_path
            vm = self.choose_vm_for_item(available_vms, item)
            if vm is None:
                skipped_unavoided.append(
                    {
                        "item_id": item.get("id"),
                        "avoid_vm_ids": item.get("avoid_vm_ids", []),
                        "available_vm_ids": [vm.get("id") for vm in available_vms],
                    }
                )
                item["workflow_stage"] = "waiting_unavoided_vm"
                self.mark_history(state, item, "queued", "all currently available VMs are in avoid list; skipped for this tick")
                self.save_state(state)
                continue
            try:
                self.audit("compile", {"item_id": item.get("id"), "spec_path": self.rel(spec_path)})
                self.validate_spec(spec_path)
                item["workflow_stage"] = "spec_validated"
                self.write_status(
                    "syncing_to_vm",
                    {
                        "item_id": item.get("id"),
                        "vm_id": vm.get("id"),
                        "vm_host": vm.get("host"),
                    },
                )
                self.sync_paths(state, item, spec_path, vm)
                item["workflow_stage"] = "synced_to_vm"
                self.write_status(
                    "checking_remote_data_quality",
                    {
                        "item_id": item.get("id"),
                        "vm_id": vm.get("id"),
                        "vm_host": vm.get("host"),
                    },
                )
                decision_path = self.validate_or_repair_data_quality(state, item, spec_path, vm)
                item["workflow_stage"] = "data_checked"
                self.sync_one_path(state, decision_path, self.rel(decision_path), vm)
                self.audit(
                    "data_quality_pass",
                    {
                        "item_id": item.get("id"),
                        "spec_path": self.rel(spec_path),
                        "vm_id": vm.get("id"),
                        "decision_path": self.rel(decision_path),
                    },
                )
                self.write_status(
                    "starting_remote_pipeline",
                    {
                        "item_id": item.get("id"),
                        "vm_id": vm.get("id"),
                        "vm_host": vm.get("host"),
                    },
                )
                pid = self.start_remote_pipeline(state, item, spec_path, vm)
                self.audit("run", {"item_id": item.get("id"), "spec_path": self.rel(spec_path), "vm_id": vm.get("id"), "remote_pid": pid})
            except Exception as exc:
                if isinstance(exc, DataQualityBlocked):
                    item["status"] = "blocked"
                    item["blocked_at"] = self.now_iso()
                    item["block_reason"] = f"data_quality_blocked: {exc}"
                    self.mark_history(state, item, "blocked", item["block_reason"])
                    self.save_state(state)
                    self.write_status(
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
                    item["last_start_error_at"] = self.now_iso()
                    item["last_start_error"] = f"{type(exc).__name__}: {exc}"
                    item["start_retry_count"] = int(item.get("start_retry_count") or 0) + 1
                    self.mark_history(state, item, "queued", f"transient VM start error on {vm.get('id')}; requeued")
                    self.save_state(state)
                    self.write_status(
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
                item["failed_at"] = self.now_iso()
                item["failure_reason"] = f"{type(exc).__name__}: {exc}"
                self.mark_history(state, item, "failed", item["failure_reason"])
                self.save_state(state)
                self.write_status("failed", {"item_id": item.get("id"), "error": item["failure_reason"]})
                raise

            item["status"] = "running"
            item["workflow_stage"] = "running_remote"
            item["started_at"] = self.now_iso()
            item["remote_pid"] = pid
            item["vm_id"] = vm["id"]
            item["vm_host"] = vm["host"]
            item["remote_repo"] = vm["remote_repo"]
            item.pop("avoid_vm_ids", None)
            item.pop("last_start_error", None)
            item.pop("last_start_error_at", None)
            self.mark_history(state, item, "running", f"started remote pid {pid} on {vm['id']}")
            self.save_state(state)
            started.append({"item_id": str(item.get("id")), "remote_pid": pid, "vm_id": vm["id"], "vm_host": vm["host"]})

        if started:
            self.write_status("running_remote", {"started": started, "started_count": len(started)})
            return f"started_{len(started)}"
        if settled_count:
            self.write_status("settled_running_items", {"settled_count": settled_count})
            return "settled_running_items"
        if requeued_repaired_count:
            self.write_status("requeued_repaired_data_items", {"requeued_repaired_count": requeued_repaired_count})
            return "requeued_repaired_data_items"
        if stale_vm_avoidance_reset_count:
            self.write_status("stale_vm_avoidance_reset", {"stale_vm_avoidance_reset_count": stale_vm_avoidance_reset_count})
            return "stale_vm_avoidance_reset"
        if skipped_unavoided:
            self.write_status("waiting_unavoided_vm", {"skipped": skipped_unavoided})
            return "waiting_unavoided_vm"
        self.write_status("idle_no_queued_specs")
        return "idle_no_queued_specs"
