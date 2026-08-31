"""单元测试: fetch_jisilu_adj_logs.

覆盖:
1. invalid_bond_id 在 reserve() 之前就地拦掉, 不占闸的名额
2. 成功(含"暂无数据"空列表)记 ok=True, 写终态记录
3. jisilu_unexpected_response / 网络错误记 ok=False, 状态非终态
4. 续跑: 终态(ok/invalid_bond_id)跳过, 非终态(throttled 除外, 见下)重试
5. 被闸拦下(day_cap/breaker_open)立刻停止本轮, 不把剩余候选当成"尝试过"写进输出
6. --limit 只影响处理条数, 不隐式生效(不传 = 全量)
"""
from __future__ import annotations

import json
import sys

import pytest

from strategies.cb_arb.fetch_jisilu_adj_logs import (
    ALWAYS_TERMINAL_STATUSES,
    append_record,
    fetch_one,
    load_bond_ids,
    load_done,
    load_ever_succeeded,
    load_matured_bond_ids,
    run,
)


class _FakeGate:
    """不落盘、不睡觉的假闸, 记录每次 reserve/record 调用方便断言。"""

    def __init__(self, refuse_after: "int | None" = None):
        self.reserve_calls: list[str] = []
        self.record_calls: list[tuple[str, bool]] = []
        self._refuse_after = refuse_after
        self._n = 0

    def reserve(self, bucket: str, **extra):
        self.reserve_calls.append(extra.get("bond_id", ""))
        self._n += 1
        if self._refuse_after is not None and self._n > self._refuse_after:
            return 0.0, {
                "provider": "jisilu", "gate": bucket,
                "error": "throttled", "reason": "day_cap",
                "raw_error": "今日已发上限次",
            }
        return 0.0, None

    @staticmethod
    def wait_for(slot: float) -> None:
        return None

    def record(self, bucket: str, ok: bool) -> None:
        self.record_calls.append((bucket, ok))


# ----------------------------------------------------------------------
# fetch_one
# ----------------------------------------------------------------------

def test_invalid_bond_id_never_touches_gate():
    gate = _FakeGate()
    rec = fetch_one(gate, "12345")  # 5 位, 格式不对
    assert rec["status"] == "invalid_bond_id"
    assert gate.reserve_calls == []
    assert gate.record_calls == []


def test_success_with_records_is_ok_and_records_true(monkeypatch):
    import strategies.cb_arb.fetch_jisilu_adj_logs as mod

    monkeypatch.setattr(
        mod.jisilu_client, "fetch_adj_logs",
        lambda bond_id, **kw: ([{"bond_name": "测试转债"}], None),
    )
    gate = _FakeGate()
    rec = fetch_one(gate, "127021")
    assert rec["status"] == "ok"
    assert rec["records"] == [{"bond_name": "测试转债"}]
    assert gate.record_calls == [("adj_logs", True)]


def test_no_history_empty_list_is_still_ok_not_failure(monkeypatch):
    """"暂无数据" -> 空列表, 是正常答案, 不能记成失败(会被冷门代码打熔断)。"""
    import strategies.cb_arb.fetch_jisilu_adj_logs as mod

    monkeypatch.setattr(mod.jisilu_client, "fetch_adj_logs", lambda bond_id, **kw: ([], None))
    gate = _FakeGate()
    rec = fetch_one(gate, "110002")
    assert rec["status"] == "ok"
    assert rec["records"] == []
    assert gate.record_calls == [("adj_logs", True)]


def test_unexpected_response_is_failure_and_non_terminal(monkeypatch):
    import strategies.cb_arb.fetch_jisilu_adj_logs as mod

    monkeypatch.setattr(
        mod.jisilu_client, "fetch_adj_logs",
        lambda bond_id, **kw: (None, {"error": "jisilu_unexpected_response", "bond_id": bond_id}),
    )
    gate = _FakeGate()
    rec = fetch_one(gate, "127021")
    assert rec["status"] == "jisilu_unexpected_response"
    assert rec["status"] not in ALWAYS_TERMINAL_STATUSES
    assert gate.record_calls == [("adj_logs", False)]


def test_network_error_is_failure(monkeypatch):
    import strategies.cb_arb.fetch_jisilu_adj_logs as mod

    monkeypatch.setattr(
        mod.jisilu_client, "fetch_adj_logs",
        lambda bond_id, **kw: (None, {"error": "Timeout", "bond_id": bond_id}),
    )
    gate = _FakeGate()
    rec = fetch_one(gate, "127021")
    assert rec["status"] == "Timeout"
    assert gate.record_calls == [("adj_logs", False)]


