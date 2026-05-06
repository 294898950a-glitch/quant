"""Editor — 机器写入 ``tunable_space.yaml`` 的唯一入口。

边界（见 docs/plans/2026-05-07-self-loop-roadmap.md）:
    红：本仓所有 .py / verifier / score 函数         — 永远人工
    黄：tunable_space.yaml 的结构（增 / 删 / 改字段） — 必须人工 PR
    绿：tunable_space.yaml 的 .current 字段           — 机器可改（本模块）

设计意图：
    1. 一次只能改一个值（``update_value`` 只接受单个 ``item_path``，无 batch 接口）。
    2. 越界 / 缺 expected_direction / 缺 reason / 未知 item_path → 直接 raise，
       拒绝写入。
    3. ``add_entry`` 永远 raise NotImplementedError — 加新条目走人工 PR。
    4. 写入用 ``fcntl.flock`` 排他锁 + tmp + rename 原子写。
    5. 每次成功写入追加一行进 ``logs/editor_writes.jsonl`` 留痕。
"""

from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ruamel.yaml import YAML

# round-trip YAML 解析器：保留顶部注释、inline 注释、空行、缩进。
# 写入时如果换成 ``yaml.safe_dump`` 会把 tunable_space.yaml 顶部说明块和每个
# parameter 的 prior 注释吃掉，违背"yaml 里的 prior 字段就是给后来者看的"设计。
_yaml = YAML(typ="rt")
_yaml.preserve_quotes = True
_yaml.indent(mapping=2, sequence=4, offset=2)

# --------------------------------------------------------------------------- #
# 默认路径
# --------------------------------------------------------------------------- #

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent

DEFAULT_SPACE_FILE = _HERE / "tunable_space.yaml"
DEFAULT_AUDIT_LOG = _REPO / "logs" / "editor_writes.jsonl"

# 顶层 list 字段（结构由人工 PR 维护，本模块只读）
_LIST_SECTIONS = ("parameters", "factors", "thresholds", "rules")
# 实际允许 ``update_value`` 写 .current 的章节
_WRITABLE_SECTIONS = ("parameters", "thresholds", "rules")


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #


class SchemaError(ValueError):
    """``tunable_space.yaml`` 结构不合规。"""


class OutOfRangeError(ValueError):
    """``new_value`` 不在条目声明的 ``range`` 内。"""


class MissingRationaleError(ValueError):
    """缺 ``expected_direction`` 或 ``reason``。"""


class UnknownItemError(KeyError):
    """``item_path`` 在 yaml 中找不到对应条目。"""


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def _locked(path: Path, mode: str) -> Iterator:
    """对 ``path`` 持排他 flock，配合 tmp+rename 实现原子写。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, mode)
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield f
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()


def _validate_schema(data: Any) -> dict:
    """最小可用 schema 校验。不做强类型 — 留给人工 PR。"""
    if not isinstance(data, dict):
        raise SchemaError("yaml 顶层必须是 mapping")
    if data.get("version") != 1:
        raise SchemaError(f"unsupported version: {data.get('version')!r}, expected 1")
    if data.get("strategy") != "cb_redemption":
        raise SchemaError(f"strategy 字段必须为 'cb_redemption'，得到 {data.get('strategy')!r}")
    for section in _LIST_SECTIONS:
        items = data.get(section)
        if not isinstance(items, list):
            raise SchemaError(f"section {section!r} 必须为 list")
        seen: set[str] = set()
        for idx, item in enumerate(items):
            if not isinstance(item, dict) or "name" not in item:
                raise SchemaError(f"{section}[{idx}] 缺少 name 字段")
            name = item["name"]
            if name in seen:
                raise SchemaError(f"{section}.{name} 重复")
            seen.add(name)
            if section in _WRITABLE_SECTIONS:
                if "current" not in item or "range" not in item:
                    raise SchemaError(f"{section}.{name} 缺 current 或 range")
                rng = item["range"]
                if not (isinstance(rng, list) and len(rng) == 2 and rng[0] < rng[1]):
                    raise SchemaError(f"{section}.{name}.range 必须是 [min, max] 且 min<max")
    return data


def _split_path(item_path: str) -> tuple[str, str]:
    if "." not in item_path:
        raise UnknownItemError(
            f"item_path={item_path!r} 必须形如 'parameters.w_redeem_progress'"
        )
    section, name = item_path.split(".", 1)
    if section not in _WRITABLE_SECTIONS:
        raise UnknownItemError(
            f"section {section!r} 不可写；可写章节={_WRITABLE_SECTIONS}"
        )
    return section, name


def _find_item(data: dict, section: str, name: str) -> dict:
    for item in data[section]:
        if item.get("name") == name:
            return item
    raise UnknownItemError(f"item_path={section}.{name} 在 yaml 中不存在")


def _atomic_dump(path: Path, data: Any) -> None:
    """tmp + rename 原子写，写过程中持目标文件的排他锁。

    使用 ruamel round-trip dump，保留 ``data`` 上挂着的注释 / 空行 / 引号风格。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with _locked(path, "a"):  # 先拿目标文件锁，避免并发 read_space 看到半成品
        with open(tmp, "w") as out:
            _yaml.dump(data, out)
            out.flush()
        tmp.replace(path)


