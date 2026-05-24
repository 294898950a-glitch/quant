from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from framework.autonomous.hermes_executor_handoff import (
    cancel_task,
    claim_task,
    complete_task,
    finalize_task_if_valid,
    format_wake_output,
    wake_once,
)


def _write_handoff(root: Path, task: dict) -> None:
    path = root / "data" / "research_framework" / "hermes_executor_handoffs.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"schema_version": 1, "tasks": [task]}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def test_wake_once_skips_agent_when_no_open_handoff(tmp_path: Path) -> None:
    payload = wake_once(tmp_path)

    assert payload == {"wakeAgent": False, "reason": "no_open_hermes_executor_handoff"}
    assert json.loads(format_wake_output(payload))["wakeAgent"] is False


def test_wake_once_exposes_only_bounded_handoff(tmp_path: Path) -> None:
    descriptor = tmp_path / "data" / "run_a" / "executor_tool_request.yaml"
    descriptor.parent.mkdir(parents=True)
    descriptor.write_text("status: awaiting_hermes_executor_code\n", encoding="utf-8")
    _write_handoff(
        tmp_path,
        {
            "id": "run_a_executor_code",
            "status": "open",
            "run_dir": str(tmp_path / "data" / "run_a"),
            "descriptor_path": str(descriptor),
            "target_script_path": "scripts/evaluate_cb_arb_profit_take_exit.py",
        },
    )

    payload = wake_once(tmp_path)

    assert payload["wakeAgent"] is True
    task = payload["tasks"][0]
    assert task["id"] == "run_a_executor_code"
    assert task["wakeup_count"] == 1
    assert "boundary" in task
    assert "scripts/research_queue_runner.py" in "\n".join(task["boundary"]["forbidden_actions"])
    assert str(descriptor) in task["boundary"]["allowed_writes"]
    assert format_wake_output(payload).splitlines()[-1].startswith('{"wakeAgent": true')


