#!/usr/bin/env python3
"""Validate truth-document synchronization for truth-affecting changes.

This checker closes the gap between "the doc is valid" and "the doc was
updated when it should have been".

Rules:
- Strategy truth triggers must be accompanied by current.yaml / baseline_registry
  changes, or by an explicit waiver under
  data/research_framework/truth_sync_waivers/*.yaml.
- Run manifests with promotion_status != experiment are truth triggers.
- Clean worktree/stage passes.

The checker uses staged files when there are staged changes; otherwise it checks
the working tree. This keeps pre-commit behavior focused on the commit while
still making manual preflight useful before staging.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml required", file=sys.stderr)
    sys.exit(2)


REPO_ROOT = Path(__file__).resolve().parent.parent

CURRENT_YAML = "data/research_framework/current.yaml"
BASELINE_YAML = "data/research_framework/baseline_registry.yaml"
STRATEGIES_YAML = "data/research_framework/strategies.yaml"
WAIVER_DIR = "data/research_framework/truth_sync_waivers/"
AUDIT_LOG = REPO_ROOT / "data" / "research_framework" / "protected_action_audit.jsonl"

TRUTH_SYNC_PATHS = {CURRENT_YAML, BASELINE_YAML}
WAIVER_REQUIRED = {"schema_version", "date", "decision", "reason", "changed_paths", "reviewer"}
ALLOWED_WAIVER_DECISIONS = {"no_truth_change", "defer_truth_update"}
ALLOWED_REVIEWERS = {"codex", "claude", "user", "auto"}
PLACEHOLDER_MARKERS = ("<TODO", "TODO>", "TBD", "<待填>", "(待填)", "placeholder", "PLACEHOLDER")


def run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git"] + args,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def staged_files() -> list[str]:
    result = run_git(["diff", "--cached", "--name-only", "--diff-filter=ACMRTD"])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git diff --cached failed")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def working_tree_files() -> list[str]:
    result = run_git(["status", "--short"])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git status failed")
    files: list[str] = []
    for raw in result.stdout.splitlines():
        if not raw.strip():
            continue
        path = raw[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if path:
            files.append(path)
    return files


def changed_files() -> tuple[list[str], str]:
    staged = staged_files()
    if staged:
        return staged, "staged"
    return working_tree_files(), "working-tree"


def is_strategy_core_path(path: str) -> bool:
    if not path.startswith("strategies/cb_arb/"):
        return False
    if "/tests/" in path or path.endswith("/tests"):
        return False
    if path.endswith("__init__.py"):
        return False
    return path.endswith((".py", ".yaml", ".yml"))


def is_run_manifest(path: str) -> bool:
    return path.startswith("data/research_framework/run_manifests/") and path.endswith(".yaml")


def is_waiver_path(path: str) -> bool:
    return path.startswith(WAIVER_DIR) and path.endswith((".yaml", ".yml"))


def load_yaml_from_worktree(path: str) -> dict[str, Any] | None:
    full = REPO_ROOT / path
    if not full.exists():
        return None
    try:
        data = yaml.safe_load(full.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: YAML parse error: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: YAML root must be dict")
    return data


def load_yaml_from_staged(path: str) -> dict[str, Any] | None:
    result = run_git(["show", f":{path}"])
    if result.returncode != 0:
        return None
    try:
        data = yaml.safe_load(result.stdout)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: staged YAML parse error: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: staged YAML root must be dict")
    return data


def manifest_promotion_status(
    path: str,
    loader: Callable[[str], dict[str, Any] | None],
) -> str | None:
    data = loader(path)
    if data is None:
        return None
    return str(data.get("promotion_status") or "").strip()


def classify_triggers(
    paths: list[str],
    manifest_loader: Callable[[str], dict[str, Any] | None] = load_yaml_from_worktree,
) -> list[dict[str, str]]:
    triggers: list[dict[str, str]] = []
    for path in paths:
        if is_strategy_core_path(path):
            triggers.append({"path": path, "reason": "strategy_core_changed"})
            continue
        if path == STRATEGIES_YAML:
            triggers.append({"path": path, "reason": "strategy_registry_changed"})
            continue
        if is_run_manifest(path):
            status = manifest_promotion_status(path, manifest_loader)
            if status is None:
                triggers.append({
                    "path": path,
                    "reason": "run_manifest_deleted_or_unreadable",
                })
                continue
            if status and status != "experiment":
                triggers.append({
                    "path": path,
                    "reason": f"run_manifest_promotion_status={status}",
                })
    return triggers


def has_truth_sync(paths: list[str]) -> bool:
    return any(path in TRUTH_SYNC_PATHS for path in paths)


def contains_placeholder(value: Any) -> bool:
    if isinstance(value, str):
        return any(marker in value for marker in PLACEHOLDER_MARKERS)
    if isinstance(value, list):
        return any(contains_placeholder(v) for v in value)
    if isinstance(value, dict):
        return any(contains_placeholder(k) or contains_placeholder(v) for k, v in value.items())
    return False


def validate_waiver_data(path: str, data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    missing = WAIVER_REQUIRED - set(data.keys())
    if missing:
        errors.append(f"{path}: missing required fields: {sorted(missing)}")
    if data.get("schema_version") != 1:
        errors.append(f"{path}: schema_version must be 1")
    if data.get("decision") not in ALLOWED_WAIVER_DECISIONS:
        errors.append(f"{path}: decision must be one of {sorted(ALLOWED_WAIVER_DECISIONS)}")
    if data.get("reviewer") not in ALLOWED_REVIEWERS:
        errors.append(f"{path}: reviewer must be one of {sorted(ALLOWED_REVIEWERS)}")
    reason = data.get("reason")
    if not isinstance(reason, str) or len(reason.strip()) < 20:
        errors.append(f"{path}: reason must be a concrete sentence (>=20 chars)")
    changed = data.get("changed_paths")
    if not isinstance(changed, list) or not any(isinstance(x, str) and x.strip() for x in changed):
        errors.append(f"{path}: changed_paths must be a non-empty list")
    date = str(data.get("date") or "")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        errors.append(f"{path}: date must be YYYY-MM-DD")
    if contains_placeholder(data):
        errors.append(f"{path}: contains placeholder/TODO text")
    return errors


def validate_waivers(
    paths: list[str],
    loader: Callable[[str], dict[str, Any] | None] = load_yaml_from_worktree,
) -> tuple[list[str], list[str]]:
    waiver_paths = [p for p in paths if is_waiver_path(p)]
    errors: list[str] = []
    covered: list[str] = []
    for path in waiver_paths:
        data = loader(path)
        if data is None:
            errors.append(f"{path}: waiver file missing")
            continue
        errors.extend(validate_waiver_data(path, data))
        changed = data.get("changed_paths")
        if isinstance(changed, list):
            covered.extend(str(p) for p in changed if isinstance(p, str))
    return errors, covered


def has_waiver_for_triggers(triggers: list[dict[str, str]], covered_paths: list[str]) -> bool:
    if not triggers:
        return True
    covered = set(covered_paths)
    return all(t["path"] in covered for t in triggers)


def audit_relevant_paths(paths: list[str], triggers: list[dict[str, str]]) -> list[str]:
    relevant = set(TRUTH_SYNC_PATHS)
    relevant.add(STRATEGIES_YAML)
    relevant.update(t["path"] for t in triggers)
    relevant.update(p for p in paths if is_waiver_path(p))
    return sorted(p for p in paths if p in relevant)


def diff_for_paths(paths: list[str], source: str) -> str:
    if not paths:
        return ""
    args = ["diff", "--cached", "--"] if source == "staged" else ["diff", "--"]
    result = run_git(args + paths)
    if result.returncode != 0:
        return result.stderr.strip()
    return result.stdout


def audit_event_hash(
    *,
    source: str,
    relevant_paths: list[str],
    triggers: list[dict[str, str]],
    status: str,
    diff_text: str,
) -> str:
    payload = {
        "source": source,
        "relevant_paths": relevant_paths,
        "triggers": triggers,
        "status": status,
        "diff_sha256": hashlib.sha256(diff_text.encode("utf-8")).hexdigest(),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]


def existing_audit_hashes(path: Path = AUDIT_LOG) -> set[str]:
    if not path.exists():
        return set()
    hashes: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        value = row.get("event_hash")
        if isinstance(value, str):
            hashes.add(value)
    return hashes


def append_protected_action_audit(
    *,
    paths: list[str],
    source: str,
    triggers: list[dict[str, str]],
    errors: list[str],
    waiver_covered: list[str],
    audit_path: Path = AUDIT_LOG,
) -> str | None:
    relevant_paths = audit_relevant_paths(paths, triggers)
    if not relevant_paths:
        return None
    status = "blocked" if errors else "accepted_by_mechanical_sync"
    diff_text = diff_for_paths(relevant_paths, source)
    event_hash = audit_event_hash(
        source=source,
        relevant_paths=relevant_paths,
        triggers=triggers,
        status=status,
        diff_text=diff_text,
    )
    if event_hash in existing_audit_hashes(audit_path):
        return event_hash
    row = {
        "schema_version": 1,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "event_hash": event_hash,
        "source": source,
        "status": status,
        "relevant_paths": relevant_paths,
        "triggers": triggers,
        "truth_sync_paths_changed": [p for p in paths if p in TRUTH_SYNC_PATHS],
        "waiver_covered_paths": sorted(set(waiver_covered)),
        "errors": errors,
        "actor": "validate_truth_sync.py",
        "note": "Automatic trace only. This record does not grant approval; it records truth/protected changes even when unauthorized.",
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
        fh.write("\n")
    return event_hash


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--waivers-only", action="store_true",
                        help="only validate changed waiver files")
    args = parser.parse_args()

    try:
        paths, source = changed_files()
        yaml_loader = load_yaml_from_staged if source == "staged" else load_yaml_from_worktree
        waiver_errors, waiver_covered = validate_waivers(paths, yaml_loader)
        if args.waivers_only:
            if waiver_errors:
                for err in waiver_errors:
                    print(f"ERROR {err}")
                return 1
            print("validate_truth_sync.py: waiver files OK")
            return 0

        triggers = classify_triggers(paths, yaml_loader)
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2

    errors: list[str] = []
    errors.extend(waiver_errors)
    if triggers and not has_truth_sync(paths) and not has_waiver_for_triggers(triggers, waiver_covered):
        errors.append(
            "truth-affecting changes require current.yaml / baseline_registry update "
            "or truth_sync_waiver"
        )
        for trigger in triggers:
            errors.append(f"  trigger: {trigger['path']} ({trigger['reason']})")
        errors.append(
            f"  add one of: {CURRENT_YAML}, {BASELINE_YAML}, "
            f"or {WAIVER_DIR}<slug>.yaml"
        )

    audit_hash = append_protected_action_audit(
        paths=paths,
        source=source,
        triggers=triggers,
        errors=errors,
        waiver_covered=waiver_covered,
    )

    if errors:
        print(f"validate_truth_sync.py: FAIL ({source})")
        for err in errors:
            print(f"  ERROR {err}")
        if audit_hash:
            print(f"  audit_event_hash: {audit_hash}")
        return 1

    if triggers:
        if has_truth_sync(paths):
            reason = "truth document changed"
        else:
            reason = "waiver covers triggers"
        print(f"validate_truth_sync.py: OK ({source}, {len(triggers)} trigger(s), {reason})")
    else:
        print(f"validate_truth_sync.py: OK ({source}, no truth-sync triggers)")
    if audit_hash:
        print(f"validate_truth_sync.py: audit_event_hash={audit_hash}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