def _append_audit(
    audit_log: Path,
    item_path: str,
    old_value: Any,
    new_value: Any,
    expected_direction: str,
    reason: str,
) -> None:
    audit_log.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": _utcnow_iso(),
        "item_path": item_path,
        "old_value": old_value,
        "new_value": new_value,
        "expected_direction": expected_direction,
        "reason": reason,
    }
    with _locked(audit_log, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()


# --------------------------------------------------------------------------- #
# 公开 API
# --------------------------------------------------------------------------- #


def read_space(path: Path | str = DEFAULT_SPACE_FILE) -> dict:
    """加载并校验 yaml；schema 不合规直接 raise SchemaError。

    返回值类型为 ruamel ``CommentedMap``（子类化 dict），与 ``dict`` 完全兼容；
    上面挂着原文件的注释 / 空行，以便 :func:`_atomic_dump` 写回时复原。
    """
    p = Path(path)
    if not p.exists():
        raise SchemaError(f"tunable_space.yaml 不存在: {p}")
    with _locked(p, "r") as f:
        raw = _yaml.load(f)
    return _validate_schema(raw)


def update_value(
    item_path: str,
    new_value: float | int,
    expected_direction: str,
    reason: str,
    path: Path | str = DEFAULT_SPACE_FILE,
    audit_log: Path | str = DEFAULT_AUDIT_LOG,
) -> dict:
    """更新一个条目的 ``current`` 值。

    硬约束 — 任何一条违反就 raise，**不写盘**：
        1. ``item_path`` 形如 ``'parameters.w_redeem_progress'``，且 section 可写。
        2. ``new_value`` 必须落在该条目 ``range`` 闭区间内。
        3. ``expected_direction`` 必须为非空字符串。
        4. ``reason`` 必须为非空字符串。

    成功写入后追加一行进 ``logs/editor_writes.jsonl``。
    """
    if not isinstance(expected_direction, str) or not expected_direction.strip():
        raise MissingRationaleError("expected_direction 必须是非空 str")
    if not isinstance(reason, str) or not reason.strip():
        raise MissingRationaleError("reason 必须是非空 str")

    section, name = _split_path(item_path)
    data = read_space(path)
    item = _find_item(data, section, name)

    lo, hi = item["range"]
    if not (lo <= new_value <= hi):
        raise OutOfRangeError(
            f"{item_path}={new_value} 越界，允许 [{lo}, {hi}]"
        )

    old_value = item.get("current")
    item["current"] = new_value
    data["last_updated"] = _utcnow_iso()

    _atomic_dump(Path(path), data)
    _append_audit(
        Path(audit_log),
        item_path=item_path,
        old_value=old_value,
        new_value=new_value,
        expected_direction=expected_direction.strip(),
        reason=reason.strip(),
    )
    return {"item_path": item_path, "old_value": old_value, "new_value": new_value}


def add_entry(*args: Any, **kwargs: Any) -> None:
    """加新条目永远走人工 PR。"""
    raise NotImplementedError(
        "加新条目必须人工 PR，机器无权 — 见 docs/plans/2026-05-07-self-loop-roadmap.md"
    )


def list_writable(path: Path | str = DEFAULT_SPACE_FILE) -> list[dict]:
    """返回所有绿线条目（机器可改的 .current 列表）。"""
    data = read_space(path)
    out: list[dict] = []
    for section in _WRITABLE_SECTIONS:
        for item in data[section]:
            out.append(
                {
                    "item_path": f"{section}.{item['name']}",
                    "current": item["current"],
                    "range": list(item["range"]),
                }
            )
    return out
