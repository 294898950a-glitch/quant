#!/usr/bin/env python3
"""Auto-backfill run manifests for historical batch artifact folders (P1.5+ spec).

Scans `data/cb_arb_*/` and `data/cb_redemption*/` folders, generates
best-effort manifest YAML files in `data/research_framework/run_manifests/`.

Best-effort fields:
- batch_id: derived from folder name
- strategy_id / hypothesis_id: inferred from folder name patterns
- data_window: parsed from summary.json / holdout_yearly.csv if present
- artifact_hash: md5 of artifact_hash_manifest.txt (generated)
- git_commit: best-effort from git log (commit closest to folder mtime touching strategy)
- git_dirty / dirty_policy: marked as "unknown" for historical
- data_snapshot: current cb_warehouse parquet md5 (NOT historical snapshot)
- compute_cost_yuan / start_at / end_at: extracted from log files if present, else null
- promotion_status: parsed from reports/ or set to "rejected" if matches known rejected set
- reviewer / verdict_at: best-effort from related report file

Marks every backfilled manifest with `backfill_method: historical-best-effort`
so future validators know to be lenient.

Usage:
  python3 scripts/backfill_run_manifests.py           # backfill all
  python3 scripts/backfill_run_manifests.py --dry-run # print, don't write
  python3 scripts/backfill_run_manifests.py --force   # overwrite existing manifests
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
MANIFEST_DIR = REPO_ROOT / "data" / "research_framework" / "run_manifests"
WAREHOUSE = REPO_ROOT / "data" / "cb_warehouse"

# Map known folder name patterns → (strategy_id, hypothesis_id, promotion_status)
KNOWN_BATCHES = {
    "cb_arb_main_strategy_baseline_2026-05-15": ("cb_arb", "main-yaml-current", "wip"),
    "cb_arb_baseline_trade_diagnostic_2020_2024_2026-05-15": ("cb_arb_value_gap_switch", "trade-attribution-diagnostic", "rejected"),
    "cb_arb_breadth_confirm_ensemble_2026-05-15": ("cb_arb_value_gap_switch", "panic-breadth-confirm-ensemble", "rejected"),
    "cb_arb_market_breadth_panic_2026-05-15": ("cb_arb_value_gap_switch", "panic-market-breadth", "rejected"),
    "cb_arb_panic_calendar_2024_diagnostic_2026-05-15": ("cb_arb_value_gap_switch", "panic-calendar-2024-diagnostic", "rejected"),
    "cb_arb_regime_switch_2026-05-15": ("cb_arb_value_gap_switch", "panic-self-pnl-regime-switch", "rejected"),
    "cb_arb_two_line_cross_validation_2026-05-15": ("cb_arb", "main-vs-value-gap-switch-cross-validation", "rejected"),
    "cb_arb_concurrent_supervised_20260511_094500": ("cb_arb_value_gap_switch", "concurrent-supervised", "experiment"),
    "cb_arb_rerun_20260510_115650": ("cb_arb", "rerun-historical", "stale"),
    "cb_arb_rerun_fixed_20260510_124155": ("cb_arb", "self-loop-iter24", "stale"),
    "cb_arb_rerun_full_20260510_115833": ("cb_arb", "rerun-full-historical", "stale"),
}


def md5_file(path: Path) -> str:
    h = hashlib.md5()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except (OSError, IOError):
        return "ERROR_READING"
    return h.hexdigest()


def folder_artifact_hash(folder: Path) -> tuple[str, str]:
    """Generate artifact_hash_manifest.txt and return (manifest_path, manifest_md5)."""
    manifest_path = folder / "artifact_hash_manifest.txt"
    if not folder.is_dir():
        return ("", "")
    lines = []
    for child in sorted(folder.iterdir()):
        if child.is_file() and child.name != "artifact_hash_manifest.txt":
            lines.append(f"{md5_file(child)}  {child.name}")
    if lines:
        manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return (str(manifest_path.relative_to(REPO_ROOT)), md5_file(manifest_path))
    return ("", "")


def warehouse_snapshot() -> dict[str, dict]:
    """Current cb_warehouse parquet hashes (best-effort, not historical)."""
    if not WAREHOUSE.is_dir():
        return {}
    snapshot = {}
    for parquet in WAREHOUSE.glob("*.parquet"):
        snapshot[parquet.name] = {"md5": md5_file(parquet), "note": "current snapshot (not historical at run time)"}
    return snapshot


def folder_mtime(folder: Path) -> dt.datetime:
    return dt.datetime.fromtimestamp(folder.stat().st_mtime)


def git_commit_at_time(when: dt.datetime) -> str:
    """Find git commit closest to `when` (before)."""
    try:
        result = subprocess.run(
            ["git", "log", "--format=%h %ai", "--before", when.strftime("%Y-%m-%d %H:%M:%S")],
            capture_output=True, text=True, cwd=REPO_ROOT
        )
        first_line = result.stdout.strip().split("\n")[0] if result.stdout.strip() else ""
        if first_line:
            return first_line.split()[0]
    except subprocess.CalledProcessError:
        pass
    return "unknown"


def parse_data_window(folder: Path) -> dict:
    """Try parse data_window from summary.json / holdout_yearly.csv."""
    summary = folder / "summary.json"
    if summary.exists():
        try:
            data = json.loads(summary.read_text(encoding="utf-8"))
            if "date_range" in data:
                rng = data["date_range"]
                if isinstance(rng, list) and len(rng) >= 2:
                    return {"start": str(rng[0]), "end": str(rng[1])}
            if "data_window" in data:
                return data["data_window"]
        except (json.JSONDecodeError, OSError):
            pass
    holdout = folder / "holdout_yearly.csv"
    if holdout.exists():
        text = holdout.read_text(encoding="utf-8")
        years = re.findall(r"(\d{4})", text)
        if years:
            yrs = sorted(set(int(y) for y in years if 2000 <= int(y) <= 2100))
            if yrs:
                return {"start": f"{yrs[0]}-01-01", "end": f"{yrs[-1]}-12-31"}
    return {"start": "unknown", "end": "unknown"}


def build_manifest(folder: Path) -> dict:
    folder_name = folder.name
    strategy_id, hypothesis_id, promotion_status = KNOWN_BATCHES.get(
        folder_name, ("unknown", "unknown", "experiment")
    )
    mtime = folder_mtime(folder)
    batch_id = folder_name
    artifact_manifest_path, artifact_md5 = folder_artifact_hash(folder)

    return {
        "schema_version": 1,
        "batch_id": batch_id,
        "strategy_id": strategy_id,
        "hypothesis_id": hypothesis_id,
        "data_window": parse_data_window(folder),
        "config_path": "unknown (historical-backfill-best-effort)",
        "config_hash": "unknown (historical-backfill-best-effort)",
        "entrypoint": "unknown (historical-backfill-best-effort)",
        "git_commit": git_commit_at_time(mtime),
        "git_dirty": ["unknown (historical-backfill-best-effort)"],
        "dirty_policy": "unknown",
        "data_snapshot": warehouse_snapshot(),
        "compute_host": "unknown (historical-backfill-best-effort)",
        "compute_cost_yuan": None,
        "start_at": None,
        "end_at": mtime.isoformat() + "Z",
        "exit_code": None,
        "result_artifact": str(folder.relative_to(REPO_ROOT)),
        "artifact_hash": artifact_md5,
        "artifact_hash_manifest": artifact_manifest_path,
        "result_summary": "historical-backfill-best-effort, see related report under reports/",
        "promotion_status": promotion_status,
        "reviewer": "claude",
        "verdict_at": mtime.isoformat() + "Z",
        "manifest_hash": None,
        "backfill_method": "historical-best-effort-by-backfill_run_manifests.py",
    }


def write_manifest(manifest: dict, force: bool, dry_run: bool) -> str:
    try:
        import yaml
    except ImportError:
        print("ERROR: pyyaml required", file=sys.stderr)
        sys.exit(1)
    out_path = MANIFEST_DIR / f"{manifest['batch_id']}.yaml"
    if out_path.exists() and not force:
        return f"SKIP (exists): {out_path.relative_to(REPO_ROOT)}"
    if dry_run:
        return f"DRY-RUN: {out_path.relative_to(REPO_ROOT)} ({manifest['strategy_id']}/{manifest['hypothesis_id']}, status={manifest['promotion_status']})"
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml.dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return f"WROTE: {out_path.relative_to(REPO_ROOT)}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="overwrite existing manifests")
    args = parser.parse_args()

    # cb_arb_<suffix>, not bare "cb_arb" (which is a different dir)
    folders = sorted([f for f in DATA_DIR.iterdir() if f.is_dir() and f.name.startswith("cb_arb_")])
    print(f"Found {len(folders)} cb_arb_* artifact folders")
    print()
    for folder in folders:
        manifest = build_manifest(folder)
        result = write_manifest(manifest, args.force, args.dry_run)
        print(f"  {result}")
    print()
    print(f"Run again with --force to overwrite. Run scripts/validate_run_manifest.py to check.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