def test_throttled_does_not_call_fetch_adj_logs(monkeypatch):
    import strategies.cb_arb.fetch_jisilu_adj_logs as mod

    called = {"n": 0}

    def fake_fetch(bond_id, **kw):
        called["n"] += 1
        return [], None

    monkeypatch.setattr(mod.jisilu_client, "fetch_adj_logs", fake_fetch)
    gate = _FakeGate(refuse_after=0)
    rec = fetch_one(gate, "127021")
    assert rec["status"] == "throttled"
    assert called["n"] == 0
    assert gate.record_calls == []  # 没发请求, 不计入 fail_streak


# ----------------------------------------------------------------------
# load_bond_ids / load_done / append_record
# ----------------------------------------------------------------------

def test_load_bond_ids_dedupes_and_preserves_order(tmp_path):
    import pandas as pd

    df = pd.DataFrame({"code": ["110002", "110003", "110002", "110004"]})
    p = tmp_path / "cb_basic.parquet"
    df.to_parquet(p)
    assert load_bond_ids(p) == ["110002", "110003", "110004"]


def test_load_matured_bond_ids_uses_delist_date(tmp_path):
    import datetime as dt

    import pandas as pd

    df = pd.DataFrame({
        "code": ["110002", "110003", "110004", "110005"],
        "delist_date": ["20200101", None, "20260102", "20260101"],  # 分别: 过去/存续/未来/正好当天
    })
    p = tmp_path / "cb_basic.parquet"
    df.to_parquet(p)
    matured = load_matured_bond_ids(p, as_of=dt.date(2026, 1, 1))
    # 110002 早已摘牌; 110003 没有 delist_date, 存续; 110004 明天才摘牌,
    # 现在还不算; 110005 就是今天摘牌, 边界上算已经摘牌(<=)。
    assert matured == {"110002", "110005"}


def test_load_matured_bond_ids_rejects_malformed_dates_as_not_matured(tmp_path):
    """"判成已摘牌"是单程票——误判会导致 `load_done` 永久跳过一只其实
    还存续的债, 未来真实发生的下修会被永久漏掉且没有报警。所以格式不对
    的 `delist_date` 必须当"还没摘牌"处理, 不能因为凑巧转成的整数比今天
    小就放行(codex review 复现: "2027011" 少一位, `int()` 直接转成
    2027011, 恰好比 20260831 还小, 静默判成已摘牌)。
    """
    import datetime as dt

    import pandas as pd

    df = pd.DataFrame({
        "code": ["110002", "110003", "110004", "110005"],
        "delist_date": [
            "2020011",       # 少一位(codex review 原始反例)
            "20201301",      # 8 位数字但月份不存在
            "20200101.0",    # pandas/parquet 里数值列常见的浮点尾巴
            "20200101",      # 对照组: 格式正确, 真的已摘牌
        ],
    })
    p = tmp_path / "cb_basic.parquet"
    df.to_parquet(p)
    matured = load_matured_bond_ids(p, as_of=dt.date(2026, 8, 31))
    assert matured == {"110005"}


def test_load_ever_succeeded_only_counts_ok_status(tmp_path):
    p = tmp_path / "out.jsonl"
    rows = [
        {"bond_id": "110002", "status": "ok"},
        {"bond_id": "110003", "status": "Timeout"},
        {"bond_id": "110002", "status": "jisilu_unexpected_response"},  # 后来又失败过一次, 不影响之前的成功
    ]
    with p.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    assert load_ever_succeeded(p) == {"110002"}


def test_load_done_invalid_bond_id_always_terminal_regardless_of_maturity(tmp_path):
    p = tmp_path / "out.jsonl"
    rows = [
        {"bond_id": "110002", "status": "ok"},
        {"bond_id": "110003", "status": "invalid_bond_id"},
        {"bond_id": "110004", "status": "jisilu_unexpected_response"},
        {"bond_id": "110005", "status": "Timeout"},
    ]
    with p.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    # 不传 matured_ids(等价于"没有任何债算摘牌") —— ok 不该被跳过,
    # 只有 invalid_bond_id 永远终态。
    assert load_done(p) == {"110003"}


def test_load_done_ok_status_only_terminal_when_matured(tmp_path):
    """还在存续期的债即使上次结果是 ok, 也不该被当成永久终态——不然
    真实发生的新一轮下修会被永久漏掉、且不会有任何报警。已经到期摘牌的
    债, ok 才是真正的终态(历史不会再变)。
    """
    p = tmp_path / "out.jsonl"
    rows = [
        {"bond_id": "110002", "status": "ok"},  # 摘牌
        {"bond_id": "110003", "status": "ok"},  # 存续
    ]
    with p.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    done = load_done(p, matured_ids={"110002"})
    assert done == {"110002"}


