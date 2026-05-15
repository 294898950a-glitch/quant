#!/usr/bin/env python3
"""Idempotent processor for the quant Claude -> Codex outbox.

This is the P1.0 second processing layer:
- parse the latest Claude outbox entry
- compute a stable key from project, l0-entry-id, source timestamp/title, claim
- detect prior coverage in Codex outbox, state, or the JSONL ledger
- append one ACK/REVIEW/HANDOFF block to Codex outbox and state
- support dry-run for watcher/Codex use
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo


QUANT_AI_ROOT = Path("/mnt/c/Users/陈教授/Desktop/ai/projects/quant")
DEFAULT_CLAUDE_BOX = QUANT_AI_ROOT / "claude" / "outbox.md"
DEFAULT_CODEX_BOX = QUANT_AI_ROOT / "codex" / "outbox.md"
DEFAULT_STATE_FILE = QUANT_AI_ROOT / "state.md"
DEFAULT_LEDGER = Path(__file__).resolve().parent.parent / "data" / "research_framework" / "processed_claude_messages.jsonl"
DEFAULT_PROJECT = "quant"
DEFAULT_GATE = "codex-standby-waiting-next-claude-or-user-message"

HEADING_RE = re.compile(
    r"^###\s+(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+CST)\s+-\s+"
    r"(?:(?P<actor>Claude|Codex|User)\s+-\s+)?(?P<title>[^\n]+?)\s*$",
    re.MULTILINE,
)
PROJECT_RE = re.compile(r"(?im)^\s*Project\s*:\s*(?P<value>\S+)\s*$")
CLAIM_RE = re.compile(r"(?im)^\s*-\s*claim\s*:\s*(?P<value>.+?)\s*$|^\s*claim\s*:\s*(?P<value2>.+?)\s*$")
GATE_RE = re.compile(r"(?im)^\s*-\s*Current gate\s*:\s*(?P<value>.+?)\s*$|^\s*Current gate\s*:\s*(?P<value2>.+?)\s*$")
L0_RE = re.compile(r"<!--\s*l0-entry-id\s*:\s*(?P<value>[^>]+?)\s*-->")
PROTOCOL_RE = re.compile(r"<!--\s*protocol-redline-v(?P<value>[0-9.]+)\s*-->")
KEY_RE = re.compile(r"processed-claude-key\s*:\s*(?P<value>[a-f0-9]{20,64})")


@dataclass(frozen=True)
class Message:
    timestamp: str
    actor: str
    title: str
    body: str

    @property
    def source_id(self) -> str:
        return f"{self.timestamp} - {self.title}"


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


def latest_message(text: str) -> Message | None:
    matches = list(HEADING_RE.finditer(text))
    if not matches:
        return None
    last = matches[-1]
    return Message(
        timestamp=last.group("ts"),
        actor=last.group("actor") or "Claude",
        title=last.group("title").strip(),
        body=text[last.end() :].strip("\n"),
    )


def first_match(regex: re.Pattern[str], text: str) -> str | None:
    match = regex.search(text)
    if not match:
        return None
    value = match.groupdict().get("value") or match.groupdict().get("value2")
    if value is None:
        return None
    return value.strip().strip("`")


def protocol_version(msg: Message) -> str:
    return first_match(PROTOCOL_RE, msg.body) or "1.5"


def message_hash(msg: Message) -> str:
    normalized = "\n".join(line.rstrip() for line in msg.body.strip().splitlines())
    payload = f"{msg.timestamp}\n{msg.title}\n{normalized}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def message_key(project: str, msg: Message) -> tuple[str, dict[str, str]]:
    parts = {
        "project": project,
        "l0_entry_id": first_match(L0_RE, msg.body) or "",
        "source": msg.source_id,
        "claim": first_match(CLAIM_RE, msg.body) or "",
    }
    digest = hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:24]
    return digest, parts


def ledger_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    keys: set[str] = set()
    with locked(path, "r") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            for field in ("message_key", "processed_key", "processor_key"):
                value = row.get(field)
                if isinstance(value, str):
                    keys.add(value)
    return keys


def text_has_ack(text: str, msg: Message, key: str, parts: dict[str, str]) -> bool:
    if key and key in set(KEY_RE.findall(text)):
        return True
    if msg.source_id in text:
        return True
    claim = parts.get("claim", "")
    return bool(claim and claim in text and parts.get("l0_entry_id", "") and parts["l0_entry_id"] in text)


def processor_time(source_timestamp: str | None = None) -> datetime:
    now = datetime.now(tz=ZoneInfo("Asia/Shanghai"))
    if not source_timestamp:
        return now
    try:
        source = datetime.strptime(source_timestamp, "%Y-%m-%d %H:%M CST").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    except ValueError:
        return now
    if now <= source:
        return source + timedelta(minutes=1)
    return now


def format_cst(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M CST")


def format_cst_iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def append_text(path: Path, block: str) -> None:
    with locked(path, "a") as fh:
        if path.exists() and path.stat().st_size > 0 and not read_text(path).endswith("\n"):
            fh.write("\n")
        fh.write(block)
        if not block.endswith("\n"):
            fh.write("\n")
        fh.flush()


def record_ledger(path: Path, msg: Message, key: str, parts: dict[str, str], mode: str, gate: str, processed_at: datetime) -> None:
    row = {
        "processed_at": format_cst_iso(processed_at),
        "processed_at_label": format_cst(processed_at),
        "message_key": key,
        "message_hash": message_hash(msg),
        "message_id": msg.source_id,
        "kind": msg.title,
        "project": parts["project"],
        "l0_entry_id": parts["l0_entry_id"],
        "claim": parts["claim"],
        "mode": mode,
        "gate": gate,
    }
    with locked(path, "a") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
        fh.write("\n")
        fh.flush()


def build_codex_block(
    msg: Message,
    key: str,
    parts: dict[str, str],
    mode: str,
    status: str,
    task: str,
    gate: str,
    note: str | None,
    processed_at: datetime,
) -> str:
    claim = parts["claim"] or "none"
    l0 = parts["l0_entry_id"] or "none"
    note_line = f"- Note: {note}\n" if note else ""
    return f"""
