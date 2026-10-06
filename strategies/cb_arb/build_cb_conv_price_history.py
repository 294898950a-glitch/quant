#!/usr/bin/env python3
"""从集思录原始抓取结果整理出一债一事件一行的下修历史权威表。

对应 spec: docs/2026-08-31-cb-conv-price-history-spec.txt。只做整理,
不做新的抓取, 不做新的校验规则——那些已经在
`strategies/cb_arb/fetch_jisilu_adj_logs.py` / `clients/jisilu_client.py`
里做过(11+2 轮 codex review)。

原始输入是 hkvm 上的 append-only jsonl(存续债每天重复抓, 同一事件会
出现多条记录), 本脚本读一份本地同步过来的快照, 按 (bond_id, meeting_date)
去重, `fetched_at` 最新的赢。

**历史 schema 不一致**(spec 第 5 节): 这份文件跨越了本次开发过程中的多次
schema 修订, 早期(2026-08-30 第一批)记录没有 `outcome` 字段(只有
`approved: true/false`), 也没有 `fetched_at` 字段。已摘牌的债一旦成功就
不会再被重新抓取, 这批老格式记录会永久留在源文件里——本脚本必须显式
做兼容转换, 不能假设所有记录都是最新 schema。
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: 唯一权威定义, 不在别处重复(呼应 AGENTS.md "状态码/枚举一律进权威表")。
OUTCOME_VALUES = frozenset({
    "approved", "rejected", "cancelled", "no_adjustment_needed",
    "both_delisted", "pending",
})

DEFAULT_RAW_INPUT = Path("data/cb_warehouse/_raw_cb_conv_price_adj_jisilu.jsonl")
DEFAULT_OUTPUT = Path("data/cb_warehouse/cb_conv_price_history.parquet")
DEFAULT_UNRESOLVED_OUTPUT = Path("data/cb_warehouse/cb_conv_price_history_unresolved.json")
DEFAULT_META_OUTPUT = Path("data/cb_warehouse/cb_conv_price_history.meta.json")

#: 去重规则版本号——去重逻辑本身改了(不是加字段这种), 才需要升这个号,
#: 好让下游知道"这张表是用哪一版规则生成的"。
DEDUPE_RULE_VERSION = 1


def _normalize_event(bond_id: str, fetched_at: "str | None", raw_event: dict[str, Any]) -> dict[str, Any]:
    """把一条原始 jisilu 事件记录整理成本表的行形状, 处理老 schema 兼容。

    `fetched_at` 是**顶层**抓取记录的字段(`{"bond_id":..., "status":"ok",
    "records":[...], "fetched_at":...}`), 不在展开后的单条事件
    (`records[]` 里的元素)上——codex review 指出旧版本从 `raw_event` 里找
    这个字段, 而 `records[]` 里的元素从来就不带这个 key, 导致
    `dedupe_latest` 实际上永远在比较两个空字符串, 只是碰巧文件是按抓取
    顺序追加、后出现的记录本来就该赢, 才没在现有数据上被看穿——如果哪次
    快照被合并/补录旧批次导致顺序变化, 会静默拿旧值盖掉新值。所以这个
    参数必须由调用方(`load_and_normalize`)从顶层记录里取出来往下传。

    `outcome` 缺失(老格式)时的推断(spec 第 6 节, 有实测代码历史依据,
    不是猜测): 早期版本的 `jisilu_client.py` 只认识"股东大会未通过"一种
    否定结局, 所以老记录里 `approved == False` 就等价于
    `outcome == "rejected"`。

    **"key 不存在"和"key 存在但值是 None"必须分开处理**(codex review
    指出): 前者是真的老格式, 后者是异常的新格式(比如未来某个 bug 让
    `outcome` 被意外写成 null)——静默按老格式推断会把这类真正的数据
    问题也掩盖掉, 所以后者直接报错, 不猜。同理, 合法枚举值之外的
    `outcome`(拼写错误)也直接报错, 不能悄悄进权威表。
    """
    if "outcome" in raw_event:
        outcome = raw_event["outcome"]
        if outcome not in OUTCOME_VALUES:
            raise ValueError(
                f"bond_id={bond_id}: outcome={outcome!r} 不在权威枚举 "
                f"OUTCOME_VALUES 里(可能是 null 或拼写错误), 不能静默当成"
                f"老格式推断。raw_event={raw_event!r}"
            )
    else:
        approved_raw = raw_event.get("approved")
        if not isinstance(approved_raw, bool):
            raise ValueError(
                f"bond_id={bond_id}: 老格式记录(没有 outcome 字段), 但 "
                f"approved 也不是合法的 bool({approved_raw!r}), 无法推断 "
                f"outcome。raw_event={raw_event!r}"
            )
        outcome = "approved" if approved_raw else "rejected"
    return {
        "bond_id": bond_id,
        "bond_name": raw_event.get("bond_name"),
        "meeting_date": raw_event.get("meeting_date"),
        "outcome": outcome,
        "approved": outcome == "approved",
        "old_conv_price": raw_event.get("old_conv_price"),
        "new_conv_price": raw_event.get("new_conv_price"),
        "effective_date": raw_event.get("effective_date"),
        "floor_price": raw_event.get("floor_price"),
        "fetched_at": fetched_at or "",
    }


def load_and_normalize(raw_path: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """读原始 jsonl, 返回 (展开+兼容转换后的事件行列表, 从未成功过的
    bond_id -> 最后一次尝试的状态信息)。

    只看每个 bond_id 在整份文件里**是否曾经出现过 `status == "ok"`**——
    一只债即使中途失败过几次, 只要最终成功过一次, 就不算"未解决"。
    """
    events: list[dict[str, Any]] = []
    ever_ok: set[str] = set()
    last_attempt: dict[str, dict[str, Any]] = {}

    with raw_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            bond_id = rec.get("bond_id")
            if not bond_id:
                continue
            status = rec.get("status")
            if status == "ok":
                ever_ok.add(bond_id)
                fetched_at = rec.get("fetched_at")
                for raw_event in rec.get("records") or []:
                    events.append(_normalize_event(bond_id, fetched_at, raw_event))
            elif status != "invalid_bond_id":
                # invalid_bond_id 是我们自己的候选列表脏了, 不是"抓不到
                # 历史", 不进未解决名单——那是数据源头的问题, 不是这张表
                # 要追踪的缺口。
                last_attempt[bond_id] = {"status": status, "error": rec.get("error")}

    unresolved = {
        bond_id: info for bond_id, info in last_attempt.items() if bond_id not in ever_ok
    }
    return events, unresolved


def dedupe_latest(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 (bond_id, meeting_date) 去重, `fetched_at` 最新的赢。

    `fetched_at` 是 ISO 格式字符串, 字典序比较等价于时间先后——缺失的
    (老格式记录)统一当空字符串, 永远排在有真实时间戳的记录之后
    (`_normalize_event` 已经把缺失值转成 `""`)。
    """
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        key = (event["bond_id"], event["meeting_date"])
        prev = latest.get(key)
        if prev is None or event["fetched_at"] >= prev["fetched_at"]:
            latest[key] = event
    return list(latest.values())


