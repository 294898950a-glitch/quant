"""Break down stop-loss outcomes by source of value gap."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.evaluate_cb_arb_value_gap_switch import (  # noqa: E402
    _load_or_build_value_ranks,
    _run_value_gap_backtest,
)
from strategies.cb_arb.verifier import _load_cb_daily  # noqa: E402


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def _source(row: Any) -> tuple[str, float, float]:
    close = float(row.close)
    bond_floor = float(row.bond_floor)
    option_value = float(row.option_value)
    theoretical = float(row.theoretical)
    gap = theoretical - close
    if gap <= 0:
        return "not_undervalued", 0.0, 0.0
    bond_gap = max(0.0, bond_floor - close)
    option_gap = max(0.0, theoretical - max(close, bond_floor))
    total = bond_gap + option_gap
    if total <= 0:
        return "mixed", 0.0, 0.0
    bond_share = bond_gap / total
    option_share = option_gap / total
    if bond_share >= 0.60:
        return "bond", bond_share, option_share
    if option_share >= 0.60:
        return "option", bond_share, option_share
    return "mixed", bond_share, option_share


def _future_return(close_by_ts: dict[str, list[tuple[str, float]]], ts: str, date: str, days: int) -> float | None:
    series = close_by_ts.get(ts)
    if not series:
        return None
    idx = None
    for i, (d, _) in enumerate(series):
        if d >= date:
            idx = i
            break
    if idx is None or idx >= len(series):
        return None
    j = min(len(series) - 1, idx + days)
    start_px = float(series[idx][1])
    end_px = float(series[j][1])
    if start_px <= 0:
        return None
    return end_px / start_px - 1.0


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root", type=Path, required=True)
    p.add_argument("--ranks-dir", type=Path, required=True)
    p.add_argument("--trades-file", type=Path, default=None)
    p.add_argument("--start", default="20190101")
    p.add_argument("--end", default="20241231")
    p.add_argument("--fixed-source", type=int, default=2)
    p.add_argument("--rule", default="score_4state")
    p.add_argument("--stop-gap-ratio-floor", type=float, default=0.30)
    p.add_argument("--output-dir", type=Path, default=None)
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    output_dir = args.output_dir or args.data_root / "stop_source_breakdown"
    output_dir.mkdir(parents=True, exist_ok=True)
    ranks = _load_or_build_value_ranks(
        args.data_root,
        args.start,
        args.end,
        args.fixed_source,
        args.rule,
        args.ranks_dir / "daily_value_gap_amounts_buy.parquet",
        True,
    )
    ranks_by_key = {
        (str(r.trade_date), str(r.ts_code)): r
        for r in ranks.itertuples(index=False)
    }

    if args.trades_file is not None and args.trades_file.exists():
        trades = pd.read_csv(args.trades_file)
    else:
        result = _run_value_gap_backtest(
            ranks,
            args.start,
            args.end,
            args.data_root,
            args.fixed_source,
            args.rule,
            {
                "min_gap_pct": 0.0,
                "sell_gap_pct": 0.0,
                "switch_hurdle_pct": 0.03,
                "max_hold_days": 180.0,
                "stop_gap_ratio_floor": float(args.stop_gap_ratio_floor),
                "stop_signal_threshold": 999.0,
            },
            stop_revalue_ranks=ranks,
        )
        trades_path = output_dir / "normal_revalue_trades.csv"
        _write_csv(trades_path, result["trades"])
        trades = pd.DataFrame(result["trades"])
    trades["exit_date"] = trades["exit_date"].astype(str)
    trades["entry_date"] = trades["entry_date"].astype(str)
    trades["cb_code"] = trades["cb_code"].astype(str)
    stop_trades = trades[trades["exit_reason"].astype(str).str.startswith("stop_loss")].copy()

    cb_daily = _load_cb_daily()
    cb_daily["trade_date"] = cb_daily["trade_date"].astype(str)
    cb_daily = cb_daily[(cb_daily["trade_date"] >= args.start) & (cb_daily["trade_date"] <= args.end)]
    close_by_ts = {
        ts: [(str(r.trade_date), float(r.close)) for r in g.itertuples(index=False)]
        for ts, g in cb_daily.sort_values(["ts_code", "trade_date"]).groupby("ts_code")
    }

    detail_rows: list[dict[str, Any]] = []
    for t in stop_trades.itertuples(index=False):
        key = (str(t.exit_date), str(t.cb_code))
        row = ranks_by_key.get(key)
        if row is None:
            src, bond_share, option_share = "missing", None, None
            gap_amount = None
            gap_pct = None
        else:
            src, bond_share, option_share = _source(row)
            gap_amount = float(row.value_gap_amount)
            gap_pct = float(row.value_gap_pct_of_cash)
        detail_rows.append(
            {
                "cb_code": str(t.cb_code),
                "cb_name": str(t.cb_name),
                "entry_date": str(t.entry_date),
                "exit_date": str(t.exit_date),
                "exit_reason": str(t.exit_reason),
                "pnl_pct": float(t.pnl_pct),
                "pnl_amount": float(t.pnl_amount),
                "entry_gap_amount": float(t.entry_gap_amount),
                "entry_gap_pct": float(t.entry_gap_pct),
                "exit_gap_amount": gap_amount,
                "exit_gap_pct": gap_pct,
                "source": src,
                "bond_share": bond_share,
                "option_share": option_share,
                "ret_30d_after_exit": _future_return(close_by_ts, str(t.cb_code), str(t.exit_date), 30),
                "ret_60d_after_exit": _future_return(close_by_ts, str(t.cb_code), str(t.exit_date), 60),
                "ret_120d_after_exit": _future_return(close_by_ts, str(t.cb_code), str(t.exit_date), 120),
                "ret_180d_after_exit": _future_return(close_by_ts, str(t.cb_code), str(t.exit_date), 180),
            }
        )

    summary_rows: list[dict[str, Any]] = []
    for src in sorted({r["source"] for r in detail_rows}):
        group = [r for r in detail_rows if r["source"] == src]
        pnl = [float(r["pnl_pct"]) for r in group]
        pnl_amount = [float(r["pnl_amount"]) for r in group]
        ret30 = [float(r["ret_30d_after_exit"]) for r in group if r["ret_30d_after_exit"] is not None]
        ret60 = [float(r["ret_60d_after_exit"]) for r in group if r["ret_60d_after_exit"] is not None]
        ret120 = [float(r["ret_120d_after_exit"]) for r in group if r["ret_120d_after_exit"] is not None]
        ret180 = [float(r["ret_180d_after_exit"]) for r in group if r["ret_180d_after_exit"] is not None]
        summary_rows.append(
            {
                "source": src,
                "count": len(group),
                "avg_pnl_pct": _mean(pnl),
                "sum_pnl_amount": round(sum(pnl_amount), 2),
                "avg_ret_30d_after_exit": _mean(ret30),
                "avg_ret_60d_after_exit": _mean(ret60),
                "avg_ret_120d_after_exit": _mean(ret120),
                "avg_ret_180d_after_exit": _mean(ret180),
                "positive_180d_count": sum(1 for v in ret180 if v > 0),
                "ret180_count": len(ret180),
            }
        )

    _write_csv(output_dir / "stop_source_detail.csv", detail_rows)
    _write_csv(output_dir / "stop_source_summary.csv", summary_rows)
    (output_dir / "summary.json").write_text(
        json.dumps(
            {
                "start": args.start,
                "end": args.end,
                "n_stop_trades": len(detail_rows),
                "summary": summary_rows,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"[stop_source_breakdown] wrote {output_dir}", flush=True)
    print(json.dumps(summary_rows, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
