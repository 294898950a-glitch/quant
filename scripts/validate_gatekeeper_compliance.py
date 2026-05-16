#!/usr/bin/env python3
"""Validate that run/evaluate/search/monitor scripts import GateKeeper.

按 Codex framework holistic review Q5: bare script bypass (跑批脚本不接 GateKeeper
也能跑, 烧算力没人挡) 是最大漏洞. 加这个 validator AST 扫脚本, 强制 import
GateKeeper + 调 stage 方法.

允许例外: data/research_framework/gatekeeper_allowlist.yaml 列已知豁免脚本.

Usage:
  python3 scripts/validate_gatekeeper_compliance.py

Exit codes:
  0 = all scripts comply or in allowlist
  1 = some script missing GateKeeper + not in allowlist
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml required", file=sys.stderr)
    sys.exit(1)


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
ALLOWLIST_PATH = REPO_ROOT / "data" / "research_framework" / "gatekeeper_allowlist.yaml"

# 需要接 GateKeeper 的脚本前缀
REQUIRED_PREFIXES = ("run_cb_", "evaluate_cb_", "search_cb_", "analyze_cb_", "monitor_cb_")


def load_allowlist() -> set[str]:
    if not ALLOWLIST_PATH.exists():
        return set()
    try:
        data = yaml.safe_load(ALLOWLIST_PATH.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        print(f"ERROR: {ALLOWLIST_PATH} parse error: {e}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict) or "allowlist" not in data:
        return set()
    return {entry.get("script", "") for entry in data["allowlist"]
            if isinstance(entry, dict) and entry.get("script")}


def script_imports_gatekeeper(path: Path) -> bool:
    """AST scan: does this script import GateKeeper from scripts.gatekeeper?"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # from scripts.gatekeeper import GateKeeper
            # from gatekeeper import GateKeeper
            mod = (node.module or "")
            if mod.endswith("gatekeeper"):
                for alias in node.names:
                    if alias.name == "GateKeeper":
                        return True
        elif isinstance(node, ast.Import):
            # import scripts.gatekeeper / import gatekeeper
            for alias in node.names:
                if alias.name.endswith("gatekeeper"):
                    return True
    return False


def list_scripts_needing_compliance() -> list[Path]:
    return sorted([
        p for p in SCRIPTS_DIR.glob("*.py")
        if any(p.name.startswith(prefix) for prefix in REQUIRED_PREFIXES)
    ])


def main() -> int:
    allowlist = load_allowlist()
    scripts = list_scripts_needing_compliance()

    failures = []
    allowed_skips = []
    compliant = []

    for path in scripts:
        rel = str(path.relative_to(REPO_ROOT))
        if rel in allowlist:
            allowed_skips.append(rel)
            continue
        if script_imports_gatekeeper(path):
            compliant.append(rel)
        else:
            failures.append(rel)

    if failures:
        print(f"FAIL: {len(failures)} script(s) missing GateKeeper import:")
        for s in failures:
            print(f"  {s}")
        print()
        print(f"  Fix options:")
        print(f"  1. Add 'from scripts.gatekeeper import GateKeeper' + call stage methods (e.g. gate.before_run_grid)")
        print(f"  2. If script is legacy/data-ETL/utility, add to data/research_framework/gatekeeper_allowlist.yaml with reason+owner")
        if allowed_skips:
            print(f"\n  Allowed skips ({len(allowed_skips)}): {allowed_skips}")
        return 1

    print(f"validate_gatekeeper_compliance.py: OK")
    print(f"  {len(compliant)} compliant, {len(allowed_skips)} in allowlist")
    return 0


if __name__ == "__main__":
    sys.exit(main())
