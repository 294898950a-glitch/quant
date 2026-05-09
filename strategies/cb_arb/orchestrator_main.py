"""cb_arb orchestrator CLI — DI on cb_redemption.Orchestrator.

设计要点
--------
- 复用 cb_redemption.Orchestrator (strategy-agnostic), 注入 cb_arb 的 verifier.
- editor 全用真实模块 (yaml strategy=cb_arb 通过校验).
- pool_prices_loader_fn: cb_arb "prices" 概念不一样 (是全 CB 全集), 这里返回
  None — pool_stats 层会跳过, 不影响主流程.

CLI
---
::

    python -m strategies.cb_arb.orchestrator_main \\
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

from strategies.cb_arb.verifier import run_backtest as cb_arb_verifier
from strategies.cb_redemption.orchestrator import (
    DEFAULT_COOLDOWN_S,
    Orchestrator,
)


_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent

DEFAULT_DATA_DIR = _REPO_ROOT / "data" / "cb_arb"
DEFAULT_YAML_PATH = _HERE / "tunable_space.yaml"


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cb_arb_orchestrator",
        description="cb_arb self-loop daemon (layer 7) — DI on cb.Orchestrator",
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
        if data_dir is None:
            data_dir = Path(tempfile.mkdtemp(prefix="cb_arb_dryrun_"))
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
        # 唯一 DI: verifier 用 cb_arb 自己的.
        verifier_fn=cb_arb_verifier,
        # cb_arb 的 "pool prices" 不是单股价格, 是 CB 全集; pool_stats 层用不上,
        # 这里设 None — orchestrator 会跳过 pool stats 计算.
        pool_prices_loader_fn=None,
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
]
