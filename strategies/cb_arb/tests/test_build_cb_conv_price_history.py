"""单元测试: build_cb_conv_price_history.

覆盖 docs/2026-08-31-cb-conv-price-history-spec.txt 第 7 节验收标准:
1. 老格式记录(outcome 缺失)的兼容转换, 以及"缺字段"和"字段是 null"的区分
2. 同一 (bond_id, meeting_date) 冲突时 fetched_at 最新的赢, 尤其是
   "pending 变 approved"这种真实状态转变的场景
3. 未解决名单只含"从未成功过"的债, 不含 invalid_bond_id, 也不含
   "曾经失败过但最终成功了"的债
4. 输出表的列(含 fetched_at)和空结果的 schema

codex review 指出的真实 bug(已修复, 均有对应回归测试):
- `fetched_at` 是顶层抓取记录的字段, 不在展开后的单条事件上——旧版本从
  `raw_event` 里找这个字段, 永远拿到 None, 导致 `dedupe_latest` 实际上
  从没按时间生效过, 只是碰巧文件按抓取顺序追加才没被看穿。
- 输出 parquet 漏掉了 `fetched_at` 这一正式列。
- `outcome` 显式为 null 被误判成"老格式缺字段", 静默按 approved 推断,
  掩盖了本该报错的异常输入。
"""
from __future__ import annotations

import json

import pytest

from strategies.cb_arb.build_cb_conv_price_history import (
    _OUTPUT_DTYPES,
    _normalize_event,
    _to_dataframe,
    dedupe_latest,
    load_and_normalize,
)


# ----------------------------------------------------------------------
# _normalize_event: 老格式兼容 + null/非法值必须报错, 不能静默推断
# ----------------------------------------------------------------------

def test_normalize_new_format_event_passes_outcome_through():
    raw = {
        "bond_name": "特发转2", "meeting_date": "2021-12-13", "approved": True,
        "outcome": "approved", "old_conv_price": 12.33, "new_conv_price": 7.33,
        "effective_date": "2021-12-14", "floor_price": 7.33,
    }
    event = _normalize_event("127021", "2026-08-31T00:00:00Z", raw)
    assert event["outcome"] == "approved"
    assert event["approved"] is True
    assert event["bond_id"] == "127021"
    assert event["fetched_at"] == "2026-08-31T00:00:00Z"


def test_normalize_legacy_approved_true_without_outcome_infers_approved():
    """老格式(2026-08-30 第一批)没有 outcome 字段, 只有 approved: true——
    早期 jisilu_client.py 只认识"approved=True 就是下修生效"这一种正面
    结局, 推断成 outcome="approved" 有代码历史依据, 不是猜测。
    """
    raw = {
        "bond_name": "北陆转债", "meeting_date": "2022-05-13", "approved": True,
        "old_conv_price": 11.45, "new_conv_price": 6.85,
        "effective_date": "2022-05-16", "floor_price": 6.85,
        # 没有 outcome
    }
    event = _normalize_event("123082", None, raw)
    assert event["outcome"] == "approved"
    assert event["approved"] is True
    assert event["fetched_at"] == ""  # 缺失统一转成空字符串, 去重时排最后


def test_normalize_legacy_approved_false_without_outcome_infers_rejected():
    """老格式版本的 jisilu_client.py 只认识"股东大会未通过"一种否定结局
    ("取消"/"无需调整"/"正股转债均退市"都是后来才加的), 所以老记录里
    approved=False 只可能是"未通过"。
    """
    raw = {
        "bond_name": "龙大转债", "meeting_date": "2026-05-22", "approved": False,
        "old_conv_price": None, "new_conv_price": None,
        "effective_date": None, "floor_price": None,
    }
    event = _normalize_event("128119", None, raw)
    assert event["outcome"] == "rejected"
    assert event["approved"] is False


