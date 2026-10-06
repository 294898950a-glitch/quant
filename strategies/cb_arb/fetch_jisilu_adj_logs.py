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

节流参数(唯一权威定义, 就在这里, 不在别处重复): 2026-08-30 起步时集思录
没有任何已知节流基线, 保守起步(2.0~3.5 秒一次抖动间隔, 每日上限 600 次,
平均约 21.8 请求/分钟)。2026-08-31 用起步参数真实跑完 600 次请求, 全程
零疑似反爬信号(11 次失败逐条人工核对原始 HTML, 全是数据本身的边界情况,
不是网站的反应)——按"先保守起步、有真实数据再调松"的规矩调松成
1.5~2.5 秒一次抖动间隔、每日上限 1200 次(codex review 指出: 间隔和上限
同时调会叠加成持续请求速率的放大, 不是简单的"间隔减半"; 原方案
1.0~2.0 秒 + 1200/天相当于把速率一次性拉高约 83%, 现在这组数字只拉高
约 37.5%, 是分阶段校准, 不是一步到位)。跑满一整轮 1200 次仍然干净的话,
再考虑要不要往 1.0~2.0 秒再松一档。

"什么算失败"由本脚本决定(`fetch_throttle.py` 自己不猜): 网络层失败和
`jisilu_unexpected_response`(疑似反爬拦截页/页面结构变了)计入熔断;
`invalid_bond_id` 在发请求之前就地拦掉, 不算网站失败, 不占闸的名额;
"暂无数据"(查无历史)是正常答案, 记成功, 不能让一批冷门代码把闸打下来。

**这不是一次性回填, 是要每天定时跑的**(2026-08-31 起, 见
data/research_framework 或 cron-registry 的对应条目): 已经到期摘牌的债
成功抓过一次就永久跳过(历史不会再变); 还在存续期的债即使上次结果是
"暂无历史", 也会被重新问一遍——不然真实发生的新一轮下修会被永久漏掉,
且不会有任何报警。所以输出文件 `cb_conv_price_adj_jisilu.jsonl` 里同一个
`bond_id` 会随时间积累多条记录, **下游必须按 `fetched_at` 取最新的那条**,
不能假设一个 bond_id 只有一行。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timezone
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
GATE_BUCKETS = {"adj_logs": {"min_gap": 1.5, "jitter": 1.0, "daily_cap": 1200}}
GATE_STATE_PATH = Path.home() / ".hermes" / "state" / "jisilu_throttle.json"

DEFAULT_CB_BASIC = Path("data/cb_warehouse/cb_basic.parquet")
DEFAULT_OUTPUT = Path("data/cb_warehouse/cb_conv_price_adj_jisilu.jsonl")

#: 无条件终态: 不管这只债是不是还在存续期, 都不再重试。`invalid_bond_id`
#: 是我们自己的输入格式就不对, 重试没用; 跟"这只债有没有到期"无关。
ALWAYS_TERMINAL_STATUSES = {"invalid_bond_id"}

#: 只对"已到期摘牌"的债才算终态的状态。一只已经摘牌的债, 转股价调整只可能
#: 发生在存续期内, 摘牌之后历史不会再变, `ok` 抓到过一次就永久够用。
#: 但如果这只债还在存续期(下面 `load_matured_bond_ids` 判定), 同样是
#: `ok` 也**不能**当成终态——本轮"暂无历史"不代表以后不会有新的下修事件,
#: 定时任务要靠"这只债每次都还在 pending 里"才能发现新事件, 一旦被判成
#: 永久终态, 未来真实发生的下修会被悄悄漏掉, 而且没有任何报警
#: ("答不出来时写什么"——这里答案是"这只债还活着, 永远别假装问完了")。
MATURED_ONLY_TERMINAL_STATUSES = {"ok"}


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


_DELIST_DATE_RE = re.compile(r"\d{8}")


def load_matured_bond_ids(cb_basic_path: Path, *, as_of: "date | None" = None) -> set[str]:
    """`cb_basic.parquet` 的 `delist_date` 列, 返回到 `as_of`(默认今天)为止
    已经到期摘牌的债券代码集合。`delist_date` 是 "YYYYMMDD" 字符串, 仍在
    存续期的债这一列是 null。

    "判成已摘牌"是一张单程票——一旦误判, `load_done` 会把这只债永久跳过,
    未来真实发生的下修会被永久漏掉、且不会有任何报警(codex review 指出:
    只做 `int()` 不校验格式, "2027011"(少一位)这类脏值会被解析成一个
    恰好小于今天的整数, 静默把仍存续的债判成摘牌)。所以这里必须严格校验
    "确实是 8 位数字, 且是一个真实存在的日历日期", 解析失败一律当"还没
    摘牌"处理(fail open 只会导致多抓一次, 不会导致漏抓——方向必须是这边,
    不能反过来)。
    """
    import pandas as pd
    from datetime import datetime as _datetime

    as_of = as_of or date.today()
    df = pd.read_parquet(cb_basic_path)
    matured: set[str] = set()
    for code_raw, delist_raw in zip(df["code"], df["delist_date"]):
        code = str(code_raw).strip()
        if not code or pd.isna(delist_raw):
            continue
        delist_str = str(delist_raw).strip()
        if not _DELIST_DATE_RE.fullmatch(delist_str):
            continue  # 格式不是严格的 8 位数字(比如浮点尾巴 "20200101.0"), 当存续处理
        try:
            delist_date = _datetime.strptime(delist_str, "%Y%m%d").date()
        except ValueError:
            continue  # 8 位数字但不是真实日历日期(比如 13 月), 当存续处理
        if delist_date <= as_of:
            matured.add(code)
    return matured