def test_load_done_missing_bond_id_field_is_skipped(tmp_path):
    p = tmp_path / "out.jsonl"
    with p.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"status": "ok"}) + "\n")
    assert load_done(p) == set()


def test_load_done_missing_file_returns_empty(tmp_path):
    assert load_done(tmp_path / "does_not_exist.jsonl") == set()


def test_append_record_creates_parent_dir(tmp_path):
    p = tmp_path / "nested" / "out.jsonl"
    append_record(p, {"bond_id": "110002", "status": "ok"})
    assert p.exists()
    assert json.loads(p.read_text().strip())["bond_id"] == "110002"


# ----------------------------------------------------------------------
# run() —— 续跑 + 限流停止 + limit 整合行为
# ----------------------------------------------------------------------

def test_run_skips_terminal_resumes_non_terminal(tmp_path, monkeypatch):
    import pandas as pd
    import strategies.cb_arb.fetch_jisilu_adj_logs as mod

    df = pd.DataFrame({
        "code": ["110002", "110003", "110004"],
        "delist_date": ["20200101", None, None],  # 110002 已摘牌, 其余存续
    })
    cb_basic = tmp_path / "cb_basic.parquet"
    df.to_parquet(cb_basic)

    output = tmp_path / "out.jsonl"
    with output.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"bond_id": "110002", "status": "ok", "records": []}) + "\n")
        f.write(json.dumps({"bond_id": "110003", "status": "Timeout", "error": {}}) + "\n")

    attempted = []

    def fake_fetch(bond_id, **kw):
        attempted.append(bond_id)
        return [], None

    monkeypatch.setattr(mod.jisilu_client, "fetch_adj_logs", fake_fetch)
    gate = _FakeGate()
    summary = run(cb_basic, output, gate=gate)

    # 110002 已摘牌且是终态(ok), 跳过; 110003(Timeout, 非终态)和
    # 110004(没试过)都要重试
    assert set(attempted) == {"110003", "110004"}
    assert summary["already_done"] == 1
    assert summary["ok"] == 2


def test_run_refetches_active_bond_even_after_prior_success(tmp_path, monkeypatch):
    """还在存续期的债, 即使上次结果已经是 ok, 这次也要重新问一遍——
    不然真实发生的新一轮下修会被永久漏掉, 且不会有任何报警。
    """
    import pandas as pd
    import strategies.cb_arb.fetch_jisilu_adj_logs as mod

    df = pd.DataFrame({"code": ["110002"], "delist_date": [None]})  # 存续
    cb_basic = tmp_path / "cb_basic.parquet"
    df.to_parquet(cb_basic)

    output = tmp_path / "out.jsonl"
    with output.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"bond_id": "110002", "status": "ok", "records": []}) + "\n")

    attempted = []
    monkeypatch.setattr(
        mod.jisilu_client, "fetch_adj_logs",
        lambda bond_id, **kw: (attempted.append(bond_id) or [], None),
    )
    gate = _FakeGate()
    summary = run(cb_basic, output, gate=gate)

    assert attempted == ["110002"]
    assert summary["already_done"] == 0
    assert summary["ok"] == 1


def test_run_never_refetches_matured_bond_after_success(tmp_path, monkeypatch):
    """已经到期摘牌的债, 历史不会再变, ok 一次就永久够用, 不该每天重抓。"""
    import pandas as pd
    import strategies.cb_arb.fetch_jisilu_adj_logs as mod

    df = pd.DataFrame({"code": ["110002"], "delist_date": ["20200101"]})  # 已摘牌
    cb_basic = tmp_path / "cb_basic.parquet"
    df.to_parquet(cb_basic)

    output = tmp_path / "out.jsonl"
    with output.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"bond_id": "110002", "status": "ok", "records": []}) + "\n")

    attempted = []
    monkeypatch.setattr(
        mod.jisilu_client, "fetch_adj_logs",
        lambda bond_id, **kw: (attempted.append(bond_id) or [], None),
    )
    gate = _FakeGate()
    summary = run(cb_basic, output, gate=gate)

    assert attempted == []
    assert summary["already_done"] == 1
    assert summary["attempted"] == 0


