#!/usr/bin/env python3
"""批量抓取集思录(jisilu.cn)转股价调整历史 —— 一债一请求, 全程走
`clients.fetch_throttle.Gate` 节流 + 熔断, 不直接裸发 HTTP。

对应 spec: docs/specs/2026-08-30-jisilu-adj-logs-client-spec.md (client 库部分)
§3.3(本脚本对应该节的 quant 侧部分)。

**上线前置条件**(用户明确要求, 不可跳过): 本脚本 + `clients/jisilu_client.py`
必须先过 codex review, ACCEPT 之后才允许对 jisilu.cn 发真实批量请求。

运行位置: 本脚本源码归属 quant git 仓库, 但反爬抓取类工具按项目规矩(用户级
CLAUDE.md)要部在家庭/办公 IP 的机器, 不用数据中心 VM —— 跟
fetch_cb_announcements.py / extract_conv_price_revisions.py 的先例一致,
实际执行在 hkvm。部署方式: 把本文件 scp 到 hkvm 的
~/projects/cb_announcement_fetch/, `clients/jisilu_client.py` +
`clients/fetch_errors.py` 的更新 scp 到 hkvm 的 ~/.hermes/scripts/clients/,
md5 校验一致后再跑。

节流参数(唯一权威定义, 就在这里, 不在别处重复): 集思录目前没有任何已知
节流基线, 保守起步(2.0~3.5 秒一次抖动间隔, 每日上限 600 次), 以后有真实
运行数据再调松, 不能反过来先猛抓再看会不会被封。

"什么算失败"由本脚本决定(`fetch_throttle.py` 自己不猜): 网络层失败和
`jisilu_unexpected_response`(疑似反爬拦截页/页面结构变了)计入熔断;
`invalid_bond_id` 在发请求之前就地拦掉, 不算网站失败, 不占闸的名额;
"暂无数据"(查无历史)是正常答案, 记成功, 不能让一批冷门代码把闸打下来。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

_CLIENT_SRC = Path.home() / "projects" / "client"
if _CLIENT_SRC.is_dir():
    sys.path.insert(0, str(_CLIENT_SRC))
else:
    # hkvm 部署环境没有 ~/projects/client 这份源码仓库, 只有下面这份
    # scp 过去的扁平拷贝(参照 extract_conv_price_revisions.py 的先例)。
    sys.path.insert(0, str(Path.home() / ".hermes" / "scripts"))

from clients import fetch_throttle, jisilu_client  # noqa: E402

_BOND_ID_RE = re.compile(r"\d{6}")

GATE_PROVIDER = "jisilu"
GATE_BUCKETS = {"adj_logs": {"min_gap": 2.0, "jitter": 1.5, "daily_cap": 600}}
GATE_STATE_PATH = Path.home() / ".hermes" / "state" / "jisilu_throttle.json"

DEFAULT_CB_BASIC = Path("data/cb_warehouse/cb_basic.parquet")
DEFAULT_OUTPUT = Path("data/cb_warehouse/cb_conv_price_adj_jisilu.jsonl")

#: 到达这些状态就不再重试(续跑时跳过)。跟 extract_conv_price_revisions.py
#: 的续跑规则一致: 只有"网站给了确定答案"或"我们自己的输入本身就是错的"
#: 才算终态, 网络失败/疑似反爬一律留到下次重跑。
TERMINAL_STATUSES = {"ok", "invalid_bond_id"}


def build_gate() -> fetch_throttle.Gate:
    return fetch_throttle.Gate(
        provider=GATE_PROVIDER,
        buckets=GATE_BUCKETS,
        state_path=GATE_STATE_PATH,
    )


def load_bond_ids(cb_basic_path: Path) -> list[str]:
    """从 cb_basic.parquet 的 `code` 列取候选债券代码, 去重、保序。

    这里只管去重, **不**校验格式——格式校验只在 `fetch_one` 里做一次
    (`_BOND_ID_RE`), 全流程只有那一处认识"什么算合法 bond_id"。格式不对的
    代码会原样传下去, 由 `fetch_one` 判成 `invalid_bond_id` 终态写进输出,
    让下游看得到"有多少候选连格式都不对"这个数, 而不是在这里悄悄滤掉、
    连痕迹都不留。
    """
    import pandas as pd

    df = pd.read_parquet(cb_basic_path)
    seen: set[str] = set()
    out: list[str] = []
    for raw in df["code"]:
        code = str(raw).strip()
        if code and code not in seen:
            seen.add(code)
            out.append(code)
    return out


def load_done(output_path: Path) -> set[str]:
    done: set[str] = set()
    if not output_path.exists():
        return done
    with output_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("status") in TERMINAL_STATUSES:
                bond_id = rec.get("bond_id")
                if bond_id:
                    done.add(bond_id)
    return done


def append_record(output_path: Path, rec: dict[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def fetch_one(gate: fetch_throttle.Gate, bond_id: str) -> dict[str, Any]:
    """抓一只债的调整历史。返回值就是要写进输出文件的那一条记录。"""
    if not _BOND_ID_RE.fullmatch(bond_id):
        return {"bond_id": bond_id, "status": "invalid_bond_id", "error": "invalid_bond_id"}

    slot, gate_err = gate.reserve("adj_logs", bond_id=bond_id)
    if gate_err is not None:
        return {"bond_id": bond_id, "status": "throttled", "error": gate_err}

    gate.wait_for(slot)
    records, err = jisilu_client.fetch_adj_logs(bond_id)

    if err is not None:
        gate.record("adj_logs", ok=False)
        return {"bond_id": bond_id, "status": err.get("error", "unknown_error"), "error": err}

    gate.record("adj_logs", ok=True)
    return {"bond_id": bond_id, "status": "ok", "records": records}


def run(cb_basic_path: Path, output_path: Path, limit: "int | None" = None,
        gate: "fetch_throttle.Gate | None" = None) -> dict[str, int]:
    """跑一轮。返回汇总计数, 供 main() 打印、也供测试直接断言。

    `limit` 不传 = 处理全部待抓候选。传了就必须是正整数——`list[:limit]`
    在 Python 里对负数是合法的"掐掉末尾几个"切片, `limit=-1` 会变成
    "几乎全部候选都抓", 跟"只抓前 N 个调试"的本意正好相反
    (codex review 2026-08-30 用真实 3 条候选复现过这个反例)。
    """
    if limit is not None and limit < 1:
        raise ValueError(f"--limit 必须是正整数(不传 = 全量待抓候选), 收到: {limit}")

    all_ids = load_bond_ids(cb_basic_path)
    done = load_done(output_path)
    pending = [b for b in all_ids if b not in done]
    if limit is not None:
        pending = pending[:limit]

    gate = gate or build_gate()
    n_ok = n_fail = n_invalid = 0
    stopped_early = False
    for i, bond_id in enumerate(pending, 1):
        rec = fetch_one(gate, bond_id)
        status = rec["status"]
        if status == "throttled":
            reason = rec["error"].get("reason", "?")
            print(
                f"[{i}/{len(pending)}] {bond_id}: 被闸拦下({reason}), 停止本次运行, "
                f"剩余 {len(pending) - i + 1} 只留到下次",
                flush=True,
            )
            stopped_early = True
            break

        append_record(output_path, rec)
        if status == "ok":
            n_ok += 1
            n_records = len(rec.get("records") or [])
            print(f"[{i}/{len(pending)}] {bond_id}: ok, {n_records} 条记录", flush=True)
        elif status == "invalid_bond_id":
            n_invalid += 1
            print(f"[{i}/{len(pending)}] {bond_id}: invalid_bond_id(cb_basic 里代码格式有问题)",
                  flush=True)
        else:
            n_fail += 1
            print(f"[{i}/{len(pending)}] {bond_id}: {status}", flush=True)

    return {
        "candidates": len(all_ids),
        "already_done": len(done),
        "attempted": n_ok + n_fail + n_invalid,
        "ok": n_ok,
        "fail_retryable": n_fail,
        "invalid_bond_id": n_invalid,
        "stopped_early": stopped_early,
    }


def _positive_int(raw: str) -> int:
    n = int(raw)
    if n < 1:
        raise argparse.ArgumentTypeError(f"--limit 必须是正整数, 收到: {raw}")
    return n


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cb-basic", type=Path, default=DEFAULT_CB_BASIC)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--limit", type=_positive_int, default=None,
        help="只处理前 N 个待抓代码(调试用); 不传 = 处理全部待抓代码, 不悄悄限量",
    )
    args = parser.parse_args()

    print(
        f"候选来源: {args.cb_basic}, 输出: {args.output}"
        + (f", limit={args.limit}" if args.limit is not None else ""),
        flush=True,
    )
    summary = run(args.cb_basic, args.output, limit=args.limit)
    print(
        "完成: 候选 {candidates}, 之前已完成 {already_done}, 本次尝试 {attempted}, "
        "成功 {ok}, 失败(可重试) {fail_retryable}, 代码格式无效 {invalid_bond_id}"
        .format(**summary),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
