from __future__ import annotations

from pathlib import Path


def test_shell_entrypoints_derive_repo_root_without_wsl_path():
    root = Path(__file__).resolve().parents[2]
    for name in ("run_quant_internal_tick.sh", "hermes_executor_handoff_wakeup.sh"):
        text = (root / "scripts" / name).read_text(encoding="utf-8")
        assert "QUANT_REPO_ROOT" in text
        assert "/home/jay/projects/quant" not in text
