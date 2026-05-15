"""Prepare and launch independent concurrent cb_arb pool workers."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from strategies.cb_arb.bootstrap import (
    DEFAULT_CB_DAILY_PARQUET,
    DEFAULT_N_POOLS,
    DEFAULT_OOS_SPLIT_DATE,
    DEFAULT_SEED,
    DEFAULT_YAML_PATH,
    bootstrap,
)


def _default_run_name() -> str:
    return "cb_arb_concurrent_" + datetime.now().strftime("%Y%m%d_%H%M%S")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _initial_state() -> dict[str, Any]:
    return {
        "state": "stopped",
        "iteration": 0,
        "since_iso": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_verdict": None,
        "paused_reason": None,
        "none_streak": 0,
        "stagnant_streak": 0,
        "recovery_attempt": 0,
        "current_pool_id": None,
        "iters_in_current_pool": 0,
        "pending_since_iso": None,
    }


def _prepare_pool_dirs(root: Path, yaml_path: Path, n_pools: int) -> None:
    master = json.loads((root / "sealed_pools.json").read_text(encoding="utf-8"))
    pools = {int(pool["id"]): pool for pool in master.get("pools", [])}
    missing = [pid for pid in range(n_pools) if pid not in pools]
    if missing:
        raise ValueError(f"sealed_pools.json missing pool ids: {missing}")

    for pool_id in range(n_pools):
        pool_dir = root / f"pool_{pool_id}"
        pool_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(yaml_path, pool_dir / "tunable_space.yaml")
        pool_payload = dict(master)
        pool_payload["parallel_parent"] = str(root)
        pool_payload["source_pool_id"] = pool_id
        pool_payload["pools"] = [dict(pools[pool_id])]
        _write_json(pool_dir / "sealed_pools.json", pool_payload)
        _write_json(pool_dir / "state.json", _initial_state())


def _load_api_key(args: argparse.Namespace) -> str | None:
    if os.environ.get("DEEPSEEK_API_KEY"):
        return os.environ["DEEPSEEK_API_KEY"]
    if args.api_key_file and args.api_key_file.exists():
        return args.api_key_file.read_text(encoding="utf-8").strip()
    return None


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


def _stop_worker(root: Path, pool_id: int) -> None:
    pid_path = root / "logs" / f"pool_{pool_id}.pid"
    if not pid_path.exists():
        return
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except ValueError:
        return
    proc_path = Path(f"/proc/{pid}")
    if not proc_path.exists():
        return
    try:
        os.kill(pid, 15)
    except OSError:
        return
    for _ in range(20):
        if not proc_path.exists():
            return
        time.sleep(0.1)
    try:
        os.kill(pid, 9)
    except OSError:
        pass


def _pool_finished(root: Path, pool_id: int) -> bool:
    state_path = root / f"pool_{pool_id}" / "state.json"
    if not state_path.exists():
        return False
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return state.get("state") in {"paused", "stopped", "running"} and int(state.get("iteration") or 0) >= 30


def _worker_env(root: Path, args: argparse.Namespace) -> dict[str, str]:
    env_base = os.environ.copy()
    api_key = _load_api_key(args)
    if api_key:
        env_base["DEEPSEEK_API_KEY"] = api_key
    env_base.setdefault("DEEPSEEK_LLM_LOCK", "1")
    env_base["LLM_QUEUE_LOCK_PATH"] = str(root / "llm_queue" / "deepseek.lock")
    env_base["LLM_QUEUE_LOG_PATH"] = str(root / "llm_queue" / "requests.jsonl")
    return env_base


def _start_worker(root: Path, args: argparse.Namespace, pool_id: int) -> int:
    env_base = _worker_env(root, args)
    logs_dir = root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    pool_dir = root / f"pool_{pool_id}"
    log_path = logs_dir / f"pool_{pool_id}.run.log"
    pid_path = logs_dir / f"pool_{pool_id}.pid"
    cmd = [
        sys.executable,
        "-m",
        "strategies.cb_arb.orchestrator_main",
        "--live",
        "--resume",
        "--data-dir",
        str(pool_dir),
        "--yaml-path",
        str(pool_dir / "tunable_space.yaml"),
        "--cooldown",
        str(args.cooldown),
        "--max-iterations",
        str(args.worker_max_iterations),
        "--no-git-commit",
    ]
    with log_path.open("ab") as log_file:
        proc = subprocess.Popen(
            cmd,
            cwd=Path.cwd(),
            env=env_base,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    pid_path.write_text(str(proc.pid) + "\n", encoding="utf-8")
    return proc.pid


def _start_workers(root: Path, args: argparse.Namespace, pool_ids: list[int]) -> list[tuple[int, int]]:
    return [(pool_id, _start_worker(root, args, pool_id)) for pool_id in pool_ids]


def _supervise(root: Path, args: argparse.Namespace) -> int:
    completed: set[int] = {
        pool_id for pool_id in range(args.n_pools) if _pool_finished(root, pool_id)
    }
    started: set[int] = {
        pool_id
        for pool_id in range(args.n_pools)
        if pool_id not in completed
        and _pid_running(root / "logs" / f"pool_{pool_id}.pid")
    }
    pending = [
        pool_id
        for pool_id in range(args.n_pools)
        if pool_id not in completed and pool_id not in started
    ]
    for pool_id in sorted(completed):
        _stop_worker(root, pool_id)
        print(f"[concurrent] pool_{pool_id} already finished", flush=True)
    for pool_id in sorted(started):
        print(f"[concurrent] pool_{pool_id} already running", flush=True)

    while len(completed) < args.n_pools:
        active = [
            pool_id
            for pool_id in started
            if _pid_running(root / "logs" / f"pool_{pool_id}.pid")
        ]
        for pool_id in list(started - completed):
            if _pool_finished(root, pool_id):
                _stop_worker(root, pool_id)
                completed.add(pool_id)
                print(f"[concurrent] pool_{pool_id} finished", flush=True)

        active = [
            pool_id
            for pool_id in started
            if _pid_running(root / "logs" / f"pool_{pool_id}.pid")
        ]
        while pending and len(active) < args.max_workers:
            pool_id = pending.pop(0)
            pid = _start_worker(root, args, pool_id)
            started.add(pool_id)
            active.append(pool_id)
            print(f"[concurrent] pool_{pool_id} pid={pid}", flush=True)

        time.sleep(args.supervisor_interval)

    print(f"[concurrent] all pools finished root={root}", flush=True)
    return 0


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-name", default=None)
    p.add_argument("--data-root", type=Path, default=None)
    p.add_argument("--yaml-path", type=Path, default=DEFAULT_YAML_PATH)
    p.add_argument("--cb-daily-parquet", type=Path, default=DEFAULT_CB_DAILY_PARQUET)
    p.add_argument("--oos-split-date", default=DEFAULT_OOS_SPLIT_DATE)
    p.add_argument("--n-pools", type=int, default=DEFAULT_N_POOLS)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--cooldown", type=float, default=0.0)
    p.add_argument("--max-workers", type=int, default=4)
    p.add_argument("--supervise", action="store_true", default=False)
    p.add_argument("--supervisor-interval", type=float, default=10.0)
    p.add_argument(
        "--worker-max-iterations",
        type=int,
        default=31,
        help="31 lets the worker run 30 pool iterations, then seal/exhaust the pool.",
    )
    p.add_argument("--api-key-file", type=Path, default=Path("/root/.deepseek_api_key"))
    p.add_argument("--force", action="store_true", default=False)
    p.add_argument(
        "--resume-existing",
        action="store_true",
        default=False,
        help="Use an existing concurrent run directory without recreating it.",
    )
    p.add_argument("--prepare-only", action="store_true", default=False)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    run_name = args.run_name or _default_run_name()
    root = args.data_root or Path("data") / run_name
    root = Path(root)

    if args.resume_existing:
        if not root.exists():
            print(f"[concurrent] ERROR: {root} does not exist", file=sys.stderr)
            return 1
    elif root.exists():
        if not args.force:
            print(f"[concurrent] ERROR: {root} already exists; pass --force", file=sys.stderr)
            return 1
        shutil.rmtree(root)
    if not args.resume_existing:
        root.mkdir(parents=True, exist_ok=True)

        bootstrap(
            data_dir=root,
            yaml_path=args.yaml_path,
            cb_daily_parquet=args.cb_daily_parquet,
            oos_split_date=args.oos_split_date,
            n_pools=args.n_pools,
            seed=args.seed,
            force=True,
            verbose=True,
        )
        _prepare_pool_dirs(root, args.yaml_path, args.n_pools)
        _write_json(
            root / "concurrent_run.json",
            {
                "run_name": run_name,
                "root": str(root),
                "n_pools": args.n_pools,
                "worker_max_iterations": args.worker_max_iterations,
                "cooldown": args.cooldown,
                "yaml_path": str(args.yaml_path),
            },
        )

    if args.prepare_only:
        print(f"[concurrent] prepared {root}")
        return 0

    if args.supervise:
        print(f"[concurrent] supervising root={root} max_workers={args.max_workers}", flush=True)
        return _supervise(root, args)

    first_batch = list(range(min(args.n_pools, args.max_workers)))
    started = _start_workers(root, args, first_batch)
    for pool_id, pid in started:
        print(f"[concurrent] pool_{pool_id} pid={pid}")
    print(f"[concurrent] root={root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