def test_normalize_explicit_null_outcome_raises_not_silently_inferred(monkeypatch):
    """"key 不存在"(老格式)和"key 存在但值是 None"(异常新格式, 比如
    未来某个 bug 让 outcome 被意外写成 null)必须分开处理——后者静默按
    老格式推断会把真正的数据问题掩盖掉(codex review 复现)。
    """
    raw = {
        "bond_name": "测试转债", "meeting_date": "2026-01-01", "approved": True,
        "outcome": None, "old_conv_price": 1.0, "new_conv_price": 2.0,
        "effective_date": "2026-01-02", "floor_price": 2.0,
    }
    with pytest.raises(ValueError, match="outcome"):
        _normalize_event("999999", "2026-08-31T00:00:00Z", raw)


def test_normalize_unknown_outcome_value_raises():
    """拼写错误的 outcome(不在权威枚举里)不能悄悄进权威表。"""
    raw = {"bond_name": "测试转债", "meeting_date": "2026-01-01", "outcome": "aproved"}
    with pytest.raises(ValueError, match="OUTCOME_VALUES"):
        _normalize_event("999999", "2026-08-31T00:00:00Z", raw)


def test_normalize_legacy_non_bool_approved_raises():
    """老格式记录连 approved 都不是合法 bool, 没法推断 outcome, 必须报错
    而不是默认当 False/rejected。
    """
    raw = {"bond_name": "测试转债", "meeting_date": "2026-01-01", "approved": None}
    with pytest.raises(ValueError, match="approved"):
        _normalize_event("999999", None, raw)


# ----------------------------------------------------------------------
# dedupe_latest: fetched_at 最新的赢
# ----------------------------------------------------------------------

def test_dedupe_keeps_latest_fetched_at_on_conflict():
    events = [
        {"bond_id": "110002", "meeting_date": "2020-01-01", "fetched_at": "2026-08-30T00:00:00Z", "old_conv_price": 1.0},
        {"bond_id": "110002", "meeting_date": "2020-01-01", "fetched_at": "2026-08-31T00:00:00Z", "old_conv_price": 2.0},
    ]
    result = dedupe_latest(events)
    assert len(result) == 1
    assert result[0]["old_conv_price"] == 2.0
    assert result[0]["fetched_at"] == "2026-08-31T00:00:00Z"


def test_dedupe_pending_becomes_approved_real_state_transition():
    """第一次抓到的时候股东大会还没开(pending, 只有预估值); 第二次抓到
    的时候会议真的开完了, 变成 approved 加真实数值。这不是"冗余重复",
    是真实的状态更新, 必须以后一次为准。
    """
    pending = {
        "bond_id": "111004", "meeting_date": "2026-09-14",
        "fetched_at": "2026-08-31T08:00:00Z", "outcome": "pending",
        "approved": False, "old_conv_price": 24.37, "new_conv_price": 19.77,
        "effective_date": None,
    }
    approved = {
        "bond_id": "111004", "meeting_date": "2026-09-14",
        "fetched_at": "2026-09-15T00:30:00Z", "outcome": "approved",
        "approved": True, "old_conv_price": 24.37, "new_conv_price": 19.5,
        "effective_date": "2026-09-16",
    }
    result = dedupe_latest([pending, approved])
    assert len(result) == 1
    assert result[0]["outcome"] == "approved"
    assert result[0]["new_conv_price"] == 19.5
    assert result[0]["effective_date"] == "2026-09-16"


def test_dedupe_missing_fetched_at_always_loses_to_real_timestamp():
    legacy = {"bond_id": "110002", "meeting_date": "2020-01-01", "fetched_at": "", "old_conv_price": 1.0}
    fresh = {"bond_id": "110002", "meeting_date": "2020-01-01", "fetched_at": "2026-08-31T00:00:00Z", "old_conv_price": 2.0}
    result = dedupe_latest([legacy, fresh])
    assert result[0]["old_conv_price"] == 2.0

    # 顺序反过来结果应该一样, 不依赖列表顺序
    result2 = dedupe_latest([fresh, legacy])
    assert result2[0]["old_conv_price"] == 2.0


def test_dedupe_different_meeting_dates_are_separate_events_not_merged():
    events = [
        {"bond_id": "128119", "meeting_date": "2026-06-24", "fetched_at": "a"},
        {"bond_id": "128119", "meeting_date": "2026-02-11", "fetched_at": "a"},
    ]
    assert len(dedupe_latest(events)) == 2


