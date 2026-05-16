#!/usr/bin/env python3
"""Recover cb_arb loop metadata from the VM git reflog.

The original data/cb_arb/runs.jsonl was untracked and has been overwritten.
This script preserves the recoverable audit trail from git reflog and rebuilds
the parameter state as far as the commit messages allow.
"""

from __future__ import annotations

import json
import re
import subprocess

import yaml
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


VM_HOST = "root@100.91.245.108"
VM_REPO = "/root/projects/quant"
OUT_DIR = Path("data/cb_arb/recovered")
REPORT_PATH = Path("data/cb_arb/recovered/cb_arb_recovery_2026-05-10.yaml")
TARGET_START = datetime.fromisoformat("2026-05-09 16:20:00 +0800")
TARGET_END = datetime.fromisoformat("2026-05-09 20:01:00 +0800")

PARAMETER_ORDER = [
    "vol_window_days",
    "vol_multiplier",
    "rank_buy_pct",
    "rank_sell_pct",
    "max_position_pct",
    "max_holdings",
    "max_holding_days",
    "stop_loss_pct",
    "min_remaining_size",
    "min_avg_amount",
    "credit_spread_aaa_bp",
    "credit_spread_aa_bp",
]
RULE_ORDER = ["rating_floor_int", "fee_pct", "initial_capital"]
CB_ARB_FIELDS = set(PARAMETER_ORDER) | set(RULE_ORDER)

BASELINE_PARAMS = {
    "parameters": {
        "vol_window_days": 60,
        "vol_multiplier": 1.0,
        "rank_buy_pct": 0.10,
        "rank_sell_pct": 0.50,
        "max_position_pct": 0.03,
        "max_holdings": 30,
        "max_holding_days": 90,
        "stop_loss_pct": -0.08,
        "min_remaining_size": 72900000.0,
        "min_avg_amount": 1000000,
        "credit_spread_aaa_bp": 50,
        "credit_spread_aa_bp": 150,
    },
    "rules": {
        "rating_floor_int": 2,
        "fee_pct": 0.0003,
        "initial_capital": 1000000,
    },
    "thresholds": {},
}

REFLOG_RE = re.compile(
    r"^(?P<commit>[0-9a-f]+) (?P<ref>\S+)@\{(?P<date>[^}]+)\}: "
    r"commit: loop iter=(?P<iteration>\d+) verdict=(?P<verdict>[^:]+): (?P<message>.*)$"
)
CHANGE_RE = re.compile(
    r"changed (?P<section>parameters|rules)\.(?P<name>[a-zA-Z0-9_]+) "
    r"from (?P<old>.+?) to (?P<new>.+?) \(llm\)$"
)
SHRINK_RE = re.compile(
    r"(?P<section>parameters|rules)\.(?P<name>[a-zA-Z0-9_]+)="
    r"(?P<old>-?\d+(?:\.\d+)?)\s*->\s*(?P<new>-?\d+(?:\.\d+)?)"
)


@dataclass(frozen=True)
class Event:
    commit: str
    ref: str
    date: str
    dt: datetime
    iteration: int
    verdict: str
    message: str


def parse_scalar(raw: str) -> Any:
    text = raw.strip().strip("'\"")
    if text.lower() == "true":
        return True
    if text.lower() == "false":
        return False
    try:
        if re.fullmatch(r"-?\d+", text):
            return int(text)
        if re.fullmatch(r"-?\d+\.\d+", text):
            return float(text)
    except ValueError:
        pass
    return text


def run_ssh(command: str) -> str:
    proc = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            VM_HOST,
            command,
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.stdout


def fetch_reflog() -> str:
    return run_ssh(f"cd {VM_REPO} && git reflog --date=iso --all")


def parse_events(raw: str) -> list[Event]:
    by_commit: dict[str, Event] = {}
    for line in raw.splitlines():
        match = REFLOG_RE.match(line)
        if not match:
            continue
        message = match.group("message")
        change = CHANGE_RE.match(message)
        shrink_fields = SHRINK_RE.findall(message)
        field_names = {change.group("name")} if change else {item[1] for item in shrink_fields}
        if field_names and not (field_names & CB_ARB_FIELDS):
            continue
        if not field_names and not any(
            token in message
            for token in (
                "apply 12 weights",
                "shrink 12 weights",
                "produced no edit",
                "recovery attempt",
            )
        ):
            continue
        date_text = match.group("date")
        dt = datetime.fromisoformat(date_text)
        commit = match.group("commit")
        by_commit.setdefault(
            commit,
            Event(
                commit=commit,
                ref=match.group("ref"),
                date=date_text,
                dt=dt,
                iteration=int(match.group("iteration")),
                verdict=match.group("verdict"),
                message=message,
            ),
        )
    return sorted(by_commit.values(), key=lambda event: event.dt)


def split_runs(events: list[Event]) -> list[list[Event]]:
    runs: list[list[Event]] = []
    current: list[Event] = []
    for event in events:
        if event.iteration == 1 and current:
            runs.append(current)
            current = []
        current.append(event)
    if current:
        runs.append(current)
    return runs


def state_to_params(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "rules": {name: state["rules"][name] for name in RULE_ORDER},
        "thresholds": {},
        "weights": [state["parameters"][name] for name in PARAMETER_ORDER],
        "parameters_by_name": {name: state["parameters"][name] for name in PARAMETER_ORDER},
    }


