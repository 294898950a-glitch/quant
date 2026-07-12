#!/usr/bin/env python3
"""Build a machine-readable inventory of local quant data files."""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "data" / "research_framework" / "data_inventory.yaml"
DATA_SUFFIXES = {".parquet", ".csv"}
REFERENCE_SUFFIXES = r"(?:parquet|csv|json)"
DATA_REF_RE = re.compile(r"data/[A-Za-z0-9_.\-/]+?\." + REFERENCE_SUFFIXES)
DATE_COLUMN_HINTS = (
    "trade_date",
    "date",
    "ann_date",
    "call_date",
    "expire_date",
    "start_date",
    "end_date",
    "entry_date",
    "exit_date",
)


def rel(path: Path, repo_root: Path = REPO_ROOT) -> str:
    try:
        return str(path.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        return str(path)


def classify_path(path: Path, repo_root: Path = REPO_ROOT) -> str:
    path_text = rel(path, repo_root)
    if path_text.startswith("data/cb_warehouse/"):
        return "core_warehouse"
    if path_text.startswith("data/benchmarks/"):
        return "benchmark"
    if "/prepared_data/" in path_text:
        return "prepared_run_data"
    if path_text.startswith("data/research_framework/"):
        return "framework_runtime"
    return "experiment_artifact"


def parse_date_value(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (dt.date, dt.datetime, pd.Timestamp)):
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    parsed = pd.to_datetime(pd.Series([text]), errors="coerce").iloc[0]
    if pd.isna(parsed):
        return text
    return pd.Timestamp(parsed).strftime("%Y-%m-%d")


def date_column_names(columns: list[str]) -> list[str]:
    lowered = {column.lower(): column for column in columns}
    exact = [lowered[name] for name in DATE_COLUMN_HINTS if name in lowered]
    fuzzy = [
        column
        for column in columns
        if column not in exact and ("date" in column.lower() or column.lower().endswith("_dt"))
    ]
    return (exact + fuzzy)[:4]


def date_ranges_for_file(path: Path, columns: list[str], fmt: str) -> dict[str, dict[str, str | None]]:
    ranges: dict[str, dict[str, str | None]] = {}
    for column in date_column_names(columns):
        try:
            if fmt == "parquet":
                series = pd.read_parquet(path, columns=[column])[column]
            else:
                series = pd.read_csv(path, usecols=[column])[column]
            series = series.dropna()
            if series.empty:
                ranges[column] = {"min": None, "max": None}
                continue
            ranges[column] = {
                "min": parse_date_value(series.min()),
                "max": parse_date_value(series.max()),
            }
        except Exception as exc:  # pragma: no cover - defensive metadata fallback
            ranges[column] = {"error": str(exc)[:200]}
    return ranges


def parquet_metadata(path: Path) -> dict[str, Any]:
    metadata = pq.read_metadata(path)
    schema = pq.read_schema(path)
    columns = list(schema.names)
    return {
        "format": "parquet",
        "rows": int(metadata.num_rows),
        "columns": columns,
        "date_ranges": date_ranges_for_file(path, columns, "parquet"),
    }


def csv_metadata(path: Path) -> dict[str, Any]:
    sample = pd.read_csv(path, nrows=1000)
    columns = [str(column) for column in sample.columns]
    rows = 0
    with path.open("rb") as handle:
        for rows, _line in enumerate(handle, start=0):
            pass
    rows = max(rows - 1, 0)
    return {
        "format": "csv",
        "rows": int(rows),
        "columns": columns,
        "date_ranges": date_ranges_for_file(path, columns, "csv"),
    }


def file_entry(path: Path, repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    stat = path.stat()
    entry: dict[str, Any] = {
        "path": rel(path, repo_root),
        "category": classify_path(path, repo_root),
        "size_bytes": stat.st_size,
        "mtime_utc": dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc).isoformat(),
        "readable": True,
    }
    try:
        if path.suffix == ".parquet":
            entry.update(parquet_metadata(path))
        elif path.suffix == ".csv":
            entry.update(csv_metadata(path))
        else:
            entry["format"] = path.suffix.lstrip(".")
            entry["readable"] = False
            entry["error"] = "unsupported suffix"
    except Exception as exc:
        entry["format"] = path.suffix.lstrip(".")
        entry["readable"] = False
        entry["error"] = str(exc)[:300]
    return entry


def discover_data_files(data_root: Path, *, include_artifacts: bool = False) -> list[Path]:
    if include_artifacts:
        roots = [data_root]
    else:
        roots = [data_root / "cb_warehouse", data_root / "benchmarks"]
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        files.extend(
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in DATA_SUFFIXES
        )
    return sorted(files)


def referenced_data_paths(data_root: Path, repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    refs: Counter[str] = Counter()
    source_counts: dict[str, int] = {}
    for path in sorted(data_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".yaml", ".yml", ".log", ".json", ".jsonl"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        found = sorted(set(match.group(0).rstrip(".,;:)\"'") for match in DATA_REF_RE.finditer(text)))
        if not found:
            continue
        source_counts[rel(path, repo_root)] = len(found)
        refs.update(found)

    missing = []
    present = []
    for ref, count in refs.most_common():
        record = {"path": ref, "reference_count": count}
        if (repo_root / ref).exists():
            present.append(record)
        else:
            missing.append(record)
    return {
        "present_top": present[:50],
        "missing_top": missing[:50],
        "source_file_count": len(source_counts),
    }


def build_inventory(
    repo_root: Path = REPO_ROOT,
    data_root: Path | None = None,
    *,
    include_artifacts: bool = False,
) -> dict[str, Any]:
    data_root = data_root or repo_root / "data"
    files = [file_entry(path, repo_root) for path in discover_data_files(data_root, include_artifacts=include_artifacts)]
    category_counts = Counter(entry["category"] for entry in files)
    format_counts = Counter(entry.get("format", "unknown") for entry in files)
    unreadable = [entry for entry in files if not entry.get("readable")]
    inventory = {
        "schema_version": 1,
        "generated_at_utc": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "purpose": (
            "Machine-readable local data inventory for strategy ideation, proposal gating, "
            "and data-quality planning. This file is descriptive; it does not approve runs."
        ),
        "repo_root": str(repo_root.resolve()),
        "summary": {
            "file_count": len(files),
            "category_counts": dict(sorted(category_counts.items())),
            "format_counts": dict(sorted(format_counts.items())),
            "unreadable_count": len(unreadable),
            "scope": "all_data_artifacts" if include_artifacts else "core_database_only",
        },
        "files": files,
    }
    if include_artifacts:
        inventory["referenced_data_paths"] = referenced_data_paths(data_root, repo_root)
    return inventory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build data/research_framework/data_inventory.yaml")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--include-artifacts",
        action="store_true",
        help="include experiment artifacts; default is core warehouse and benchmarks only",
    )
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    data_root = args.data_root.resolve() if args.data_root else repo_root / "data"
    output = args.output if args.output.is_absolute() else repo_root / args.output
    inventory = build_inventory(repo_root=repo_root, data_root=data_root, include_artifacts=args.include_artifacts)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(yaml.safe_dump(inventory, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"wrote {rel(output, repo_root)} ({inventory['summary']['file_count']} data files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
