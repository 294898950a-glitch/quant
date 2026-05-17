from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_CHANGE_LOG_PATH = Path("data/research_framework/framework_change_log.jsonl")
AUTO_RECORDED_PATH_PREFIXES = (
    "framework/autonomous/",
    "scripts/run_strategy_ideation_once.py",
    "scripts/register_evidence_tool.py",
    "scripts/framework_preflight.py",
    "scripts/install_pre_commit_hook.sh",
    "data/research_framework/evidence_tool_registry.yaml",
    "data/research_framework/executor_registry.yaml",
    "data/research_framework/ai_providers.yaml",
    "data/research_framework/mechanics_vocab.yaml",
    "data/research_framework/autonomous_research_acceptance_criteria.yaml",
    "AGENTS.md",
    "CLAUDE.md",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _event_hash(row: dict[str, Any]) -> str:
    payload = {
        key: value
        for key, value in row.items()
        if key not in {"recorded_at", "event_hash"}
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:24]


def _existing_hashes(path: Path) -> set[str]:
    if not path.exists():
        return set()
    hashes = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("event_hash"):
            hashes.add(str(row["event_hash"]))
    return hashes


def record_framework_change(
    change_type: str,
    summary: str,
    changed_paths: list[str],
    actor: str,
    reason: str,
    evidence: dict[str, Any] | None = None,
    impact: str | None = None,
    log_path: Path | str = DEFAULT_CHANGE_LOG_PATH,
) -> str:
    path = Path(log_path)
    row = {
        "schema_version": 1,
        "recorded_at": _now_iso(),
        "change_type": change_type,
        "summary": summary,
        "changed_paths": changed_paths,
        "actor": actor,
        "reason": reason,
        "impact": impact,
        "evidence": evidence or {},
    }
    row["event_hash"] = _event_hash(row)
    if row["event_hash"] in _existing_hashes(path):
        return row["event_hash"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
        handle.write("\n")
    return row["event_hash"]


def _run_git(args: list[str], repo_root: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.stdout if result.returncode == 0 else ""


def _is_framework_path(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix) for prefix in AUTO_RECORDED_PATH_PREFIXES)


def _status_paths(repo_root: Path) -> list[dict[str, str]]:
    output = _run_git(["status", "--short"], repo_root)
    rows: list[dict[str, str]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        status = line[:2].strip()
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if _is_framework_path(path):
            rows.append({"status": status, "path": path})
    return rows


def _content_hash(repo_root: Path, paths: list[str]) -> str:
    digest = hashlib.sha256()
    diff = _run_git(["diff", "--", *paths], repo_root) if paths else ""
    digest.update(diff.encode("utf-8"))
    for rel_path in sorted(paths):
        path = repo_root / rel_path
        digest.update(rel_path.encode("utf-8"))
        digest.update(b"\0")
        if path.exists() and path.is_file():
            digest.update(path.read_bytes())
        digest.update(b"\n")
    return digest.hexdigest()


def auto_record_framework_changes(
    repo_root: Path | str = Path("."),
    actor: str = "auto",
    reason: str = "framework preflight detected framework-scope working tree changes",
    log_path: Path | str = DEFAULT_CHANGE_LOG_PATH,
) -> str | None:
    root = Path(repo_root).resolve()
    rows = _status_paths(root)
    if not rows:
        return None
    paths = [row["path"] for row in rows]
    content_hash = _content_hash(root, paths)
    return record_framework_change(
        change_type="framework_worktree_change_detected",
        summary=f"Detected {len(paths)} framework-scope changed path(s)",
        changed_paths=paths,
        actor=actor,
        reason=reason,
        impact="Automatic trace only; does not approve or reject the change.",
        evidence={
            "status_rows": rows,
            "content_hash": content_hash,
            "source": "framework_preflight",
        },
        log_path=Path(log_path),
    )
