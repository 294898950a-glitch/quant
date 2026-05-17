#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from framework.result_reviewer import write_review  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate review.yaml for a research run.")
    parser.add_argument("run_dir", help="Run directory, for example data/<run_id>.")
    args = parser.parse_args()

    output_path = write_review(args.run_dir)
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
