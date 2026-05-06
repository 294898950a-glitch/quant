"""Bootstrap layer — one-shot setup for the cb_redemption self-loop.

Why this exists
---------------

The auditor (layer 8) refuses to bless any run unless
``data/cb_redemption/sealed_pools.json`` exists and at least one pool
has not yet been read. On a fresh checkout that file does NOT exist,
so the very first orchestrator iteration is *always* vetoed, the loop
enters recovery, and after three failed attempts pauses with
``recovery exhausted``. That's by design — but operating the system
needs an obvious one-shot to cut the holdout pools and seed an
initial state.json. This module IS that one-shot.

What it does
------------

In strict order:

1. Validate ``yaml_path`` exists and parses.
2. Validate the cb_warehouse parquets we need actually live on disk.
3. Validate the events CSV exists and isn't empty.
4. Slice the OOS slice (events with ``meeting_date >= oos_split_date``)
   into ``n_pools`` disjoint pools via :mod:`holdout`.
5. Write a fresh ``state.json`` so resume()-style logic has a starting
   FSM record.
6. Print a friendly summary and the next command to run.

CLI
---

Run it directly::

    python -m strategies.cb_redemption.bootstrap [--data-dir PATH]
                                                 [--yaml-path PATH]
                                                 [--events-csv PATH]
                                                 [--oos-split-date YYYY-MM-DD]
                                                 [--n-pools N]
                                                 [--seed N]
                                                 [--force]

If ``--force`` is omitted and ``sealed_pools.json`` already exists in
the target ``data-dir``, this raises rather than silently overwriting.
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

from strategies.cb_redemption import editor as editor_mod
from strategies.cb_redemption import holdout as holdout_mod


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent

DEFAULT_DATA_DIR = _REPO_ROOT / "data" / "cb_redemption"
DEFAULT_YAML_PATH = editor_mod.DEFAULT_SPACE_FILE
DEFAULT_EVENTS_CSV = _REPO_ROOT / "data" / "cb_pead" / "raw" / "cb_down_events_full.csv"
DEFAULT_OOS_SPLIT_DATE = "2025-01-01"
DEFAULT_N_POOLS = 4
DEFAULT_SEED = 42

#: Required parquets in data/cb_warehouse/. The bootstrap refuses to
#: proceed if any of these are missing — running a live loop without
#: them would crash later anyway, and surfacing the failure here
#: produces a much more useful error message.
REQUIRED_WAREHOUSE_PARQUETS = (
    "cb_basic.parquet",
    "cb_daily.parquet",
    "strong_timeline_snapshots.parquet",
)


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


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
    except Exception as exc:  # pragma: no cover - ruamel error class varies
        raise ValueError(f"failed to parse {yaml_path}: {exc}") from exc
    if not isinstance(data, dict) or "parameters" not in data:
        raise ValueError(
            f"{yaml_path} does not look like a tunable_space.yaml "
            "(missing top-level 'parameters' key)."
        )


def _validate_warehouse(warehouse_dir: Path) -> None:
    missing = [
        name
        for name in REQUIRED_WAREHOUSE_PARQUETS
        if not (warehouse_dir / name).exists()
    ]
    if missing:
        raise FileNotFoundError(
            f"missing required parquet(s) in {warehouse_dir}: "
            f"{', '.join(missing)}. Refusing to bootstrap a loop that "
            "cannot run a real backtest."
        )


def _validate_events_csv(events_csv: Path) -> pd.DataFrame:
    if not events_csv.exists():
        raise FileNotFoundError(
            f"events CSV not found at {events_csv}; "
            "pass --events-csv or generate the file first."
        )
    df = pd.read_csv(events_csv)
    if df.empty:
        raise ValueError(f"events CSV at {events_csv} is empty")
    if "bond_id" not in df.columns or "meeting_date" not in df.columns:
        raise ValueError(
            f"events CSV at {events_csv} is missing required columns "
            f"(need bond_id + meeting_date; have {list(df.columns)})"
        )
    return df


def _build_oos_events(events_df: pd.DataFrame, oos_split_date: str) -> pd.DataFrame:
    md = pd.to_datetime(events_df["meeting_date"], errors="coerce")
    cutoff = pd.to_datetime(oos_split_date)
    oos_mask = md >= cutoff
    oos = events_df.loc[oos_mask].copy()
    if oos.empty:
        raise ValueError(
            f"No OOS events with meeting_date >= {oos_split_date}; "
            "either lower the cutoff or wait for fresh data."
        )
    # Build a stable event_id of the form "<bond_id>_<meeting_date>".
    oos["event_id"] = (
        oos["bond_id"].astype(str).str.strip()
        + "_"
        + md.loc[oos_mask].dt.strftime("%Y-%m-%d").astype(str)
    )
    # Drop dupes (same bond_id + meeting_date appearing twice). Keep the
    # first; warn if any were dropped — caller probably wants to know.
    before = len(oos)
    oos = oos.drop_duplicates(subset=["event_id"], keep="first")
    if len(oos) < before:
        print(
            f"[bootstrap] dropped {before - len(oos)} duplicate event_ids",
            file=sys.stderr,
        )
    return oos


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
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.flush()
    tmp.replace(state_path)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def bootstrap(
    data_dir: Path = DEFAULT_DATA_DIR,
    yaml_path: Path = DEFAULT_YAML_PATH,
    events_csv: Path = DEFAULT_EVENTS_CSV,
    oos_split_date: str = DEFAULT_OOS_SPLIT_DATE,
    n_pools: int = DEFAULT_N_POOLS,
    seed: int = DEFAULT_SEED,
    force: bool = False,
    warehouse_dir: Path | None = None,
    verbose: bool = True,
) -> dict:
    """One-shot setup. Returns a summary dict.

    Parameters
    ----------
    data_dir
        Where to write ``sealed_pools.json`` + ``state.json``.
    yaml_path
        Path to tunable_space.yaml; only validated for parseability.
    events_csv
        Source of OOS events; must contain ``bond_id`` + ``meeting_date``.
    oos_split_date
        Events with ``meeting_date >= oos_split_date`` form the OOS pool
        material.
    n_pools
        Number of holdout pools to cut.
    seed
        RNG seed for the round-robin shuffle inside :func:`holdout.slice_oos_into_pools`.
    force
        If True, delete any pre-existing ``sealed_pools.json`` first.
    warehouse_dir
        Where to look for the required parquets. Defaults to
        ``data/cb_warehouse`` next to ``data_dir``'s repo root.
    verbose
        Print friendly progress messages to stderr.

    Returns
    -------
    dict with keys ``events_total``, ``events_oos``, ``n_pools``,
    ``pool_sizes``, ``sealed_pools_path``, ``state_path``,
    ``oos_split_date``.
    """
    data_dir = Path(data_dir)
    yaml_path = Path(yaml_path)
    events_csv = Path(events_csv)
    if warehouse_dir is None:
        warehouse_dir = _REPO_ROOT / "data" / "cb_warehouse"
    else:
        warehouse_dir = Path(warehouse_dir)

    sealed_pools_path = data_dir / "sealed_pools.json"
    state_path = data_dir / "state.json"

    if sealed_pools_path.exists() and not force:
        raise FileExistsError(
            f"holdout pool already exists at {sealed_pools_path}; "
            "pass --force to recut (this destroys read-count history)."
        )

    if verbose:
        print(f"[bootstrap] validating yaml at {yaml_path}", file=sys.stderr)
    _validate_yaml(yaml_path)

    if verbose:
        print(
            f"[bootstrap] validating warehouse parquets in {warehouse_dir}",
            file=sys.stderr,
        )
    _validate_warehouse(warehouse_dir)

    if verbose:
        print(f"[bootstrap] reading events from {events_csv}", file=sys.stderr)
    events_df = _validate_events_csv(events_csv)
    events_total = len(events_df)

    oos = _build_oos_events(events_df, oos_split_date)
    events_oos = len(oos)

    if verbose:
        print(
            f"[bootstrap] {events_oos}/{events_total} events fall in OOS "
            f"(meeting_date >= {oos_split_date}); slicing into {n_pools} pools",
            file=sys.stderr,
        )

    # If --force and file exists, remove first so slice_oos_into_pools
    # doesn't raise PoolFileExistsError.
    if sealed_pools_path.exists() and force:
        sealed_pools_path.unlink()

    pool_data = holdout_mod.slice_oos_into_pools(
        oos,
        event_id_col="event_id",
        n_pools=n_pools,
        seed=seed,
        pool_file=sealed_pools_path,
        split_at=oos_split_date,
    )
    pool_sizes = [len(p["event_ids"]) for p in pool_data["pools"]]

    _write_initial_state(state_path)

    summary: dict[str, Any] = {
        "events_total": events_total,
        "events_oos": events_oos,
        "n_pools": n_pools,
        "pool_sizes": pool_sizes,
        "sealed_pools_path": str(sealed_pools_path),
        "state_path": str(state_path),
        "oos_split_date": oos_split_date,
    }

    if verbose:
        print(
            f"[bootstrap] done. {events_oos} OOS events split into "
            f"{n_pools} pools of sizes {pool_sizes}",
            file=sys.stderr,
        )
        print(
            f"[bootstrap] sealed_pools.json -> {sealed_pools_path}",
            file=sys.stderr,
        )
        print(
            f"[bootstrap] state.json        -> {state_path}",
            file=sys.stderr,
        )
        print(
            "[bootstrap] OK. Now you can run: "
            "python -m strategies.cb_redemption.orchestrator --live",
            file=sys.stderr,
        )

    return summary


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bootstrap",
        description=(
            "One-shot setup for cb_redemption self-loop: cuts holdout "
            "pools and seeds state.json so the orchestrator can start."
        ),
    )
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    p.add_argument("--yaml-path", type=Path, default=DEFAULT_YAML_PATH)
    p.add_argument("--events-csv", type=Path, default=DEFAULT_EVENTS_CSV)
    p.add_argument("--oos-split-date", type=str, default=DEFAULT_OOS_SPLIT_DATE)
    p.add_argument("--n-pools", type=int, default=DEFAULT_N_POOLS)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument(
        "--warehouse-dir",
        type=Path,
        default=None,
        help="Override path to data/cb_warehouse/ (mostly for tests).",
    )
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
            events_csv=args.events_csv,
            oos_split_date=args.oos_split_date,
            n_pools=args.n_pools,
            seed=args.seed,
            force=args.force,
            warehouse_dir=args.warehouse_dir,
        )
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        print(f"[bootstrap] ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "bootstrap",
    "main",
    "DEFAULT_DATA_DIR",
    "DEFAULT_YAML_PATH",
    "DEFAULT_EVENTS_CSV",
    "DEFAULT_OOS_SPLIT_DATE",
    "DEFAULT_N_POOLS",
    "REQUIRED_WAREHOUSE_PARQUETS",
]