def test_wake_once_does_not_wake_hermes_when_quant_queue_is_active(tmp_path: Path) -> None:
    descriptor = tmp_path / "data" / "run_a" / "executor_tool_request.yaml"
    descriptor.parent.mkdir(parents=True)
    descriptor.write_text("status: awaiting_hermes_executor_code\n", encoding="utf-8")
    _write_handoff(
        tmp_path,
        {
            "id": "run_a_executor_code",
            "status": "open",
            "run_dir": str(tmp_path / "data" / "run_a"),
            "descriptor_path": str(descriptor),
        },
    )
    queue_path = tmp_path / "data" / "research_framework" / "research_queue.yaml"
    queue_path.write_text(
        yaml.safe_dump(
            {
                "queue": [
                    {
                        "id": "main_task",
                        "status": "running",
                    }
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    payload = wake_once(tmp_path)

    assert payload["wakeAgent"] is False
    assert payload["reason"] == "quant_workflow_active"
    assert payload["active"]["item_id"] == "main_task"


def test_cancel_task_removes_handoff_from_wake_queue(tmp_path: Path) -> None:
    _write_handoff(
        tmp_path,
        {
            "id": "run_a_executor_code",
            "status": "open",
            "run_dir": str(tmp_path / "data" / "run_a"),
            "descriptor_path": str(tmp_path / "data" / "run_a" / "executor_tool_request.yaml"),
        },
    )

    result = cancel_task("run_a_executor_code", tmp_path, actor="test", reason="obsolete")
    payload = wake_once(tmp_path)

    assert result["status"] == "cancelled"
    assert payload == {"wakeAgent": False, "reason": "no_open_hermes_executor_handoff"}
    doc = yaml.safe_load(
        (tmp_path / "data" / "research_framework" / "hermes_executor_handoffs.yaml").read_text(encoding="utf-8")
    )
    assert doc["tasks"][0]["cancel_reason"] == "obsolete"


def test_claim_and_complete_require_registered_completion_receipt(tmp_path: Path) -> None:
    descriptor = tmp_path / "data" / "run_a" / "executor_tool_request.yaml"
    descriptor.parent.mkdir(parents=True)
    descriptor.write_text("status: awaiting_hermes_executor_code\n", encoding="utf-8")
    _write_handoff(
        tmp_path,
        {
            "id": "run_a_executor_code",
            "status": "open",
            "run_dir": str(tmp_path / "data" / "run_a"),
            "descriptor_path": str(descriptor),
        },
    )

    assert claim_task("run_a_executor_code", tmp_path, actor="hermes")["status"] == "claimed"
    not_ready = complete_task("run_a_executor_code", tmp_path, actor="hermes")
    assert not_ready["status"] == "descriptor_not_ready"

    descriptor.write_text("status: draft_tool_code\n", encoding="utf-8")
    done = complete_task("run_a_executor_code", tmp_path, actor="hermes")
    assert done["status"] == "descriptor_not_ready"

    doc = yaml.safe_load(
        (tmp_path / "data" / "research_framework" / "hermes_executor_handoffs.yaml").read_text(encoding="utf-8")
    )
    assert doc["tasks"][0]["status"] == "open"


def _write_valid_completion_receipt(run_dir: Path, handoff_id: str, generated_name: str) -> None:
    (run_dir / "generated_executor" / "executor_completion.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "handoff_id": handoff_id,
                "generated_executor": f"generated_executor/{generated_name}",
                "completed_by": "hermes",
                "checks": {
                    "compile_passed": True,
                    "has_main": True,
                    "has_declare_data_requirements": True,
                    "writes_summary_json": True,
                    "summary_has_adoption_pass": True,
                    "writes_report_yaml": True,
                    "writes_l4_ack_yaml": True,
                    "writes_diagnostic_yaml": True,
                    "no_forbidden_markers": True,
                    "imports_gatekeeper": True,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_wake_once_reopens_stale_claim(tmp_path: Path) -> None:
    stale_time = (datetime.now() - timedelta(minutes=20)).isoformat(timespec="seconds")
    descriptor = tmp_path / "data" / "run_a" / "executor_tool_request.yaml"
    descriptor.parent.mkdir(parents=True)
    descriptor.write_text("status: awaiting_hermes_executor_code\n", encoding="utf-8")
    _write_handoff(
        tmp_path,
        {
            "id": "run_a_executor_code",
            "status": "claimed",
            "claimed_at": stale_time,
            "run_dir": str(tmp_path / "data" / "run_a"),
            "descriptor_path": str(descriptor),
        },
    )

    payload = wake_once(tmp_path)

    assert payload["wakeAgent"] is True
    task = payload["tasks"][0]
    assert task["status"] == "open"
    assert task["retry_count"] == 1
    assert task["previous_errors"]


def test_finalize_task_if_valid_registers_completed_generated_executor(tmp_path: Path) -> None:
    run_dir = tmp_path / "data" / "run_a"
    descriptor = run_dir / "executor_tool_request.yaml"
    generated = run_dir / "generated_executor" / "evaluate_cb_arb_profit_take_exit.py"
    generated.parent.mkdir(parents=True)
    descriptor.write_text("status: awaiting_hermes_executor_code\n", encoding="utf-8")
    generated.write_text(
        "\n".join(
            [
                "from scripts.gatekeeper import GateKeeper",
                "",
                "def declare_data_requirements(command, spec=None):",
                "    return {'required_files': []}",
                "",
                "def main():",
                "    names = ['summary.json', 'report.yaml', 'l4_ack.yaml', 'diagnostic.yaml', 'adoption_pass']",
                "    return len(names)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    _write_valid_completion_receipt(run_dir, "run_a_executor_code", generated.name)
    _write_handoff(
        tmp_path,
        {
            "id": "run_a_executor_code",
            "status": "claimed",
            "run_dir": str(run_dir),
            "descriptor_path": str(descriptor),
            "target_script_path": "scripts/evaluate_cb_arb_profit_take_exit.py",
        },
    )

    result = finalize_task_if_valid("run_a_executor_code", tmp_path, actor="test")

    assert result["status"] == "completed"
    package = yaml.safe_load(descriptor.read_text(encoding="utf-8"))
    assert package["status"] == "draft_tool_code"
    assert package["written_files"] == ["generated_executor/evaluate_cb_arb_profit_take_exit.py"]
    assert package["tool_code_response"]["completion_receipt"].endswith("executor_completion.yaml")
    assert "def main" in package["files"][0]["content"]
    doc = yaml.safe_load(
        (tmp_path / "data" / "research_framework" / "hermes_executor_handoffs.yaml").read_text(encoding="utf-8")
    )
    assert doc["tasks"][0]["status"] == "completed"


def test_finalize_requires_completion_receipt(tmp_path: Path) -> None:
    run_dir = tmp_path / "data" / "run_a"
    descriptor = run_dir / "executor_tool_request.yaml"
    generated = run_dir / "generated_executor" / "evaluate_cb_arb_profit_take_exit.py"
    generated.parent.mkdir(parents=True)
    descriptor.write_text("status: awaiting_hermes_executor_code\n", encoding="utf-8")
    generated.write_text(
        "\n".join(
            [
                "from scripts.gatekeeper import GateKeeper",
                "",
                "def declare_data_requirements(command, spec=None):",
                "    return {'required_files': []}",
                "",
                "def main():",
                "    names = ['summary.json', 'report.yaml', 'l4_ack.yaml', 'diagnostic.yaml', 'adoption_pass']",
                "    return len(names)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    _write_handoff(
        tmp_path,
        {
            "id": "run_a_executor_code",
            "status": "claimed",
            "run_dir": str(run_dir),
            "descriptor_path": str(descriptor),
            "target_script_path": "scripts/evaluate_cb_arb_profit_take_exit.py",
        },
    )

    result = finalize_task_if_valid("run_a_executor_code", tmp_path, actor="test")

    assert result["status"] == "validation_failed"
    assert any("completion receipt" in error for error in result["errors"])


def test_complete_task_auto_registers_from_completion_receipt(tmp_path: Path) -> None:
    run_dir = tmp_path / "data" / "run_a"
    descriptor = run_dir / "executor_tool_request.yaml"
    generated = run_dir / "generated_executor" / "evaluate_cb_arb_profit_take_exit.py"
    generated.parent.mkdir(parents=True)
    descriptor.write_text("status: awaiting_hermes_executor_code\n", encoding="utf-8")
    generated.write_text(
        "\n".join(
            [
                "from scripts.gatekeeper import GateKeeper",
                "",
                "def declare_data_requirements(command, spec=None):",
                "    return {'required_files': []}",
                "",
                "def main():",
                "    names = ['summary.json', 'report.yaml', 'l4_ack.yaml', 'diagnostic.yaml', 'adoption_pass']",
                "    return len(names)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    _write_valid_completion_receipt(run_dir, "run_a_executor_code", generated.name)
    _write_handoff(
        tmp_path,
        {
            "id": "run_a_executor_code",
            "status": "claimed",
            "run_dir": str(run_dir),
            "descriptor_path": str(descriptor),
            "target_script_path": "scripts/evaluate_cb_arb_profit_take_exit.py",
        },
    )

    done = complete_task("run_a_executor_code", tmp_path, actor="hermes")

    assert done["status"] == "completed"
    package = yaml.safe_load(descriptor.read_text(encoding="utf-8"))
    assert package["status"] == "draft_tool_code"


# ---------------------------------------------------------------------------
# Pickable-status repair-flow tests (2026-05-25)
#
# After install_generated_executors flips a noncompliant task to
# status="needs_compliance_repair", the handoff layer must pick it back up
# and let Hermes re-claim it. These four tests pin down the exact contract:
#
#   1. needs_compliance_repair IS pickable (open_tasks + wake_once both see it,
#      and claim_task succeeds and transitions it to "claimed")
#   2. completed / installed are terminal and NOT pickable
#   3. failed is terminal and NOT pickable
#   4. stale claim recovery (the original "open" + STALE_CLAIM_MINUTES path)
#      is unchanged
# ---------------------------------------------------------------------------


from framework.autonomous.hermes_executor_handoff import (  # noqa: E402
    HANDOFF_PICKABLE_STATUSES,
    open_tasks,
)


def test_needs_compliance_repair_is_pickable_open_tasks(tmp_path: Path) -> None:
    """open_tasks must surface tasks whose install path flipped them to
    needs_compliance_repair, not just status==open."""
    _write_handoff(
        tmp_path,
        {
            "id": "task_a_executor_code",
            "status": "needs_compliance_repair",
            "compliance_errors": ["compliance_failed: missing GateKeeper import"],
            "compliance_failed_at": "2026-05-25T00:00:00",
            "target_script_path": "scripts/evaluate_a.py",
            "run_dir": str(tmp_path / "data" / "task_a"),
        },
    )

    picked = open_tasks(tmp_path)
    assert len(picked) == 1
    assert picked[0]["id"] == "task_a_executor_code"
    assert picked[0]["status"] == "needs_compliance_repair"


def test_needs_compliance_repair_is_pickable_wake_once_and_claim(tmp_path: Path) -> None:
    """End-to-end: wake_once selects the repair task, then claim_task
    transitions it from needs_compliance_repair → claimed."""
    run_dir = tmp_path / "data" / "task_b"
    run_dir.mkdir(parents=True)
    descriptor = run_dir / "executor_tool_request.yaml"
    descriptor.write_text(
        yaml.safe_dump({"status": "needs_compliance_repair"}, sort_keys=False),
        encoding="utf-8",
    )
    _write_handoff(
        tmp_path,
        {
            "id": "task_b_executor_code",
            "status": "needs_compliance_repair",
            "compliance_errors": ["compliance_failed: missing GateKeeper import"],
            "compliance_repair_request": str(run_dir / "generated_executor" / "compliance_repair_request.yaml"),
            "target_script_path": "scripts/evaluate_b.py",
            "run_dir": str(run_dir),
            "descriptor_path": str(descriptor),
        },
    )

    payload = wake_once(tmp_path)
    assert payload["wakeAgent"] is True, (
        f"wake_once must surface needs_compliance_repair tasks; got {payload}"
    )
    assert payload["reason"] == "open_hermes_executor_handoff"
    assert any(t["id"] == "task_b_executor_code" for t in payload.get("tasks") or [])

    claim = claim_task("task_b_executor_code", tmp_path, actor="hermes_repair_worker")
    assert claim["status"] == "claimed"
    # Persisted state should now show the task as claimed by the repair worker.
    handoffs = yaml.safe_load(
        (tmp_path / "data" / "research_framework" / "hermes_executor_handoffs.yaml").read_text(encoding="utf-8")
    )
    task = handoffs["tasks"][0]
    assert task["status"] == "claimed"
    assert task["claimed_by"] == "hermes_repair_worker"


def test_completed_and_installed_are_not_pickable(tmp_path: Path) -> None:
    """Terminal statuses (completed, installed) must NOT be re-picked.
    Otherwise install_generated_executors would keep re-running successful
    handoffs."""
    _write_handoff(
        tmp_path,
        {
            "id": "task_c_executor_code",
            "status": "completed",
            "installed_at": "2026-05-24T07:35:01+00:00",
            "target_script_path": "scripts/evaluate_c.py",
        },
    )
    assert open_tasks(tmp_path) == []

    # Sanity: claim_task on a completed task must refuse.
    claim = claim_task("task_c_executor_code", tmp_path, actor="hermes")
    assert claim["status"] == "not_claimable"


def test_failed_is_not_pickable(tmp_path: Path) -> None:
    """Terminal failure must NOT be re-picked. Only an explicit repair
    status (e.g. needs_compliance_repair) should make a task pickable again."""
    _write_handoff(
        tmp_path,
        {
            "id": "task_d_executor_code",
            "status": "failed",
            "target_script_path": "scripts/evaluate_d.py",
        },
    )
    assert open_tasks(tmp_path) == []
    claim = claim_task("task_d_executor_code", tmp_path, actor="hermes")
    assert claim["status"] == "not_claimable"


def test_stale_claim_recovery_unchanged(tmp_path: Path) -> None:
    """The original stale-claim → open recovery path must still work after
    the pickable-status refactor. A claimed task whose claimed_at is older
    than STALE_CLAIM_MINUTES should be surfaced by open_tasks AND by
    wake_once (which also reopens it)."""
    long_ago = (datetime.now() - timedelta(minutes=60)).isoformat(timespec="seconds")
    _write_handoff(
        tmp_path,
        {
            "id": "task_e_executor_code",
            "status": "claimed",
            "claimed_at": long_ago,
            "claimed_by": "hermes",
            "target_script_path": "scripts/evaluate_e.py",
        },
    )

    picked = open_tasks(tmp_path)
    assert len(picked) == 1 and picked[0]["id"] == "task_e_executor_code"

    payload = wake_once(tmp_path)
    assert payload["wakeAgent"] is True
    # wake_once reopens the stale claim — task status should now be "open".
    handoffs = yaml.safe_load(
        (tmp_path / "data" / "research_framework" / "hermes_executor_handoffs.yaml").read_text(encoding="utf-8")
    )
    task = handoffs["tasks"][0]
    assert task["status"] == "open"
    assert "stale_claim_reopened_at" in task


def test_handoff_pickable_statuses_set_is_explicit(tmp_path: Path) -> None:
    """Sanity guard: the set itself must contain exactly the documented
    pickable statuses. Adding a new repair status means editing this set;
    this test exists so the change is visible in code review.

    Updated 2026-05-25 to include needs_executor_regeneration once the
    source-lost recovery path landed."""
    assert HANDOFF_PICKABLE_STATUSES == {
        "open",
        "needs_compliance_repair",
        "needs_executor_regeneration",
    }


# ---------------------------------------------------------------------------
# Executor regeneration handoff tests (2026-05-25 round 4)
#
# Adds needs_executor_regeneration to HANDOFF_PICKABLE_STATUSES. Mirrors the
# needs_compliance_repair tests but for the "source file was lost" path.
# ---------------------------------------------------------------------------


def test_needs_executor_regeneration_is_pickable_open_tasks(tmp_path: Path) -> None:
    """A task flipped by install_one to needs_executor_regeneration must
    be visible to open_tasks()."""
    _write_handoff(
        tmp_path,
        {
            "id": "task_regen_executor_code",
            "status": "needs_executor_regeneration",
            "source_lost_detected_at": "2026-05-25T08:00:00+00:00",
            "regeneration_request": "data/run_x/generated_executor/executor_regeneration_request.yaml",
            "previous_installed_at": "2026-05-24T07:35:01+00:00",
            "previous_installed_sha256": "lost_hash",
            "target_script_path": "scripts/evaluate_regen.py",
            "run_dir": str(tmp_path / "data" / "task_regen"),
        },
    )
    picked = open_tasks(tmp_path)
    assert len(picked) == 1
    assert picked[0]["id"] == "task_regen_executor_code"
    assert picked[0]["status"] == "needs_executor_regeneration"


def test_needs_executor_regeneration_wake_once_includes_regeneration_context(tmp_path: Path) -> None:
    """wake_once must include a regeneration_context in the boundary so
    Hermes can see the source was lost and how to recover."""
    run_dir = tmp_path / "data" / "task_regen"
    run_dir.mkdir(parents=True)
    descriptor = run_dir / "executor_tool_request.yaml"
    descriptor.write_text(
        yaml.safe_dump({"status": "awaiting_hermes_executor_code"}, sort_keys=False),
        encoding="utf-8",
    )
    _write_handoff(
        tmp_path,
        {
            "id": "task_regen_executor_code",
            "status": "needs_executor_regeneration",
            "source_lost_detected_at": "2026-05-25T08:00:00+00:00",
            "regeneration_request": "data/task_regen/generated_executor/executor_regeneration_request.yaml",
            "previous_installed_at": "2026-05-24T07:35:01+00:00",
            "previous_installed_sha256": "lost_hash",
            "target_script_path": "scripts/evaluate_regen.py",
            "run_dir": str(run_dir),
            "descriptor_path": str(descriptor),
        },
    )

    payload = wake_once(tmp_path)
    assert payload["wakeAgent"] is True
    task = payload["tasks"][0]
    boundary = task["boundary"]
    assert "regeneration_context" in boundary, (
        "needs_executor_regeneration tasks must surface a regeneration_context "
        f"in the boundary so Hermes sees the source-loss reason; got {sorted(boundary)}"
    )
    ctx = boundary["regeneration_context"]
    assert "lost" in ctx["reason"].lower()
    assert ctx["previous_installed_sha256"] == "lost_hash"


def test_needs_executor_regeneration_can_be_claimed(tmp_path: Path) -> None:
    """claim_task must transition needs_executor_regeneration → claimed
    just like it does for open and needs_compliance_repair."""
    run_dir = tmp_path / "data" / "task_regen2"
    run_dir.mkdir(parents=True)
    descriptor = run_dir / "executor_tool_request.yaml"
    descriptor.write_text(
        yaml.safe_dump({"status": "awaiting_hermes_executor_code"}, sort_keys=False),
        encoding="utf-8",
    )
    _write_handoff(
        tmp_path,
        {
            "id": "task_regen2_executor_code",
            "status": "needs_executor_regeneration",
            "source_lost_detected_at": "2026-05-25T08:00:00+00:00",
            "regeneration_request": "data/task_regen2/generated_executor/executor_regeneration_request.yaml",
            "target_script_path": "scripts/evaluate_regen2.py",
            "run_dir": str(run_dir),
            "descriptor_path": str(descriptor),
        },
    )
    claim = claim_task("task_regen2_executor_code", tmp_path, actor="hermes_regen_worker")
    assert claim["status"] == "claimed"


def test_pickable_statuses_set_now_includes_regeneration(tmp_path: Path) -> None:
    """Sanity guard: the constant set must contain the three documented
    pickable statuses. Adding a new repair/regen status is a visible
    change requiring a code review update."""
    assert HANDOFF_PICKABLE_STATUSES == {
        "open",
        "needs_compliance_repair",
        "needs_executor_regeneration",
    }


def test_normal_open_task_does_not_get_regeneration_context(tmp_path: Path) -> None:
    """Regression guard: tasks that are NOT in needs_executor_regeneration
    must NOT have regeneration_context in their boundary."""
    run_dir = tmp_path / "data" / "task_normal"
    run_dir.mkdir(parents=True)
    descriptor = run_dir / "executor_tool_request.yaml"
    descriptor.write_text(
        yaml.safe_dump({"status": "awaiting_hermes_executor_code"}, sort_keys=False),
        encoding="utf-8",
    )
    _write_handoff(
        tmp_path,
        {
            "id": "task_normal_executor_code",
            "status": "open",
            "target_script_path": "scripts/evaluate_normal.py",
            "run_dir": str(run_dir),
            "descriptor_path": str(descriptor),
        },
    )
    payload = wake_once(tmp_path)
    assert payload["wakeAgent"] is True
    boundary = payload["tasks"][0]["boundary"]
    assert "regeneration_context" not in boundary
