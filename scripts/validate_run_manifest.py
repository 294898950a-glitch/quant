"""Validate run manifest YAML schema (F5 spec, phase 1 warn-only).

Checks every data/research_framework/run_manifests/*.yaml has:
- parseable YAML
- required schema fields (per run_manifest_schema.md)
- valid promotion_status / dirty_policy enums

Warn-only: exits 0 on missing fields. Exits 1 only on YAML parse error.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml required (pip install pyyaml)", file=sys.stderr)
    sys.exit(1)

MANIFEST_DIR = Path(__file__).resolve().parent.parent / "data" / "research_framework" / "run_manifests"

REQUIRED = {"schema_version", "batch_id", "strategy_id", "hypothesis_id", "data_window",
            "config_path", "config_hash", "entrypoint", "git_commit", "git_dirty", "dirty_policy",
            "data_snapshot", "compute_host", "compute_cost_yuan", "start_at", "end_at",
            "exit_code", "result_artifact", "artifact_hash", "result_summary",
            "promotion_status", "reviewer", "verdict_at"}
ALLOWED_PROMOTION = {"experiment", "wip", "adopted", "rejected", "archived", "stale", "invalidated"}
ALLOWED_DIRTY = {"allowed_with_list", "forbidden", "unknown"}
ALLOWED_REVIEWER = {"claude", "codex", "user", "auto"}


def validate(path: Path) -> list[str]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        print(f"ERROR: {path.name}: YAML parse failure: {e}", file=sys.stderr)
        sys.exit(1)
    warnings = []
    missing = REQUIRED - set(data.keys() if data else [])
    if missing:
        warnings.append(f"missing required fields: {missing}")
    if data:
        if data.get("promotion_status") not in ALLOWED_PROMOTION:
            warnings.append(f"invalid promotion_status: {data.get('promotion_status')}")
        if data.get("dirty_policy") not in ALLOWED_DIRTY:
            warnings.append(f"invalid dirty_policy: {data.get('dirty_policy')}")
        if data.get("reviewer") not in ALLOWED_REVIEWER:
            warnings.append(f"invalid reviewer: {data.get('reviewer')}")
        if data.get("schema_version") != 1:
            warnings.append(f"schema_version != 1: {data.get('schema_version')}")
    return warnings


def main() -> int:
    if not MANIFEST_DIR.exists():
        print(f"validate_run_manifest.py: {MANIFEST_DIR} does not exist yet (phase 1 OK)")
        return 0
    yamls = list(MANIFEST_DIR.glob("*.yaml"))
    if not yamls:
        print(f"validate_run_manifest.py: no manifests in {MANIFEST_DIR} (phase 1 OK)")
        return 0
    total_warnings = 0
    for path in sorted(yamls):
        warnings = validate(path)
        for w in warnings:
            print(f"  WARN {path.name}: {w}")
            total_warnings += 1
    if total_warnings == 0:
        print(f"validate_run_manifest.py: {len(yamls)} manifest(s) OK")
    else:
        print(f"validate_run_manifest.py: {total_warnings} warning(s) across {len(yamls)} manifest(s) (phase 1 warn-only)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
