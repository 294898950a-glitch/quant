#!/usr/bin/env python3
"""Framework preflight — aggregate validators + dirty-file inventory (P1.2 spec).

Usage:
  python3 scripts/framework_preflight.py           # check all + dirty inventory
  python3 scripts/framework_preflight.py --quiet   # only show failures

Exit codes:
  0 = all OK
  1 = strict failure in any validator
  2 = only warnings

Run by:
  - git pre-commit hook (P1.3) when touching strategy files
  - Codex/Claude before claims touching strategy truth
  - validate_truth_sync.py closes the "truth changed but CURRENT/baseline not updated" gap
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from framework.autonomous.framework_change_recorder import auto_record_framework_changes  # noqa: E402

STRATEGY_FILE_PATTERNS = [
    "strategies/cb_arb/*.py",
    "strategies/cb_arb/*.yaml",
    "strategies/cb_redemption/*.py",
    "scripts/evaluate_cb_arb_*.py",
    "scripts/search_cb_arb_*.py",
]


def run_validator(script: str) -> int:
    print(f"\n=== {script} ===")
    result = subprocess.run([sys.executable, str(SCRIPTS / script)], cwd=REPO_ROOT)
    return result.returncode


def dirty_inventory() -> list[str]:
    """List untracked + modified strategy/script files relevant to research."""
    result = subprocess.run(
        ["git", "status", "--short"], capture_output=True, text=True, cwd=REPO_ROOT
    )
    dirty = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        status, _, path = line.partition(" ")
        path = path.strip()
        if path in {"AGENTS.md", "CLAUDE.md"} \
           or path.startswith("strategies/") or path.startswith("scripts/evaluate_cb_") \
           or path.startswith("scripts/search_cb_") or path.startswith("data/research_framework/") \
           or path.startswith("docs/research_framework/"):
            dirty.append(f"{status} {path}")
    return dirty


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    exits = []
    exits.append(run_validator("validate_entrypoints.py"))
    exits.append(run_validator("validate_compute_budget.py"))
    exits.append(run_validator("validate_current_md.py"))
    exits.append(run_validator("validate_run_manifest.py"))
    exits.append(run_validator("validate_truth_sync.py"))
    exits.append(run_validator("validate_no_background_llm.py"))
    exits.append(run_validator("validate_module_decoupling.py"))
    exits.append(run_validator("validate_data_schema.py"))
    exits.append(run_validator("validate_spec.py"))
    exits.append(run_validator("validate_report.py"))
    exits.append(run_validator("validate_l4_ack.py"))
    exits.append(run_validator("validate_l5_diagnostic.py"))
    exits.append(run_validator("validate_baseline_registry.py"))
    exits.append(run_validator("validate_gatekeeper_compliance.py"))

    print("\n=== dirty-file inventory (research-relevant) ===")
    dirty = dirty_inventory()
    if dirty:
        for entry in dirty:
            print(f"  {entry}")
        print(f"\nTotal: {len(dirty)} dirty/untracked file(s) in research scope")
    else:
        print("  clean")

    print("\n=== framework change recorder ===")
    change_hash = auto_record_framework_changes(repo_root=REPO_ROOT)
    if change_hash:
        print(f"  recorded framework_change_event_hash={change_hash}")
    else:
        print("  no framework-scope changes detected")

    print("\n=== preflight summary ===")
    if 1 in exits:
        print("FAIL: at least one strict failure")
        return 1
    if dirty and not args.quiet:
        print("WARN: dirty research files present (preflight passes, but commit/claim should be intentional)")
        return 2
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
