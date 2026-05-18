#!/usr/bin/env python3
"""新研究 batch 启动工具.

替代手动 mkdir + cp spec_template.yaml. 一条命令起新研究:

    python3 scripts/new_research.py <strategy_id> <hypothesis_slug>

做的事:
1. 算 run_id = <strategy_id>_<hypothesis_slug>_<YYYY-MM-DD>
2. 检查 data/<run_id>/ 不存在 (不覆盖现有)
3. 跑 search_ledger.py 查机器实验账本是否 reject 过相似方向
   - 命中 strong match → 拒绝建立 (用户确认后用 --force)
4. mkdir data/<run_id>/
5. 直接生成 data/<run_id>/spec.yaml
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
STRATEGIES_YAML = REPO_ROOT / "data" / "research_framework" / "strategies.yaml"


def load_strategies() -> dict:
    """Load strategies.yaml. 按 Codex 13:02 Finding 2: 明确错误处理."""
    if not STRATEGIES_YAML.exists():
        raise FileNotFoundError(
            f"strategies.yaml missing at {STRATEGIES_YAML}. "
            f"框架损坏, 不能起新研究."
        )
    try:
        data = yaml.safe_load(STRATEGIES_YAML.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ValueError(f"strategies.yaml parse error: {e}")
    if not isinstance(data, dict):
        raise ValueError(f"strategies.yaml root must be dict, got {type(data).__name__}")
    if "strategies" not in data:
        raise ValueError("strategies.yaml missing 'strategies' key")
    if not isinstance(data["strategies"], list):
        raise ValueError(f"strategies.yaml 'strategies' must be list, got {type(data['strategies']).__name__}")
    for i, s in enumerate(data["strategies"]):
        if not isinstance(s, dict) or "id" not in s:
            raise ValueError(f"strategies.yaml entry [{i}] missing 'id' field")
    return data


def validate_strategy_id(strategy_id: str) -> tuple[bool, list[str]]:
    strategies = load_strategies()
    available = [s["id"] for s in strategies.get("strategies", [])]
    return strategy_id in available, available


def slugify(s: str) -> str:
    """Slugify ASCII. 按 Codex 13:02 Finding 3: 中文字符会被去掉, 这里 raise 不让 silent loss."""
    original = s.strip()
    s = original.lower()
    s = re.sub(r"[^a-z0-9-]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    if not s:
        raise ValueError(f"hypothesis_slug '{original}' has no ASCII letter/digit content "
                         f"after slugify. 改用英文 hypothesis slug.")
    # Detect substantial Chinese content loss
    if any("一" <= c <= "鿿" for c in original):
        chinese_chars = sum(1 for c in original if "一" <= c <= "鿿")
        if chinese_chars >= 2:
            raise ValueError(f"hypothesis_slug '{original}' 含 {chinese_chars} 个中文字符, "
                             f"slugify 会丢. 改用英文 slug, 例如 'panic-detector-v2'.")
    return s


def check_ledger_duplicate(hypothesis_slug: str) -> tuple[str, str]:
    """Return (status, output). status: 'ok' / 'strong_match' / 'error'.

    按 Codex 13:02 Finding 4: 区分 strong match vs infrastructure error.
    search_ledger.py rc=1 是 strong match (设计), 其他 non-zero 是 fail.
    """
    cmd = [sys.executable, str(REPO_ROOT / "scripts" / "search_ledger.py"),
           hypothesis_slug.replace("-", " ")]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT,
                                timeout=30)
    except (subprocess.TimeoutExpired, OSError) as e:
        return "error", f"search_ledger invocation failed: {e}"
    if result.returncode == 0:
        return "ok", result.stdout
    if result.returncode == 1:
        return "strong_match", result.stdout + result.stderr
    return "error", (f"search_ledger.py exit {result.returncode}:\n"
                     f"stdout: {result.stdout}\nstderr: {result.stderr}")


def fill_template(out_path: Path, run_id: str, strategy_id: str,
                  hypothesis_slug: str, date: str) -> None:
    """生成 DRAFT spec.yaml, 所有 user-must-fill 字段标 <TODO> sentinel.

    按 Codex 13:02 review Finding 1: 不能复用模板示例值, 否则 validate_spec 会
    误判 DRAFT 已填好. 改成显式 <TODO> sentinel, validate_spec.py 检测到拒收.
    """
    spec = {
        "schema_version": 1,
        "run_id": run_id,
        "date": date,
        "strategy_id": strategy_id,
        "l0_entry_id": "<TODO: 1=user-driven, 2=AI-introspection, 3=arxiv>",
        "l0_source": "<TODO: 简短来源>",
        "hypothesis": f"<TODO: 一句话假设 about '{hypothesis_slug}'>",
        "source_insight": "<TODO: 来源洞察 (为什么这么想)>",
        "parameter_space": [
            {"name": "<TODO>", "range": [0, 1], "type": "<TODO: float/int>",
             "description": "<TODO>"}
        ],
        "new_data_sources": [],
        "grid": {
            "dimensions": ["<TODO>"],
            "candidates_count": 0,
            "description": "<TODO>",
        },
        "hard_floors": {"replay_<TODO_year>": "<TODO: floor 数值>"},
        "hard_floors_baseline_source": "<TODO: floor 来自哪个 baseline>",
        "auxiliary_metrics": ["<TODO>"],
        "cv_design": "<TODO: leave-one-year-out / sealed-pool-8 / walk-forward / etc>",
        "cv_holdout_years": ["<TODO>"],
        "cv_adoption_threshold": "<TODO>",
        "compute_estimate": {
            "sig_minutes": 0, "spot_minutes": 0, "estimated_cost_yuan": 0.0,
        },
        "budget_cap_yuan": 30,
        "spot_decision": "<TODO>",
        "stop_conditions": ["<TODO>"],
        "artifacts_required": ["ranked.csv", "trades.csv", "daily_equity.csv",
                               "summary.csv", "run_summary.json"],
        "automation": {
            "output_dir": f"data/{run_id}",
            "command": ["<TODO: python3 scripts/... --output-dir data/{run_id}>"],
            "verdict": {"pass_field": "adoption_pass"},
        },
        "status": "DRAFT",
        "escalation": [],
        "notes": f"由 new_research.py 自动创建 (hypothesis_slug={hypothesis_slug}). 删除所有 <TODO> 后才能 validate 通过.",
    }
    out_path.write_text(yaml.dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("strategy_id")
    parser.add_argument("hypothesis_slug")
    parser.add_argument("--force", action="store_true", help="跳过经验账本查重")
    parser.add_argument("--dry-run", action="store_true", help="只显示要做什么")
    args = parser.parse_args()

    try:
        ok, available = validate_strategy_id(args.strategy_id)
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    if not ok:
        print(f"ERROR: strategy_id '{args.strategy_id}' not in strategies.yaml", file=sys.stderr)
        print(f"  Available: {available}", file=sys.stderr)
        print(f"  Hint: 加新策略改 data/research_framework/strategies.yaml", file=sys.stderr)
        return 1

    try:
        hypothesis_slug = slugify(args.hypothesis_slug)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    date = dt.date.today().isoformat()
    run_id = f"{args.strategy_id}_{hypothesis_slug}_{date}"
    target_dir = DATA_DIR / run_id

    if target_dir.exists():
        print(f"ERROR: data/{run_id}/ already exists. 不覆盖.", file=sys.stderr)
        print(f"  Hint: 用不同 hypothesis_slug, 或先 rm 旧目录 (谨慎)", file=sys.stderr)
        return 1

    if not args.force:
        print(f"Checking machine experiment ledger for similar '{hypothesis_slug}'...")
        status, output = check_ledger_duplicate(hypothesis_slug)
        if status == "strong_match":
            print()
            print(output)
            print()
            print(f"⚠ STRONG MATCH found in machine experiment ledger.")
            print(f"  This direction may have been already tried/rejected.")
            print(f"  If you want to proceed anyway, re-run with --force")
            print(f"  and write 'aware-of-prior-match' in spec.yaml notes.")
            return 1
        if status == "error":
            print(f"\nERROR running search_ledger.py:\n{output}", file=sys.stderr)
            print(f"  这不是 strong match, 是 search_ledger 自己 fail.", file=sys.stderr)
            print(f"  请检查 search_ledger.py 或 data/research_framework/experiments.yaml 是否完好.", file=sys.stderr)
            return 2
        print("  ✓ No strong duplicate found")

    # Codex 13:02 Finding 6: warn 跨天同 prefix run 已存在
    prefix = f"{args.strategy_id}_{hypothesis_slug}_"
    existing = sorted([d.name for d in DATA_DIR.iterdir()
                       if d.is_dir() and d.name.startswith(prefix) and d.name != run_id])
    if existing and not args.force:
        print()
        print(f"⚠ WARN: 跨天同 strategy+slug 已存在 run(s):")
        for e in existing:
            print(f"    {e}")
        print(f"  如果是有意重做, 用 --force 继续; 否则换 hypothesis_slug")
        return 1

    if args.dry_run:
        print()
        print(f"DRY-RUN:")
        print(f"  Would create: data/{run_id}/spec.yaml")
        print(f"  strategy_id={args.strategy_id}, date={date}, status=DRAFT")
        return 0

    target_dir.mkdir(parents=True)
    out_path = target_dir / "spec.yaml"
    fill_template(out_path, run_id=run_id, strategy_id=args.strategy_id,
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