#: spec 第 3 节定义的表结构, 唯一权威列表——`fetched_at` 是表的正式一列
#: (不只是内部去重用的辅助字段), 之前漏掉过一次(codex review 指出)。
_OUTPUT_DTYPES = {
    "bond_id": "object", "bond_name": "object", "meeting_date": "object",
    "outcome": "object", "approved": "bool",
    "old_conv_price": "float64", "new_conv_price": "float64",
    "effective_date": "object", "floor_price": "float64",
    "fetched_at": "object",
}


def _to_dataframe(deduped: list[dict[str, Any]]):
    import pandas as pd

    if not deduped:
        # 空结果也要有正确的列和类型, 不能让 pandas 从空列表猜出一份
        # 全 object dtype 的空表——那不是这张表真实的 schema
        # (codex review 指出)。
        return pd.DataFrame({c: pd.Series(dtype=t) for c, t in _OUTPUT_DTYPES.items()})
    # 非空路径之前只指定了列顺序, 没有真正按 _OUTPUT_DTYPES 转类型——如果
    # 某条记录的价格字段碰巧全是 Python int(合法的 JSON 数值, 不算脏数据),
    # pandas 会推断出 int64 列, 悄悄跟"价格字段一律 float64"这条权威 schema
    # 不一致(codex review 第二轮复现)。显式 `.astype()` 一次, 不依赖推断。
    return pd.DataFrame(deduped, columns=list(_OUTPUT_DTYPES)).astype(_OUTPUT_DTYPES)


def build(raw_path: Path, output_path: Path, unresolved_path: Path,
          meta_path: Path) -> dict[str, int]:
    events, unresolved = load_and_normalize(raw_path)
    deduped = dedupe_latest(events)

    df = _to_dataframe(deduped)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(output_path, index=False)

    unresolved_path.write_text(
        json.dumps(unresolved, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8",
    )

    meta = {
        "source_path": str(raw_path),
        "source_line_count": sum(1 for _ in raw_path.open("r", encoding="utf-8")),
        "source_mtime": datetime.fromtimestamp(
            raw_path.stat().st_mtime, tz=timezone.utc,
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dedupe_rule_version": DEDUPE_RULE_VERSION,
        "row_count": len(df),
        "unresolved_count": len(unresolved),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"raw_events": len(events), "deduped_rows": len(df), "unresolved": len(unresolved)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-input", type=Path, default=DEFAULT_RAW_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--unresolved-output", type=Path, default=DEFAULT_UNRESOLVED_OUTPUT)
    parser.add_argument("--meta-output", type=Path, default=DEFAULT_META_OUTPUT)
    args = parser.parse_args()

    summary = build(args.raw_input, args.output, args.unresolved_output, args.meta_output)
    print(
        "原始事件 {raw_events} 条, 去重后 {deduped_rows} 行, 未解决 {unresolved} 只"
        .format(**summary),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
