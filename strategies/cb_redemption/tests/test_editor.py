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


# --------------------------------------------------------------------------- #
# Round-trip 注释 / 空行保留（ruamel.yaml 替换 PyYAML 的回归保护）
# --------------------------------------------------------------------------- #


_MINIMAL_YAML_WITH_COMMENTS = """\
# top comment line A — explains the purpose of this file
# top comment line B — second line of the header

version: 1
strategy: cb_redemption
last_updated: '2026-05-07T00:00:00Z'

parameters:
  - name: w_redeem_progress
    current: 2.1997
    range: [0.5, 5.0]
    prior: inline prior comment about why this knob is allowed  # tail-of-line note

factors: []

thresholds:
  - name: action
    current: 0.65
    range: [0.0, 1.0]
    prior: precision baseline knob

rules:
  - name: hold_max_days
    current: 15
    range: [1, 60]
    prior: PEAD drift window cap
"""


@pytest.fixture
def commented_space_file(tmp_path: Path) -> Path:
    dst = tmp_path / "tunable_space.yaml"
    dst.write_text(_MINIMAL_YAML_WITH_COMMENTS)
    return dst


def test_update_value_preserves_yaml_comments(
    commented_space_file: Path, audit_log: Path
) -> None:
    """改完一个 .current 后，顶部注释和 inline 注释都必须仍在原始字节里。"""
    update_value(
        "parameters.w_redeem_progress",
        new_value=2.5,
        expected_direction="↑",
        reason="comment preservation regression guard",
        path=commented_space_file,
        audit_log=audit_log,
    )

    raw = commented_space_file.read_text()
    # 顶部注释（双行块）必须原样保留
    assert "# top comment line A — explains the purpose of this file" in raw
    assert "# top comment line B — second line of the header" in raw
    # inline 注释（行尾的 # tail-of-line note）也必须保留
    assert "# tail-of-line note" in raw
    # 真的写进去了：
    assert "current: 2.5" in raw


def test_update_value_preserves_blank_lines(
    commented_space_file: Path, audit_log: Path
) -> None:
    """改完一个 .current 后，原文件里的 section 间空行不能被吃掉。"""
    # 原文件里的"显著"空行：
    #   - 顶部 header 块之后、version 之前的那一行
    #   - last_updated 之后、parameters 之前的那一行
    #   - parameters 块之后、factors 之前的那一行
    before = commented_space_file.read_text().splitlines()
    blank_indices_before = {i for i, line in enumerate(before) if line == ""}
    assert len(blank_indices_before) >= 3, "fixture 自身应至少有 3 条空行"

    update_value(
        "parameters.w_redeem_progress",
        new_value=2.5,
        expected_direction="↑",
        reason="blank-line preservation regression guard",
        path=commented_space_file,
        audit_log=audit_log,
    )

    after = commented_space_file.read_text().splitlines()
    blank_count_after = sum(1 for line in after if line == "")
    # ruamel round-trip 应保留至少同等数量的空行（精确位置可能因 dump 略有偏移，
    # 但绝不应像 PyYAML 那样被压成 0 条）。
    assert blank_count_after >= len(blank_indices_before), (
        f"空行被吃掉了：原 {len(blank_indices_before)} 条，写回后 {blank_count_after} 条"
    )
