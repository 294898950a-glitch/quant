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


# GateKeeper stage method 必至少调用其中一个 (Codex 13:52 verify 加)
STAGE_METHODS = {"before_run_grid", "after_run_grid", "before_l5_diagnostic",
                 "before_commit_truth", "quick_check"}


def script_complies(path: Path) -> tuple[bool, str]:
    """AST scan: import GateKeeper + 实例化 + 用该实例调 stage 方法.

    按 Codex 13:52/13:57 verify: dead import + dummy object bypass 都拒.
    必须 (1) import GateKeeper (2) GateKeeper(...) 实例化 (3) 用实例变量调
    stage method. 三件齐才算 compliant.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError) as e:
        return False, f"parse error: {e}"

    imports_gatekeeper = False
    # 收集"赋值给 GateKeeper() 的变量名" (e.g. gate = GateKeeper() → 'gate')
    gatekeeper_instance_names: set[str] = set()
    # 收集"在 GateKeeper 实例上调用的 stage method" (must match instance var name)
    stage_calls_on_gatekeeper_instance: set[str] = set()

    # Pass 1: find imports + 实例化赋值
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = (node.module or "")
            if mod.endswith("gatekeeper"):
                for alias in node.names:
                    if alias.name == "GateKeeper":
                        imports_gatekeeper = True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith("gatekeeper"):
                    imports_gatekeeper = True
        # 赋值 var = GateKeeper(...) → 记录 var 名
        if isinstance(node, ast.Assign):
            value = node.value
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
                if value.func.id == "GateKeeper":
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            gatekeeper_instance_names.add(target.id)

    # Pass 2: 找 var.stage_method(...) 调用, 验证 var 是 GateKeeper 实例
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in STAGE_METHODS:
                # func.value 是 ast.Name (var 名) 或更复杂
                if isinstance(func.value, ast.Name):
                    if func.value.id in gatekeeper_instance_names:
                        stage_calls_on_gatekeeper_instance.add(func.attr)

    if not imports_gatekeeper:
        return False, "missing 'from scripts.gatekeeper import GateKeeper'"
    if not gatekeeper_instance_names:
        return False, ("import GateKeeper 但没实例化 (GateKeeper() 调用); "
                       "dead import 不算 compliant")
    if not stage_calls_on_gatekeeper_instance:
        return False, (f"实例化了 GateKeeper 但没调 stage method "
                       f"({sorted(STAGE_METHODS)}); "
                       f"dummy object 调同名方法也不算")
    return True, f"GateKeeper instances: {sorted(gatekeeper_instance_names)}; calls: {sorted(stage_calls_on_gatekeeper_instance)}"


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
        ok, reason = script_complies(path)
        if ok:
            compliant.append((rel, reason))
        else:
            failures.append((rel, reason))

    if failures:
        print(f"FAIL: {len(failures)} script(s) not GateKeeper-compliant:")
        for rel, reason in failures:
            print(f"  {rel}")
            print(f"    {reason}")
        print()
        print(f"  Fix options:")
        print(f"  1. Add 'from scripts.gatekeeper import GateKeeper' + 调用 stage 方法 (before_run_grid / after_run_grid / etc)")
        print(f"  2. If script is legacy/data-ETL/utility, add to data/research_framework/gatekeeper_allowlist.yaml with reason+owner")
        if allowed_skips:
            print(f"\n  Allowed skips ({len(allowed_skips)}): {allowed_skips}")
        return 1

    print(f"validate_gatekeeper_compliance.py: OK")
    print(f"  {len(compliant)} compliant, {len(allowed_skips)} in allowlist")
    if compliant:
        for rel, reason in compliant:
            print(f"  ✓ {rel}: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
