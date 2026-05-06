"""Tests for the tunable_space.yaml editor.

All tests are isolated via ``tmp_path`` — they MUST NOT touch the real
``strategies/cb_redemption/tunable_space.yaml`` or
``logs/editor_writes.jsonl``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from strategies.cb_redemption.editor import (
    DEFAULT_SPACE_FILE,
    MissingRationaleError,
    OutOfRangeError,
    SchemaError,
    UnknownItemError,
    add_entry,
    list_writable,
    read_space,
    update_value,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def space_file(tmp_path: Path) -> Path:
    """Copy of the real tunable_space.yaml inside tmp_path."""
    dst = tmp_path / "tunable_space.yaml"
    shutil.copy(DEFAULT_SPACE_FILE, dst)
    return dst


@pytest.fixture
def audit_log(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "editor_writes.jsonl"


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_read_space_normal(space_file: Path) -> None:
    data = read_space(space_file)
    assert data["version"] == 1
    assert data["strategy"] == "cb_redemption"
    # 5 维参数（AI 因子已删）
    assert len(data["parameters"]) == 5
    names = {p["name"] for p in data["parameters"]}
    assert "w_redeem_progress" in names


def test_read_space_invalid_schema_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"version": 2, "strategy": "cb_redemption"}))
    with pytest.raises(SchemaError):
        read_space(bad)


def test_update_value_happy_path(space_file: Path, audit_log: Path) -> None:
    out = update_value(
        "parameters.w_redeem_progress",
        new_value=2.5,
        expected_direction="↑ → oos_sharpe ↑",
        reason="CMA-ES 第 3 代采样命中，IS score +0.4",
        path=space_file,
        audit_log=audit_log,
    )
    assert out["new_value"] == 2.5
    assert out["old_value"] == pytest.approx(2.1997)

    # 重新读盘，验证 current 已落地。
    data = read_space(space_file)
    item = next(p for p in data["parameters"] if p["name"] == "w_redeem_progress")
    assert item["current"] == 2.5
    # last_updated 应该已被刷新
    assert data["last_updated"].endswith("Z")


def test_update_value_out_of_range_raises(space_file: Path, audit_log: Path) -> None:
    with pytest.raises(OutOfRangeError):
        update_value(
            "parameters.w_redeem_progress",
            new_value=10.0,  # range = [0.5, 5.0]
            expected_direction="↑",
            reason="break the bounds",
            path=space_file,
            audit_log=audit_log,
        )

    # 越界拒写：原值不变，audit 不写入。
    data = read_space(space_file)
    item = next(p for p in data["parameters"] if p["name"] == "w_redeem_progress")
    assert item["current"] == pytest.approx(2.1997)
    assert not audit_log.exists()


def test_update_value_missing_expected_direction_raises(
    space_file: Path, audit_log: Path
) -> None:
    with pytest.raises(MissingRationaleError):
        update_value(
            "thresholds.action",
            new_value=0.7,
            expected_direction="   ",  # 空白也算缺
            reason="some reason",
            path=space_file,
            audit_log=audit_log,
        )


def test_update_value_missing_reason_raises(space_file: Path, audit_log: Path) -> None:
    with pytest.raises(MissingRationaleError):
        update_value(
            "thresholds.action",
            new_value=0.7,
            expected_direction="↑",
            reason="",
            path=space_file,
            audit_log=audit_log,
        )


def test_update_value_unknown_item_path_raises(
    space_file: Path, audit_log: Path
) -> None:
    # 章节不存在
    with pytest.raises(UnknownItemError):
        update_value(
            "factors.redeem_progress",  # factors 不可写
            new_value=1.0,
            expected_direction="↑",
            reason="trying to bypass",
            path=space_file,
            audit_log=audit_log,
        )
    # 条目不存在
    with pytest.raises(UnknownItemError):
        update_value(
            "parameters.w_does_not_exist",
            new_value=1.0,
            expected_direction="↑",
            reason="typo test",
            path=space_file,
            audit_log=audit_log,
        )


def test_add_entry_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        add_entry("parameters", name="w_new", current=0.0, range=[-1, 1])


def test_update_value_appends_audit_log_line(
    space_file: Path, audit_log: Path
) -> None:
    assert not audit_log.exists()

    update_value(
        "thresholds.action",
        new_value=0.7,
        expected_direction="↑ → 提高 precision",
        reason="基线胜率偏低，收紧准入",
        path=space_file,
        audit_log=audit_log,
    )

    assert audit_log.exists()
    lines_after_first = audit_log.read_text().strip().splitlines()
    assert len(lines_after_first) == 1
    rec = json.loads(lines_after_first[0])
    assert rec["item_path"] == "thresholds.action"
    assert rec["new_value"] == 0.7
    assert rec["old_value"] == pytest.approx(0.65)
    assert rec["expected_direction"].startswith("↑")
    assert rec["reason"]
    assert rec["ts"].endswith("Z")

    # 第二次写入应再 append 一行。
    update_value(
        "rules.hold_max_days",
        new_value=20,
        expected_direction="↑ → 多吃 PEAD 漂移",
        reason="60 天 PEAD 数据显示 15 日提前出场",
        path=space_file,
        audit_log=audit_log,
    )
    lines_after_second = audit_log.read_text().strip().splitlines()
    assert len(lines_after_second) == 2


def test_list_writable_returns_all_green_items(space_file: Path) -> None:
    items = list_writable(space_file)
    paths = {it["item_path"] for it in items}
    assert "parameters.w_redeem_progress" in paths
    assert "thresholds.action" in paths
    assert "rules.hold_max_days" in paths
    # factors 章节不应出现（黄线，结构改动走 PR）
    assert not any(p.startswith("factors.") for p in paths)
