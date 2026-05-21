#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from framework.autonomous.ai_provider_adapter import RegisteredProviderAdapter  # noqa: E402
from framework.autonomous.result_reviewer import write_ai_review, write_review  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate review.yaml for a research run.")
    parser.add_argument("run_dir", help="Run directory, for example data/<run_id>.")
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="Use deterministic fact-only review without calling the registered provider.",
    )
    args = parser.parse_args()

    if args.no_ai:
        output_path = write_review(args.run_dir)
    else:
        adapter = RegisteredProviderAdapter(
            PROJECT_ROOT / "data" / "research_framework" / "ai_providers.yaml",
            repo_root=PROJECT_ROOT,
            entrypoint="scripts/review_result.py",
        )
        output_path = write_ai_review(args.run_dir, ai_adapter=adapter)
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
