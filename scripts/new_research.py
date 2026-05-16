#!/usr/bin/env python3
"""新研究 batch 启动工具.

替代手动 mkdir + cp spec_template.yaml. 一条命令起新研究:

    python3 scripts/new_research.py <strategy_id> <hypothesis_slug>

做的事:
1. 算 run_id = <strategy_id>_<hypothesis_slug>_<YYYY-MM-DD>
2. 检查 data/<run_id>/ 不存在 (不覆盖现有)
3. 跑 search_ledger.py 查经验账本是否 reject 过相似方向
   - 命中 strong match → 拒绝建立 (用户确认后用 --force)
4. mkdir data/<run_id>/
5. cp spec_template.yaml → data/<run_id>/spec.yaml
6. 自动填: schema_version=1, run_id, date, strategy_id, status: DRAFT
7. 提示下一步

Usage:
    python3 scripts/new_research.py cb_arb panic-detector-v2
    python3 scripts/new_research.py cb_arb panic-detector-v2 --force
    python3 scripts/new_research.py cb_arb panic-detector-v2 --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml required", file=sys.stderr)
    sys.exit(1)


REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
TEMPLATE_PATH = REPO_ROOT / "docs" / "research_framework" / "spec_template.yaml"
STRATEGIES_YAML = REPO_ROOT / "data" / "research_framework" / "strategies.yaml"


def load_strategies() -> dict:
    if not STRATEGIES_YAML.exists():
        return {}
    return yaml.safe_load(STRATEGIES_YAML.read_text(encoding="utf-8")) or {}


def validate_strategy_id(strategy_id: str) -> tuple[bool, list[str]]:
    strategies = load_strategies()
    available = [s["id"] for s in strategies.get("strategies", [])]
    return strategy_id in available, available


def slugify(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9-]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "untitled"


def check_ledger_duplicate(hypothesis_slug: str) -> tuple[int, str]:
    cmd = [sys.executable, str(REPO_ROOT / "scripts" / "search_ledger.py"),
           hypothesis_slug.replace("-", " ")]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT)
    return result.returncode, result.stdout + result.stderr


def fill_template(template_path: Path, out_path: Path,
                  run_id: str, strategy_id: str, hypothesis_slug: str,
                  date: str) -> None:
    data = yaml.safe_load(template_path.read_text(encoding="utf-8"))
    data["schema_version"] = 1
    data["run_id"] = run_id
    data["date"] = date
    data["strategy_id"] = strategy_id
    data["status"] = "DRAFT"
    data["hypothesis"] = f"(待填) — about '{hypothesis_slug}'"
    out_path.write_text(yaml.dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("strategy_id")
    parser.add_argument("hypothesis_slug")
    parser.add_argument("--force", action="store_true", help="跳过经验账本查重")
    parser.add_argument("--dry-run", action="store_true", help="只显示要做什么")
    args = parser.parse_args()

    ok, available = validate_strategy_id(args.strategy_id)
    if not ok:
        print(f"ERROR: strategy_id '{args.strategy_id}' not in strategies.yaml", file=sys.stderr)
        print(f"  Available: {available}", file=sys.stderr)
        print(f"  Hint: 加新策略改 data/research_framework/strategies.yaml", file=sys.stderr)
        return 1

    hypothesis_slug = slugify(args.hypothesis_slug)
    date = dt.date.today().isoformat()
    run_id = f"{args.strategy_id}_{hypothesis_slug}_{date}"
    target_dir = DATA_DIR / run_id

    if target_dir.exists():
        print(f"ERROR: data/{run_id}/ already exists. 不覆盖.", file=sys.stderr)
        print(f"  Hint: 用不同 hypothesis_slug, 或先 rm 旧目录 (谨慎)", file=sys.stderr)
        return 1

    if not TEMPLATE_PATH.exists():
        print(f"ERROR: template missing at {TEMPLATE_PATH}", file=sys.stderr)
        return 1

    if not args.force:
        print(f"Checking experience_ledger for similar '{hypothesis_slug}'...")
        rc, output = check_ledger_duplicate(hypothesis_slug)
        if rc != 0:
            print()
            print(output)
            print()
            print(f"⚠ STRONG MATCH found in experience_ledger.")
            print(f"  This direction may have been already tried/rejected.")
            print(f"  If you want to proceed anyway, re-run with --force")
            print(f"  and write 'aware-of-prior-match' in spec.yaml notes.")
            return 1
        print("  ✓ No strong duplicate found")

    if args.dry_run:
        print()
        print(f"DRY-RUN:")
        print(f"  Would create: data/{run_id}/spec.yaml")
        print(f"  strategy_id={args.strategy_id}, date={date}, status=DRAFT")
        return 0

    target_dir.mkdir(parents=True)
    out_path = target_dir / "spec.yaml"
    fill_template(TEMPLATE_PATH, out_path,
                  run_id=run_id, strategy_id=args.strategy_id,
                  hypothesis_slug=hypothesis_slug, date=date)

    print()
    print(f"✓ 新研究 batch 已建立")
    print(f"  目录: data/{run_id}/")
    print(f"  spec: data/{run_id}/spec.yaml")
    print()
    print(f"下一步:")
    print(f"  1. 编辑 data/{run_id}/spec.yaml, 填实际值 (hypothesis / parameter_space / etc)")
    print(f"  2. 跑 python3 scripts/validate_spec.py data/{run_id}/spec.yaml")
    print(f"  3. 通过后, 跑批脚本入口调 GateKeeper.before_run_grid(spec_path)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