def load_done(output_path: Path, matured_ids: "set[str] | None" = None) -> set[str]:
    """哪些 bond_id 这次可以跳过不抓。

    `invalid_bond_id` 永久跳过(不管存续状态)。`ok` 只有在这只债已经
    到期摘牌(`matured_ids`)时才跳过——还在存续期的债即使上次结果是
    "暂无历史", 这次也要重新问一遍(见 `MATURED_ONLY_TERMINAL_STATUSES`
    的注释)。`matured_ids` 不传等价于"没有任何债算摘牌", 即所有 `ok`
    都不跳过, 全部重新抓。
    """
    matured_ids = matured_ids or set()
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
            status = rec.get("status")
            bond_id = rec.get("bond_id")
            if not bond_id:
                continue
            if status in ALWAYS_TERMINAL_STATUSES:
                done.add(bond_id)
            elif status in MATURED_ONLY_TERMINAL_STATUSES and bond_id in matured_ids:
                done.add(bond_id)
    return done


def load_ever_succeeded(output_path: Path) -> set[str]:
    """哪些 bond_id 曾经至少成功过一次(`status == "ok"`), 不管现在算不算
    终态。只用来给续跑排优先级(见 `run()`): 从没成功过的候选(没试过或者
    一直失败)优先处理, 已经成功过、只是等着按存续期规则每天刷新的债往后
    排——不然每天都要重刷一遍存续期内的债, 会跟"把全量候选第一次跑完"
    抢当日预算, 拖慢首次回填。
    """
    seen: set[str] = set()
    if not output_path.exists():
        return seen
    with output_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("status") == "ok":
                bond_id = rec.get("bond_id")
                if bond_id:
                    seen.add(bond_id)
    return seen


def append_record(output_path: Path, rec: dict[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_one(gate: fetch_throttle.Gate, bond_id: str) -> dict[str, Any]:
    """抓一只债的调整历史。返回值就是要写进输出文件的那一条记录。

    `fetched_at` 是必须字段, 不是可有可无的元数据——存续期内的债每天都会
    被重新抓一遍(见 `MATURED_ONLY_TERMINAL_STATUSES`), 输出文件里同一个
    `bond_id` 会积累多条记录, 下游必须靠这个字段才能判断"哪一条是最新的",
    不能只靠文件里出现的先后顺序去猜。
    """
    if not _BOND_ID_RE.fullmatch(bond_id):
        return {
            "bond_id": bond_id, "status": "invalid_bond_id", "error": "invalid_bond_id",
            "fetched_at": _now_iso(),
        }

    slot, gate_err = gate.reserve("adj_logs", bond_id=bond_id)
    if gate_err is not None:
        return {"bond_id": bond_id, "status": "throttled", "error": gate_err, "fetched_at": _now_iso()}

    gate.wait_for(slot)
    records, err = jisilu_client.fetch_adj_logs(bond_id)

    if err is not None:
        gate.record("adj_logs", ok=False)
        return {
            "bond_id": bond_id, "status": err.get("error", "unknown_error"), "error": err,
            "fetched_at": _now_iso(),
        }

    gate.record("adj_logs", ok=True)
    return {"bond_id": bond_id, "status": "ok", "records": records, "fetched_at": _now_iso()}


def run(cb_basic_path: Path, output_path: Path, limit: "int | None" = None,
        gate: "fetch_throttle.Gate | None" = None,
        as_of: "date | None" = None) -> dict[str, int]:
    """跑一轮。返回汇总计数, 供 main() 打印、也供测试直接断言。

    `limit` 不传 = 处理全部待抓候选。传了就必须是正整数——`list[:limit]`
    在 Python 里对负数是合法的"掐掉末尾几个"切片, `limit=-1` 会变成
    "几乎全部候选都抓", 跟"只抓前 N 个调试"的本意正好相反
    (codex review 2026-08-30 用真实 3 条候选复现过这个反例)。

    候选排序: 从没成功过的(没试过/一直失败)排在前面, 已经成功过、只是
    按存续期规则等着刷新的排在后面——见 `load_ever_succeeded`。
    """
    if limit is not None and limit < 1:
        raise ValueError(f"--limit 必须是正整数(不传 = 全量待抓候选), 收到: {limit}")

    all_ids = load_bond_ids(cb_basic_path)
    matured_ids = load_matured_bond_ids(cb_basic_path, as_of=as_of)
    done = load_done(output_path, matured_ids)
    ever_succeeded = load_ever_succeeded(output_path)
    candidates = [b for b in all_ids if b not in done]
    pending = (
        [b for b in candidates if b not in ever_succeeded]
        + [b for b in candidates if b in ever_succeeded]
    )
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
