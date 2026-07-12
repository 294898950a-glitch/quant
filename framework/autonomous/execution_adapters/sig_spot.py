"""SIG/Spot transport adapter; it owns launch mechanics, never queue state."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Callable

import yaml

from .base import ExecutionHandle


class SigSpotExecutionAdapter:
    executor_id = "sig_spot"

    def __init__(self, *, repo_root: Path, rel: Callable[[Path], str], ssh_vm: Callable[..., str], vm: dict):
        self.repo_root = repo_root
        self.rel = rel
        self.ssh_vm = ssh_vm
        self.vm = vm

    def submit(self, request_path: Path) -> ExecutionHandle:
        request = yaml.safe_load(request_path.read_text(encoding="utf-8")) or {}
        run_id = str(request["run_id"])
        nonce = str(request["request_nonce"])
        remote_repo = str(self.vm["remote_repo"])
        spec_rel = str(request.get("spec_path") or "spec.yaml")
        remote_run_dir = f"{remote_repo}/{self.rel(request_path.parent)}"
        remote_spec = f"{self.rel(request_path.parent)}/{spec_rel}"
        command = (
            f"cd {shlex.quote(remote_repo)} && "
            f"mkdir -p {shlex.quote(remote_run_dir)} && "
            f'PY=.venv/bin/python; [ -x "$PY" ] || PY=python3; '
            f"(nohup env QUANT_COMPUTE_NODE=1 $PY scripts/auto_research_pipeline.py {shlex.quote(remote_spec)} --quiet "
            f"> {shlex.quote(remote_run_dir + '/vm_pipeline_stdout.log')} 2>&1 < /dev/null & echo $!)"
        )
        pid = self.ssh_vm(self.vm, command).strip().splitlines()[-1].strip()
        if not pid:
            raise RuntimeError(f"remote pipeline did not return pid for {run_id}")
        return ExecutionHandle(run_id=run_id, request_nonce=nonce, executor_id=self.executor_id, remote_handle=pid)

    def probe(self, handle: ExecutionHandle) -> bool:
        command = f"kill -0 {shlex.quote(handle.remote_handle)} 2>/dev/null && printf alive || printf dead"
        return self.ssh_vm(self.vm, command, check=False).strip() == "alive"

    def collect(self, handle: ExecutionHandle, destination: Path) -> Path | None:
        return destination

    def cancel(self, handle: ExecutionHandle) -> None:
        self.ssh_vm(self.vm, f"kill {shlex.quote(handle.remote_handle)} 2>/dev/null || true", check=False)
