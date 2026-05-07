"""sp500_grid orchestrator CLI 入口 —— 复用 cb_redemption.Orchestrator 类。

设计要点
--------
- 不复制 Orchestrator 类。直接 import + DI 把 sp500_grid 的 verifier
  以及一组定制 editor adapter 注进去。
- editor adapter 的存在是因为：cb 的 ``editor.read_space`` 硬校验
  ``strategy: cb_redemption``（不可改 editor），而我们的 yaml 是
  ``strategy: sp500_grid``。所以这里走自己的 mini-editor，绕开 cb
  editor 的 strategy 校验，但保持 ``current``/``range`` 写入语义。
- ``Orchestrator`` 的 ``_read_current_params`` 把 yaml.parameters 映射成
  ``weights[i] = params_by_name['w_'+FACTOR_NAMES[i]].current``。本模块
  暴露 3 个 writable 项目，名字借用 FACTOR_NAMES 的前三个，分别对应:

    w_redeem_progress   <-> parameters.grid_count
    w_premium_ratio     <-> parameters.range_window
    w_remaining_size    <-> parameters.position_per_grid

  剩余 weights[3] / weights[4] 默认 0.0；verifier 只解包前 3 维。
- ``rules`` 透传 fee_pct，让 verifier 解析。

CLI
---
::

    python -m strategies.sp500_grid.orchestrator_main \\
        [--max-iterations N] [--cooldown S] \\
        [--data-dir PATH] [--yaml-path PATH] \\
        [--dry-run | --live] [--resume]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from strategies.cb_redemption.orchestrator import (
    DEFAULT_COOLDOWN_S,
    FACTOR_NAMES,
    Orchestrator,
)
from strategies.sp500_grid.verifier import run_backtest as grid_verifier


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent

DEFAULT_DATA_DIR = _REPO_ROOT / "data" / "sp500_grid"
DEFAULT_YAML_PATH = _HERE / "tunable_space.yaml"

# 把 yaml.parameters 的实际名字映射到 cb FACTOR_NAMES 前 N 个。顺序敏感。
# 必须与 tunable_space.yaml 的 parameters 顺序对齐。
PARAM_NAME_MAPPING = (
    # (cb_synthetic_name_for_orchestrator, real_yaml_name)
    ("w_" + FACTOR_NAMES[0], "grid_count"),         # w_redeem_progress
    ("w_" + FACTOR_NAMES[1], "range_window"),       # w_premium_ratio
    ("w_" + FACTOR_NAMES[2], "position_per_grid"),  # w_remaining_size
)
_SYNTH_TO_REAL = dict(PARAM_NAME_MAPPING)
_REAL_TO_SYNTH = {real: synth for synth, real in PARAM_NAME_MAPPING}


# --------------------------------------------------------------------------- #
# YAML helpers (round-trip 保留注释)
# --------------------------------------------------------------------------- #

_yaml = YAML(typ="rt")
_yaml.preserve_quotes = True
_yaml.indent(mapping=2, sequence=4, offset=2)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_yaml(path: Path) -> dict:
    if not Path(path).exists():
        raise FileNotFoundError(f"yaml not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = _yaml.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"yaml top-level must be mapping: {path}")
    return data


def _dump_yaml(path: Path, data: Any) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(path).with_suffix(Path(path).suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        _yaml.dump(data, f)
        f.flush()
    tmp.replace(path)


# --------------------------------------------------------------------------- #
# Editor adapter (替代 cb editor.read_space / list_writable / update_value)
# --------------------------------------------------------------------------- #


def adapter_read_space(path: Path | str) -> dict:
    """读 sp500 yaml 并伪装成 cb-shape：parameters 段用合成的 w_<FACTOR_NAME>。

    Orchestrator._read_current_params:

        params_by_name = {p["name"]: p for p in data.get("parameters", [])}
        weights = [params_by_name.get(f"w_{name}", {}).get("current", 0.0)
                   for name in FACTOR_NAMES]

    所以我们把 grid_count/range_window/position_per_grid 重命名为
    w_redeem_progress/w_premium_ratio/w_remaining_size 返回。
    weights[3:5] 没有对应项，会自动 default 0.0。
    """
    data = _load_yaml(Path(path))
    real_params = list(data.get("parameters", []) or [])

    synth_params = []
    for synth_name, real_name in PARAM_NAME_MAPPING:
        match = next((p for p in real_params if p.get("name") == real_name), None)
        if match is None:
            continue
        synth_params.append({
            "name": synth_name,
            "current": match.get("current"),
            "range": list(match.get("range", [])),
            "prior": match.get("prior", ""),
        })

    return {
        "version": data.get("version", 1),
        "strategy": data.get("strategy", "sp500_grid"),
        "last_updated": data.get("last_updated", ""),
        "parameters": synth_params,
        "factors": list(data.get("factors", []) or []),
        "thresholds": list(data.get("thresholds", []) or []),
        "rules": list(data.get("rules", []) or []),
    }


def adapter_list_writable(path: Path | str) -> list[dict]:
    """暴露 (parameters.w_<synth>, thresholds.*, rules.*) 给 hypothesizer。"""
    data = _load_yaml(Path(path))
    out: list[dict] = []

    real_params = list(data.get("parameters", []) or [])
    for synth_name, real_name in PARAM_NAME_MAPPING:
        match = next((p for p in real_params if p.get("name") == real_name), None)
        if match is None:
            continue
        out.append({
            "item_path": f"parameters.{synth_name}",
            "current": match.get("current"),
            "range": list(match.get("range", [])),
        })
    for t in (data.get("thresholds", []) or []):
        if "current" in t and "range" in t:
            out.append({
                "item_path": f"thresholds.{t['name']}",
                "current": t["current"],
                "range": list(t["range"]),
            })
    for r in (data.get("rules", []) or []):
        if "current" in r and "range" in r:
            out.append({
                "item_path": f"rules.{r['name']}",
                "current": r["current"],
                "range": list(r["range"]),
            })
    return out


def adapter_update_value(
    item_path: str,
    new_value: Any,
    expected_direction: str,
    reason: str,
    path: Path | str,
    audit_log_path: Path | str | None = None,
    audit_log: Path | str | None = None,
) -> dict:
    """单条 .current 值的写入。映射 synth -> real name。"""
    if not isinstance(expected_direction, str) or not expected_direction.strip():
        raise ValueError("expected_direction must be non-empty str")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason must be non-empty str")
    if "." not in item_path:
        raise ValueError(f"item_path must look like 'parameters.<name>': {item_path}")
    section, name = item_path.split(".", 1)
    if section not in {"parameters", "thresholds", "rules"}:
        raise ValueError(f"section {section!r} not writable")

    yaml_path = Path(path)
    data = _load_yaml(yaml_path)

    # parameters 用 synth->real 映射
    real_name = name
    if section == "parameters":
        real_name = _SYNTH_TO_REAL.get(name, name)

    items = data.get(section, []) or []
    target = next((i for i in items if i.get("name") == real_name), None)
    if target is None:
        raise KeyError(f"{section}.{real_name} not in yaml (item_path={item_path})")

    rng = target.get("range")
    if (
        not isinstance(rng, list)
        or len(rng) != 2
        or not (rng[0] <= new_value <= rng[1])
    ):
        raise ValueError(f"{item_path}={new_value} out of range {rng}")

    old = target.get("current")
    target["current"] = new_value
    data["last_updated"] = _utcnow_iso()
    _dump_yaml(yaml_path, data)

    # 审计行
    audit_target: Path | None = None
    if audit_log_path is not None:
        audit_target = Path(audit_log_path)
    elif audit_log is not None:
        audit_target = Path(audit_log)
    if audit_target is not None:
        audit_target.parent.mkdir(parents=True, exist_ok=True)
        with open(audit_target, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": _utcnow_iso(),
                "item_path": item_path,
                "real_name": real_name,
                "old_value": old,
                "new_value": new_value,
                "expected_direction": expected_direction.strip(),
                "reason": reason.strip(),
            }, ensure_ascii=False) + "\n")
            f.flush()

    return {"item_path": item_path, "old_value": old, "new_value": new_value}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sp500_grid_orchestrator",
        description="sp500_grid self-loop daemon (layer 7) — DI on cb.Orchestrator",
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
        # Default: tmp dir so real data/sp500_grid/ + yaml are untouched.
        if data_dir is None:
            data_dir = Path(tempfile.mkdtemp(prefix="cb_dryrun_"))
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
        # 关键 DI:
        verifier_fn=grid_verifier,
        editor_read_fn=adapter_read_space,
        editor_list_fn=adapter_list_writable,
        editor_update_fn=adapter_update_value,
    )
    if args.resume:
        orch.resume()
    final = orch.run()
    return 0 if final.state in {"stopped", "running", "paused"} else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "main",
    "adapter_read_space",
    "adapter_list_writable",
    "adapter_update_value",
    "PARAM_NAME_MAPPING",
    "DEFAULT_DATA_DIR",
    "DEFAULT_YAML_PATH",
]
