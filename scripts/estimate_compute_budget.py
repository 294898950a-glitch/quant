#!/usr/bin/env python3
"""Mechanical compute budget estimator for autonomous research runs."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "data" / "research_framework" / "compute_budget_config.json"


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


def load_config(path: Path) -> dict[str, float]:
    data = json.loads(path.read_text(encoding="utf-8"))
    required = [
        "auto_approve_limit_yuan",
        "spot_yuan_per_hour",
        "sig_yuan_per_hour",
        "local_yuan_per_hour",
        "safety_multiplier",
    ]
    missing = [key for key in required if key not in data]
    if missing:
        raise ValueError(f"missing config keys: {', '.join(missing)}")
    return data


def yuan_round(value: float) -> float:
    return math.ceil(value * 100) / 100


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--spot-hours", type=positive_float, default=0.0)
    parser.add_argument("--sig-hours", type=positive_float, default=0.0)
    parser.add_argument("--local-hours", type=positive_float, default=0.0)
    parser.add_argument("--paid-data-yuan", type=positive_float, default=0.0)
    parser.add_argument("--fixed-yuan", type=positive_float, default=0.0)
    parser.add_argument("--json", action="store_true", help="print JSON only")
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
        raw_cost = (
            args.spot_hours * float(cfg["spot_yuan_per_hour"])
            + args.sig_hours * float(cfg["sig_yuan_per_hour"])
            + args.local_hours * float(cfg["local_yuan_per_hour"])
            + args.paid_data_yuan
            + args.fixed_yuan
        )
        estimated = yuan_round(raw_cost * float(cfg["safety_multiplier"]))
        limit = float(cfg["auto_approve_limit_yuan"])
    except Exception as exc:
        print(f"estimate_compute_budget.py: ERROR: {exc}", file=sys.stderr)
        return 2

    result = {
        "currency": cfg.get("currency", "CNY"),
        "spot_hours": args.spot_hours,
        "sig_hours": args.sig_hours,
        "local_hours": args.local_hours,
        "paid_data_yuan": args.paid_data_yuan,
        "fixed_yuan": args.fixed_yuan,
        "safety_multiplier": float(cfg["safety_multiplier"]),
        "estimated_budget_yuan": estimated,
        "auto_approve_limit_yuan": limit,
        "decision": "auto-approve" if estimated <= limit else "wait-user",
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"estimated_budget_yuan: {estimated:.2f}")
        print(f"auto_approve_limit_yuan: {limit:.2f}")
        print(f"decision: {result['decision']}")
    return 0 if estimated <= limit else 10


if __name__ == "__main__":
    sys.exit(main())