# ----------------------------------------------------------------------
# load_and_normalize: fetched_at 必须从顶层记录取, 未解决名单
# ----------------------------------------------------------------------

def test_fetched_at_is_read_from_top_level_record_not_from_event(tmp_path):
    """codex review 复现的核心 bug: `fetched_at` 是顶层抓取记录的字段,
    `records[]` 里的元素从来没有这个 key。如果错误地从 `raw_event` 里找,
    会永远拿到空字符串, 后续的按时间去重就形同虚设。
    """
    p = tmp_path / "raw.jsonl"
    rows = [
        {
            "bond_id": "110076", "status": "ok", "fetched_at": "2026-08-31T10:15:35Z",
            "records": [
                {"bond_name": "测试转债", "meeting_date": "2020-01-01", "outcome": "approved",
                 "approved": True, "old_conv_price": 1.0, "new_conv_price": 2.0,
                 "effective_date": "2020-01-02", "floor_price": 2.0},
            ],
        },
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    events, _ = load_and_normalize(p)
    assert len(events) == 1
    assert events[0]["fetched_at"] == "2026-08-31T10:15:35Z"


def test_two_fetches_of_same_event_dedupe_by_real_top_level_timestamp(tmp_path):
    """端到端复现: 同一个 pending 事件被抓了两次, 后一次(顶层 fetched_at
    更新)的估值必须赢, 不能因为 fetched_at 取错层级而失效。
    """
    p = tmp_path / "raw.jsonl"
    rows = [
        {
            "bond_id": "111004", "status": "ok", "fetched_at": "2026-08-31T08:00:00Z",
            "records": [
                {"bond_name": "明新转债", "meeting_date": "2026-09-14", "outcome": "pending",
                 "approved": False, "old_conv_price": 24.37, "new_conv_price": 19.71,
                 "effective_date": None, "floor_price": 19.71},
            ],
        },
        {
            "bond_id": "111004", "status": "ok", "fetched_at": "2026-09-01T08:00:00Z",
            "records": [
                {"bond_name": "明新转债", "meeting_date": "2026-09-14", "outcome": "pending",
                 "approved": False, "old_conv_price": 24.37, "new_conv_price": 19.77,
                 "effective_date": None, "floor_price": 19.77},
            ],
        },
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    events, _ = load_and_normalize(p)
    deduped = dedupe_latest(events)
    assert len(deduped) == 1
    assert deduped[0]["new_conv_price"] == 19.77  # 后一次抓到的估值, 不是先出现的那条


def test_unresolved_excludes_invalid_bond_id(tmp_path):
    p = tmp_path / "raw.jsonl"
    rows = [
        {"bond_id": "999999", "status": "invalid_bond_id"},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    events, unresolved = load_and_normalize(p)
    assert events == []
    assert unresolved == {}


def test_unresolved_excludes_bond_that_eventually_succeeded(tmp_path):
    """一只债中途失败过几次, 但最终成功过一次——不算未解决, 不管失败记录
    出现在成功记录之前还是之后。
    """
    p = tmp_path / "raw.jsonl"
    rows = [
        {"bond_id": "123015", "status": "jisilu_unexpected_response", "error": {"reason": "bond_name_inconsistent"}},
        {"bond_id": "123015", "status": "ok", "fetched_at": "2026-08-31T00:00:00Z", "records": [
            {"bond_name": "普利转债", "meeting_date": "2025-03-10", "outcome": "approved",
             "approved": True, "old_conv_price": 4.89, "new_conv_price": 3.0,
             "effective_date": "2025-03-11", "floor_price": 3.0},
        ]},
        {"bond_id": "123015", "status": "jisilu_unexpected_response", "error": {"reason": "bond_name_inconsistent"}},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    events, unresolved = load_and_normalize(p)
    assert len(events) == 1
    assert unresolved == {}


def test_unresolved_includes_bond_that_never_succeeded(tmp_path):
    p = tmp_path / "raw.jsonl"
    rows = [
        {"bond_id": "123015", "status": "jisilu_unexpected_response", "error": {"reason": "bond_name_inconsistent"}},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    events, unresolved = load_and_normalize(p)
    assert events == []
    assert unresolved == {"123015": {"status": "jisilu_unexpected_response", "error": {"reason": "bond_name_inconsistent"}}}


def test_unresolved_keeps_last_attempt_info(tmp_path):
    """从没成功过的债, 未解决名单里记的是最后一次尝试的状态, 不是第一次。"""
    p = tmp_path / "raw.jsonl"
    rows = [
        {"bond_id": "123015", "status": "Timeout", "error": {"reason": None}},
        {"bond_id": "123015", "status": "jisilu_unexpected_response", "error": {"reason": "bond_name_inconsistent"}},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    _, unresolved = load_and_normalize(p)
    assert unresolved["123015"]["status"] == "jisilu_unexpected_response"


def test_load_and_normalize_skips_blank_lines_and_bad_json(tmp_path):
    p = tmp_path / "raw.jsonl"
    p.write_text(
        "\n"
        + json.dumps({"bond_id": "110002", "status": "ok", "fetched_at": "2026-08-31T00:00:00Z", "records": []})
        + "\nnot json\n",
        encoding="utf-8",
    )
    events, unresolved = load_and_normalize(p)
    assert events == []
    assert unresolved == {}


# ----------------------------------------------------------------------
# _to_dataframe: 输出表的列(含 fetched_at)和空结果的 schema
# ----------------------------------------------------------------------

def test_output_dataframe_includes_fetched_at_column():
    """codex review 复现: 输出 parquet 之前漏掉了 fetched_at 这一列——
    它是 spec 第 3 节定义的正式一列, 不只是内部去重用的辅助字段。
    """
    df = _to_dataframe([{
        "bond_id": "127021", "bond_name": "特发转2", "meeting_date": "2021-12-13",
        "outcome": "approved", "approved": True, "old_conv_price": 12.33,
        "new_conv_price": 7.33, "effective_date": "2021-12-14", "floor_price": 7.33,
        "fetched_at": "2026-08-31T00:00:00Z",
    }])
    assert "fetched_at" in df.columns
    assert df.iloc[0]["fetched_at"] == "2026-08-31T00:00:00Z"
    assert list(df.columns) == list(_OUTPUT_DTYPES)


def test_output_dataframe_casts_int_valued_prices_to_float64(tmp_path):
    """codex review 第二轮复现: 非空路径之前只指定了列顺序, 没有真正按
    _OUTPUT_DTYPES 转类型——如果价格字段碰巧全是 Python int(合法的 JSON
    数值, 不是脏数据), pandas 会推断出 int64 列, 悄悄跟"价格字段一律
    float64"这条权威 schema 不一致, 而且这个偏差只有真的写读一遍 parquet
    才会暴露(DataFrame 内存里的 int64/float64 差异容易被忽略)。
    """
    df = _to_dataframe([{
        "bond_id": "999999", "bond_name": "测试转债", "meeting_date": "2026-01-01",
        "outcome": "approved", "approved": True,
        "old_conv_price": 1, "new_conv_price": 2, "floor_price": 3,  # 故意用 int 不用 float
        "effective_date": "2026-01-02", "fetched_at": "2026-08-31T00:00:00Z",
    }])
    assert df["old_conv_price"].dtype == "float64"
    assert df["new_conv_price"].dtype == "float64"
    assert df["floor_price"].dtype == "float64"

    # 写读一遍 parquet, 确认落盘后的类型也是 float64, 不是内存里凑巧对了
    p = tmp_path / "test.parquet"
    df.to_parquet(p, index=False)
    import pandas as pd
    reloaded = pd.read_parquet(p)
    assert reloaded["old_conv_price"].dtype == "float64"


def test_output_dataframe_empty_input_has_correct_schema():
    """空结果也要有正确的列和类型, 不能让 pandas 从空列表猜出一份
    schema 不对的空表(codex review 指出)。
    """
    df = _to_dataframe([])
    assert list(df.columns) == list(_OUTPUT_DTYPES)
    assert len(df) == 0
    assert df["approved"].dtype == bool
    assert df["old_conv_price"].dtype == "float64"
