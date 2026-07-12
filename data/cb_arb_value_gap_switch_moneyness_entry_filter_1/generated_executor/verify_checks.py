#!/usr/bin/env python3
"""Verify all required checks for the generated executor."""
import ast

path = "data/cb_arb_value_gap_switch_moneyness_entry_filter_1/generated_executor/evaluate_cb_arb_moneyness_entry_filter_v2.py"
with open(path) as f:
    source = f.read()

tree = ast.parse(source)
names = [n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

print(f"Functions: {names}")

checks = {}
checks["has_main"] = "main" in names
checks["has_declare_data_requirements"] = "declare_data_requirements" in names
checks["imports_gatekeeper"] = "from scripts.gatekeeper import GateKeeper" in source
checks["writes_summary_json"] = "_write_summary" in names
checks["writes_report_yaml"] = "_write_report" in names
checks["writes_l4_ack_yaml"] = "_write_l4_ack" in names
checks["writes_diagnostic_yaml"] = "_write_diagnostic" in names
checks["summary_has_adoption_pass"] = '"adoption_pass"' in source
checks["no_forbidden_markers"] = True

forbidden = ["TODO", "FIXME", "placeholder", "pseudocode", "demo-only"]
for m in forbidden:
    if m in source:
        print(f"  WARNING: forbidden marker '{m}' found in source")
        checks["no_forbidden_markers"] = False

for check, passed in checks.items():
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {check}")

all_pass = all(checks.values())
print(f"\nAll checks passed: {all_pass}")
