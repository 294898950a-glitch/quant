#!/usr/bin/env python3
"""Validate machine-owned runtime entrypoints.

Markdown maps are not runtime inputs. The AI context must be loaded from
data/research_framework/runtime_entrypoints.yaml.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml required", file=sys.stderr)
    sys.exit(1)


REPO_ROOT = Path(__file__).resolve().parent.parent
RUNTIME = REPO_ROOT / "data" / "research_framework" / "runtime_entrypoints.yaml"
PROTOCOL = REPO_ROOT / "data" / "research_framework" / "protocol_rules.yaml"
CURRENT = REPO_ROOT / "data" / "research_framework" / "current.yaml"
EXPERIMENTS = REPO_ROOT / "data" / "research_framework" / "experiments.yaml"

FORBIDDEN_RUNTIME_MD = [
    "docs/INDEX.md",
    "docs/research_framework/CURRENT.md",
    "docs/research_framework/protocol_redline.md",
]
ALLOWED_MARKDOWN = {"AGENTS.md", "CLAUDE.md"}


def is_markdown_artifact(path: Path) -> bool:
    name = path.name
    return name.endswith(".md") or ".md." in name


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(str(path.relative_to(REPO_ROOT)))
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.relative_to(REPO_ROOT)} root must be a mapping")
    return data


def main() -> int:
    issues: list[str] = []
    try:
        runtime = load_yaml(RUNTIME)
        protocol = load_yaml(PROTOCOL)
        load_yaml(CURRENT)
        load_yaml(EXPERIMENTS)
    except (FileNotFoundError, ValueError, yaml.YAMLError) as exc:
        print(f"validate_entrypoints.py: FAIL {exc}")
        return 1

    files = runtime.get("runtime_context", {}).get("files")
    prompt_contract = runtime.get("runtime_context", {}).get("prompt_contract")
    load_order = []
    if isinstance(prompt_contract, dict):
        raw_load_order = prompt_contract.get("load_order")
        if isinstance(raw_load_order, list):
            load_order = [str(item) for item in raw_load_order]
    if not load_order:
        issues.append("runtime_entrypoints.yaml missing prompt_contract.load_order")
    elif len(load_order) > 5:
        issues.append("runtime prompt load_order must stay at 5 or fewer core nodes")
    if not isinstance(files, dict) or not files:
        issues.append("runtime_entrypoints.yaml missing runtime_context.files")
    else:
        unknown_load_keys = [key for key in load_order if key not in files]
        for key in unknown_load_keys:
            issues.append(f"runtime load_order references unknown file key: {key}")
        for key, spec in files.items():
            if not isinstance(spec, dict):
                issues.append(f"runtime_context.files.{key} must be mapping")
                continue
            if spec.get("required") is True and key not in load_order:
                issues.append(f"required runtime file {key} must appear in prompt load_order")
            rel = spec.get("path")
            if not isinstance(rel, str) or not rel:
                issues.append(f"runtime_context.files.{key}.path missing")
                continue
            if rel.endswith(".md") and rel not in ALLOWED_MARKDOWN:
                issues.append(f"runtime entry {key} points to markdown: {rel}")
            if spec.get("required") is True and not (REPO_ROOT / rel).exists():
                issues.append(f"required runtime file missing: {rel}")

    protocol_rules = protocol.get("rules")
    if not isinstance(protocol_rules, list) or not protocol_rules:
        issues.append("protocol_rules.yaml missing non-empty rules")
    else:
        rule_ids = {str(rule.get("id")) for rule in protocol_rules if isinstance(rule, dict)}
        for required in {"R1", "R4", "R5", "R6", "R7", "R9"}:
            if required not in rule_ids:
                issues.append(f"protocol_rules.yaml missing {required}")

    for rel in FORBIDDEN_RUNTIME_MD:
        if (REPO_ROOT / rel).exists():
            issues.append(f"forbidden runtime markdown still exists: {rel}")

    agent = REPO_ROOT / "AGENTS.md"
    if not agent.exists():
        issues.append("AGENTS.md must remain as the primary AI-facing markdown bootstrap")

    claude = REPO_ROOT / "CLAUDE.md"
    if not claude.exists():
        issues.append("CLAUDE.md must remain as the Claude Code auto-entry pointer")

    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or not is_markdown_artifact(path):
            continue
        rel = path.relative_to(REPO_ROOT)
        rel_str = str(rel)
        if rel_str.startswith((".git/", ".venv/", ".pytest_cache/")):
            continue
        if rel_str not in ALLOWED_MARKDOWN:
            issues.append(f"markdown file is not allowed: {rel_str}")

    if issues:
        print("validate_entrypoints.py: FAIL")
        for issue in issues:
            print(f"  ERROR {issue}")
        return 1

    print("validate_entrypoints.py: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
