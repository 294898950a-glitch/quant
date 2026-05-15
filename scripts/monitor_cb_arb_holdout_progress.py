"""Write a compact progress snapshot for the current cb_arb holdout rerun."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


RUN_NAME = os.environ.get(
    "CB_ARB_RUN_NAME",
    "cb_arb_rerun_holdout_fixed_20260510_144000",
)
RUN_DIR = Path(os.environ.get("CB_ARB_RUN_DIR", f"data/{RUN_NAME}"))
STATUS_PATH = Path(
    os.environ.get("CB_ARB_STATUS_PATH", f"reports/{RUN_NAME}_latest.txt")
)
HISTORY_PATH = Path(
    os.environ.get("CB_ARB_HISTORY_PATH", f"logs/{RUN_NAME}_progress.log")
)
PROC_PATTERN = (
    "python -m strategies.cb_arb.orchestrator_main.*"
    f"{RUN_NAME}"
)


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as f:
        return sum(1 for _ in f)


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _read_last_jsonl(path: Path) -> dict:
    if not path.exists():
        return {}
    last = ""
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                last = line
    if not last:
        return {}
    try:
        return json.loads(last)
    except json.JSONDecodeError:
        return {}


def _process_status() -> str:
    result = subprocess.run(
        ["pgrep", "-af", PROC_PATTERN],
        check=False,
        capture_output=True,
        text=True,
    )
    lines = [
        line
        for line in result.stdout.splitlines()
        if "pgrep -af" not in line and "monitor_cb_arb_holdout_progress.py" not in line
    ]
    return "running" if lines else "not_running"


def main() -> int:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = _read_json(RUN_DIR / "state.json")
    best = _read_json(RUN_DIR / "best_params.json")
    last = _read_last_jsonl(RUN_DIR / "outbox.jsonl")
    metrics = best.get("metrics") or {}

    lines = [
        f"ts={now}",
        f"process={_process_status()}",
        f"runs={_count_lines(RUN_DIR / 'runs.jsonl')}",
        f"decisions={_count_lines(RUN_DIR / 'decisions.jsonl')}",
        f"outbox={_count_lines(RUN_DIR / 'outbox.jsonl')}",
        (
            "state="
            f"{state.get('state')} iteration={state.get('iteration')} "
            f"pool={state.get('current_pool_id')} pool_iters={state.get('iters_in_current_pool')} "
            f"reason={state.get('paused_reason')}"
        ),
        (
            "best="
            f"iteration={best.get('iteration')} "
            f"excess={metrics.get('excess_return')} "
            f"return={metrics.get('total_return')} "
            f"drawdown={metrics.get('max_drawdown')} "
            f"sharpe={metrics.get('sharpe')} "
            f"trades={metrics.get('total_trades')}"
        ),
        (
            "last="
            f"iteration={last.get('iteration')} phase={last.get('phase')} "
            f"verdict={last.get('verdict')} change={last.get('change_summary')}"
        ),
    ]
    text = "\n".join(lines) + "\n"

    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(text, encoding="utf-8")
    with HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(text.replace("\n", " | ").strip(" | ") + "\n")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