### {format_cst(processed_at)} - Codex - {mode}/{status}

<!-- protocol-redline-v{protocol_version(msg)} -->
<!-- l0-entry-id: {l0} -->
<!-- processed-claude-key: {key} -->
<!-- source-msg-hash: {message_hash(msg)} -->
Project: quant
Task: {task}

Result:

- Latest Claude `{msg.source_id}` acknowledged and not previously covered in Codex outbox/state.
- Stable processor key uses `(project, l0-entry-id, timestamp/title, claim)`: `{key}`.
- Source claim: `{claim}`.
{note_line}- No backtest, spot, strategy truth change, or duplicate research work was started by the processor.
- Current gate: {gate}
- claim: quant-claude-outbox-processed-{key}
"""


def build_state_block(
    msg: Message,
    key: str,
    parts: dict[str, str],
    gate: str,
    note: str | None,
    processed_at: datetime,
) -> str:
    note_line = f"- Note: {note}\n" if note else ""
    return f"""
### {format_cst(processed_at)} - Codex - STATE/CLAUDE-OUTBOX-PROCESSED

Project: quant
Task: quant Claude outbox second-layer processor state update

Result:

- Latest processed Claude: `{msg.source_id}`.
- processed-claude-key: `{key}`.
- Source claim: `{parts['claim'] or 'none'}`.
{note_line}- Current gate: {gate}
- claim: quant-claude-outbox-processed-{key}
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Process latest quant Claude outbox entry idempotently")
    parser.add_argument("--claude-box", type=Path, default=DEFAULT_CLAUDE_BOX)
    parser.add_argument("--codex-box", type=Path, default=DEFAULT_CODEX_BOX)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--mode", choices=("ACK", "REVIEW", "HANDOFF"), default="ACK")
    parser.add_argument("--status", default="CLAUDE-OUTBOX-PROCESSED")
    parser.add_argument("--task", default="automatic quant Claude outbox processing")
    parser.add_argument("--gate", help="Gate to write; defaults to latest Claude gate or standby")
    parser.add_argument("--note", help="One concise note to include in outbox/state blocks")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    msg = latest_message(read_text(args.claude_box))
    if msg is None:
        print("skip: no headed Claude outbox entry", file=sys.stderr)
        return 10
    if msg.actor != "Claude":
        print(f"skip: latest actor is {msg.actor}")
        return 10

    project = first_match(PROJECT_RE, msg.body) or args.project
    if project != args.project:
        print(f"skip: latest project is {project}, expected {args.project}")
        return 10

    key, parts = message_key(project, msg)
    gate = args.gate or first_match(GATE_RE, msg.body) or DEFAULT_GATE
    codex_text = read_text(args.codex_box)
    state_text = read_text(args.state_file)
    already = key in ledger_keys(args.ledger) or text_has_ack(codex_text, msg, key, parts) or text_has_ack(state_text, msg, key, parts)

    summary = {
        "already_acknowledged": already,
        "dry_run": bool(args.dry_run),
        "gate": gate,
        "message_hash": message_hash(msg),
        "message_key": key,
        "source": msg.source_id,
        "would_append": not already,
    }
    if args.dry_run:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if already:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    codex_block = build_codex_block(msg, key, parts, args.mode, args.status, args.task, gate, args.note)
    state_block = build_state_block(msg, key, parts, gate, args.note)
    append_text(args.codex_box, codex_block)
    append_text(args.state_file, state_block)
    record_ledger(args.ledger, msg, key, parts, args.mode, gate)
    summary["appended"] = True
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