def apply_event(state: dict[str, Any], event: Event) -> dict[str, Any]:
    record: dict[str, Any] = {
        "commit": event.commit,
        "date": event.date,
        "iteration": event.iteration,
        "verdict": event.verdict,
        "message": event.message,
        "recovery_quality": "exact",
        "changes": [],
    }
    change = CHANGE_RE.match(event.message)
    if change:
        section = change.group("section")
        name = change.group("name")
        old_value = parse_scalar(change.group("old"))
        new_value = parse_scalar(change.group("new"))
        before = state[section].get(name)
        record["changes"].append(
            {
                "section": section,
                "name": name,
                "old": old_value,
                "new": new_value,
                "state_before": before,
                "old_matches_state": before == old_value,
            }
        )
        state[section][name] = new_value
        if before != old_value:
            record["recovery_quality"] = "inferred_with_mismatch"
        return record

    shrink_changes = []
    for section, name, old_raw, new_raw in SHRINK_RE.findall(event.message):
        old_value = parse_scalar(old_raw)
        new_value = parse_scalar(new_raw)
        before = state[section].get(name)
        shrink_changes.append(
            {
                "section": section,
                "name": name,
                "old": old_value,
                "new": new_value,
                "state_before": before,
                "old_matches_state": before == old_value,
            }
        )
        state[section][name] = new_value
    if shrink_changes:
        record["changes"] = shrink_changes
        record["recovery_quality"] = (
            "partial_shrink_message"
            if len(shrink_changes) < 12
            else "exact_shrink_message"
        )
        return record

    if "produced no edit" in event.message:
        record["recovery_quality"] = "exact_no_edit"
        return record

    record["recovery_quality"] = "unresolved_recovery_attempt"
    record["note"] = "Commit message does not include concrete parameter values."
    return record


def recover_run(events: list[Event]) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    state = json.loads(json.dumps(BASELINE_PARAMS))
    records: list[dict[str, Any]] = []
    params_before_iter_240: dict[str, Any] | None = None
    for event in events:
        if event.iteration == 240:
            params_before_iter_240 = state_to_params(state)
        record = apply_event(state, event)
        record["params_after"] = state_to_params(state)
        records.append(record)
    return records, params_before_iter_240 or {}, state_to_params(state)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    raw = fetch_reflog()
    (OUT_DIR / "vm_git_reflog_raw.txt").write_text(raw)

    events = parse_events(raw)
    windowed = [event for event in events if TARGET_START <= event.dt <= TARGET_END]
    runs = split_runs(windowed)
    target = next(
        (
            run
            for run in runs
            if min(event.iteration for event in run) == 1
            and max(event.iteration for event in run) == 240
        ),
        [],
    )
    if not target:
        raise SystemExit("No cb_arb 240-iteration run found in reflog")

    records, before_240, after_240 = recover_run(target)
    raw_rows = [event.__dict__ | {"dt": event.dt.isoformat()} for event in target]

    write_jsonl(OUT_DIR / "cb_arb_reflog_iter_1_240.jsonl", raw_rows)
    write_jsonl(OUT_DIR / "cb_arb_recovery_trace_iter_1_240.jsonl", records)
    write_json(OUT_DIR / "cb_arb_iter_240_params_before_commit.json", before_240)
    write_json(OUT_DIR / "cb_arb_iter_240_params_after_commit.json", after_240)

    quality_counts: dict[str, int] = {}
    for record in records:
        quality_counts[record["recovery_quality"]] = quality_counts.get(record["recovery_quality"], 0) + 1
    unresolved = [record for record in records if record["recovery_quality"] == "unresolved_recovery_attempt"]
    mismatches = [record for record in records if record["recovery_quality"] == "inferred_with_mismatch"]

    start = target[0]
    end = target[-1]
    report_data = {
        "schema_version": 1,
        "title": "cb_arb VM Reflog Recovery",
        "scope": {
            "vm_repo": f"{VM_HOST}:{VM_REPO}",
            "iter_start": {"iteration": start.iteration, "date": start.date},
            "iter_end": {"iteration": end.iteration, "date": end.date},
            "commits_recovered": len(target),
            "source": "git reflog --date=iso --all",
            "note": "original data/cb_arb/runs.jsonl remains unavailable",
        },
        "artifacts": [
            str(OUT_DIR / "vm_git_reflog_raw.txt"),
            str(OUT_DIR / "cb_arb_reflog_iter_1_240.jsonl"),
            str(OUT_DIR / "cb_arb_recovery_trace_iter_1_240.jsonl"),
            str(OUT_DIR / "cb_arb_iter_240_params_before_commit.json"),
            str(OUT_DIR / "cb_arb_iter_240_params_after_commit.json"),
        ],
        "recovery_quality": dict(sorted(quality_counts.items())),
        "quality_notes": {
            "unresolved_recovery_attempt": "recovery commits whose messages did not include concrete values",
            "inferred_with_mismatch": "later commit message provided a value that did not match earlier-reconstructed state",
            "missing_runs_jsonl": "exact JSON run records were not recoverable because runs.jsonl was untracked and overwritten",
        },
        "iteration_240": {
            "before_commit_note": "best proxy for the parameters used by the 240th backtest row",
            "after_commit_note": "includes the iteration-240 LLM edit itself",
            "before_params": before_240,
            "after_params": after_240,
        },
        "gaps": {
            "unresolved_recovery_attempts": len(unresolved),
            "state_value_mismatches": len(mismatches),
        },
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(yaml.safe_dump(report_data, sort_keys=False, allow_unicode=True), encoding="utf-8")

    print(f"wrote {REPORT_PATH}")
    print(f"wrote {OUT_DIR}")
    print(f"quality_counts={quality_counts}")


if __name__ == "__main__":
    main()
