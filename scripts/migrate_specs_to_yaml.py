#!/usr/bin/env python3
"""Migrate existing data/<run-id>/spec.md → spec.yaml (best-effort).

Old spec.md is markdown with section headers ("## L0 假设" etc). This script
parses the markdown structure and converts to spec.yaml schema. Best-effort,
not perfect — manual review recommended after migration.

Usage:
  python3 scripts/migrate_specs_to_yaml.py             # migrate all spec.md
  python3 scripts/migrate_specs_to_yaml.py --dry-run   # show what would happen
  python3 scripts/migrate_specs_to_yaml.py --keep-md   # don't delete spec.md after
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml required", file=sys.stderr)
    sys.exit(1)


REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"


def extract_metadata_comments(text: str) -> dict:
    """Parse <!-- l0-entry-id: 2 --> etc. metadata comments."""
    md = {}
    for m in re.finditer(r"<!--\s*([a-zA-Z0-9_-]+):\s*(.+?)\s*-->", text):
        key = m.group(1).strip().replace("-", "_")
        md[key] = m.group(2).strip()
    return md


def extract_h1_title(text: str) -> str:
    m = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def extract_section_body(text: str, heading: str) -> str:
    """Extract body of '## heading' to next ## or end."""
    pattern = rf"^##\s+{re.escape(heading)}.*?\n(.*?)(?=\n##\s+|\Z)"
    m = re.search(pattern, text, re.DOTALL | re.MULTILINE)
    return m.group(1).strip() if m else ""


def parse_top_quote(text: str) -> str:
    """Find a > quote line (typically the hypothesis)."""
    m = re.search(r"^>\s+(.+)$", text, re.MULTILINE)
    return m.group(1).strip().strip('"') if m else ""


def migrate(md_path: Path, dry_run: bool, keep_md: bool) -> bool:
    text = md_path.read_text(encoding="utf-8")
    metadata = extract_metadata_comments(text)
    title = extract_h1_title(text)

    # Hypothesis
    hyp_body = extract_section_body(text, "L0 假设") or extract_section_body(text, "L0 假设 (用户给, 一句话)") or extract_section_body(text, "L0 假设 (一句话)")
    hypothesis = parse_top_quote(hyp_body) or hyp_body.split("\n")[0][:200]

    source_insight = extract_section_body(text, "来源洞察") or ""
    notes = "原 spec.md 已迁移到 spec.yaml, 详细 markdown 内容见 git 历史. 原文件路径: " + str(md_path.relative_to(REPO_ROOT))

    # Try parse 日期 / 研究 id / 策略 from text
    run_id = None
    m = re.search(r"研究\s+id:\s*([\S]+)", text)
    if m:
        run_id = m.group(1).strip("`")
    else:
        run_id = md_path.parent.name

    date = None
    m = re.search(r"日期:\s*(\d{4}-\d{2}-\d{2})", text)
    if m:
        date = m.group(1)

    strategy = None
    m = re.search(r"策略:\s*([a-z_]+)", text)
    if m:
        strategy = m.group(1)
    else:
        strategy = "cb_arb"

    spec = {
        "schema_version": 1,
        "run_id": run_id,
        "date": date or "unknown",
        "strategy_id": strategy,
        "l0_entry_id": int(metadata.get("l0_entry_id", 1)) if metadata.get("l0_entry_id", "1").isdigit() else 1,
        "l0_source": metadata.get("l0_source", "migrated from spec.md"),
        "hypothesis": hypothesis or "(migrated from md, see notes)",
        "source_insight": source_insight or "(migrated from md, see notes)",
        # Mark migrated specs with placeholder structures (the historical batches
        # are already complete; validator should treat these as ARCHIVED not RUNNING)
        "parameter_space": [{"name": "see-original-md", "range": [0, 1], "type": "float",
                             "description": "原 spec.md 表格未自动迁移, 详见 notes 段"}],
        "hard_floors": {"replay_2020": -0.130604, "replay_2021": -0.050441,
                        "replay_2022": 0.014425, "replay_2023": -0.031027},
        "hard_floors_baseline_source": "spec v1.1 normal-state baseline (migrated)",
        "cv_design": "leave-one-year-out",
        "cv_holdout_years": [2019, 2020, 2021, 2022, 2023, 2024],
        "compute_estimate": {"sig_minutes": 30, "spot_minutes": 10, "estimated_cost_yuan": 3.0},
        "budget_cap_yuan": 30,
        "stop_conditions": [
            "0 候选过 4 floor → 停",
            "≥50% 候选过 floor → 必跑 5 项质疑",
            "真 CV < 5/6 → 已确认无效",
        ],
        "artifacts_required": ["ranked.csv", "trades.csv", "daily_equity.csv",
                               "trigger_dates.csv", "summary.csv"],
        "status": "ARCHIVED",  # 历史 spec 已完成
        "notes": notes,
        "title_from_md": title,
        "migrated_from": str(md_path.relative_to(REPO_ROOT)),
        "migrate_method": "best-effort by scripts/migrate_specs_to_yaml.py",
    }

    yaml_path = md_path.parent / "spec.yaml"
    if dry_run:
        print(f"DRY-RUN: {yaml_path.relative_to(REPO_ROOT)}")
        print(f"  hypothesis: {hypothesis[:80]}...")
        print(f"  status: ARCHIVED (migrated)")
        return True

    if yaml_path.exists():
        print(f"SKIP (exists): {yaml_path.relative_to(REPO_ROOT)}")
        return False

    yaml_path.write_text(yaml.dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"WROTE: {yaml_path.relative_to(REPO_ROOT)}")
    if not keep_md:
        backup_path = md_path.parent / "spec.md.archived"
        md_path.rename(backup_path)
        print(f"  archived md to: {backup_path.relative_to(REPO_ROOT)}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-md", action="store_true", help="don't archive spec.md after migration")
    args = parser.parse_args()

    paths = sorted(DATA_DIR.glob("*/spec.md"))
    print(f"Found {len(paths)} spec.md to migrate")
    for p in paths:
        migrate(p, args.dry_run, args.keep_md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
