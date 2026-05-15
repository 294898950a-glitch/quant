#!/usr/bin/env python3
"""Preflight gate for quant Claude -> Codex outbox processing.

The watcher calls this before launching ``codex exec``.  It is intentionally
mechanical: version, duplicate, and required-field checks are enforced here;
semantic research judgment stays with Claude/Codex.
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
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo


EXIT_OK = 0
EXIT_SKIP = 10
EXIT_HANDOFF = 20
EXIT_ERROR = 1

DEFAULT_PROTOCOL_DOC = Path("docs/research_framework/protocol_redline.md")
DEFAULT_CACHE = Path("data/research_framework/processed_claude_messages.jsonl")
DEFAULT_DISAGREEMENT_COUNTERS = Path("data/research_framework/disagreement_counters.json")
DEFAULT_LEDGER = Path("docs/research_framework/experience_ledger.md")

HEADING_RE = re.compile(
    r"^###\s+(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+CST)\s+-\s+"
    r"(?:(?P<actor>Claude|Codex|User)\s+-\s+)?(?P<kind>[^\n]+)\s*$",
    re.MULTILINE,
)
PROTOCOL_RE = re.compile(r"<!--\s*protocol-redline-v(?P<version>\d+\.\d+)\s*-->")
HASH_RE = re.compile(r"<!--\s*msg-hash-(?P<hash>[A-Za-z0-9_.:-]+)\s*-->")
L0_ENTRY_RE = re.compile(r"<!--\s*l0-entry-id:\s*(?P<entry>1(?:\.[12])?|2(?:\.[12])?|3)\s*-->")

SPEC_FIELDS = {
    "hypothesis": ("hypothesis", "假设"),
    "parameter_space": ("parameter_space", "parameter space", "参数空间", "参数"),
    "hard_floors": ("hard_floors", "hard floors", "floor", "底线"),
    "output_artifacts": ("output_artifacts", "output artifacts", "artifacts", "产物"),
    "compute_estimate": ("compute_estimate", "compute estimate", "算力", "runtime estimate"),
    "data_sources": ("data_sources", "data sources", "数据来源", "baseline 出处"),
    "true_cv_design": ("true_cv_design", "true cv", "真 cv", "leave-y", "leave year"),
    "stop_conditions": ("stop_conditions", "stop conditions", "停止条件"),
}


@dataclass(frozen=True)
class Message:
    heading: str
    timestamp: str
    actor: str
    kind: str
    body: str

    @property
    def id_label(self) -> str:
        return f"{self.timestamp} - {self.kind}"


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
        stripped = text.strip()
        if not stripped:
            return None
        return Message(
            heading="",
            timestamp="unknown",
            actor="Claude",
            kind="UNKNOWN",
            body=stripped,
        )

    last = matches[-1]
    body = text[last.end() :].strip("\n")
    return Message(
        heading=last.group(0),
        timestamp=last.group("ts"),
        actor=last.group("actor") or "Claude",
        kind=last.group("kind").strip(),
        body=body,
    )


def first_nonempty_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def local_protocol_version(protocol_doc: Path) -> str | None:
    text = read_text(protocol_doc)
    match = re.search(r"协议红线\s+v(?P<version>\d+\.\d+)", text)
    if match:
        return match.group("version")
    match = re.search(r"当前版本:\s*\*\*v(?P<version>\d+\.\d+)\*\*", text)
    if match:
        return match.group("version")
    match = PROTOCOL_RE.search(text)
    if match:
        return match.group("version")
    return None


def message_protocol_version(msg: Message) -> str | None:
    line = first_nonempty_line(msg.body)
    match = PROTOCOL_RE.fullmatch(line)
    if match:
        return match.group("version")
    return None


def message_hash(msg: Message) -> str:
    explicit = HASH_RE.search(msg.body)
    if explicit:
        return explicit.group("hash")
    normalized = "\n".join(line.rstrip() for line in msg.body.strip().splitlines())
    payload = f"{msg.timestamp}\n{msg.kind}\n{normalized}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def read_cache(path: Path) -> set[str]:
    if not path.exists():
        return set()
    seen: set[str] = set()
    with locked(path, "r") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            h = row.get("message_hash")
            if isinstance(h, str):
                seen.add(h)
    return seen


def mark_cache(path: Path, msg: Message) -> None:
    row = {
        "processed_at": now_cst_iso(),
        "message_hash": message_hash(msg),
        "message_id": msg.id_label,
        "kind": msg.kind,
    }
    with locked(path, "a") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
        fh.write("\n")
        fh.flush()


def already_acknowledged(msg: Message, msg_hash: str, codex_text: str, state_text: str, cache: set[str]) -> bool:
    if msg_hash in cache:
        return True
    if codex_or_state_acknowledges(codex_text, msg, msg_hash):
        return True
    if codex_or_state_acknowledges(state_text, msg, msg_hash):
        return True
    return False


def codex_or_state_acknowledges(text: str, msg: Message, msg_hash: str) -> bool:
    markers = (msg.id_label, f"msg-hash-{msg_hash}")
    matches = list(re.finditer(r"(?m)^###\s+.*\bCodex\b.*$", text))
    for idx, match in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        block = text[match.start() : end]
        if any(marker and marker in block for marker in markers):
            return True
    return False


def contains_any(text: str, needles: tuple[str, ...]) -> bool:
    lower = text.lower()
    return any(needle.lower() in lower for needle in needles)


def l0_entry_id(msg: Message) -> str | None:
    match = L0_ENTRY_RE.search(msg.body)
    return match.group("entry") if match else None


def is_l1_direct(msg: Message) -> bool:
    kind_upper = msg.kind.upper()
    if "DIRECT" not in kind_upper:
        return False
    if any(level in kind_upper for level in ("L4", "L5", "L6", "L7")):
        return False
    if "ROUND-" in kind_upper or "PROTOCOL/" in kind_upper:
        return False
    body = msg.body
    return bool(re.search(r"\bL1\b", body, re.IGNORECASE) or ("Spec:" in body and contains_any(body, SPEC_FIELDS["hypothesis"])))


def direct_requires_full_spec(msg: Message) -> bool:
    """Return true only for directives that are actually starting L1 research.

    Operational directives such as pause/standby/protocol ACK/heartbeat can be
    valid DIRECT messages, but they should not be forced through the L1 eight
    field research-spec gate.
    """

    kind_upper = msg.kind.upper()
    body = msg.body
    body_lower = body.lower()
    if is_l1_direct(msg):
        return True
    if "Spec:" in body and contains_any(body, SPEC_FIELDS["hypothesis"]):
        return True
    if "<!-- l0-entry-id:" in body and all(contains_any(body, aliases) for aliases in SPEC_FIELDS.values()):
        return True
    control_terms = (
        "PROTOCOL/",
        "HEARTBEAT",
        "PAUSE",
        "STANDBY",
        "ACK",
        "TRIGGER_EXECUTE",
        "FATAL_ACK",
        "COST_FATAL",
    )
    if any(term in kind_upper for term in control_terms):
        return False
    if any(term in body_lower for term in ("claim:", "gate:", "current gate:")) and not contains_any(body, SPEC_FIELDS["hypothesis"]):
        return False
    return False


def extract_run_id(msg: Message) -> str | None:
    patterns = (
        r"\brun[-_ ]?id\s*[:=]\s*`?([A-Za-z0-9_.-]+)`?",
        r"`data/([^/`]+)/(?:spec|l0_[^/`]+)\.md`",
        r"data/([^/\s`]+)/(?:spec|l0_[^/\s`]+)\.md",
    )
    for pattern in patterns:
        match = re.search(pattern, msg.body, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def check_l0_preconditions(msg: Message, repo: Path) -> list[str]:
    entry = l0_entry_id(msg)
    if entry is None:
        return ["L1 DIRECT missing <!-- l0-entry-id: 1|2|3 -->."]
    run_id = extract_run_id(msg)
    if entry == "1.1":
        if not run_id:
            return ["l0-entry-id 1.1 requires a run-id for data/<run-id>/l0_intuition.md."]
        path = repo / "data" / run_id / "l0_intuition.md"
        text = read_text(path)
        if not path.exists() or "强" not in text or "中" not in text or "弱" not in text:
            return [f"missing L0 intuition precondition: {path} with 强/中/弱 ladder."]
    if entry == "2.2":
        if not run_id:
            return ["l0-entry-id 2.2 requires a run-id for data/<run-id>/l0_anomaly.md."]
        path = repo / "data" / run_id / "l0_anomaly.md"
        text = read_text(path)
        if not path.exists() or not ("异常" in text and "假设" in text):
            return [f"missing L0 anomaly precondition: {path} with anomaly and hypothesis."]
    if entry == "3":
        if not run_id:
            return ["l0-entry-id 3 requires a run-id for data/<run-id>/l0_reproduction.md."]
        path = repo / "data" / run_id / "l0_reproduction.md"
        text = read_text(path)
        if not path.exists() or not ("作者数据" in text and "我方数据" in text):
            return [f"missing L0 reproduction precondition: {path} with 作者数据 vs 我方数据."]
    return []


def count_block_items(text: str, block_name: str) -> int | None:
    match = re.search(rf"(?im)^\s*{re.escape(block_name)}\s*:\s*$", text)
    if not match:
        return None
    tail = text[match.end() :].splitlines()
    count = 0
    in_block = False
    for line in tail:
        if not line.strip():
            if in_block:
                break
            continue
        if re.match(r"^\S[^:]*:\s*$", line):
            break
        in_block = True
        if re.match(r"^\s*[-*]\s+", line):
            count += 1
    return count


def collaborative_count_reasons(msg: Message) -> list[str]:
    body_lower = msg.body.lower()
    kind_lower = msg.kind.lower()
    if not any(term in body_lower or term in kind_lower for term in ("collaborative", "协作", "candidates", "codex_additions")):
        return []
    reasons: list[str] = []
    candidates = count_block_items(msg.body, "candidates")
    if candidates is not None and not (3 <= candidates <= 5):
        reasons.append(f"candidates count {candidates} is outside required 3-5.")
    additions = count_block_items(msg.body, "codex_additions")
    if additions is not None and not (0 <= additions <= 2):
        reasons.append(f"codex_additions count {additions} is outside required 0-2.")
    return reasons


def tokenize(text: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9_]{3,}|[\u4e00-\u9fff]{2,}", text)
        if token.lower() not in {"the", "and", "for", "with", "this", "that", "data"}
    }


def section_between(text: str, start: str, end: str | None) -> str:
    start_match = re.search(re.escape(start), text)
    if not start_match:
        return ""
    if end is None:
        return text[start_match.end() :]
    end_match = re.search(re.escape(end), text[start_match.end() :])
    if not end_match:
        return text[start_match.end() :]
    return text[start_match.end() : start_match.end() + end_match.start()]


def hypothesis_text(msg: Message) -> str:
    match = re.search(r"(hypothesis|假设)\s*[:：]\s*(?P<value>.+)", msg.body, flags=re.IGNORECASE)
    if match:
        return match.group("value")
    return msg.body[:1000]


def duplicate_reasons(msg: Message, ledger: Path) -> list[str]:
    ledger_text = read_text(ledger)
    if not ledger_text.strip():
        return []
    query_tokens = tokenize(hypothesis_text(msg))
    if not query_tokens:
        return []
    rejected = section_between(ledger_text, "## 二、已确认无效", "## 三、未完成线索")
    open_threads = section_between(ledger_text, "## 三、未完成线索", "## 四、未来探索方向")
    reasons: list[str] = []
    for label, block, status in (
        ("rejected", rejected, "HANDOFF/L0-DUPLICATE-REJECTED"),
        ("open_threads", open_threads, "HANDOFF/L0-DUPLICATE-MERGE"),
    ):
        for raw_line in block.splitlines():
            line = raw_line.strip()
            if not line.startswith(("-", "|")) or "---" in line:
                continue
            line_tokens = tokenize(line)
            if not line_tokens:
                continue
            score = len(query_tokens & line_tokens) / max(1, len(query_tokens | line_tokens))
            if score >= 0.35:
                reasons.append(f"{status}: fuzzy duplicate in {label} (score={score:.2f}): {line[:160]}")
                return reasons
    return reasons


def load_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked(path, "w") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()


def spec_id(msg: Message) -> str:
    for pattern in (r"\bspec[-_ ]?id\s*[:=]\s*`?([A-Za-z0-9_.-]+)`?", r"\bTask:\s*(.+)"):
        match = re.search(pattern, msg.body, flags=re.IGNORECASE)
        if match:
            return re.sub(r"\s+", "-", match.group(1).strip())[:80]
    return msg.kind


def update_disagreement_counter(msg: Message, counters_path: Path) -> list[str]:
    body = msg.body.lower()
    if not any(term in body for term in ("反驳", "不同意", "disagree", "push back", "objection")):
        return []
    data = load_json(counters_path, {})
    if not isinstance(data, dict):
        data = {}
    sid = spec_id(msg)
    row = data.get(sid, {})
    if not isinstance(row, dict):
        row = {}
    round_num = int(row.get("round_num", 0))
    if row.get("last_speaker") == "codex":
        round_num += 1
    row = {"round_num": round_num, "last_speaker": "claude", "updated_at": now_cst_iso()}
    data[sid] = row
    write_json(counters_path, data)
    if round_num >= 3 and "round-3-deferral" not in msg.body:
        return [f"U12 round {round_num + 1} requires literal round-3-deferral."]
    return []


def missing_spec_fields(msg: Message) -> list[str]:
    body = msg.body
    return [field for field, aliases in SPEC_FIELDS.items() if not contains_any(body, aliases)]


def missing_q_ack(msg: Message) -> list[str]:
    missing: list[str] = []
    for idx in range(1, 6):
        if not re.search(rf"\bQ{idx}\b|Q{idx}[：:]", msg.body, flags=re.IGNORECASE):
            missing.append(f"Q{idx}")
    return missing


def mode_b_violations(msg: Message) -> list[str]:
    body = msg.body.lower()
    if not ("模式 b" in body or "mode b" in body or "模式B" in msg.body):
        return []
    checks = {
        "B1-code-change": (".py", "strategies/", "scripts/"),
        "B2-new-data-source": ("新数据", "new data", "data source"),
        "B3-yellow-yaml-param": ("yaml 黄区", "yellow", "tunable_space.yaml"),
        "B4-redline-change": ("protocol_redline", "redline", "协议红线"),
        "B5-invalid-ledger": ("已确认无效", "confirmed invalid"),
    }
    out: list[str] = []
    for code, aliases in checks.items():
        if any(alias.lower() in body for alias in aliases):
            out.append(code)
    return out


def validate_message(
    msg: Message,
    protocol_doc: Path,
    repo: Path,
    ledger: Path,
    disagreement_counters: Path,
) -> tuple[bool, str, list[str]]:
    local_version = local_protocol_version(protocol_doc)
    remote_version = message_protocol_version(msg)
    if remote_version is None:
        return False, "HANDOFF/MISSING-PROTOCOL", [
            "First non-empty message line is not <!-- protocol-redline-vX.Y -->.",
        ]
    if local_version is None:
        return False, "HANDOFF/VERSION-MISSING-LOCAL", [
            f"Could not read local protocol version from {protocol_doc}.",
        ]
    if remote_version != local_version:
        return False, "HANDOFF/VERSION-MISMATCH", [
            f"Claude message protocol v{remote_version}; local protocol v{local_version}.",
        ]

    kind_upper = msg.kind.upper()
    if "DIRECT" in kind_upper:
        requires_full_spec = direct_requires_full_spec(msg)
        if requires_full_spec and is_l1_direct(msg):
            precondition_reasons = check_l0_preconditions(msg, repo)
            if precondition_reasons:
                status = "HANDOFF/MISSING-L0-ENTRY" if l0_entry_id(msg) is None else "HANDOFF/MISSING-L0-PRECONDITION"
                return False, status, precondition_reasons
            duplicate = duplicate_reasons(msg, ledger)
            if duplicate:
                return False, duplicate[0].split(":", 1)[0], duplicate
        if requires_full_spec:
            missing = missing_spec_fields(msg)
            if missing:
                return False, "HANDOFF/MISSING-SPEC", [
                    "Missing U3 spec fields: " + ", ".join(missing),
                ]
        collab_reasons = collaborative_count_reasons(msg)
        if collab_reasons:
            return False, "HANDOFF/INVALID-CANDIDATE-COUNT", collab_reasons
        if requires_full_spec and not contains_any(msg.body, SPEC_FIELDS["compute_estimate"]):
            return False, "HANDOFF/MISSING-COMPUTE", [
                "DIRECT message does not include compute/runtime estimate.",
            ]
        if "L4" in kind_upper or re.search(r"\bL4\b", msg.body):
            missing_q = missing_q_ack(msg)
            if missing_q:
                return False, "HANDOFF/MISSING-Q1-5", [
                    "L4 DIRECT missing ACK items: " + ", ".join(missing_q),
                ]
        violations = mode_b_violations(msg)
        if violations:
            return False, "HANDOFF/MODE-B-VIOLATION", [
                "Mode B message contains blocked terms: " + ", ".join(violations),
            ]
        disagreement_reasons = update_disagreement_counter(msg, disagreement_counters)
        if disagreement_reasons:
            return False, "HANDOFF/U12-ROUND-DEFERRAL-REQUIRED", disagreement_reasons

    return True, "OK", []


def validate_paper_candidates(path: Path) -> tuple[bool, list[str]]:
    text = read_text(path)
    if not text.strip():
        return False, [f"{path} is empty or missing."]
    chunks = re.split(r"(?m)^###\s+", text)
    allowed_rule_re = re.compile(r"passed_rule\s*[:：]\s*(试盘|引用|顶会|复现)")
    reasons: list[str] = []
    candidate_chunks = [chunk for chunk in chunks if "- priority:" in chunk or "- passed_rule:" in chunk or "- evidence:" in chunk]
    for idx, chunk in enumerate(candidate_chunks, 1):
        if "passed_rule" not in chunk:
            reasons.append(f"candidate {idx} missing passed_rule.")
        elif not allowed_rule_re.search(chunk):
            reasons.append(f"candidate {idx} passed_rule is not one of 试盘|引用|顶会|复现.")
        if "evidence" not in chunk:
            reasons.append(f"candidate {idx} missing evidence.")
        if not re.search(r"priority\s*[:：]\s*(高|中|低)", chunk):
            reasons.append(f"candidate {idx} missing priority 高/中/低.")
    return not reasons, reasons


def now_cst() -> str:
    return datetime.now(tz=ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M CST")


def now_cst_iso() -> str:
    return datetime.now(tz=ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def append_handoff(
    codex_box: Path,
    state_file: Path,
    msg: Message,
    status: str,
    reasons: list[str],
    protocol_doc: Path,
) -> None:
    local_version = local_protocol_version(protocol_doc) or "unknown"
    msg_hash = message_hash(msg)
    reason_lines = "\n".join(f"- {reason}" for reason in reasons)
    block = f"""
