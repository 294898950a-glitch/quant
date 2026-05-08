"""Tests for the layer 8.5 sanity_checker.

All LLM-related tests use injected ``llm_client`` callables — the suite
MUST NOT make real network requests to DeepSeek (or anywhere else). Hard
rules are pure functions; we exercise each branch and confirm the
"missing parameter → skip" degradation.
"""

from __future__ import annotations

import json

import httpx
import pytest

from strategies.cb_redemption.sanity_checker import (
    SanityReport,
    check,
    check_hard_rules,
    check_with_llm,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def grid_writable() -> list[dict]:
    """Grid-strategy writable items with all relevant grid params present."""
    return [
        {"item_path": "parameters.grid_count", "current": 10, "range": [5, 30]},
        {"item_path": "parameters.range_window", "current": 60, "range": [20, 200]},
        {"item_path": "parameters.position_per_grid", "current": 0.10, "range": [0.03, 0.25]},
        {"item_path": "parameters.trend_short_window", "current": 20, "range": [5, 60]},
        {"item_path": "parameters.trend_long_window", "current": 60, "range": [30, 200]},
        {"item_path": "rules.fee_pct", "current": 0.0003, "range": [0.0001, 0.0010]},
        {"item_path": "rules.vol_atr_window", "current": 14, "range": [5, 30]},
    ]


@pytest.fixture
def cb_writable() -> list[dict]:
    """cb_redemption-shaped writable items — none of the grid keys exist."""
    return [
        {"item_path": "parameters.w_premium_ratio", "current": -0.7, "range": [-5.0, -0.5]},
        {"item_path": "parameters.w_redeem_progress", "current": 2.0, "range": [0.5, 5.0]},
        {"item_path": "thresholds.action", "current": 0.65, "range": [0.4, 0.9]},
        {"item_path": "rules.stop_loss_pct", "current": -8.0, "range": [-15.0, -3.0]},
    ]


@pytest.fixture(autouse=True)
def _set_api_key_default(monkeypatch):
    """Default: API key set so LLM path is exercised. Individual tests
    override via monkeypatch.delenv when they want the no-key path."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")


# --------------------------------------------------------------------------- #
# 1. Hard-rule layer
# --------------------------------------------------------------------------- #


def test_range_window_exceeds_pool_is_fatal(grid_writable):
    """range_window=60, pool_size=40 → fatal (engine never warms up)."""
    issues = check_hard_rules(grid_writable, pool_size=40)
    fatals = [i for i in issues if i["severity"] == "fatal"]
    codes = {i["code"] for i in fatals}
    assert "range_window_exceeds_pool" in codes
    msg = next(i["message"] for i in fatals if i["code"] == "range_window_exceeds_pool")
    assert "range_window=60" in msg
    assert "pool_size=40" in msg


def test_trend_short_geq_long_is_fatal(grid_writable):
    """trend_short_window=80 >= trend_long_window=60 → fatal."""
    # Mutate the fixture to flip the relationship.
    for it in grid_writable:
        if it["item_path"] == "parameters.trend_short_window":
            it["current"] = 80
            it["range"] = [5, 100]
        elif it["item_path"] == "parameters.trend_long_window":
            it["current"] = 60

    issues = check_hard_rules(grid_writable, pool_size=200)
    codes = {i["code"] for i in issues if i["severity"] == "fatal"}
    assert "trend_short_not_less_than_long" in codes


def test_grid_total_position_exceeds_full_is_warn(grid_writable):
    """grid_count=20, position_per_grid=0.10 → 20*0.10=2.0 > 1.5 → warn."""
    for it in grid_writable:
        if it["item_path"] == "parameters.grid_count":
            it["current"] = 20
        elif it["item_path"] == "parameters.position_per_grid":
            it["current"] = 0.10

    issues = check_hard_rules(grid_writable, pool_size=200)
    warns = [i for i in issues if i["severity"] == "warn"]
    codes = {i["code"] for i in warns}
    assert "grid_total_position_exceeds_full" in codes
    # No fatal in this scenario.
    assert not any(i["severity"] == "fatal" for i in issues)


def test_vol_atr_window_exceeds_range_window_is_warn(grid_writable):
    """vol_atr_window=30, range_window=20 → warn."""
    for it in grid_writable:
        if it["item_path"] == "parameters.range_window":
            it["current"] = 20
        elif it["item_path"] == "rules.vol_atr_window":
            it["current"] = 30

    issues = check_hard_rules(grid_writable, pool_size=200)
    codes = {i["code"] for i in issues if i["severity"] == "warn"}
    assert "vol_atr_window_exceeds_range_window" in codes


def test_cb_yaml_skips_grid_rules(cb_writable):
    """cb_redemption yaml: no range_window / grid_count / trend_* params,
    so none of the grid-specific rules should fire. With sane currents the
    output is an empty list (no fatal, no warn)."""
    issues = check_hard_rules(cb_writable, pool_size=50)
    # Universal rule (current_out_of_range) should also be silent — fixture
    # values are inside their ranges.
    assert issues == []


def test_current_out_of_range_is_fatal():
    """A value outside its declared range is flagged fatal regardless of
    strategy shape (paranoid double-check of editor's work)."""
    items = [
        {"item_path": "parameters.alpha", "current": 10.0, "range": [0.0, 5.0]},
    ]
    issues = check_hard_rules(items, pool_size=None)
    codes = {i["code"] for i in issues if i["severity"] == "fatal"}
    assert "current_out_of_range" in codes


def test_clean_grid_params_no_issues(grid_writable):
    """Default fixture (60-day range, 20/60 trend, 10×0.10=1.0, vol 14<60)
    is clean — verdict should be ok via top-level :func:`check`."""
    rep = check(
        writable_items=grid_writable,
        pool_size=200,
        strategy_name="sp500_grid",
        use_llm=False,
    )
    assert rep.verdict == "ok"
    assert rep.layer == "rules"
    assert rep.issues == []
    assert rep.score == 10.0
    assert "通过" in rep.summary or "无明显问题" in rep.summary


# --------------------------------------------------------------------------- #
# 2. LLM layer (mocked)
# --------------------------------------------------------------------------- #


def test_llm_returns_ok_yields_rules_plus_llm(grid_writable):
    """Mocked LLM returns a perfectly valid 'ok' verdict → SanityReport
    with verdict=ok and layer=rules+llm."""
    payload = {
        "verdict": "ok",
        "score": 9.2,
        "issues": [],
        "advice": "",
        "summary": "参数与数据形状均合理, 可放心进入回测",
    }

    def fake_call(api_key, system_prompt, user_prompt):
        return json.dumps(payload)

    rep = check(
        writable_items=grid_writable,
        pool_size=200,
        strategy_name="sp500_grid",
        asset_hint="美国大盘 ETF (SPY 影子)",
        use_llm=True,
        llm_client=fake_call,
    )
    assert rep.verdict == "ok"
    assert rep.layer == "rules+llm"
    assert rep.score == pytest.approx(9.2)
    assert "合理" in rep.summary
    # to_dict round-trips
    d = rep.to_dict()
    assert d["layer"] == "rules+llm"
    assert d["score"] == pytest.approx(9.2)


def test_llm_returns_fatal_merges_into_report(grid_writable):
    """LLM declares fatal with extra issues → final report is fatal and
    contains the LLM's issues alongside any rule warns."""
    payload = {
        "verdict": "fatal",
        "score": 1.5,
        "issues": [
            {
                "severity": "fatal",
                "code": "logic_inconsistent",
                "message": "网格策略下趋势 filter 全关 + 单边趋势资产 = 逻辑不通",
            }
        ],
        "advice": "考虑开启 trend_filter_enabled, 或换更震荡的标的",
        "summary": "策略逻辑与标的形态不匹配, 建议放弃当前方向",
    }

    def fake_call(api_key, system_prompt, user_prompt):
        return json.dumps(payload)

    rep = check(
        writable_items=grid_writable,
        pool_size=200,
        strategy_name="sp500_grid",
        use_llm=True,
        llm_client=fake_call,
    )
    assert rep.verdict == "fatal"
    assert rep.layer == "rules+llm"
    assert rep.advice and "trend_filter" in rep.advice
    codes = {i["code"] for i in rep.issues}
    assert "logic_inconsistent" in codes


def test_llm_returns_garbage_falls_back_to_rules(grid_writable):
    """Non-JSON LLM output → all retries exhausted → SanityReport from
    rules layer only."""
    calls = {"n": 0}

    def fake_call(api_key, system_prompt, user_prompt):
        calls["n"] += 1
        return "this is not json {{{ broken"

    rep = check(
        writable_items=grid_writable,
        pool_size=200,
        strategy_name="sp500_grid",
        use_llm=True,
        llm_client=fake_call,
        max_retries=2,
    )
    # Garbage → LLM rejected → rules-only layer.
    assert rep.layer == "rules"
    # Default fixture is clean → verdict ok.
    assert rep.verdict == "ok"
    # All retries fired.
    assert calls["n"] == 3  # initial + 2 retries


def test_llm_score_out_of_range_is_rejected(grid_writable):
    """LLM returns score=11 (>10) → rejected → rules-only fallback."""
    payload = {
        "verdict": "warn",
        "score": 11.0,  # invalid
        "issues": [{"severity": "warn", "code": "x", "message": "msg"}],
        "advice": "advice text non-empty",
        "summary": "this summary is at least ten characters long for sure",
    }

    def fake_call(api_key, system_prompt, user_prompt):
        return json.dumps(payload)

    rep = check(
        writable_items=grid_writable,
        pool_size=200,
        strategy_name="sp500_grid",
        use_llm=True,
        llm_client=fake_call,
        max_retries=1,
    )
    assert rep.layer == "rules"  # LLM rejected, rules-only.


def test_no_api_key_uses_rules_only(grid_writable, monkeypatch):
    """DEEPSEEK_API_KEY unset → LLM is never called even with use_llm=True."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    calls = {"n": 0}

    def fake_call(api_key, system_prompt, user_prompt):
        calls["n"] += 1
        return "{}"

    rep = check(
        writable_items=grid_writable,
        pool_size=200,
        strategy_name="sp500_grid",
        use_llm=True,
        llm_client=fake_call,
    )
    assert rep.layer == "rules"
    assert calls["n"] == 0  # LLM never invoked


def test_rule_fatal_skips_llm_call_to_save_cost(grid_writable):
    """When a hard-rule fatal is already detected, the LLM must NOT be
    invoked (saves DeepSeek tokens) — even with use_llm=True."""
    # Force range_window > pool_size: pool_size=40 < range_window=60.
    calls = {"n": 0}

    def fake_call(api_key, system_prompt, user_prompt):
        calls["n"] += 1
        # Even if called, return a 'pass' to make sure the assertion below
        # is testing the gating, not the merge logic.
        return json.dumps({
            "verdict": "ok",
            "score": 10.0,
            "issues": [],
            "advice": "",
            "summary": "should not be visible because LLM was skipped",
        })

    rep = check(
        writable_items=grid_writable,
        pool_size=40,  # 40 < range_window 60 → fatal
        strategy_name="sp500_grid",
        use_llm=True,
        llm_client=fake_call,
    )
    assert rep.verdict == "fatal"
    assert rep.layer == "rules"
    assert calls["n"] == 0
    # The hard-rule fatal must surface in summary.
    assert any(i["code"] == "range_window_exceeds_pool" for i in rep.issues)


def test_llm_network_error_falls_back_to_rules(grid_writable):
    """LLM network error → rules-only fallback (mirrors hypothesizer's
    behaviour for consistency)."""

    def fake_call(api_key, system_prompt, user_prompt):
        raise httpx.TimeoutException("simulated timeout")

    rep = check(
        writable_items=grid_writable,
        pool_size=200,
        strategy_name="sp500_grid",
        use_llm=True,
        llm_client=fake_call,
    )
    # Default fixture is clean → verdict ok, layer rules.
    assert rep.verdict == "ok"
    assert rep.layer == "rules"


def test_llm_warn_merges_with_rule_warn(grid_writable):
    """If both rule and LLM emit warns, final verdict is warn and both
    issue sources are preserved in the merged list."""
    # Mutate fixture so a rule warn fires (vol_atr_window > range_window).
    for it in grid_writable:
        if it["item_path"] == "parameters.range_window":
            it["current"] = 20
        elif it["item_path"] == "rules.vol_atr_window":
            it["current"] = 30

    payload = {
        "verdict": "warn",
        "score": 6.0,
        "issues": [
            {"severity": "warn", "code": "boundary_hugging", "message": "参数贴在范围下沿"},
        ],
        "advice": "考虑加大探索步长, 离开范围下沿",
        "summary": "参数有贴边迹象, 但还不至于致命, 继续可观察",
    }

    def fake_call(api_key, system_prompt, user_prompt):
        return json.dumps(payload)

    rep = check(
        writable_items=grid_writable,
        pool_size=200,
        strategy_name="sp500_grid",
        use_llm=True,
        llm_client=fake_call,
    )
    assert rep.verdict == "warn"
    assert rep.layer == "rules+llm"
    codes = {i["code"] for i in rep.issues}
    assert "vol_atr_window_exceeds_range_window" in codes
    assert "boundary_hugging" in codes


def test_check_with_llm_returns_none_when_invalid_after_retries(grid_writable):
    """Direct invocation of check_with_llm returns None when all retries
    are rejected — used by check() to decide on rules-only fallback."""
    def fake_call(api_key, system_prompt, user_prompt):
        return json.dumps({"verdict": "nope"})  # invalid verdict

    rep = check_with_llm(
        writable_items=grid_writable,
        pool_size=200,
        recent_runs=[],
        diagnosis={},
        strategy_name="sp500_grid",
        llm_client=fake_call,
        max_retries=1,
    )
    assert rep is None


def test_to_dict_round_trips_all_fields(grid_writable):
    """SanityReport.to_dict preserves every field for outbox / state.json."""
    rep = SanityReport(
        verdict="fatal",
        score=2.0,
        issues=[{"severity": "fatal", "code": "x", "message": "msg"}],
        advice="advice text",
        summary="summary text long enough",
        layer="rules+llm",
    )
    d = rep.to_dict()
    assert d == {
        "verdict": "fatal",
        "score": 2.0,
        "issues": [{"severity": "fatal", "code": "x", "message": "msg"}],
        "advice": "advice text",
        "summary": "summary text long enough",
        "layer": "rules+llm",
    }
