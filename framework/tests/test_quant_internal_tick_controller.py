from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml


def _load_tick_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "quant_internal_tick.py"
    spec = importlib.util.spec_from_file_location("quant_internal_tick_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_controller_owner_mismatch_skips_ticket(monkeypatch, tmp_path):
    tick = _load_tick_module()
    current = tmp_path / "current.yaml"
    current.write_text(yaml.safe_dump({"controller": {"owner_host": "macbook", "generation": 2}}))
    monkeypatch.setattr(tick, "CURRENT_PATH", current)
    monkeypatch.setenv("QUANT_CONTROLLER_HOST", "jay-wsl")

    allowed, reason = tick.controller_owner_allows_tick()

    assert allowed is False
    assert "owner mismatch" in reason


def test_controller_owner_match_allows_ticket(monkeypatch, tmp_path):
    tick = _load_tick_module()
    current = tmp_path / "current.yaml"
    current.write_text(yaml.safe_dump({"controller": {"owner_host": "macbook", "generation": 2}}))
    monkeypatch.setattr(tick, "CURRENT_PATH", current)
    monkeypatch.setenv("QUANT_CONTROLLER_HOST", "macbook")

    assert tick.controller_owner_allows_tick()[0] is True


def test_controller_noop_is_audited(monkeypatch, tmp_path):
    tick = _load_tick_module()
    path = tmp_path / "orchestrator_log.jsonl"
    monkeypatch.setattr(tick, "CONTROLLER_AUDIT_PATH", path)
    tick.audit_controller_noop("owner mismatch")
    assert '"action": "controller_owner_noop"' in path.read_text(encoding="utf-8")


def test_malformed_controller_metadata_denies_without_crashing(monkeypatch, tmp_path):
    tick = _load_tick_module()
    current = tmp_path / "current.yaml"
    current.write_text("controller: [unterminated\n", encoding="utf-8")
    monkeypatch.setattr(tick, "CURRENT_PATH", current)

    allowed, reason = tick.controller_owner_allows_tick()

    assert allowed is False
    assert "unreadable" in reason
