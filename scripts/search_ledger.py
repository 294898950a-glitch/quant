#!/usr/bin/env python3
"""Search experience_ledger.md for similar historical entries (phase 1.5 #3 spec).

Auto-dedupe research direction before launching new batch: if a hypothesis text
matches existing rejected/archived entries (section 二/三/四), warn loudly.

Usage:
  python3 scripts/search_ledger.py "panic detector based on market breadth"
  python3 scripts/search_ledger.py "value-gap ranking" --section rejected

Algorithm: simple keyword token overlap (jaccard) on Chinese+English text.
Stop-words removed. Top 5 matches by score returned.

Exit codes:
  0 OK (no strong match)
  1 strong match found (score > 0.30) → caller should reconsider
  2 operational error (no tokens, ledger missing, parse error) — 跟 strong match 区分

按 Codex 13:12 verify: caller (new_research.py) 需要区分 strong match vs
infrastructure error, 不能把 query-empty / ledger-missing 当成 strong match.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

LEDGER = Path(__file__).resolve().parent.parent / "docs" / "research_framework" / "experience_ledger.md"

# Only genuine stop-words (low semantic signal). Keep strategy/method terms (panic/detector/value-gap) for matching.
STOP = {
    "the", "a", "is", "of", "to", "in", "for", "and", "or", "with", "on", "by",
    "未完成", "已采用", "已确认", "线索", "未来", "探索",
    "分区", "经验", "账本", "本", "本次", "本研究", "本批次",
    "spec", "yaml",
    "x", "y", "n", "k", "abc", "xyz", "todo",
}

ALL_TOKENS_RE = re.compile(r"[一-龥]|[a-zA-Z0-9_]+")


def tokenize(text: str) -> set[str]:
    """Crude tokenization: Chinese char + English word. Lowercase, drop stop-words."""
    text = text.lower()
    tokens = set()
    for token in ALL_TOKENS_RE.findall(text):
        if token in STOP:
            continue
        if len(token) == 1 and not re.match(r"[一-龥]", token):
            continue
        tokens.add(token)
    return tokens


def parse_ledger() -> list[dict]:
    """Parse all entries in experience_ledger.md sections 二/三/四."""
    if not LEDGER.exists():
        print(f"ERROR: {LEDGER} missing", file=sys.stderr)
        sys.exit(2)  # operational error, not strong match (Codex 13:12 fix)
    text = LEDGER.read_text(encoding="utf-8")
    entries = []
    current_section = None
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("## 二、"):
            current_section = "rejected"
            continue
        if line.startswith("## 三、"):
            current_section = "open_thread"
            continue
        if line.startswith("## 四、"):
            current_section = "future_backlog"
            continue
        if line.startswith("## "):
            current_section = None
            continue
        if not current_section:
            continue
        # markdown list items or pipe table rows
        if line.startswith("- `") or line.startswith("- ") or line.startswith("|"):
            entries.append({"section": current_section, "text": line})
    return entries


def query_coverage(query: set, entry: set) -> float:
    """Query coverage: 多少 query token 在 entry 里出现.

    比 jaccard 更适合 short query vs long entry (经验账本条目动辄 ≥ 100 token,
    短假设 query 用 jaccard 永远跑不到 high score).
    """
    if not query:
        return 0.0
    return len(query & entry) / len(query)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("query", help="hypothesis text to search for")
    parser.add_argument("--section", choices=["rejected", "open_thread", "future_backlog", "all"], default="all")
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--threshold", type=float, default=0.50, help="exit 1 if any match >= this (query coverage)")
    args = parser.parse_args()

    query_tokens = tokenize(args.query)
    if not query_tokens:
        print("ERROR: query has no meaningful tokens (all stop-words or 1-char tokens)", file=sys.stderr)
        return 2  # operational error, not strong match (Codex 13:12 fix)
    print(f"Query tokens: {sorted(query_tokens)}")
    print()

    entries = parse_ledger()
    scored = []
    for e in entries:
        if args.section != "all" and e["section"] != args.section:
            continue
        score = query_coverage(query_tokens, tokenize(e["text"]))
        scored.append((score, e))

    scored.sort(reverse=True, key=lambda x: x[0])

    found_strong = False
    print(f"Top {args.top} matches:")
    for score, e in scored[: args.top]:
        marker = " ⚠ STRONG MATCH" if score >= args.threshold else ""
        if score >= args.threshold:
            found_strong = True
        section_label = {"rejected": "二·已确认无效",
                         "open_thread": "三·未完成线索",
                         "future_backlog": "四·未来探索"}[e["section"]]
        print(f"  [{score:.2f}] [{section_label}]{marker}")
        text_preview = e["text"][:150] + ("..." if len(e["text"]) > 150 else "")
        print(f"    {text_preview}")

    if found_strong:
        print(f"\n⚠ STRONG MATCH (>={args.threshold}). Reconsider before launching new batch.")
        print("   If proceeding, write 'aware-of-prior-match' in DIRECT spec.")
        return 1
    print(f"\nOK: no strong match (threshold={args.threshold})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
