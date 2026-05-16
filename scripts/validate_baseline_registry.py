#!/usr/bin/env python3
"""Validate data/research_framework/baseline_registry.yaml.

按 Codex framework holistic review Q2-D: baseline_registry 升 yaml +
transition validator. 防"偷偷换 baseline 充作 stress test 已过".

检查项:
1. yaml parse OK + schema_version + baselines 是 list
2. 每个 baseline 必填字段:
   pk, strategy_id, date, implementation, config_summary, period,
   protocol, metrics, artifact, status
3. pk unique
4. strategy_id 必须在 data/research_framework/strategies.yaml 中
5. status in {WIP, adopted, rejected, archived}
6. supersedes 必引用现有 pk (或 null)
7. supersedes != null → supersede_reason + supersede_audit 必填
   supersede_audit 必含 committed_by / audited_by / audit_date
   committed_by / audited_by in {claude, codex, user}
8. 防循环 supersede (DAG check)
9. 同 strategy_id 最多一个 status=adopted (当前活跃 baseline)
10. supersedes != null 时, 被 supersede 的 entry 的 superseded_by 必反向指 (consistency)

Usage:
  python3 scripts/validate_baseline_registry.py

Exit codes:
  0 = OK
  1 = schema/transition error
  2 = operational error (yaml parse / strategies.yaml 缺失)
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml required", file=sys.stderr)
    sys.exit(2)


REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / "data" / "research_framework" / "baseline_registry.yaml"
STRATEGIES_PATH = REPO_ROOT / "data" / "research_framework" / "strategies.yaml"

REQUIRED_FIELDS = {
    "pk", "strategy_id", "date", "implementation", "config_summary",
    "period", "protocol", "metrics", "artifact", "status",
}

ALLOWED_STATUS = {"WIP", "adopted", "rejected", "archived"}
ALLOWED_AUDIT_ROLES = {"claude", "codex", "user"}

PLACEHOLDER_TOKENS = {"TBD", "tbd", "...", "TODO", "todo", "n/a", "N/A",
                      "pending", "PENDING", "<待填>"}


def is_placeholder(value) -> bool:
    if not isinstance(value, str):
        return False
    s = value.strip()
    if not s:
        return True
    return any(s == t or s.startswith(t) for t in PLACEHOLDER_TOKENS)


def load_strategy_ids() -> set[str]:
    if not STRATEGIES_PATH.exists():
        print(f"ERROR: strategies.yaml 不存在: {STRATEGIES_PATH}", file=sys.stderr)
        sys.exit(2)
    with STRATEGIES_PATH.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "strategies" not in data:
        print(f"ERROR: strategies.yaml 顶层缺 strategies 字段", file=sys.stderr)
        sys.exit(2)
    return {s["id"] for s in data["strategies"] if isinstance(s, dict) and "id" in s}


def detect_cycles(baselines: list[dict]) -> list[str]:
    """检测 supersedes 链环引用 (DAG check)."""
    errors = []
    pk_to_supersedes = {b.get("pk"): b.get("supersedes") for b in baselines if b.get("pk")}
    for start_pk in pk_to_supersedes:
        visited = set()
        current = start_pk
        while current is not None:
            if current in visited:
                errors.append(f"supersede 循环引用: 从 {start_pk} 走到环 (在 {current})")
                break
            visited.add(current)
            current = pk_to_supersedes.get(current)
    return list(set(errors))  # 去重


def validate(registry: dict, strategy_ids: set[str]) -> list[str]:
    errors = []

    if registry.get("schema_version") != 1:
        errors.append(f"schema_version 必须是 1, got {registry.get('schema_version')!r}")

    baselines = registry.get("baselines")
    if not isinstance(baselines, list):
        errors.append(f"baselines 必须是 list, got {type(baselines).__name__}")
        return errors

    # 收集 pk 集合 + 用于 reverse pointer check
    all_pks = set()
    pk_to_entry = {}
    for i, b in enumerate(baselines):
        if not isinstance(b, dict):
            errors.append(f"baselines[{i}] 不是 dict")
            continue
        pk = b.get("pk")
        if pk:
            if pk in all_pks:
                errors.append(f"baselines[{i}].pk={pk!r} 重复")
            all_pks.add(pk)
            pk_to_entry[pk] = b

    # 每个 entry 逐项检查
    adopted_by_strategy: dict[str, list[str]] = {}
    for i, b in enumerate(baselines):
        if not isinstance(b, dict):
            continue
        pk = b.get("pk", f"[{i}]")

        # 必填字段
        missing = REQUIRED_FIELDS - set(b.keys())
        if missing:
            errors.append(f"baselines[{pk}] 缺必填字段: {sorted(missing)}")

        # strategy_id 必须存在
        sid = b.get("strategy_id")
        if sid and sid not in strategy_ids:
            errors.append(f"baselines[{pk}].strategy_id={sid!r} 不在 strategies.yaml 中")

        # status 合法
        status = b.get("status")
        if status and status not in ALLOWED_STATUS:
            errors.append(f"baselines[{pk}].status={status!r} 不在 {sorted(ALLOWED_STATUS)}")

        # 同 strategy_id 最多一个 adopted
        if status == "adopted" and sid:
            adopted_by_strategy.setdefault(sid, []).append(pk)

        # supersedes 引用合法
        supersedes = b.get("supersedes")
        if supersedes is not None:
            if supersedes not in all_pks:
                errors.append(
                    f"baselines[{pk}].supersedes={supersedes!r} 不是现有 pk"
                )
            else:
                # 反向指针 consistency: 被 supersede 的 entry 应有 superseded_by=pk
                target = pk_to_entry.get(supersedes)
                if target:
                    reverse = target.get("superseded_by")
                    if reverse != pk:
                        errors.append(
                            f"baselines[{pk}] supersedes {supersedes}, "
                            f"但 {supersedes}.superseded_by={reverse!r} (应是 {pk!r})"
                        )

            # supersede_reason + supersede_audit 必填非 placeholder
            reason = b.get("supersede_reason")
            if not reason or is_placeholder(reason):
                errors.append(
                    f"baselines[{pk}] supersedes 但 supersede_reason 缺/空/placeholder"
                )
            audit = b.get("supersede_audit")
            if not isinstance(audit, dict):
                errors.append(
                    f"baselines[{pk}] supersedes 但 supersede_audit 缺/不是 dict"
                )
            else:
                for field in ("committed_by", "audited_by", "audit_date"):
                    v = audit.get(field)
                    if not v:
                        errors.append(
                            f"baselines[{pk}].supersede_audit.{field} 缺"
                        )
                for role_field in ("committed_by", "audited_by"):
                    role = audit.get(role_field)
                    if role and role not in ALLOWED_AUDIT_ROLES:
                        errors.append(
                            f"baselines[{pk}].supersede_audit.{role_field}={role!r} "
                            f"不在 {sorted(ALLOWED_AUDIT_ROLES)}"
                        )

    # 同 strategy_id 最多一个 adopted (active baseline)
    for sid, pks in adopted_by_strategy.items():
        if len(pks) > 1:
            errors.append(
                f"strategy_id={sid!r} 有 {len(pks)} 个 status=adopted "
                f"({pks}); 同 strategy 同时只能一个 active baseline"
            )

    # DAG 环检测
    errors.extend(detect_cycles(baselines))

    return errors


def main() -> int:
    if not REGISTRY_PATH.exists():
        print(f"ERROR: baseline_registry.yaml 不存在: {REGISTRY_PATH}", file=sys.stderr)
        return 2

    try:
        with REGISTRY_PATH.open(encoding="utf-8") as f:
            registry = yaml.safe_load(f)
    except yaml.YAMLError as e:
        print(f"ERROR: yaml parse 失败: {e}", file=sys.stderr)
        return 2

    if not isinstance(registry, dict):
        print(f"ERROR: 顶层不是 dict (got {type(registry).__name__})", file=sys.stderr)
        return 2

    strategy_ids = load_strategy_ids()
    errors = validate(registry, strategy_ids)

    n_baselines = len(registry.get("baselines", []))
    print(f"validate_baseline_registry.py:")
    print(f"  loaded {n_baselines} baselines, strategy_ids in registry: {len(strategy_ids)}")

    if errors:
        print(f"\nFATAL: {len(errors)} 错误:")
        for e in errors:
            print(f"  {e}")
        return 1

    print(f"  OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
