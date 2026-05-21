#!/usr/bin/env python3
"""Hermes-facing one-minute wake gate for executor-code handoffs.

This script is intentionally narrow. It only exposes open executor-code handoffs
to Hermes and lets Hermes claim/complete those handoffs. It never advances the
quant queue, launches a VM, or calls an AI provider.
"""

from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from framework.autonomous.hermes_executor_handoff import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
