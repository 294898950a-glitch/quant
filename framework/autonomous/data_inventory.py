from __future__ import annotations

from typing import Any


CORE_CATEGORIES = {"core_warehouse", "benchmark"}


def compact_data_inventory(
    inventory: dict[str, Any],
    *,
    max_missing_refs: int = 20,
) -> dict[str, Any]:
    """Return a prompt-safe view of the machine-owned data inventory."""
    if not isinstance(inventory, dict) or not inventory:
        return {
            "available": False,
            "reason": "data_inventory.yaml missing or empty",
        }

    files = inventory.get("files") if isinstance(inventory.get("files"), list) else []
    core_files = [compact_file_entry(item) for item in files if isinstance(item, dict) and item.get("category") in CORE_CATEGORIES]
    refs = inventory.get("referenced_data_paths") if isinstance(inventory.get("referenced_data_paths"), dict) else {}
    missing_refs = refs.get("missing_top") if isinstance(refs.get("missing_top"), list) else []
    return {
        "available": True,
        "summary": inventory.get("summary") or {},
        "core_files": core_files,
        "missing_referenced_data_top": missing_refs[:max_missing_refs],
        "rule": (
            "This is not data-quality approval. This inventory is limited to core database and benchmark files. "
            "Do not infer research conclusions from this inventory; use review memory and recent digest for experiment results. "
            "Use only paths shown here or paths declared by the matched executor. "
            "If a desired path is listed under missing_referenced_data_top, do not require it unless the proposal is explicitly about creating that data."
        ),
    }


def compact_file_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": entry.get("path"),
        "category": entry.get("category"),
        "format": entry.get("format"),
        "rows": entry.get("rows"),
        "columns": entry.get("columns"),
        "date_ranges": entry.get("date_ranges") or {},
        "readable": entry.get("readable"),
    }
