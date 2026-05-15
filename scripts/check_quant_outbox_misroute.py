#!/usr/bin/env python3
"""Detect Claude messages accidentally written to Codex outbox.

This does not process the message.  It appends a loud HANDOFF so the channel
mistake is visible and the sender can repost to the Claude outbox.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import re
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo


HEADING_RE = re.compile(
    r"^###\s+(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+CST)\s+-\s+"
    r"(?:(?P<actor>Claude|Codex|User)\s+-\s+)?(?P<title>[^\n]+?)\s*$",
    re.MULTILINE,
)
PROTOCOL_RE = re.compile(r"<!--\s*protocol-redline-v(?P<version>\d+\.\d+)\s*-->")


@contextmanager
def locked(path: Path, mode: str) -> Iterator:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode, encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield fh
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def latest_block(text: str) -> tuple[str, str, str, str] | None:
    matches = list(HEADING_RE.finditer(text))
    if not matches:
        return None
    last = matches[-1]
    return (
        last.group("ts"),
        last.group("actor") or "Claude",
        last.group("title").strip(),
        text[last.end() :].strip("\n"),
    )


def message_hash(timestamp: str, title: str, body: str) -> str:
    normalized = "\n".join(line.rstrip() for line in body.strip().splitlines())
    return hashlib.sha256(f"{timestamp}\n{title}\n{normalized}".encode("utf-8")).hexdigest()[:16]


def protocol_version(body: str, fallback: str) -> str:
    match = PROTOCOL_RE.search(body)
    return match.group("version") if match else fallback


def local_protocol_version(protocol_doc: Path) -> str:
    text = read_text(protocol_doc)
    match = re.search(r"协议红线\s+v(?P<version>\d+\.\d+)", text)
    return match.group("version") if match else "unknown"


def now_cst() -> str:
    return datetime.now(tz=ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M CST")


def cache_has(cache: Path, msg_hash: str) -> bool:
    if not cache.exists():
        return False
    with locked(cache, "r") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("message_hash") == msg_hash:
                return True
    return False


def cache_mark(cache: Path, msg_hash: str, source: str) -> None:
    row = {
        "detected_at": datetime.now(tz=ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "message_hash": msg_hash,
        "source": source,
        "status": "misrouted_claude_in_codex_outbox",
    }
    with locked(cache, "a") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
        fh.write("\n")


def append_alert(codex_box: Path, state_file: Path, block: str) -> None:
    for path in (codex_box, state_file):
        with locked(path, "a") as fh:
            fh.write(block)
            if not block.endswith("\n"):
                fh.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Detect Claude messages in Codex outbox")
    parser.add_argument("--codex-box", type=Path, required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--protocol-doc", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    args = parser.parse_args()

    latest = latest_block(read_text(args.codex_box))
    if latest is None:
        print("skip: no headed codex outbox entry")
        return 10
    timestamp, actor, title, body = latest
    if actor != "Claude":
        print(f"skip: latest codex outbox actor is {actor}")
        return 10

    msg_hash = message_hash(timestamp, title, body)
    if cache_has(args.cache, msg_hash):
        print(f"skip: misroute already alerted msg-hash-{msg_hash}")
        return 10

    version = protocol_version(body, local_protocol_version(args.protocol_doc))
    source = f"{timestamp} - Claude - {title}"
    block = f"""
### {now_cst()} - Codex - HANDOFF/MISROUTED-CLAUDE-OUTBOX

<!-- protocol-redline-v{version} -->
Project: quant
Task: channel integrity guard

Summary:

- Detected a Claude-authored message in `codex/outbox.md`: `{source}`.
- message hash: `msg-hash-{msg_hash}`.
- This channel is Codex -> Claude only, so the message was not auto-processed as an inbound Claude directive.
- Please repost the directive to `claude/outbox.md`; no research work or backtest was started from this misrouted block.
"""
    append_alert(args.codex_box, args.state_file, block)
    cache_mark(args.cache, msg_hash, source)
    print(f"alerted: msg-hash-{msg_hash}")
    return 20


if __name__ == "__main__":
    raise SystemExit(main())