def test_run_prioritizes_never_succeeded_over_refresh_candidates(tmp_path, monkeypatch):
    """从没成功过的候选优先处理, 已经成功过只是等着按存续期规则刷新的
    往后排——不然每天都要重刷存续期内的债, 会跟"把全量候选第一次跑完"
    抢当日预算。
    """
    import pandas as pd
    import strategies.cb_arb.fetch_jisilu_adj_logs as mod

    df = pd.DataFrame({
        "code": ["110002", "110003"],
        "delist_date": [None, None],  # 都存续
    })
    cb_basic = tmp_path / "cb_basic.parquet"
    df.to_parquet(cb_basic)

    output = tmp_path / "out.jsonl"
    with output.open("w", encoding="utf-8") as f:
        # 110002 已经成功过一次(存续, 等着刷新); 110003 从没试过
        f.write(json.dumps({"bond_id": "110002", "status": "ok", "records": []}) + "\n")

    order = []
    monkeypatch.setattr(
        mod.jisilu_client, "fetch_adj_logs",
        lambda bond_id, **kw: (order.append(bond_id) or [], None),
    )
    gate = _FakeGate()
    run(cb_basic, output, limit=1, gate=gate)

    # limit=1 只处理一个, 应该优先处理从没成功过的 110003, 不是刷新 110002
    assert order == ["110003"]


def test_run_stops_on_throttle_without_marking_remaining_attempted(tmp_path, monkeypatch):
    import pandas as pd
    import strategies.cb_arb.fetch_jisilu_adj_logs as mod

    df = pd.DataFrame({"code": ["110002", "110003", "110004"], "delist_date": [None, None, None]})
    cb_basic = tmp_path / "cb_basic.parquet"
    df.to_parquet(cb_basic)
    output = tmp_path / "out.jsonl"

    monkeypatch.setattr(mod.jisilu_client, "fetch_adj_logs", lambda bond_id, **kw: ([], None))
    gate = _FakeGate(refuse_after=1)  # 第 1 只之后就被拦
    summary = run(cb_basic, output, gate=gate)

    assert summary["stopped_early"] is True
    assert summary["ok"] == 1
    # 输出文件里只有真正跑完的那一条, 被拦下的两只没有被写成"尝试过"
    lines = output.read_text().strip().splitlines()
    assert len(lines) == 1


def test_run_limit_caps_attempts_when_explicitly_passed(tmp_path, monkeypatch):
    import pandas as pd
    import strategies.cb_arb.fetch_jisilu_adj_logs as mod

    df = pd.DataFrame({"code": ["110002", "110003", "110004"], "delist_date": [None, None, None]})
    cb_basic = tmp_path / "cb_basic.parquet"
    df.to_parquet(cb_basic)
    output = tmp_path / "out.jsonl"

    monkeypatch.setattr(mod.jisilu_client, "fetch_adj_logs", lambda bond_id, **kw: ([], None))
    gate = _FakeGate()
    summary = run(cb_basic, output, limit=2, gate=gate)
    assert summary["attempted"] == 2


def test_run_rejects_negative_limit(tmp_path, monkeypatch):
    """`list[:limit]` 对负数是合法的"掐掉末尾几个"切片, limit=-1 会变成
    "几乎全部候选都抓", 跟"只抓前 N 个调试"的本意正好相反
    (codex review 2026-08-30 用真实 3 条候选复现过)。
    """
    import pandas as pd
    import strategies.cb_arb.fetch_jisilu_adj_logs as mod

    df = pd.DataFrame({"code": ["110002", "110003", "110004"], "delist_date": [None, None, None]})
    cb_basic = tmp_path / "cb_basic.parquet"
    df.to_parquet(cb_basic)
    output = tmp_path / "out.jsonl"

    called = {"n": 0}
    monkeypatch.setattr(mod.jisilu_client, "fetch_adj_logs",
                         lambda bond_id, **kw: (called.__setitem__("n", called["n"] + 1) or ([], None)))
    gate = _FakeGate()
    with pytest.raises(ValueError):
        run(cb_basic, output, limit=-1, gate=gate)
    assert called["n"] == 0, "校验必须在发任何请求之前就拦下, 不能先抓再报错"


def test_cli_rejects_negative_limit_before_running(monkeypatch, capsys):
    import strategies.cb_arb.fetch_jisilu_adj_logs as mod

    monkeypatch.setattr(sys, "argv", ["fetch_jisilu_adj_logs.py", "--limit", "-1"])
    with pytest.raises(SystemExit):
        mod.main()


def test_run_no_limit_processes_all_pending(tmp_path, monkeypatch):
    import pandas as pd
    import strategies.cb_arb.fetch_jisilu_adj_logs as mod

    df = pd.DataFrame({"code": [f"{110000 + i}" for i in range(5)], "delist_date": [None] * 5})
    cb_basic = tmp_path / "cb_basic.parquet"
    df.to_parquet(cb_basic)
    output = tmp_path / "out.jsonl"

    monkeypatch.setattr(mod.jisilu_client, "fetch_adj_logs", lambda bond_id, **kw: ([], None))
    gate = _FakeGate()
    summary = run(cb_basic, output, gate=gate)  # limit 不传
    assert summary["attempted"] == 5
