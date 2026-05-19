#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from framework.autonomous.ideation_cycle import IdeationCycle  # noqa: E402
from framework.autonomous.paths import ResearchPaths  # noqa: E402
from scripts.hermes_access_guard import require_ticket  # noqa: E402


DEFAULT_PATHS = ResearchPaths.from_repo_root(REPO_ROOT)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one autonomous ideation -> proposal -> spec compile cycle.")
    parser.add_argument("--config", type=Path, default=DEFAULT_PATHS.strategy_ideator_config)
    parser.add_argument("--digest", type=Path, default=DEFAULT_PATHS.recent_results_digest)
    parser.add_argument("--registry", type=Path, default=DEFAULT_PATHS.executor_registry)
    parser.add_argument("--mechanics-vocab", type=Path, default=DEFAULT_PATHS.mechanics_vocab)
    parser.add_argument("--tool-registry", type=Path, default=DEFAULT_PATHS.evidence_tool_registry)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_PATHS.data_root)
    parser.add_argument("--budget-cap-yuan", type=float, default=None)
    parser.add_argument("--dry-run", action="store_true", help="Generate proposal but do not compile/write spec.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    require_ticket("strategy_ideation_once")
    payload = IdeationCycle(paths=DEFAULT_PATHS).run_once(
        config_path=args.config,
        digest_path=args.digest,
        registry_path=args.registry,
        mechanics_vocab_path=args.mechanics_vocab,
        tool_registry_path=args.tool_registry,
        output_root=args.output_root,
        budget_cap_yuan=args.budget_cap_yuan,
        dry_run=args.dry_run,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("status") in {"READY", "DRAFT", "PROPOSAL_ONLY"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
