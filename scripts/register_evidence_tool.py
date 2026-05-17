#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from framework.autonomous.artifacts import ArtifactStore  # noqa: E402
from framework.autonomous.evidence_tool_registry import (  # noqa: E402
    DEFAULT_TOOL_REGISTRY_PATH,
    EvidenceToolRegistry,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Register a new EvidenceToolkit tool after reviewing existing tools.")
    parser.add_argument("--registry", type=Path, default=DEFAULT_TOOL_REGISTRY_PATH)
    parser.add_argument("--tool-yaml", type=Path, help="YAML descriptor for the new tool.")
    parser.add_argument("--print-existing-manifest", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    registry_store = EvidenceToolRegistry(args.registry)
    registry = registry_store.load()
    if args.print_existing_manifest:
        print(yaml.safe_dump(registry_store.manifest(registry), allow_unicode=True, sort_keys=False))
        return 0
    if args.tool_yaml is None:
        print("ERROR: --tool-yaml is required unless --print-existing-manifest is used", file=sys.stderr)
        return 2

    descriptor = ArtifactStore().read_yaml(args.tool_yaml)
    errors = registry_store.validate_descriptor(descriptor, registry)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    event_hash = registry_store.register(descriptor)
    print(f"registered evidence tool: {descriptor['id']}")
    print(f"framework_change_event_hash: {event_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
