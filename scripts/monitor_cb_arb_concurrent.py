"""Write a compact snapshot for a concurrent cb_arb run."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as f:
        return sum(1 for _ in f)


def _pid_running(pid_path: Path) -> bool:
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except ValueError:
        return False
    cmdline = Path(f"/proc/{pid}/cmdline")
    if not cmdline.exists():
        return False
    try:
        cmd = cmdline.read_bytes().replace(b"\x00", b" ").decode(errors="ignore")
    except OSError:
        return False
    return "strategies.cb_arb.orchestrator_main" in cmd


def _pool_line(root: Path, pool_id: int) -> str:
    pool_dir = root / f"pool_{pool_id}"
    state = _read_json(pool_dir / "state.json")
    best = _read_json(pool_dir / "best_params.json")
    metrics = best.get("metrics") or {}
    running = _pid_running(root / "logs" / f"pool_{pool_id}.pid")
    return (
        f"pool_{pool_id} process={'running' if running else 'not_running'} "
        f"state={state.get('state')} iter={state.get('iteration')} "
        f"pool_iters={state.get('iters_in_current_pool')} "
        f"reason={state.get('paused_reason')} "
        f"runs={_count_lines(pool_dir / 'runs.jsonl')} "
        f"best_iter={best.get('iteration')} "
        f"excess={metrics.get('excess_return')} "
        f"return={metrics.get('total_return')} "
        f"drawdown={metrics.get('max_drawdown')} "
        f"sharpe={metrics.get('sharpe')} "
        f"trades={metrics.get('total_trades')}"
    )


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--data-root",
        type=Path,
        default=Path(os.environ.get("CB_ARB_CONCURRENT_ROOT", "")) if os.environ.get("CB_ARB_CONCURRENT_ROOT") else None,
    )
    p.add_argument("--n-pools", type=int, default=8)
    p.add_argument("--status-path", type=Path, default=None)
    p.add_argument("--history-path", type=Path, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    if args.data_root is None:
        print("ERROR: pass --data-root or set CB_ARB_CONCURRENT_ROOT")
        return 1
    root = args.data_root
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [f"ts={now}", f"root={root}"]
    lines.extend(_pool_line(root, pool_id) for pool_id in range(args.n_pools))
    queue_log = root / "llm_queue" / "requests.jsonl"
    lines.append(f"llm_queue_events={_count_lines(queue_log)}")
    text = "\n".join(lines) + "\n"

    status_path = args.status_path or root / "latest.txt"
    history_path = args.history_path or root / "progress.log"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(text, encoding="utf-8")
    with history_path.open("a", encoding="utf-8") as f:
        f.write(text.replace("\n", " | ").strip(" | ") + "\n")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
