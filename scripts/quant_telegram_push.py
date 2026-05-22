#!/usr/bin/env python3
"""Push freshly-settled quant experiment reviews to Telegram.

The autonomous research framework writes review.yaml into each run directory
once review_memory has classified the experiment. This script tails those
files and sends a short, plain-language Telegram message for every NEW review
that has not been pushed before.

It reuses the same credential discovery pattern as
`/home/jay/projects/invest/publisher/cron_driver.py`:
    - TELEGRAM_BOT_TOKEN from env or from `~/.hermes/.env`
    - TELEGRAM_CHAT_ID hard-coded to the user's personal chat (6403706808)

A state file at `logs/quant_telegram_push_state.json` tracks which run_ids
have already been pushed, so cron re-runs are idempotent.

Hard boundaries (per CLAUDE.md / AGENTS.md):
- Does not start runs, does not modify strategy state, does not promote.
- Read-only on review.yaml; only writes its own state file + log.
- Silently skips any review with no review_status (still being written).

Usage:
    python3 scripts/quant_telegram_push.py            # send every new review
    python3 scripts/quant_telegram_push.py --dry-run  # show what it would send
    python3 scripts/quant_telegram_push.py --since 24 # only last 24h of reviews
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = REPO_ROOT / "data"
STATE_PATH = REPO_ROOT / "logs" / "quant_telegram_push_state.json"
HERMES_ENV = Path.home() / ".hermes" / ".env"
TELEGRAM_CHAT_ID = "6403706808"  # Jay (mirrors publisher/cron_driver.py)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def telegram_token() -> str | None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if token:
        return token.strip().strip('"').strip("'")
    if HERMES_ENV.exists():
        for line in HERMES_ENV.read_text(encoding="utf-8").splitlines():
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"pushed": {}}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"pushed": {}}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_review(path: Path) -> dict[str, Any] | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def review_signature(review: dict[str, Any]) -> str:
    """sha-like signature of review content so re-writes can re-push."""
    interp = review.get("interpretation") or {}
    parts = [
        str(review.get("run_id") or ""),
        str(interp.get("review_status") or ""),
        str(interp.get("result_summary") or "")[:200],
        str(interp.get("main_reason") or "")[:200],
    ]
    import hashlib

    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def emoji_for(verdict: str) -> str:
    v = verdict.lower()
    if v in {"accept", "accepted", "adopt"}:
        return "✅"
    if v in {"reject", "rejected"}:
        return "❌"
    if v in {"inconclusive", "needs_manual_review"}:
        return "⚠️"
    return "🧪"


def format_message(run_id: str, review: dict[str, Any]) -> str:
    interp = review.get("interpretation") or {}
    verdict = str(interp.get("review_status") or "?")
    summary = str(interp.get("result_summary") or "").strip()
    main_reason = str(interp.get("main_reason") or "").strip()
    next_dirs = interp.get("next_research_directions") or []

    body_lines = [f"{emoji_for(verdict)} <b>{run_id}</b>", f"判定: <b>{verdict}</b>"]
    if summary:
        body_lines.append("结论: " + summary[:280])
    if main_reason and main_reason != summary:
        body_lines.append("原因: " + main_reason[:200])
    if isinstance(next_dirs, list) and next_dirs:
        first = next_dirs[0]
        if isinstance(first, dict):
            d = first.get("direction") or first.get("name") or ""
            why = first.get("why") or ""
            line = "下一步: " + str(d)[:120]
            if why:
                line += " — " + str(why)[:120]
            body_lines.append(line)
    return "\n".join(body_lines)


def send_telegram(token: str, message: str) -> tuple[bool, str]:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode(
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
            return True, body[:200]
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def candidate_reviews(since_hours: float | None) -> list[tuple[str, Path, dict[str, Any]]]:
    cutoff = None
    if since_hours is not None and since_hours > 0:
        cutoff = time.time() - since_hours * 3600
    out: list[tuple[str, Path, dict[str, Any]]] = []
    if not DATA_ROOT.is_dir():
        return out
    for run_dir in sorted(DATA_ROOT.iterdir()):
        if not run_dir.is_dir():
            continue
        review_path = run_dir / "review.yaml"
        if not review_path.exists():
            continue
        if cutoff is not None and review_path.stat().st_mtime < cutoff:
            continue
        review = load_review(review_path)
        if review is None:
            continue
        interp = review.get("interpretation") or {}
        if not interp.get("review_status"):
            continue
        run_id = str(review.get("run_id") or run_dir.name)
        out.append((run_id, review_path, review))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print messages instead of sending")
    parser.add_argument("--since", type=float, default=72, help="only consider reviews modified within the last N hours (default 72, 0=all)")
    parser.add_argument("--force", action="store_true", help="resend even if already pushed")
    args = parser.parse_args()

    token = telegram_token() if not args.dry_run else "DRYRUN-NO-TOKEN-NEEDED"
    if not token and not args.dry_run:
        print("TELEGRAM_BOT_TOKEN not found in env or ~/.hermes/.env", file=sys.stderr)
        return 2

    state = load_state()
    pushed = state.setdefault("pushed", {})

    cutoff_hours = args.since if args.since > 0 else None
    candidates = candidate_reviews(cutoff_hours)
    sent: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for run_id, review_path, review in candidates:
        sig = review_signature(review)
        prev = pushed.get(run_id) or {}
        if not args.force and prev.get("signature") == sig:
            skipped.append({"run_id": run_id, "reason": "already-pushed"})
            continue
        message = format_message(run_id, review)
        if args.dry_run:
            print("--- would push ---")
            print(message)
            print()
            sent.append({"run_id": run_id, "action": "would_push"})
            continue
        ok, detail = send_telegram(token, message)
        if ok:
            pushed[run_id] = {"signature": sig, "pushed_at": now_iso(), "verdict": str((review.get("interpretation") or {}).get("review_status") or "")}
            sent.append({"run_id": run_id, "action": "sent"})
        else:
            failed.append({"run_id": run_id, "error": detail})

    if not args.dry_run:
        state["last_run_at"] = now_iso()
        state["last_sent_count"] = len(sent)
        save_state(state)

    summary = {
        "timestamp": now_iso(),
        "dry_run": args.dry_run,
        "sent": sent,
        "skipped_count": len(skipped),
        "failed": failed,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
