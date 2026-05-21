from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from framework.autonomous.hermes_executor_handoff import (
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
