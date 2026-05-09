"""cb_arb bootstrap — 一次性初始化 holdout 池 + state.json.

为什么要存在
-----------
auditor 拒绝 bless 任何在 ``sealed_pools.json`` 缺失下产生的 run.
新仓没这个文件, 需要一次性切. cb_arb 没有"事件"(它每天都跑全集), 所以 OOS pool
按交易日时间顺序切成 N 段连续区间 (不能 random shuffle — 时间序列连续性
不能破坏).

与 cb / yzm 的差异
------------------
- 事件 ID = 交易日字符串 (从 cb_daily.parquet 拿).
- 不读单股价格 parquet, 直接读 cb_daily.parquet 的 trade_date 列.

CLI
---
::

    python -m strategies.cb_arb.bootstrap [--data-dir PATH]
                                          [--n-pools N]
                                          [--force]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from ruamel.yaml import YAML


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent

DEFAULT_DATA_DIR = _REPO_ROOT / "data" / "cb_arb"
DEFAULT_YAML_PATH = _HERE / "tunable_space.yaml"
DEFAULT_CB_DAILY_PARQUET = _REPO_ROOT / "data" / "cb_warehouse" / "cb_daily.parquet"
DEFAULT_OOS_SPLIT_DATE = "20220101"
DEFAULT_N_POOLS = 8
DEFAULT_SEED = 42
SCHEMA_VERSION = 1
STRATEGY_NAME = "cb_arb"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_yaml(yaml_path: Path) -> None:
    if not yaml_path.exists():
        raise FileNotFoundError(
            f"tunable_space.yaml not found at {yaml_path}; "
            "pass --yaml-path or put the file in place first."
        )
    try:
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = YAML(typ="rt").load(f)
    except Exception as exc:
        raise ValueError(f"failed to parse {yaml_path}: {exc}") from exc
    if not isinstance(data, dict) or "parameters" not in data:
        raise ValueError(
            f"{yaml_path} does not look like a tunable_space.yaml "
            "(missing top-level 'parameters' key)."
        )


def _validate_cb_daily(cb_daily_parquet: Path) -> pd.DataFrame:
    if not cb_daily_parquet.exists():
        raise FileNotFoundError(
            f"cb_daily parquet not found at {cb_daily_parquet}; "
            "pull data first."
        )
    df = pd.read_parquet(cb_daily_parquet)
    if df.empty:
        raise ValueError(f"cb_daily parquet at {cb_daily_parquet} is empty")
    if "trade_date" not in df.columns:
        raise ValueError(
            f"cb_daily parquet at {cb_daily_parquet} missing 'trade_date' column "
            f"(have {list(df.columns)})"
        )
    return df


def _slice_dates_chronologically(
    dates: list[str], n_pools: int
) -> list[list[str]]:
    """按时间顺序切成 n_pools 段连续区间 (保证连续性).

    长度差最多为 1.
    """
    if n_pools < 2:
        raise ValueError(f"n_pools must be >= 2, got {n_pools}")
    if len(dates) < n_pools:
        raise ValueError(
            f"only {len(dates)} dates but n_pools={n_pools}; "
            "cannot make non-empty pools."
        )
    n = len(dates)
    base = n // n_pools
    rem = n % n_pools
    pools: list[list[str]] = []
    cursor = 0
    for i in range(n_pools):
        size = base + (1 if i < rem else 0)
        pools.append(dates[cursor:cursor + size])
        cursor += size
    return pools


def _write_sealed_pools(
    sealed_pools_path: Path,
    pools: list[list[str]],
    split_at: str,
    seed: int,
) -> dict:
    data = {
        "version": SCHEMA_VERSION,
        "strategy": STRATEGY_NAME,
        "split_at": split_at,
        "n_pools": len(pools),
        "seed": seed,
        "event_id_col": "trade_date",
        "created_at": _utcnow_iso(),
        "pools": [
            {
                "id": pid,
                "event_ids": list(pool_dates),
                "read_count": 0,
                "first_read_at": None,
                "sealed_at": None,
            }
            for pid, pool_dates in enumerate(pools)
        ],
    }
    sealed_pools_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = sealed_pools_path.with_suffix(sealed_pools_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
    tmp.replace(sealed_pools_path)
    return data


def _write_initial_state(state_path: Path) -> None:
    state = {
        "state": "stopped",
        "iteration": 0,
        "since_iso": _utcnow_iso(),
        "last_verdict": None,
        "paused_reason": None,
        "none_streak": 0,
        "stagnant_streak": 0,
        "recovery_attempt": 0,
        "current_pool_id": None,
        "iters_in_current_pool": 0,
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.flush()
    tmp.replace(state_path)


def bootstrap(
    data_dir: Path = DEFAULT_DATA_DIR,
    yaml_path: Path = DEFAULT_YAML_PATH,
    cb_daily_parquet: Path = DEFAULT_CB_DAILY_PARQUET,
    oos_split_date: str = DEFAULT_OOS_SPLIT_DATE,
    n_pools: int = DEFAULT_N_POOLS,
    seed: int = DEFAULT_SEED,
    force: bool = False,
    verbose: bool = True,
) -> dict:
    """One-shot setup for cb_arb. Returns a summary dict.

    1. Validate yaml + cb_daily parquet.
    2. 取 cb_daily.trade_date 中 date >= oos_split_date 全部唯一日期 (升序).
    3. 按交易日时间顺序切成 ``n_pools`` 段.
    4. 写 sealed_pools.json + state.json.
    """
    data_dir = Path(data_dir)
    yaml_path = Path(yaml_path)
    cb_daily_parquet = Path(cb_daily_parquet)

    sealed_pools_path = data_dir / "sealed_pools.json"
    state_path = data_dir / "state.json"

    if sealed_pools_path.exists() and not force:
        raise FileExistsError(
            f"holdout pool already exists at {sealed_pools_path}; "
            "pass --force to recut (this destroys read-count history)."
        )

    if verbose:
        print(f"[cb_arb bootstrap] validating yaml at {yaml_path}", file=sys.stderr)
    _validate_yaml(yaml_path)

    if verbose:
        print(
            f"[cb_arb bootstrap] validating cb_daily parquet at {cb_daily_parquet}",
            file=sys.stderr,
        )
    df = _validate_cb_daily(cb_daily_parquet)
    total_rows = len(df)

    # 取交易日 (唯一 + 升序), 切到 OOS 段
    dates_str = df["trade_date"].astype(str)
    oos_mask = dates_str >= str(oos_split_date)
    oos_dates = sorted(set(dates_str.loc[oos_mask].tolist()))
    if not oos_dates:
        raise ValueError(
            f"No OOS dates with trade_date >= {oos_split_date}; "
            "either lower the cutoff or wait for fresh data."
        )

    if verbose:
        print(
            f"[cb_arb bootstrap] {len(oos_dates)} unique OOS trading days "
            f"(out of {total_rows} cb_daily rows); slicing into {n_pools} pools",
            file=sys.stderr,
        )

    pools = _slice_dates_chronologically(oos_dates, n_pools)
    pool_sizes = [len(p) for p in pools]

    if sealed_pools_path.exists() and force:
        sealed_pools_path.unlink()

    _write_sealed_pools(sealed_pools_path, pools, oos_split_date, seed)
    _write_initial_state(state_path)

    summary: dict[str, Any] = {
        "rows_total": total_rows,
        "oos_dates": len(oos_dates),
        "n_pools": n_pools,
        "pool_sizes": pool_sizes,
        "pool_date_ranges": [
            {"start": p[0], "end": p[-1]} for p in pools
        ],
        "sealed_pools_path": str(sealed_pools_path),
        "state_path": str(state_path),
        "oos_split_date": oos_split_date,
    }

    if verbose:
        print(
            f"[cb_arb bootstrap] OK. {len(oos_dates)} OOS dates split into "
            f"{n_pools} pools of sizes {pool_sizes}",
            file=sys.stderr,
        )
        for i, p in enumerate(pools):
            print(
                f"[cb_arb bootstrap]   pool {i}: {len(p)} dates ({p[0]} -> {p[-1]})",
                file=sys.stderr,
            )
        print(
            f"[cb_arb bootstrap] sealed_pools.json -> {sealed_pools_path}",
            file=sys.stderr,
        )
        print(
            f"[cb_arb bootstrap] state.json        -> {state_path}",
            file=sys.stderr,
        )
        print(
            "[cb_arb bootstrap] OK. Now you can run: "
            "python -m strategies.cb_arb.orchestrator_main --live",
            file=sys.stderr,
        )

    return summary


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bootstrap",
        description=(
            "One-shot setup for cb_arb self-loop: cuts holdout pools "
            "(time-ordered slices) and seeds state.json."
        ),
    )
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    p.add_argument("--yaml-path", type=Path, default=DEFAULT_YAML_PATH)
    p.add_argument("--cb-daily-parquet", type=Path, default=DEFAULT_CB_DAILY_PARQUET)
    p.add_argument("--oos-split-date", type=str, default=DEFAULT_OOS_SPLIT_DATE)
    p.add_argument("--n-pools", type=int, default=DEFAULT_N_POOLS)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Re-cut pools even if sealed_pools.json already exists.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    try:
        bootstrap(
            data_dir=args.data_dir,
            yaml_path=args.yaml_path,
            cb_daily_parquet=args.cb_daily_parquet,
            oos_split_date=args.oos_split_date,
            n_pools=args.n_pools,
            seed=args.seed,
            force=args.force,
        )
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        print(f"[cb_arb bootstrap] ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "bootstrap",
    "main",
    "DEFAULT_DATA_DIR",
    "DEFAULT_YAML_PATH",
    "DEFAULT_CB_DAILY_PARQUET",
    "DEFAULT_OOS_SPLIT_DATE",
    "DEFAULT_N_POOLS",
    "_slice_dates_chronologically",
]
