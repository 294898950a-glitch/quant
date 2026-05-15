"""Validate docs/research_framework/CURRENT.md schema (F5 spec, phase 1 warn-only).

Checks each active strategy section has:
- machine-readable YAML front-matter with required fields
- 决策契约 section (假设 / 证伪条件 / 成本上限 / 下一步)
- Baseline 摘要 section
- Deployment contract 判定 section

Warn-only: exits 0 on missing fields, prints warnings.
Exits 1 only on YAML parse error or fatal file structure issues.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml required (pip install pyyaml)", file=sys.stderr)
    sys.exit(1)

CURRENT_MD = Path(__file__).resolve().parent.parent / "docs" / "research_framework" / "CURRENT.md"

REQUIRED_FRONTMATTER = {"status", "strategy_id", "baseline_row", "kill_date", "last_decision_at",
                        "deployment_contract_status", "research_direction"}
ALLOWED_STATUS = {"experiment", "wip", "adopted", "rejected", "archived", "stale", "invalidated", "n/a"}
ALLOWED_RESEARCH_DIRECTION = {"open", "closed"}
ALLOWED_DEPLOYMENT_STATUS = {"passing", "failing", "unknown", "n/a"}


def main() -> int:
    if not CURRENT_MD.exists():
        print(f"ERROR: {CURRENT_MD} missing", file=sys.stderr)
        return 1

    text = CURRENT_MD.read_text(encoding="utf-8")
    warnings = []
    sections = re.split(r"^## (.+)$", text, flags=re.MULTILINE)
    # sections[0] = preamble, then alternating (heading, body)
    for i in range(1, len(sections), 2):
        heading = sections[i].strip()
        body = sections[i + 1] if i + 1 < len(sections) else ""
        if heading in ("总览", "WIP 文件清单 (untracked / modified 关键文件)", "策略关系图 (lineage 标签)",
                       "协议触发 CURRENT.md 更新事件 (U15, see protocol_redline.md)",
                       "下一步 owner (当前等谁)", "维护规则"):
            continue
        # strategy section
        fm_match = re.search(r"```yaml\n(.*?)\n```", body, re.DOTALL)
        if not fm_match:
            warnings.append(f"[{heading}] missing machine-readable YAML front-matter")
            continue
        try:
            fm = yaml.safe_load(fm_match.group(1))
        except yaml.YAMLError as e:
            print(f"ERROR: [{heading}] YAML parse failure: {e}", file=sys.stderr)
            return 1
        missing = REQUIRED_FRONTMATTER - set(fm.keys())
        if missing:
            warnings.append(f"[{heading}] missing required front-matter fields: {missing}")
        if fm.get("status") not in ALLOWED_STATUS:
            warnings.append(f"[{heading}] invalid status: {fm.get('status')} (allowed: {ALLOWED_STATUS})")
        if fm.get("research_direction") not in ALLOWED_RESEARCH_DIRECTION:
            warnings.append(f"[{heading}] invalid research_direction: {fm.get('research_direction')}")
        if fm.get("deployment_contract_status") not in ALLOWED_DEPLOYMENT_STATUS:
            warnings.append(f"[{heading}] invalid deployment_contract_status: {fm.get('deployment_contract_status')}")
        # Active strategies (not archived) must have decision contract
        if fm.get("status") not in ("archived", "rejected", "invalidated"):
            if "### 决策契约" not in body:
                warnings.append(f"[{heading}] active strategy missing 决策契约 section")
            if "### Baseline 摘要" not in body:
                warnings.append(f"[{heading}] active strategy missing Baseline 摘要 section")
            if "### Deployment contract 判定" not in body:
                warnings.append(f"[{heading}] active strategy missing Deployment contract 判定 section")

    if warnings:
        print(f"validate_current_md.py: {len(warnings)} warning(s) (phase 1 warn-only)")
        for w in warnings:
            print(f"  WARN {w}")
    else:
        print("validate_current_md.py: OK")
    return 0  # phase 1 warn-only, never fail except YAML parse error


if __name__ == "__main__":
    sys.exit(main())
