#!/usr/bin/env python3
"""Search machine-readable experiment and direction ledgers for similar work."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml required", file=sys.stderr)
    sys.exit(2)


REPO_ROOT = Path(__file__).resolve().parent.parent
EXPERIMENTS = REPO_ROOT / "data" / "research_framework" / "experiments.yaml"
TRIED = REPO_ROOT / "data" / "research_framework" / "tried_directions.jsonl"

STOP = {
    "the", "a", "is", "of", "to", "in", "for", "and", "or", "with", "on", "by",
    "未完成", "已采用", "已确认", "线索", "未来", "探索",
    "分区", "经验", "账本", "本", "本次", "本研究", "本批次",
    "spec", "yaml",
    "x", "y", "n", "k", "abc", "xyz", "todo",
}
ALL_TOKENS_RE = re.compile(r"[一-龥]|[a-zA-Z0-9_]+")


def tokenize(text: str) -> set[str]:
    text = text.lower()
    tokens = set()
    for token in ALL_TOKENS_RE.findall(text):
        if token in STOP:
            continue
        if len(token) == 1 and not re.match(r"[一-龥]", token):
            continue
        tokens.add(token)
    return tokens


def load_entries() -> list[dict]:
    entries: list[dict] = []
    if EXPERIMENTS.exists():
        data = yaml.safe_load(EXPERIMENTS.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for item in data.get("experiments", []):
                if not isinstance(item, dict):
                    continue
                artifacts = item.get("artifacts")
                artifact_text = " ".join(str(x) for x in artifacts) if isinstance(artifacts, list) else str(artifacts or "")
                text = " ".join(
                    str(item.get(key, ""))
                    for key in (
                        "id",
                        "strategy_id",
                        "hypothesis_id",
                        "status",
                        "hypothesis",
                        "reject_reason",
                        "summary",
                        "current_strategy_effect",
                        "commit_ref",
                    )
                )
                text = f"{text} {artifact_text}"
                entries.append({
                    "section": str(item.get("status") or "experiment"),
                    "text": text,
                    "source": str(item.get("id") or "experiments.yaml"),
                })
            for item in data.get("research_insights", []):
                if not isinstance(item, dict):
                    continue
                implications = item.get("implications")
                implication_text = " ".join(str(x) for x in implications) if isinstance(implications, list) else str(implications or "")
                text = " ".join(str(item.get(key, "")) for key in ("id", "source", "summary"))
                entries.append({
                    "section": "insight",
                    "text": f"{text} {implication_text}",
                    "source": str(item.get("id") or "experiments.yaml#research_insights"),
                })
    if TRIED.exists():
        for raw in TRIED.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            text = " ".join(str(row.get(key, "")) for key in ("direction_key", "outcome", "outcome_reason"))
            entries.append({
                "section": str(row.get("outcome") or "tried"),
                "text": text,
                "source": str(row.get("direction_key") or "tried_directions.jsonl"),
            })
    if not entries:
        print("ERROR: no machine-readable experiment ledger entries found", file=sys.stderr)
        sys.exit(2)
    return entries


def query_coverage(query: set[str], entry: set[str]) -> float:
    if not query:
        return 0.0
    return len(query & entry) / len(query)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("query", help="hypothesis text to search for")
    parser.add_argument("--section", default="all")
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--threshold", type=float, default=0.50)
    args = parser.parse_args()

    query_tokens = tokenize(args.query)
    if not query_tokens:
        print("ERROR: query has no meaningful tokens", file=sys.stderr)
        return 2

    entries = load_entries()
    scored = []
    for entry in entries:
        if args.section != "all" and entry["section"] != args.section:
            continue
        score = query_coverage(query_tokens, tokenize(entry["text"]))
        scored.append((score, entry))
    scored.sort(reverse=True, key=lambda x: x[0])

    found_strong = False
    print(f"Query tokens: {sorted(query_tokens)}\n")
    print(f"Top {args.top} matches:")
    for score, entry in scored[: args.top]:
        marker = " STRONG_MATCH" if score >= args.threshold else ""
        if score >= args.threshold:
            found_strong = True
        print(f"  [{score:.2f}] [{entry['section']}] {entry['source']}{marker}")
        print(f"    {entry['text'][:180]}")

    if found_strong:
        print(f"\nSTRONG MATCH (>={args.threshold}). Reconsider before launching new batch.")
        return 1
    print(f"\nOK: no strong match (threshold={args.threshold})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
