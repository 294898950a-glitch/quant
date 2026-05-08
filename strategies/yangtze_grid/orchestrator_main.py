"""yangtze_grid orchestrator CLI 入口 —— 复用 cb_redemption.Orchestrator 类。

设计要点
--------
- 不复制 Orchestrator 类。直接 import + DI 把 yangtze_grid 的 verifier 注进去。
- editor / orchestrator 现在是 strategy-agnostic 的：
  ``editor.read_space`` 只校验 ``strategy`` 字段是非空字符串；
  ``Orchestrator`` 每轮通过 ``editor.read_space()`` 动态构造 ``param_paths``
  和 ``factor_names``，不再硬编码 cb 的 5 个名字。
  本 CLI 直接复用真 editor，无需 mini-editor adapter。
- weights 解包顺序由 yaml ``parameters`` 段顺序决定：
    weights[0] -> grid_count
    weights[1] -> range_window
    weights[2] -> position_per_grid
- ``rules`` 透传 fee_pct，让 verifier 解析。

CLI
---
::

    python -m strategies.yangtze_grid.orchestrator_main \\
        [--max-iterations N] [--cooldown S] \\
        [--data-dir PATH] [--yaml-path PATH] \\
        [--dry-run | --live] [--resume]
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Iterable

import pandas as pd

from strategies.cb_redemption.orchestrator import (
    DEFAULT_COOLDOWN_S,
    Orchestrator,
)
from strategies.yangtze_grid.verifier import run_backtest as grid_verifier


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent

DEFAULT_DATA_DIR = _REPO_ROOT / "data" / "yangtze_grid"
DEFAULT_YAML_PATH = _HERE / "tunable_space.yaml"
DEFAULT_PRICES_PARQUET = _REPO_ROOT / "data" / "yangtze_grid" / "raw" / "600900_daily.parquet"


# --------------------------------------------------------------------------- #
# Pool prices loader
# --------------------------------------------------------------------------- #


def _build_pool_prices_loader(parquet_path: Path):
    """Return a callable ``event_ids -> DataFrame`` for the pool stats layer.

    Mirrors :func:`strategies.sp500_grid.orchestrator_main._build_pool_prices_loader`:
    grid event_ids ARE date strings, so we read the daily parquet once
    and filter on ``date isin event_ids``. Empty / missing parquet is a
    no-op; the orchestrator wraps the call so a parquet glitch never
    paralyses the loop.
    """

    def _loader(event_ids: Iterable[str]) -> pd.DataFrame:
        if not parquet_path.exists():
            return pd.DataFrame(columns=["date", "close"])
        df = pd.read_parquet(parquet_path)
        ids = {str(x) for x in (event_ids or [])}
        if not ids:
            return df.iloc[0:0]
        return df[df["date"].astype(str).isin(ids)].sort_values("date").reset_index(drop=True)

    return _loader


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="yangtze_grid_orchestrator",
        description="yangtze_grid self-loop daemon (layer 7) — DI on cb.Orchestrator",
    )
    p.add_argument("--max-iterations", type=int, default=None)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True)
    mode.add_argument("--live", action="store_false", dest="dry_run")
    p.add_argument("--cooldown", type=float, default=DEFAULT_COOLDOWN_S)
    p.add_argument("--resume", action="store_true", default=False)
    p.add_argument("--data-dir", type=Path, default=None)
    p.add_argument("--yaml-path", type=Path, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)

    data_dir = args.data_dir
    yaml_path = args.yaml_path

    if args.dry_run:
        # Default: tmp dir so real data/yangtze_grid/ + yaml are untouched.
        if data_dir is None:
            data_dir = Path(tempfile.mkdtemp(prefix="yangtze_grid_dryrun_"))
        else:
            data_dir = Path(data_dir)
            data_dir.mkdir(parents=True, exist_ok=True)
        if yaml_path is None:
            src_yaml = DEFAULT_YAML_PATH
            dst_yaml = data_dir / "tunable_space.yaml"
            if src_yaml.exists() and not dst_yaml.exists():
                shutil.copy2(src_yaml, dst_yaml)
            yaml_path = dst_yaml
        else:
            yaml_path = Path(yaml_path)
        print(
            f"[orchestrator_main] dry-run: writing to {data_dir}, yaml at {yaml_path}",
            file=sys.stderr,
        )
        print(
            f"[orchestrator_main] dry-run artifacts at: {data_dir}",
            file=sys.stderr,
        )
    else:
        if data_dir is None:
            data_dir = DEFAULT_DATA_DIR
        else:
            data_dir = Path(data_dir)
        if yaml_path is None:
            yaml_path = DEFAULT_YAML_PATH
        else:
            yaml_path = Path(yaml_path)
        print(
            f"[orchestrator_main] live: writing to {data_dir}, yaml at {yaml_path}",
            file=sys.stderr,
        )

    orch = Orchestrator(
        data_dir=data_dir,
        space_path=yaml_path,
        cooldown_s=args.cooldown,
        max_iterations=args.max_iterations,
        dry_run=args.dry_run,
        # editor / hypothesizer / auditor / memory 全走默认值（真实模块）。
        # 唯一 DI 是 verifier —— 用 yangtze_grid 自己的回测,而不是 cb 的。
        verifier_fn=grid_verifier,
        # 第 9 个角色:pool_stats 层把当前 pool 的 K 线归约成 5 个数字
        # (无任何 bull/bear/震荡 标签), 再交给出主意者让 LLM 自己判断.
        pool_prices_loader_fn=_build_pool_prices_loader(DEFAULT_PRICES_PARQUET),
    )
    if args.resume:
        orch.resume()
    final = orch.run()
    return 0 if final.state in {"stopped", "running", "paused"} else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "main",
    "DEFAULT_DATA_DIR",
    "DEFAULT_YAML_PATH",
    "DEFAULT_PRICES_PARQUET",
    "_build_pool_prices_loader",
]
