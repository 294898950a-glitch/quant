#!/usr/bin/env python3
"""Append-only JSONL memory helpers for the quant research framework."""

from __future__ import annotations

import argparse
import fcntl
import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo


DATA_DIR = Path("data/research_framework")
RUNS_FILE = DATA_DIR / "runs.jsonl"
TRIED_FILE = DATA_DIR / "tried_directions.jsonl"
DECISIONS_FILE = DATA_DIR / "decisions.jsonl"


@contextmanager
def locked(path: Path, mode: str) -> Iterator:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode, encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield fh
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def now_iso() -> str:
    return datetime.now(tz=ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with locked(path, "r") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSONL") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_no}: JSONL row must be an object")
            rows.append(row)
    return rows


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    with locked(path, "a") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
        fh.write("\n")
        fh.flush()


def append_run(row: dict[str, Any], path: Path = RUNS_FILE) -> None:
    if "run_id" not in row:
        raise ValueError("run row requires run_id")
    row = dict(row)
    row.setdefault("recorded_at", now_iso())
    append_jsonl(path, row)


def list_runs(
    strategy: str | None = None,
    mode: str | None = None,
    outcome: str | None = None,
    since: str | None = None,
    path: Path = RUNS_FILE,
) -> list[dict[str, Any]]:
    rows = read_jsonl(path)
    out: list[dict[str, Any]] = []
    for row in rows:
        if strategy is not None and row.get("strategy") != strategy:
            continue
        if mode is not None and row.get("mode") != mode:
            continue
        if outcome is not None and row.get("outcome") != outcome:
            continue
        if since is not None:
            stamp = str(row.get("started_at") or row.get("recorded_at") or "")
            if stamp and stamp < since:
                continue
        out.append(row)
    return out


def record_tried_direction(
    direction_key: str,
    outcome: str,
    outcome_reason: str = "",
    report_path: str = "",
    path: Path = TRIED_FILE,
) -> None:
    if not direction_key:
        raise ValueError("direction_key is required")
    append_jsonl(
        path,
        {
            "direction_key": direction_key,
            "first_tried": now_iso(),
            "outcome": outcome,
            "outcome_reason": outcome_reason,
            "report_path": report_path,
        },
    )


def has_been_tried(direction_key: str, path: Path = TRIED_FILE) -> bool:
    return any(row.get("direction_key") == direction_key for row in read_jsonl(path))


def get_recent_oos(strategy: str, last_n: int = 3, path: Path = RUNS_FILE) -> list[dict[str, Any]]:
    rows = [row for row in read_jsonl(path) if row.get("strategy") == strategy]
    rows = rows[-last_n:]
    return [
        {
            "run_id": row.get("run_id"),
            "oos_metrics_by_year": row.get("oos_metrics_by_year", {}),
            "outcome": row.get("outcome"),
            "report_path": row.get("report_path"),
        }
        for row in rows
    ]


def record_decision(
    actor: str,
    action: str,
    context: dict[str, Any] | None = None,
    path: Path = DECISIONS_FILE,
) -> None:
    if actor not in {"user", "claude", "codex"}:
        raise ValueError("actor must be one of: user, claude, codex")
    if not action:
        raise ValueError("action is required")
    append_jsonl(
        path,
        {
            "timestamp": now_iso(),
            "actor": actor,
            "action": action,
            "context": context or {},
        },
    )


def cmd_list_runs(args: argparse.Namespace) -> int:
    rows = list_runs(args.strategy, args.mode, args.outcome, args.since, args.path)
    print(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_has_been_tried(args: argparse.Namespace) -> int:
    tried = has_been_tried(args.direction_key, args.path)
    print("true" if tried else "false")
    return 0 if tried else 1


def cmd_recent_oos(args: argparse.Namespace) -> int:
    rows = get_recent_oos(args.strategy, args.last_n, args.path)
    print(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_record_decision(args: argparse.Namespace) -> int:
    context = json.loads(args.context) if args.context else {}
    if not isinstance(context, dict):
        raise ValueError("--context must decode to a JSON object")
    record_decision(args.actor, args.action, context, args.path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Quant research framework memory")
    sub = parser.add_subparsers(dest="command", required=True)

    list_p = sub.add_parser("list-runs")
    list_p.add_argument("--strategy")
    list_p.add_argument("--mode")
    list_p.add_argument("--outcome")
    list_p.add_argument("--since")
    list_p.add_argument("--path", type=Path, default=RUNS_FILE)
    list_p.set_defaults(func=cmd_list_runs)

    tried_p = sub.add_parser("has-been-tried")
    tried_p.add_argument("direction_key")
    tried_p.add_argument("--path", type=Path, default=TRIED_FILE)
    tried_p.set_defaults(func=cmd_has_been_tried)

    oos_p = sub.add_parser("recent-oos")
    oos_p.add_argument("strategy")
    oos_p.add_argument("--last-n", type=int, default=3)
    oos_p.add_argument("--path", type=Path, default=RUNS_FILE)
    oos_p.set_defaults(func=cmd_recent_oos)

    dec_p = sub.add_parser("record-decision")
    dec_p.add_argument("actor")
    dec_p.add_argument("action")
    dec_p.add_argument("--context", default="{}")
    dec_p.add_argument("--path", type=Path, default=DECISIONS_FILE)
    dec_p.set_defaults(func=cmd_record_decision)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
