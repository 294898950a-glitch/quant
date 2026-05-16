#!/usr/bin/env python3
"""Validate data/<run-id>/l4_ack.yaml against HDRF L4 schema.

Replaces soft "Codex grep Q1-Q5 ACK string" check with strict YAML schema.

Required fields (缺 / answer 空 / answer 占位符 → exit 1):
- schema_version, run_id, reviewer, ack_at
- q1_floor_binding, q2_selection_score, q3_baseline_alignment, q4_monotonic, q5_trade_overlap
  - each must have: description, answer (non-empty, non-placeholder), pass (bool)
- overall_pass, overall_decision (adopt/reject/mini-spec-retry/archive-direction)
- overall_reason (non-empty)

Q6/Q7 only required when applicable=true (新机制改变交易路径).

Only validates l4_ack.yaml in data/<run-id>/ where spec.yaml status is
COMPLETE or RUNNING (not DRAFT/ARCHIVED) — historical archived batches
don't need l4_ack.yaml.

Usage:
  python3 scripts/validate_l4_ack.py
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

REQUIRED_TOPLEVEL = {"schema_version", "run_id", "reviewer", "ack_at",
                     "q1_floor_binding", "q2_selection_score", "q3_baseline_alignment",
                     "q4_monotonic", "q5_trade_overlap", "overall_pass",
                     "overall_decision", "overall_reason"}

REQUIRED_Q_FIELDS = {"description", "answer", "pass"}
# Questions that must have computed_data filled by auto_compute_l4_data.py (not Claude手抄)
# 按 Codex 12:07 review: Q3 docstring 已承诺 auto-compute baseline 对齐, 加进来
AUTO_COMPUTED_QUESTIONS = {"q1_floor_binding", "q3_baseline_alignment",
                           "q4_monotonic", "q5_trade_overlap"}

ALLOWED_DECISION = {"adopt", "reject", "mini-spec-retry", "archive-direction"}
ALLOWED_REVIEWER = {"claude", "codex", "user"}

PLACEHOLDER_TOKENS = {"TBD", "tbd", "...", "skip", "TODO", "todo", "n/a", "N/A",
                      "pending", "PENDING"}


def is_placeholder(value: str) -> bool:
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if not stripped:
        return True
    return stripped in PLACEHOLDER_TOKENS or stripped.startswith("(") and stripped.endswith(")")


def validate_question(qkey: str, q: dict, required_apply: bool = True) -> list[str]:
    """Validate a Q1-Q7 entry. Returns list of errors."""
    errs = []
    if not isinstance(q, dict):
        return [f"{qkey}: must be dict"]
    if qkey in ("q6_trigger_timing", "q7_path_contamination"):
        applicable = q.get("applicable")
        if not isinstance(applicable, bool):
            errs.append(f"{qkey}: applicable must be boolean")
            return errs
        if not applicable:
            return []  # 不适用, 跳过
    for f in REQUIRED_Q_FIELDS:
        if f not in q:
            errs.append(f"{qkey}: missing field '{f}'")
    if "answer" in q and is_placeholder(q["answer"]):
        errs.append(f"{qkey}: answer is placeholder/empty — must fill real content")
    if "pass" in q and not isinstance(q["pass"], bool):
        errs.append(f"{qkey}: pass must be boolean")
    # AUTO_COMPUTED_QUESTIONS must have computed_data + computed_at (filled by auto tool, not Claude)
    if qkey in AUTO_COMPUTED_QUESTIONS:
        if "computed_data" not in q:
            errs.append(f"{qkey}: missing computed_data (run scripts/auto_compute_l4_data.py first)")
        if "computed_at" not in q:
            errs.append(f"{qkey}: missing computed_at timestamp (data must be auto-computed, not hand-filled)")
    return errs


def validate(path: Path) -> tuple[list[str], list[str]]:
    """Return (errors, warnings)."""
    errs = []
    warns = []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        return [f"YAML parse error: {e}"], []
    if not isinstance(data, dict):
        return ["root must be dict"], []

    missing = REQUIRED_TOPLEVEL - set(data.keys())
    if missing:
        errs.append(f"missing required fields: {sorted(missing)}")

    if data.get("schema_version") != 1:
        errs.append(f"schema_version must be 1, got {data.get('schema_version')}")
    if data.get("reviewer") not in ALLOWED_REVIEWER:
        errs.append(f"reviewer must be {ALLOWED_REVIEWER}, got {data.get('reviewer')}")
    if data.get("overall_decision") not in ALLOWED_DECISION:
        errs.append(f"overall_decision must be {ALLOWED_DECISION}, got {data.get('overall_decision')}")
    if "overall_pass" in data and not isinstance(data["overall_pass"], bool):
        errs.append("overall_pass must be boolean")
    if "overall_reason" in data and is_placeholder(data["overall_reason"]):
        errs.append("overall_reason is placeholder/empty")

    # Q1-Q5 must be filled
    for qkey in ["q1_floor_binding", "q2_selection_score", "q3_baseline_alignment",
                 "q4_monotonic", "q5_trade_overlap"]:
        if qkey in data:
            errs.extend(validate_question(qkey, data[qkey] or {}))

    # Q6/Q7 only required when applicable
    for qkey in ["q6_trigger_timing", "q7_path_contamination"]:
        if qkey in data:
            errs.extend(validate_question(qkey, data[qkey] or {}))
        else:
            warns.append(f"{qkey} missing — assume not applicable, recommend explicit applicable=false")

    return errs, warns


def is_ack_required(run_dir: Path) -> bool:
    """Check if this run requires l4_ack.yaml.

    Required when spec.yaml status is RUNNING or COMPLETE.
    DRAFT/READY/ARCHIVED don't need it.
    """
    spec_path = run_dir / "spec.yaml"
    if not spec_path.exists():
        return False
    try:
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        return spec.get("status") in ("RUNNING", "COMPLETE")
    except Exception:
        return False


def main() -> int:
    # 按 Codex framework Q2-B: 加 --run-dir 让 GateKeeper 能锁单 run; 不传时 fall back 扫全部
    import argparse
    parser = argparse.ArgumentParser(description="Validate L4 ack yaml")
    parser.add_argument("--run-dir", type=Path, default=None,
                        help="单 run 模式: 只扫这个 dir 的 l4_ack.yaml (GateKeeper 透传用)")
    args = parser.parse_args()

    if args.run_dir is not None:
        if not args.run_dir.exists() or not (args.run_dir / "spec.yaml").exists():
            print(f"ERROR: --run-dir {args.run_dir} 不存在或缺 spec.yaml", file=sys.stderr)
            return 1
        run_dirs = [args.run_dir]
    else:
        run_dirs = sorted([d for d in DATA_DIR.iterdir() if d.is_dir() and (d / "spec.yaml").exists()])

    total_err = 0
    total_warn = 0
    checked = 0
    skipped = 0

    for run_dir in run_dirs:
        if not is_ack_required(run_dir):
            skipped += 1
            continue
        ack_path = run_dir / "l4_ack.yaml"
        if not ack_path.exists():
            print(f"FAIL: {run_dir.name} — spec.yaml status RUNNING/COMPLETE but l4_ack.yaml missing")
            total_err += 1
            continue
        errors, warnings = validate(ack_path)
        checked += 1
        if errors:
            print(f"\nFAIL: {ack_path.relative_to(REPO_ROOT)}")
            for e in errors:
                print(f"  ERROR {e}")
            total_err += len(errors)
        if warnings:
            print(f"\nWARN: {ack_path.relative_to(REPO_ROOT)}")
            for w in warnings:
                print(f"  WARN  {w}")
            total_warn += len(warnings)
        if not errors and not warnings:
            print(f"OK: {ack_path.relative_to(REPO_ROOT)}")

    print(f"\nvalidate_l4_ack.py: {checked} checked, {skipped} skipped (DRAFT/READY/ARCHIVED), "
          f"{total_err} error(s), {total_warn} warning(s)")
    return 1 if total_err else 0


if __name__ == "__main__":
    sys.exit(main())
