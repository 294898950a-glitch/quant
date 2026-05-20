#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from framework.autonomous.recent_results_digest import (  # noqa: E402
    DEFAULT_CURRENT_PATH,
    DEFAULT_DIGEST_PATH,
    update_current_with_recent_results_digest,
    write_recent_results_digest,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a recent results digest from data/*/review.yaml files."
    )
    parser.add_argument("--limit", type=int, default=10, help="Maximum runs to include.")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="Directory containing run directories.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_DIGEST_PATH,
        help="Digest YAML output path.",
    )
    parser.add_argument(
        "--update-current",
        action="store_true",
        help="Update current.yaml recent_results_digest entry.",
    )
    parser.add_argument(
        "--current-path",
        type=Path,
        default=DEFAULT_CURRENT_PATH,
        help="Path to current.yaml when --update-current is set.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    digest = write_recent_results_digest(
        output_path=args.output,
        data_dir=args.data_dir,
        limit=args.limit,
    )
    if args.update_current:
        update_current_with_recent_results_digest(
            current_path=args.current_path,
            digest_path=args.output,
            digest=digest,
        )

    print(
        f"wrote {args.output} with {len(digest.get('runs', []))} runs "
        f"from {digest.get('source_reviews_count', 0)} reviews"
    )
    if args.update_current:
        print(f"updated {args.current_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
