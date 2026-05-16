#!/usr/bin/env python3
"""Semantic sanity checker for HDRF L1 spec.yaml.

跟 validate_spec.py 分工:
- validate_spec.py: schema layer (字段存在 + 类型/格式/枚举值)
- research_sanity_checker.py: semantic layer (字段值是否合理)

按 Codex framework holistic review Q2-A: spec yaml 升级后, sanity_checker 也要
升级读 yaml 做 semantic 检查, 再接入 GateKeeper.before_run_grid 做"跑回测前
最后一道闸".

Usage:
  python3 scripts/research_sanity_checker.py --spec data/<run-id>/spec.yaml
  python3 scripts/research_sanity_checker.py --spec ... --json

Exit codes:
  0 = pass (无 fatal)
  1 = fatal (语义错误, 拒跑)
  2 = operational error (yaml 无法 parse / 文件不存在)

Severity:
- fatal: 跑批前必须修, GateKeeper exit 1
- warning: 跑批可继续, 但应该 reviewer ack

检查项 (semantic, 不重复 schema):
1. parameter_space[i].range: low < high
2. hard_floors: 数值在合理 scale
3. data_sources / new_data_sources: 路径存在
4. cv_holdout_years: 在 2018-2035 范围
5. budget_cap_yuan ≥ compute_estimate.estimated_cost_yuan
6. compute_estimate.sig_minutes, spot_minutes ≥ 0
7. parameter_space 不为空 list
8. grid.candidates_count > 0 (如有)
9. stop_conditions 至少 1 个非空字符串
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml required", file=sys.stderr)
    sys.exit(2)


REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Issue:
    severity: str  # 'fatal' or 'warning'
    rule_id: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "rule_id": self.rule_id, "message": self.message}


def check_parameter_space_ranges(spec: dict[str, Any]) -> list[Issue]:
    """parameter_space[i].range: low < high"""
    issues = []
    ps = spec.get("parameter_space") or []
    if not isinstance(ps, list):
        return issues
    for i, dim in enumerate(ps):
        if not isinstance(dim, dict):
            continue
        name = dim.get("name", f"dim{i}")
        rng = dim.get("range")
        if rng is None:
            continue
        if isinstance(rng, list) and len(rng) >= 2:
            try:
                low = float(rng[0])
                high = float(rng[-1])
                if low >= high:
                    issues.append(Issue(
                        "fatal", "parameter_range_inverted",
                        f"parameter_space[{name}].range first={low:g} must be < last={high:g}"
                    ))
            except (TypeError, ValueError):
                # 非数值 range (e.g. ['linear', 'log']) 跳过
                pass
    return issues


def check_hard_floors_scale(spec: dict[str, Any]) -> list[Issue]:
    """hard_floors 数值在合理 scale."""
    issues = []
    floors = spec.get("hard_floors") or {}
    if not isinstance(floors, dict):
        return issues
    for name, value in floors.items():
        if isinstance(value, (int, float)):
            v = float(value)
            if abs(v) > 10.0:
                issues.append(Issue(
                    "warning", "hard_floor_unusual_scale",
                    f"hard_floors.{name}={v:g} 超出典型 return/dd scale (|v|>10); 单位错?"
                ))
            if ("dd" in name.lower() or "drawdown" in name.lower()) and v > 0:
                issues.append(Issue(
                    "warning", "hard_floor_dd_positive",
                    f"hard_floors.{name}={v:g} 是正值但名字像 drawdown (应 ≤ 0)"
                ))
    return issues


def check_data_sources_exist(spec: dict[str, Any], repo: Path) -> list[Issue]:
    """data_sources / new_data_sources 路径存在."""
    issues = []
    for key in ("new_data_sources", "data_sources"):
        sources = spec.get(key) or []
        if not isinstance(sources, list):
            continue
        for i, src in enumerate(sources):
            path_str = None
            if isinstance(src, str):
                path_str = src
            elif isinstance(src, dict):
                path_str = src.get("path")
            if not path_str:
                continue
            p = Path(path_str)
            if not p.is_absolute():
                p = repo / p
            if not p.exists():
                issues.append(Issue(
                    "fatal", "data_source_missing",
                    f"{key}[{i}].path={path_str} 不存在 (resolved: {p})"
                ))
    return issues


def check_cv_holdout_years(spec: dict[str, Any]) -> list[Issue]:
    """cv_holdout_years 在 2018-2035."""
    issues = []
    years = spec.get("cv_holdout_years") or []
    if not isinstance(years, list):
        return issues
    for y in years:
        try:
            yi = int(y)
            if yi < 2018 or yi > 2035:
                issues.append(Issue(
                    "fatal", "cv_year_out_of_range",
                    f"cv_holdout_years 含 {yi}, 必须在 [2018, 2035]"
                ))
        except (TypeError, ValueError):
            issues.append(Issue(
                "fatal", "cv_year_not_int",
                f"cv_holdout_years 含非整数值: {y!r}"
            ))
    return issues


def check_budget_vs_compute_cost(spec: dict[str, Any]) -> list[Issue]:
    """budget_cap_yuan ≥ compute_estimate.estimated_cost_yuan."""
    issues = []
    cap = spec.get("budget_cap_yuan")
    compute = spec.get("compute_estimate") or {}
    if not isinstance(compute, dict):
        return issues
    cost = compute.get("estimated_cost_yuan")
    if isinstance(cap, (int, float)) and isinstance(cost, (int, float)):
        if float(cost) > float(cap):
            issues.append(Issue(
                "fatal", "estimate_exceeds_budget",
                f"compute_estimate.estimated_cost_yuan={cost} > budget_cap_yuan={cap}; "
                f"必超预算, 调小 grid 或加 budget cap"
            ))
    return issues


def check_compute_estimate_positive(spec: dict[str, Any]) -> list[Issue]:
    """compute_estimate.sig_minutes, spot_minutes ≥ 0."""
    issues = []
    compute = spec.get("compute_estimate") or {}
    if not isinstance(compute, dict):
        return issues
    for field in ("sig_minutes", "spot_minutes"):
        v = compute.get(field)
        if isinstance(v, (int, float)) and v < 0:
            issues.append(Issue(
                "fatal", "compute_minutes_negative",
                f"compute_estimate.{field}={v} 必须 ≥ 0"
            ))
    return issues


def check_parameter_space_nonempty(spec: dict[str, Any]) -> list[Issue]:
    """parameter_space 至少 1 个 dimension."""
    issues = []
    ps = spec.get("parameter_space")
    if isinstance(ps, list) and len(ps) == 0:
        issues.append(Issue(
            "fatal", "parameter_space_empty",
            "parameter_space 是空 list; grid 没东西可跑"
        ))
    return issues


def check_grid_candidates(spec: dict[str, Any]) -> list[Issue]:
    """grid.candidates_count > 0."""
    issues = []
    grid = spec.get("grid")
    if not isinstance(grid, dict):
        return issues
    count = grid.get("candidates_count")
    if isinstance(count, int) and count <= 0:
        issues.append(Issue(
            "fatal", "grid_empty",
            f"grid.candidates_count={count} 必须 > 0"
        ))
    return issues


def check_stop_conditions(spec: dict[str, Any]) -> list[Issue]:
    """stop_conditions 至少 1 个非空字符串."""
    issues = []
    stops = spec.get("stop_conditions") or []
    if not isinstance(stops, list):
        return issues
    if not any(isinstance(s, str) and s.strip() for s in stops):
        issues.append(Issue(
            "fatal", "stop_conditions_all_empty",
            "stop_conditions 全是空字符串或非字符串; 跑批没停止边界"
        ))
    return issues


def run_checks(spec: dict[str, Any], repo: Path) -> dict[str, Any]:
    issues = []
    issues.extend(check_parameter_space_nonempty(spec))
    issues.extend(check_parameter_space_ranges(spec))
    issues.extend(check_hard_floors_scale(spec))
    issues.extend(check_data_sources_exist(spec, repo))
    issues.extend(check_cv_holdout_years(spec))
    issues.extend(check_budget_vs_compute_cost(spec))
    issues.extend(check_compute_estimate_positive(spec))
    issues.extend(check_grid_candidates(spec))
    issues.extend(check_stop_conditions(spec))

    fatal = [i for i in issues if i.severity == "fatal"]
    warning = [i for i in issues if i.severity == "warning"]
    verdict = "fatal" if fatal else ("warning" if warning else "pass")

    return {
        "sanity_report": {
            "verdict": verdict,
            "fatal_count": len(fatal),
            "warning_count": len(warning),
            "issues": [i.to_dict() for i in issues],
            "recommendation": _recommendation(verdict),
        }
    }


def _recommendation(verdict: str) -> str:
    if verdict == "fatal":
        return "spec 有 semantic 错误, GateKeeper 拒跑, 修完再来"
    if verdict == "warning":
        return "spec 可跑, 但有 warning, reviewer 应该 ack"
    return "pass; spec semantic OK"


def main() -> int:
    parser = argparse.ArgumentParser(description="HDRF L1 spec.yaml semantic sanity check")
    parser.add_argument("--spec", type=Path, required=True, help="path to spec.yaml")
    parser.add_argument("--repo", type=Path, default=REPO_ROOT, help="repo root")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    if not args.spec.exists():
        print(f"ERROR: spec file 不存在: {args.spec}", file=sys.stderr)
        return 2

    try:
        with args.spec.open(encoding="utf-8") as f:
            spec = yaml.safe_load(f)
    except yaml.YAMLError as e:
        print(f"ERROR: yaml parse 失败: {e}", file=sys.stderr)
        return 2

    if not isinstance(spec, dict):
        print(f"ERROR: spec 顶层不是 dict (got {type(spec).__name__})", file=sys.stderr)
        return 2

    report = run_checks(spec, args.repo.resolve())

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        sr = report["sanity_report"]
        print(f"verdict: {sr['verdict']}  (fatal={sr['fatal_count']}, warning={sr['warning_count']})")
        for issue in sr["issues"]:
            print(f"  {issue['severity']:7s} {issue['rule_id']}: {issue['message']}")
        print(f"recommendation: {sr['recommendation']}")

    return 1 if report["sanity_report"]["verdict"] == "fatal" else 0


if __name__ == "__main__":
    sys.exit(main())
