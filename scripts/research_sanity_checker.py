#!/usr/bin/env python3
"""Hard-rule sanity checker for quant research specs.

This is a first-pass deterministic gate.  It validates the spec shape and
obvious contradictions before any expensive run is started.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REQUIRED_FIELDS = {
    "hypothesis": ("hypothesis", "假设"),
    "parameter_space": ("parameter_space", "parameter space", "参数空间"),
    "hard_floors": ("hard_floors", "hard floors", "floor", "底线"),
    "output_artifacts": ("output_artifacts", "output artifacts", "artifacts", "产物"),
    "compute_estimate": ("compute_estimate", "compute estimate", "算力"),
    "data_sources": ("data_sources", "data sources", "数据来源"),
    "true_cv_design": ("true_cv_design", "true cv", "真 cv", "leave-y"),
    "stop_conditions": ("stop_conditions", "stop conditions", "停止条件"),
}


@dataclass
class Issue:
    severity: str
    rule_id: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "rule_id": self.rule_id,
            "message": self.message,
        }


def contains_any(text: str, aliases: tuple[str, ...]) -> bool:
    lower = text.lower()
    return any(alias.lower() in lower for alias in aliases)


def parse_years(text: str) -> set[int]:
    return {int(y) for y in re.findall(r"\b(20\d{2})\b", text)}


def parse_key_values(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, value in re.findall(
        r"\b([A-Za-z_][A-Za-z0-9_]*(?:window|days|year|pct|threshold|hurdle)[A-Za-z0-9_]*)"
        r"\s*[:=]\s*(-?\d+(?:\.\d+)?)",
        text,
        flags=re.IGNORECASE,
    ):
        out[key.lower()] = float(value)
    return out


def check_required_fields(text: str) -> list[Issue]:
    issues: list[Issue] = []
    for field, aliases in REQUIRED_FIELDS.items():
        if not contains_any(text, aliases):
            issues.append(Issue("fatal", "missing_spec_field", f"missing {field}"))
    return issues


def check_windows(text: str) -> list[Issue]:
    issues: list[Issue] = []
    values = parse_key_values(text)
    for key, value in values.items():
        if ("window" in key or "days" in key) and value <= 0:
            issues.append(Issue("fatal", "non_positive_window", f"{key}={value:g} must be > 0"))

    short_values = {k: v for k, v in values.items() if "short" in k and "window" in k}
    long_values = {k: v for k, v in values.items() if "long" in k and "window" in k}
    for sk, sv in short_values.items():
        for lk, lv in long_values.items():
            if sv >= lv:
                issues.append(
                    Issue(
                        "fatal",
                        "short_window_ge_long_window",
                        f"{sk}={sv:g} must be < {lk}={lv:g}",
                    )
                )
    return issues


def check_grid_nonempty(text: str) -> list[Issue]:
    if re.search(r"(parameter_space|参数空间)[\s\S]{0,300}(\{\s*\}|\[\s*\])", text, re.IGNORECASE):
        return [Issue("fatal", "empty_parameter_space", "parameter_space appears empty")]
    return []


def check_hard_floor_range(text: str) -> list[Issue]:
    issues: list[Issue] = []
    floor_block_match = re.search(
        r"(hard_floors|hard floors|底线|floor)(?P<body>[\s\S]{0,800})",
        text,
        flags=re.IGNORECASE,
    )
    if not floor_block_match:
        return issues
    body = floor_block_match.group("body")
    for raw in re.findall(r"replay_20\d{2}\s*[>=:]\s*(-?\d+(?:\.\d+)?)", body):
        value = float(raw)
        if abs(value) > 1.0:
            issues.append(
                Issue(
                    "warning",
                    "hard_floor_unusual_scale",
                    f"hard floor {value:g} is outside normal return scale [-1, 1]",
                )
            )
    return issues


def check_data_paths(text: str, repo: Path) -> list[Issue]:
    issues: list[Issue] = []
    for raw in re.findall(r"`([^`]+\.(?:csv|json|jsonl|parquet|md))`", text):
        path = Path(raw)
        if not path.is_absolute():
            path = repo / path
        if not path.exists():
            issues.append(Issue("fatal", "data_path_missing", f"{raw} does not exist"))
    return issues


def check_true_cv(text: str) -> list[Issue]:
    issues: list[Issue] = []
    lower = text.lower()
    if "leave" in lower or "真 cv" in lower or "true cv" in lower:
        years = parse_years(text)
        if years and not any(2018 <= year <= 2035 for year in years):
            issues.append(Issue("fatal", "cv_year_out_of_range", "CV years are outside expected range"))
    return issues


def run_checks(spec_text: str, repo: Path) -> dict[str, Any]:
    issues: list[Issue] = []
    issues.extend(check_required_fields(spec_text))
    issues.extend(check_windows(spec_text))
    issues.extend(check_grid_nonempty(spec_text))
    issues.extend(check_hard_floor_range(spec_text))
    issues.extend(check_data_paths(spec_text, repo))
    issues.extend(check_true_cv(spec_text))

    fatal = [issue for issue in issues if issue.severity == "fatal"]
    warning = [issue for issue in issues if issue.severity == "warning"]
    if fatal:
        verdict = "fatal"
    elif warning:
        verdict = "warning"
    else:
        verdict = "pass"

    return {
        "sanity_report": {
            "verdict": verdict,
            "hard_rules": {
                "passed": [] if issues else ["required_fields", "windows", "grid", "hard_floors", "data_paths", "true_cv"],
                "failed": [issue.to_dict() for issue in issues],
            },
            "recommendation": recommendation(verdict),
        }
    }


def recommendation(verdict: str) -> str:
    if verdict == "fatal":
        return "spec has mechanical contradictions or missing required fields; do not start the run"
    if verdict == "warning":
        return "run is mechanically possible, but reviewer should acknowledge warnings"
    return "pass; run may proceed after compute gate"


def main() -> int:
    parser = argparse.ArgumentParser(description="Quant research spec sanity checker")
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    spec_text = args.spec.read_text(encoding="utf-8")
    report = run_checks(spec_text, args.repo.resolve())
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        sr = report["sanity_report"]
        print(f"verdict: {sr['verdict']}")
        for issue in sr["hard_rules"]["failed"]:
            print(f"{issue['severity']} {issue['rule_id']}: {issue['message']}")
        print(f"recommendation: {sr['recommendation']}")
    return 2 if report["sanity_report"]["verdict"] == "fatal" else 0


if __name__ == "__main__":
    raise SystemExit(main())
