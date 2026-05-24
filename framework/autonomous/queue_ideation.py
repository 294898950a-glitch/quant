"""Queue-facing ideation service.

This module owns the boundary between the mechanical queue and the AI ideation
entrypoint. The queue runner may ask for one actionable spec, but it should not
parse provider failures or enqueue READY specs itself.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import yaml


EXCLUDE_STATUSES = {"DRAFT", "REJECT", "PROPOSAL_ONLY"}
RETRYABLE_IDEATION_RESULTS = {
    "ideation_not_runnable",
    "ideation_timeout",
    "executor_tool_response_invalid",
}

# Statuses inside executor_tool_package that mark a DRAFT proposal as a
# valid pending-capability case: ideation_cycle has already invoked
# request_executor_tool_code, the executor code is being drafted, and
# hermes_executor_handoff_tick.py will pick it up via find_pending_tool_draft.
# A DRAFT in any of these states must NOT be suppressed as non-runnable —
# it is "accepted_pending_capability", not "skipped_non_runnable".
PENDING_CAPABILITY_PACKAGE_STATUSES = {
    "awaiting_hermes_executor_code",
    "draft_tool_code",
    "draft_tool_code_compile_not_ready",
    "draft_tool_design_pending_code",
}


def _is_draft_pending_capability(payload: dict[str, Any]) -> bool:
    """Detect "DRAFT + missing_capability_request" — a valid pending-capability proposal.

    Distinguishes the "Hermes proposed something valid but no executor exists yet,
    and ideation_cycle has already kicked off executor-code drafting via
    request_executor_tool_code" case from genuine non-runnable garbage (malformed
    proposals, REJECT, PROPOSAL_ONLY without any pending tool work).

    Signal: payload.executor_tool_package.status is one of the
    PENDING_CAPABILITY_PACKAGE_STATUSES. If ideation_cycle did not produce a
    package (or it landed in a terminal error state), this returns False and
    suppression proceeds as before.
    """
    if not isinstance(payload, dict):
        return False
    if str(payload.get("status") or "") != "DRAFT":
        return False
    package = payload.get("executor_tool_package")
    if not isinstance(package, dict):
        return False
    status = str(package.get("status") or "")
    return status in PENDING_CAPABILITY_PACKAGE_STATUSES


class QueueIdeationService:
    def __init__(
        self,
        *,
        repo_root: Path,
        load_state: Callable[[], dict[str, Any]],
        save_state: Callable[[dict[str, Any]], None],
        write_status: Callable[[str, dict[str, Any] | None], None],
        audit: Callable[[str, dict[str, Any] | None], None],
        log: Callable[[str], None],
        mark_history: Callable[[dict[str, Any], dict[str, Any], str, str], None],
        rel: Callable[[Path], str],
        now_iso: Callable[[], str],
        ideation_env: Callable[[], dict[str, str]],
        timeout_seconds: int = 180,
    ) -> None:
        self.repo_root = repo_root
        self.load_state = load_state
        self.save_state = save_state
        self.write_status = write_status
        self.audit = audit
        self.log = log
        self.mark_history = mark_history
        self.rel = rel
        self.now_iso = now_iso
        self.ideation_env = ideation_env
        self.timeout_seconds = timeout_seconds

    def enqueue_ready_spec(self, state: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        spec_raw = payload.get("spec_path")
        if not spec_raw:
            raise ValueError("ideation returned READY without spec_path")
        spec_path = Path(str(spec_raw))
        if not spec_path.is_absolute():
            spec_path = self.repo_root / spec_path
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
        if not isinstance(spec, dict):
            raise ValueError(f"{self.rel(spec_path)} root must be dict")
        run_id = str(spec.get("run_id") or spec_path.parent.name)
        queue = state.get("queue")
        if not isinstance(queue, list):
            raise ValueError("research_queue.queue must be list")
        for item in queue:
            if isinstance(item, dict) and (item.get("id") == run_id or item.get("spec_path") == self.rel(spec_path)):
                return {"item_id": run_id, "spec_path": self.rel(spec_path), "already_present": True}
        automation = spec.get("automation") if isinstance(spec.get("automation"), dict) else {}
        item = {
            "id": run_id,
            "family": str(spec.get("family") or (spec.get("proposal") or {}).get("family") or "auto_deepseek_ideation"),
            "status": "queued",
            "spec_path": self.rel(spec_path),
            "process_pattern": run_id,
            "sync_paths": automation.get("sync_paths") if isinstance(automation.get("sync_paths"), list) else [],
            "discovered_at": self.now_iso(),
            "source": "project_owned_ideation",
        }
        queue.append(item)
        state["queue"] = queue
        escalation = state.get("escalation")
        if isinstance(escalation, dict) and escalation.get("status") == "blocked_awaiting_user":
            escalation["status"] = "auto_resolved"
            escalation["resolved_at"] = self.now_iso()
            escalation["resolution_reason"] = "old directions exhausted; registered provider available for new project-owned ideation"
        self.save_state(state)
        self.mark_history(state, item, "queued", "queued READY spec generated by project-owned ideation")
        self.save_state(state)
        return {"item_id": run_id, "spec_path": self.rel(spec_path), "already_present": False}

    def generate_next_spec_if_idle(self, state: dict[str, Any]) -> str:
        self.write_status(
            "generating_research_direction",
            {
                "note": "No queued READY specs. Project-owned ideation is generating one candidate through the registered provider.",
            },
        )
        try:
            result = subprocess.run(
                [sys.executable, "scripts/run_strategy_ideation_once.py"],
                cwd=self.repo_root,
                env=self.ideation_env(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            self.write_status(
                "ideation_timeout",
                {
                    "timeout_seconds": self.timeout_seconds,
                    "output": output[-4000:],
                    "note": "Provider or rewrite loop timed out; next tick may try again.",
                },
            )
            self.audit("ideation_timeout", {"timeout_seconds": self.timeout_seconds, "output": output[-4000:]})
            self.log(f"ideation timed out after {self.timeout_seconds} seconds")
            return "ideation_timeout"
        output = result.stdout or ""
        if result.returncode != 0:
            return self._handle_failed_ideation(result.returncode, output)
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            self.write_status("ideation_failed", {"error": f"JSONDecodeError: {exc}", "output": output[-4000:]})
            self.audit("ideation_failed", {"error": f"JSONDecodeError: {exc}"})
            return "ideation_failed"
        status = str(payload.get("status") or "")
        self.audit("ideate", {"status": status, "proposal_id": payload.get("proposal_id"), "spec_path": payload.get("spec_path")})
        if status == "READY":
            item = self.enqueue_ready_spec(state, payload)
            self.write_status("queued_ideation_spec", item)
            return "queued_ideation_spec"
        if _is_draft_pending_capability(payload):
            return self._accept_pending_capability(payload)
        self.suppress_non_runnable_draft(payload, status)
        self.write_status("ideation_not_runnable", self._non_runnable_payload(status, payload))
        return "ideation_not_runnable"

    def generate_until_actionable(self, state: dict[str, Any], *, max_attempts: int = 5) -> str:
        last_result = "ideation_not_runnable"
        attempts: list[str] = []
        for attempt_index in range(max_attempts):
            state = self.load_state()
            result = self.generate_next_spec_if_idle(state)
            attempts.append(result)
            last_result = result
            if result == "queued_ideation_spec":
                break
            if result not in RETRYABLE_IDEATION_RESULTS:
                break
            self.audit(
                "ideation_retry",
                {
                    "attempt": attempt_index + 1,
                    "max_attempts": max_attempts,
                    "result": result,
                    "reason": "candidate was not actionable; trying another direction in the same tick",
                },
            )
        if len(attempts) > 1:
            self.audit("ideation_attempts", {"attempts": attempts, "final_result": last_result})
        if attempts and last_result != "queued_ideation_spec" and all(
            result in RETRYABLE_IDEATION_RESULTS for result in attempts
        ):
            self.write_status(
                "ideation_attempts_exhausted",
                {
                    "ideation_attempts": attempts,
                    "max_attempts": max_attempts,
                    "note": "No actionable candidate was produced in this tick; next tick will retry.",
                },
            )
        return last_result

    def _handle_failed_ideation(self, returncode: int, output: str) -> str:
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            payload = {}
        payload_status = str(payload.get("status") or "")
        if payload_status in EXCLUDE_STATUSES:
            self.audit(
                "ideate",
                {
                    "status": payload_status,
                    "proposal_id": payload.get("proposal_id"),
                    "spec_path": payload.get("spec_path"),
                    "reason": payload.get("reason"),
                },
            )
            if _is_draft_pending_capability(payload):
                return self._accept_pending_capability(payload)
            self.suppress_non_runnable_draft(payload, payload_status)
            self.write_status("ideation_not_runnable", self._non_runnable_payload(payload_status, payload))
            return "ideation_not_runnable"
        if "executor_tool_code" in output or "returned empty content" in output:
            self.write_status(
                "executor_tool_response_invalid",
                {
                    "returncode": returncode,
                    "reason": "tool-code AI response was empty or invalid",
                    "output": output[-4000:],
                },
            )
            self.audit("executor_tool_response_invalid", {"returncode": returncode, "output": output[-4000:]})
            return "executor_tool_response_invalid"
        self.write_status("ideation_failed", {"returncode": returncode, "output": output[-4000:]})
        self.audit("ideation_failed", {"returncode": returncode, "output": output[-4000:]})
        self.log(f"ideation failed rc={returncode}: {output[-1000:]}")
        return "ideation_failed"

    def _accept_pending_capability(self, payload: dict[str, Any]) -> str:
        """Accept a DRAFT + missing_capability_request proposal as a valid pending draft.

        Does NOT suppress. Does NOT touch executor_tool_request.status (it is
        already in a pending-capability state set by ideation_cycle.request_executor_tool_code).
        Records an audit event and writes a non-retryable status so the ideation
        loop does not retry; hermes_executor_handoff_tick.py will drive the
        executor-code drafting on its own cadence.

        Dedupe: writes a persistent ``ideation_accept_marker.yaml`` in the run
        directory on the first accept. Subsequent calls on the same run with
        the same package_status see the marker and emit a noop event instead of
        a fresh accept. The marker is the source of truth for "we have already
        decided to accept this proposal-pending-capability" so that retries
        across ticks are idempotent.
        """
        package = payload.get("executor_tool_package")
        package_status = str(package.get("status") or "") if isinstance(package, dict) else ""
        spec_path = payload.get("spec_path")
        proposal_id = payload.get("proposal_id")
        accept_payload = {
            "proposal_id": proposal_id,
            "spec_path": spec_path,
            "package_status": package_status,
            "reason": payload.get("reason"),
            "note": (
                "DRAFT proposal carries a valid executor_tool_request in a "
                "pending-capability state; hermes_executor_handoff_tick.py will "
                "draft the executor code on its own cadence."
            ),
        }
        marker_path = self._accept_marker_path(spec_path)
        if marker_path is not None and marker_path.exists():
            try:
                marker = yaml.safe_load(marker_path.read_text(encoding="utf-8")) or {}
            except Exception:
                marker = {}
            if isinstance(marker, dict) and str(marker.get("package_status") or "") == package_status:
                self.audit(
                    "ideation_pending_capability_noop",
                    {**accept_payload, "dedupe": "accept marker exists with same package_status"},
                )
                self.write_status("ideation_accepted_pending_capability", accept_payload)
                return "ideation_accepted_pending_capability"
        if marker_path is not None:
            try:
                marker_path.write_text(
                    yaml.safe_dump(
                        {
                            "accepted_at": self.now_iso(),
                            "proposal_id": proposal_id,
                            "package_status": package_status,
                            "reason": payload.get("reason"),
                        },
                        allow_unicode=True,
                        sort_keys=False,
                    ),
                    encoding="utf-8",
                )
            except Exception:
                pass
        self.audit("ideation_accepted_pending_capability", accept_payload)
        self.write_status("ideation_accepted_pending_capability", accept_payload)
        return "ideation_accepted_pending_capability"

    def _accept_marker_path(self, spec_path: Any) -> Path | None:
        """Resolve the run dir's accept-marker path, or None if spec_path missing."""
        if not spec_path:
            return None
        spec_p = Path(str(spec_path))
        if not spec_p.is_absolute():
            spec_p = self.repo_root / spec_p
        return spec_p.parent / "ideation_accept_marker.yaml"

    @staticmethod
    def _non_runnable_payload(status: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "proposal_status": status,
            "proposal_id": payload.get("proposal_id"),
            "spec_path": payload.get("spec_path"),
            "implementation_plan_path": payload.get("implementation_plan_path"),
            "reason": payload.get("reason"),
            "errors": payload.get("errors"),
        }

    def suppress_non_runnable_draft(self, payload: dict[str, Any], status: str) -> None:
        """Mark a generated non-runnable draft so this tick can move to a new idea.

        DRAFT specs remain available as research artifacts, but they should not be
        re-selected as the next pending tool task when the current automation loop
        is trying to produce one runnable queue item.
        """
        spec_raw = payload.get("spec_path")
        if not spec_raw:
            return
        spec_path = Path(str(spec_raw))
        if not spec_path.is_absolute():
            spec_path = self.repo_root / spec_path
        if not spec_path.exists():
            return
        try:
            spec = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
        except Exception:
            return
        if not isinstance(spec, dict) or str(spec.get("status") or "") != "DRAFT":
            return
        skip_payload = {
            "status": "skipped_non_runnable",
            "at": self.now_iso(),
            "proposal_status": status,
            "reason": payload.get("reason"),
            "errors": payload.get("errors"),
        }
        spec["automation_skip"] = skip_payload
        spec_path.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
        request_path = spec_path.parent / "executor_tool_request.yaml"
        if request_path.exists():
            try:
                request = yaml.safe_load(request_path.read_text(encoding="utf-8")) or {}
            except Exception:
                request = {}
            if isinstance(request, dict):
                request["status_before_automation_skip"] = request.get("status")
                request["status"] = "automation_skipped_non_runnable"
                request["automation_skip"] = skip_payload
                request_path.write_text(
                    yaml.safe_dump(request, allow_unicode=True, sort_keys=False),
                    encoding="utf-8",
                )
        self.audit(
            "ideation_non_runnable_suppressed",
            {
                "proposal_id": payload.get("proposal_id"),
                "spec_path": self.rel(spec_path),
                "status": status,
                "reason": payload.get("reason"),
            },
        )