### {now_cst()} - Codex - {status}

<!-- protocol-redline-v{local_version} -->
Project: quant
Task: outbox protocol preflight

Summary:

- Latest Claude `{msg.id_label}` was blocked by mechanical preflight before Codex execution.
- message hash: `msg-hash-{msg_hash}`
{reason_lines}
- No repo execution or backtest was started.
"""
    for path in (codex_box, state_file):
        path.parent.mkdir(parents=True, exist_ok=True)
        with locked(path, "a") as fh:
            fh.write(block)
            if not block.endswith("\n"):
                fh.write("\n")
            fh.flush()


def cmd_check(args: argparse.Namespace) -> int:
    claude_text = read_text(args.claude_box)
    msg = latest_message(claude_text)
    if msg is None:
        print("skip: no Claude message")
        return EXIT_SKIP
    if msg.actor != "Claude":
        print(f"skip: latest actor is {msg.actor}")
        return EXIT_SKIP

    codex_text = read_text(args.codex_box)
    state_text = read_text(args.state_file)
    msg_hash = message_hash(msg)
    cache = read_cache(args.cache)
    if already_acknowledged(msg, msg_hash, codex_text, state_text, cache):
        print(f"skip: already acknowledged {msg.id_label} msg-hash-{msg_hash}")
        return EXIT_SKIP

    ok, status, reasons = validate_message(
        msg,
        args.protocol_doc,
        args.repo,
        args.ledger,
        args.disagreement_counters,
    )
    if ok:
        print(f"ok: {msg.id_label} msg-hash-{msg_hash}")
        return EXIT_OK

    if args.emit_handoff:
        append_handoff(args.codex_box, args.state_file, msg, status, reasons, args.protocol_doc)
        mark_cache(args.cache, msg)
        print(f"handoff: {status} for {msg.id_label} msg-hash-{msg_hash}")
        return EXIT_HANDOFF

    print(f"{status}: {'; '.join(reasons)}")
    return EXIT_HANDOFF


def cmd_mark(args: argparse.Namespace) -> int:
    msg = latest_message(read_text(args.claude_box))
    if msg is None:
        print("skip: no Claude message")
        return EXIT_SKIP
    mark_cache(args.cache, msg)
    print(f"marked: {msg.id_label} msg-hash-{message_hash(msg)}")
    return EXIT_OK


def cmd_validate_paper_candidates(args: argparse.Namespace) -> int:
    ok, reasons = validate_paper_candidates(args.path)
    if ok:
        print(f"ok: {args.path}")
        return EXIT_OK
    for reason in reasons:
        print(reason)
    return EXIT_HANDOFF


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Quant outbox protocol preflight")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--claude-box", type=Path, required=True)
        p.add_argument("--cache", type=Path, default=DEFAULT_CACHE)

    check = sub.add_parser("check", help="Check latest Claude message")
    add_common(check)
    check.add_argument("--codex-box", type=Path, required=True)
    check.add_argument("--state-file", type=Path, required=True)
    check.add_argument("--protocol-doc", type=Path, default=DEFAULT_PROTOCOL_DOC)
    check.add_argument("--repo", type=Path, default=Path("."))
    check.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    check.add_argument("--disagreement-counters", type=Path, default=DEFAULT_DISAGREEMENT_COUNTERS)
    check.add_argument("--emit-handoff", action="store_true")
    check.set_defaults(func=cmd_check)

    mark = sub.add_parser("mark", help="Mark latest Claude message processed")
    add_common(mark)
    mark.set_defaults(func=cmd_mark)

    paper = sub.add_parser("validate-paper-candidates", help="Validate arxiv candidate markdown")
    paper.add_argument("path", type=Path)
    paper.set_defaults(func=cmd_validate_paper_candidates)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
