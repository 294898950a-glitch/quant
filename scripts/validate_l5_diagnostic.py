#!/usr/bin/env python3
"""Validate data/<run-id>/diagnostic.yaml against HDRF L5 schema.

按 Codex framework holistic review Q1 P1: L5 反向诊断当前是纯文本黑洞, 无结构,
无 validator. mini-spec-retry / reject 决定的 root cause 应该结构化追溯.

强制规则:
- 扫 data/<run-id>/l4_ack.yaml, 看 overall_decision
- overall_decision in {mini-spec-retry, reject} → data/<run-id>/diagnostic.yaml 必有
- adopt / archive-direction 时不强制 (但有也校验)

Required fields (缺 / 占位符 → exit 1):
- schema_version, run_id, diagnostic_date, diagnostic_by, verdict_referenced
- summary (non-empty, non-placeholder)
- verdict_rationale (non-empty, non-placeholder)

Conditional required:
- verdict_referenced == 'mini-spec-retry' → next_step_spec_changes 必非空 list,
  每个 entry 必有 field / old_value / new_value / reason

Optional:
- trade_level_diff: 如有则结构必正确 (baseline_trades_count, candidate_trades_count, ...)

Usage:
  python3 scripts/validate_l5_diagnostic.py
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml required", file=sys.stderr)
    sys.exit(1)


REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

REQUIRED_TOPLEVEL = {
    "schema_version", "run_id", "diagnostic_date", "diagnostic_by",
    "verdict_referenced", "summary", "verdict_rationale",
}

ALLOWED_VERDICTS = {"mini-spec-retry", "reject", "adopt", "archive-direction"}
ALLOWED_DIAGNOSTIC_BY = {"claude", "codex", "user"}

# 触发"必有 diagnostic.yaml"的 l4_ack 决定
DIAGNOSTIC_REQUIRED_DECISIONS = {"mini-spec-retry", "reject"}

PLACEHOLDER_TOKENS = {"TBD", "tbd", "...", "skip", "TODO", "todo", "n/a", "N/A",
                      "pending", "PENDING", "<待填>"}


def is_placeholder(value) -> bool:
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if not stripped:
        return True
    for token in PLACEHOLDER_TOKENS:
        if stripped == token or stripped.startswith(token):
            return True
    return False


def validate_one(diagnostic_path: Path, l4_decision: str | None) -> list[str]:
    """返回错误列表 (空 = OK)."""
    errors = []

    try:
        with diagnostic_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        return [f"{diagnostic_path}: yaml parse 失败: {e}"]

    if not isinstance(data, dict):
        return [f"{diagnostic_path}: 顶层不是 dict"]

    # 必填字段
    missing = REQUIRED_TOPLEVEL - set(data.keys())
    if missing:
        errors.append(f"{diagnostic_path}: 缺必填字段: {sorted(missing)}")

    # verdict_referenced 合法值
    verdict = data.get("verdict_referenced")
    if verdict and verdict not in ALLOWED_VERDICTS:
        errors.append(
            f"{diagnostic_path}: verdict_referenced={verdict!r} 不在 {sorted(ALLOWED_VERDICTS)}"
        )

    # verdict_referenced 跟 l4_ack.overall_decision 应该一致 (如有 l4_ack)
    if l4_decision and verdict and verdict != l4_decision:
        errors.append(
            f"{diagnostic_path}: verdict_referenced={verdict!r} 跟 l4_ack.overall_decision={l4_decision!r} 不一致"
        )

    # diagnostic_by 合法值
    by = data.get("diagnostic_by")
    if by and by not in ALLOWED_DIAGNOSTIC_BY:
        errors.append(
            f"{diagnostic_path}: diagnostic_by={by!r} 不在 {sorted(ALLOWED_DIAGNOSTIC_BY)}"
        )

    # summary / verdict_rationale 不能是 placeholder
    for field in ("summary", "verdict_rationale"):
        value = data.get(field)
        if value is not None and is_placeholder(value):
            errors.append(f"{diagnostic_path}: {field} 是占位符/空")

    # mini-spec-retry 时 next_step_spec_changes 必非空
    if verdict == "mini-spec-retry":
        changes = data.get("next_step_spec_changes")
        if not isinstance(changes, list) or not changes:
            errors.append(
                f"{diagnostic_path}: verdict=mini-spec-retry 但 next_step_spec_changes 空; "
                f"retry 必须明确改什么"
            )
        else:
            for i, change in enumerate(changes):
                if not isinstance(change, dict):
                    errors.append(f"{diagnostic_path}: next_step_spec_changes[{i}] 不是 dict")
                    continue
                required_change_fields = {"field", "old_value", "new_value", "reason"}
                missing_change = required_change_fields - set(change.keys())
                if missing_change:
                    errors.append(
                        f"{diagnostic_path}: next_step_spec_changes[{i}] 缺字段 {sorted(missing_change)}"
                    )
                # reason 不能 placeholder
                if "reason" in change and is_placeholder(change["reason"]):
                    errors.append(
                        f"{diagnostic_path}: next_step_spec_changes[{i}].reason 是占位符/空"
                    )

    # trade_level_diff 如有, 结构正确
    tld = data.get("trade_level_diff")
    if tld is not None:
        if not isinstance(tld, dict):
            errors.append(f"{diagnostic_path}: trade_level_diff 不是 dict")
        else:
            for f in ("baseline_trades_count", "candidate_trades_count"):
                v = tld.get(f)
                if v is not None and not isinstance(v, int):
                    errors.append(
                        f"{diagnostic_path}: trade_level_diff.{f}={v!r} 不是 int"
                    )

    return errors


def read_l4_decision(l4_ack_path: Path) -> str | None:
    """读 l4_ack.yaml.overall_decision."""
    if not l4_ack_path.exists():
        return None
    try:
        with l4_ack_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict):
            return data.get("overall_decision")
    except yaml.YAMLError:
        pass
    return None


def main() -> int:
    # 按 Codex framework Q2-B: 加 --run-dir 让 GateKeeper 能锁单 run; 不传时 fall back 扫全部
    import argparse
    parser = argparse.ArgumentParser(description="Validate L5 diagnostic.yaml")
    parser.add_argument("--run-dir", type=Path, default=None,
                        help="单 run 模式: 只扫这个 dir 的 diagnostic.yaml (GateKeeper 透传用)")
    args = parser.parse_args()

    if not DATA_DIR.exists():
        print(f"validate_l5_diagnostic.py: data/ 不存在, skip")
        return 0

    all_errors = []
    diagnostic_required_missing = []
    diagnostic_validated = []

    if args.run_dir is not None:
        if not args.run_dir.is_absolute():
            args.run_dir = (REPO_ROOT / args.run_dir).resolve()
        if not args.run_dir.exists():
            print(f"ERROR: --run-dir {args.run_dir} 不存在", file=sys.stderr)
            return 1
        run_dirs_to_scan = [args.run_dir]
    else:
        run_dirs_to_scan = sorted(DATA_DIR.iterdir())

    for run_dir in run_dirs_to_scan:
        if not run_dir.is_dir():
            continue
        spec_path = run_dir / "spec.yaml"
        l4_ack_path = run_dir / "l4_ack.yaml"
        diagnostic_path = run_dir / "diagnostic.yaml"

        # 没 spec 跳过 (不是 research run)
        if not spec_path.exists():
            continue

        l4_decision = read_l4_decision(l4_ack_path)

        # diagnostic 强制条件: l4_ack.overall_decision in {retry, reject}
        if l4_decision in DIAGNOSTIC_REQUIRED_DECISIONS:
            if not diagnostic_path.exists():
                diagnostic_required_missing.append(
                    f"{run_dir.relative_to(REPO_ROOT)}/diagnostic.yaml: "
                    f"l4_ack.overall_decision={l4_decision} 但 diagnostic.yaml 缺失"
                )
                continue

        # 有 diagnostic 就 validate (即使非强制场景)
        if diagnostic_path.exists():
            errors = validate_one(diagnostic_path, l4_decision)
            if errors:
                all_errors.extend(errors)
            else:
                diagnostic_validated.append(str(diagnostic_path.relative_to(REPO_ROOT)))

    print(f"validate_l5_diagnostic.py:")
    print(f"  validated {len(diagnostic_validated)} diagnostic.yaml")
    for v in diagnostic_validated:
        print(f"    ✓ {v}")

    if diagnostic_required_missing:
        print(f"\nFATAL: {len(diagnostic_required_missing)} 强制 diagnostic.yaml 缺失:")
        for m in diagnostic_required_missing:
            print(f"  {m}")
        print(f"\n  按 HDRF Q1 P1: l4_ack.overall_decision=mini-spec-retry/reject 时, "
              f"data/<run-id>/diagnostic.yaml 必填 (结构化反向诊断, 不靠 free text)")

    if all_errors:
        print(f"\nFATAL: {len(all_errors)} 现有 diagnostic.yaml 不合规:")
        for e in all_errors:
            print(f"  {e}")

    if diagnostic_required_missing or all_errors:
        return 1

    print(f"  OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
