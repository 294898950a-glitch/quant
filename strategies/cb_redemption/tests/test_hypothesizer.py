"""Tests for the Layer-9 hypothesizer.

All tests mock the LLM call site — the suite MUST NOT make real network
requests to DeepSeek (or anywhere else). Persistent state lives under
``tmp_path``; nothing is written to ``data/cb_redemption/``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from strategies.cb_redemption import memory as memory_mod
from strategies.cb_redemption.hypothesizer import (
    Hypothesis,
    propose,
    propose_via_llm,
    propose_via_rules,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def writable_items() -> list[dict]:
    """A small but realistic writable surface — mirrors tunable_space.yaml."""
    return [
        {
            "item_path": "parameters.w_premium_ratio",
            "current": -0.6910,
            "range": [-5.0, -0.5],
        },
        {
            "item_path": "parameters.w_redeem_progress",
            "current": 2.1997,
            "range": [0.5, 5.0],
        },
        {
            "item_path": "parameters.w_remaining_size",
            "current": -3.6818,
            "range": [-4.0, -0.5],
        },
        {
            "item_path": "thresholds.action",
            "current": 0.65,
            "range": [0.4, 0.9],
        },
        {
            "item_path": "rules.stop_loss_pct",
            "current": -8.0,
            "range": [-15.0, -3.0],
        },
    ]


@pytest.fixture
def clean_diagnosis() -> dict:
    """Diagnosis where no rule should fire."""
    return {
        "is_oos_gap_sharpe": 0.05,
        "is_oos_gap_winrate": 1.2,
        "by_quarter": [
            {"period": "2025Q1", "n_trades": 8, "winrate": 60.0, "avg_return": 1.2},
            {"period": "2025Q2", "n_trades": 7, "winrate": 55.0, "avg_return": 0.9},
        ],
        "by_year": [
            {"period": "2025", "n_trades": 30, "winrate": 58.0, "avg_return": 1.0},
        ],
        "factor_contributions": [],
        "weak_factors": [],
        "drawdown_max": 4.5,
        "drawdown_periods": 0,
        "weakness_text": "all clean",
    }


@pytest.fixture
def diagnosis_weak_factor() -> dict:
    """Diagnosis where rule 1 should fire on weak_factors=['premium_ratio']."""
    return {
        "is_oos_gap_sharpe": 0.05,
        "is_oos_gap_winrate": 1.0,
        "by_quarter": [
            {"period": "2025Q1", "n_trades": 8, "winrate": 60.0, "avg_return": 1.2},
        ],
        "by_year": [
            {"period": "2025", "n_trades": 30, "winrate": 58.0, "avg_return": 1.0},
        ],
        "factor_contributions": [],
        "weak_factors": ["premium_ratio"],
        "drawdown_max": 3.0,
        "drawdown_periods": 0,
        "weakness_text": "weak premium",
    }


@pytest.fixture
def tried_path(tmp_path) -> Path:
    """Empty tried-directions file (path under tmp)."""
    return tmp_path / "tried_directions.jsonl"


@pytest.fixture
def runs_path(tmp_path) -> Path:
    return tmp_path / "runs.jsonl"


@pytest.fixture
def space_path(tmp_path) -> Path:
    return tmp_path / "tunable_space.yaml"


@pytest.fixture(autouse=True)
def _set_api_key_default(monkeypatch):
    """Default: API key set so LLM path is exercised. Individual tests
    override via monkeypatch.delenv when they want the no-key path."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")


# --------------------------------------------------------------------------- #
# LLM path tests (all mocked — no network)
# --------------------------------------------------------------------------- #


def test_llm_returns_valid_json(
    writable_items, clean_diagnosis, tried_path, runs_path, space_path
):
    """Mocked LLM returns a perfectly valid hypothesis → propose returns
    a Hypothesis with source='llm'."""
    payload = {
        "item_path": "parameters.w_premium_ratio",
        "new_value": -1.0,
        "expected_direction": "oos_sharpe up by >=0.02",
        "reason": "降低 |w_premium_ratio| 以缩小 IS/OOS gap, 期望 oos_sharpe 上升",
        "confidence": "medium",
    }

    def fake_call(api_key, system_prompt, user_prompt):
        return json.dumps(payload)

    h = propose(
        writable_items=writable_items,
        recent_runs=[],
        diagnosis=clean_diagnosis,
        tried_directions=[],
        llm_client=fake_call,
        space_path=space_path,
        runs_path=runs_path,
        tried_path=tried_path,
    )

    assert h is not None
    assert h.source == "llm"
    assert h.item_path == "parameters.w_premium_ratio"
    assert h.new_value == -1.0
    assert h.confidence == "medium"
    assert "oos_sharpe" in h.expected_direction
    # to_dict round trips
    d = h.to_dict()
    assert d["source"] == "llm"
    assert d["new_value"] == -1.0


def test_llm_returns_garbage_falls_back_to_rules(
    writable_items, diagnosis_weak_factor, tried_path, runs_path, space_path
):
    """Non-JSON LLM output → all retries exhausted → rules path produces
    a Hypothesis with source='rules'."""
    calls = {"n": 0}

    def fake_call(api_key, system_prompt, user_prompt):
        calls["n"] += 1
        return "this is not json at all <<>> {{{ broken"

    h = propose(
        writable_items=writable_items,
        recent_runs=[],
        diagnosis=diagnosis_weak_factor,
        tried_directions=[],
        llm_client=fake_call,
        space_path=space_path,
        runs_path=runs_path,
        tried_path=tried_path,
        max_llm_retries=2,
    )

    assert h is not None
    assert h.source == "rules"
    assert calls["n"] == 3  # initial + 2 retries
    # Rule 1 should have fired on weak_factors=['premium_ratio']
    assert h.item_path == "parameters.w_premium_ratio"


def test_llm_value_out_of_range_falls_back_to_rules(
    writable_items, diagnosis_weak_factor, tried_path, runs_path, space_path
):
    """LLM proposes new_value outside the declared range → rejected →
    rules path runs."""
    payload = {
        "item_path": "parameters.w_premium_ratio",
        "new_value": -99.0,  # range is [-5.0, -0.5]
        "expected_direction": "oos_sharpe up",
        "reason": "提升 oos_sharpe 测试越界拒绝逻辑, 应被退回到规则版",
        "confidence": "high",
    }

    def fake_call(api_key, system_prompt, user_prompt):
        return json.dumps(payload)

    h = propose(
        writable_items=writable_items,
        recent_runs=[],
        diagnosis=diagnosis_weak_factor,
        tried_directions=[],
        llm_client=fake_call,
        space_path=space_path,
        runs_path=runs_path,
        tried_path=tried_path,
    )

    assert h is not None
    assert h.source == "rules"


def test_llm_unknown_item_path_falls_back(
    writable_items, diagnosis_weak_factor, tried_path, runs_path, space_path
):
    """LLM picks an item_path that isn't in writable_items → rejected."""
    payload = {
        "item_path": "parameters.nonexistent_factor",
        "new_value": 1.0,
        "expected_direction": "oos_sharpe up",
        "reason": "测试未知 item_path 应被拒绝并退回到规则版分支处理",
        "confidence": "low",
    }

    def fake_call(api_key, system_prompt, user_prompt):
        return json.dumps(payload)

    h = propose(
        writable_items=writable_items,
        recent_runs=[],
        diagnosis=diagnosis_weak_factor,
        tried_directions=[],
        llm_client=fake_call,
        space_path=space_path,
        runs_path=runs_path,
        tried_path=tried_path,
    )

    assert h is not None
    assert h.source == "rules"


def test_llm_reason_too_short_falls_back(
    writable_items, diagnosis_weak_factor, tried_path, runs_path, space_path
):
    """reason length after strip < 30 → rejected."""
    payload = {
        "item_path": "parameters.w_premium_ratio",
        "new_value": -1.0,
        "expected_direction": "oos_sharpe up",
        "reason": "short",  # 5 chars
        "confidence": "low",
    }

    def fake_call(api_key, system_prompt, user_prompt):
        return json.dumps(payload)

    h = propose(
        writable_items=writable_items,
        recent_runs=[],
        diagnosis=diagnosis_weak_factor,
        tried_directions=[],
        llm_client=fake_call,
        space_path=space_path,
        runs_path=runs_path,
        tried_path=tried_path,
    )

    assert h is not None
    assert h.source == "rules"


def test_llm_invalid_confidence_falls_back(
    writable_items, diagnosis_weak_factor, tried_path, runs_path, space_path
):
    """confidence not in {low, medium, high} → rejected."""
    payload = {
        "item_path": "parameters.w_premium_ratio",
        "new_value": -1.0,
        "expected_direction": "oos_sharpe up by >=0.02",
        "reason": "测试 confidence 不合法时应被拒绝并退回规则版进行处理",
        "confidence": "very-high",  # not in enum
    }

    def fake_call(api_key, system_prompt, user_prompt):
        return json.dumps(payload)

    h = propose(
        writable_items=writable_items,
        recent_runs=[],
        diagnosis=diagnosis_weak_factor,
        tried_directions=[],
        llm_client=fake_call,
        space_path=space_path,
        runs_path=runs_path,
        tried_path=tried_path,
    )

    assert h is not None
    assert h.source == "rules"


def test_llm_suggests_already_tried_falls_back(
    writable_items, diagnosis_weak_factor, tried_path, runs_path, space_path
):
    """LLM proposes a direction that memory.has_been_tried marks as
    outcome='rejected' → must be refused, falls back to rules."""
    # Pre-populate tried_directions.jsonl so the LLM proposal is "already
    # rejected".
    key = memory_mod.AttemptKey.from_value(
        item_path="parameters.w_premium_ratio",
        direction="increase",
        new_value=-0.55,
    )
    memory_mod.record_attempt(
        key,
        run_id="prior-run-1",
        outcome="rejected",
        path=tried_path,
    )

    payload = {
        "item_path": "parameters.w_premium_ratio",
        "new_value": -0.55,  # current is -0.691 → -0.55 is "increase" toward 0
        "expected_direction": "oos_sharpe up by >=0.02",
        "reason": "测试 LLM 提议被 has_been_tried(rejected) 拦截后退回规则的路径",
        "confidence": "medium",
    }

    def fake_call(api_key, system_prompt, user_prompt):
        return json.dumps(payload)

    h = propose(
        writable_items=writable_items,
        recent_runs=[],
        diagnosis=diagnosis_weak_factor,
        tried_directions=[],
        llm_client=fake_call,
        space_path=space_path,
        runs_path=runs_path,
        tried_path=tried_path,
    )

    assert h is not None
    assert h.source == "rules"


def test_llm_network_error_falls_back(
    writable_items, diagnosis_weak_factor, tried_path, runs_path, space_path
):
    """LLM call raises a TimeoutException → rules fire."""

    def fake_call(api_key, system_prompt, user_prompt):
        raise httpx.TimeoutException("simulated timeout")

    h = propose(
        writable_items=writable_items,
        recent_runs=[],
        diagnosis=diagnosis_weak_factor,
        tried_directions=[],
        llm_client=fake_call,
        space_path=space_path,
        runs_path=runs_path,
        tried_path=tried_path,
    )

    assert h is not None
    assert h.source == "rules"


def test_llm_no_api_key_uses_rules_directly(
    writable_items,
    diagnosis_weak_factor,
    tried_path,
    runs_path,
    space_path,
    monkeypatch,
):
    """DEEPSEEK_API_KEY unset → LLM is never called; rules run directly."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    calls = {"n": 0}

    def fake_call(api_key, system_prompt, user_prompt):
        calls["n"] += 1
        return "{}"

    h = propose(
        writable_items=writable_items,
        recent_runs=[],
        diagnosis=diagnosis_weak_factor,
        tried_directions=[],
        llm_client=fake_call,
        space_path=space_path,
        runs_path=runs_path,
        tried_path=tried_path,
    )

    assert h is not None
    assert h.source == "rules"
    assert calls["n"] == 0  # injected LLM was never invoked


def test_llm_default_call_never_hits_network_when_no_key(
    writable_items, diagnosis_weak_factor, tried_path, runs_path, space_path,
    monkeypatch,
):
    """Belt-and-braces: when DEEPSEEK_API_KEY is empty/unset, even the
    default (real httpx) llm_call wrapper must not be invoked.

    Patch httpx.Client to blow up if anyone tries to construct it.
    """
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with patch("httpx.Client") as mocked_client:
        mocked_client.side_effect = AssertionError(
            "httpx.Client should not be constructed when API key is missing"
        )
        h = propose(
            writable_items=writable_items,
            recent_runs=[],
            diagnosis=diagnosis_weak_factor,
            tried_directions=[],
            llm_client=None,  # exercise default path
            space_path=space_path,
            runs_path=runs_path,
            tried_path=tried_path,
        )

    assert h is not None
    assert h.source == "rules"
    mocked_client.assert_not_called()


# --------------------------------------------------------------------------- #
# Rule-based fallback tests
# --------------------------------------------------------------------------- #


def test_rules_weak_factor_triggers_first(
    writable_items, diagnosis_weak_factor, tried_path
):
    """Rule 1: weak_factors=['premium_ratio'] → halve w_premium_ratio
    weight toward 0."""
    h = propose_via_rules(
        writable_items=writable_items,
        diagnosis=diagnosis_weak_factor,
        tried_path=tried_path,
    )
    assert h is not None
    assert h.source == "rules"
    assert h.item_path == "parameters.w_premium_ratio"
    # current=-0.691 → halved toward 0 = -0.3455, but range upper bound is
    # -0.5 so it clips to -0.5.
    assert h.new_value == pytest.approx(-0.5, abs=1e-6)
    assert "规则 1" in h.reason
    assert h.confidence == "low"
    assert "oos_sharpe" in h.expected_direction


def test_rules_gap_triggers_rule_2(writable_items, tried_path):
    """Rule 2: |is_oos_gap_sharpe|>0.1 with no weak factor → shrink the
    largest |w| parameter by 10%."""
    diag = {
        "is_oos_gap_sharpe": 0.4,
        "is_oos_gap_winrate": 0.0,
        "by_quarter": [
            {"period": "2025Q1", "n_trades": 8, "winrate": 80.0, "avg_return": 2.0}
        ],
        "by_year": [
            {"period": "2025", "n_trades": 30, "winrate": 80.0, "avg_return": 2.0}
        ],
        "weak_factors": [],
    }
    h = propose_via_rules(
        writable_items=writable_items,
        diagnosis=diag,
        tried_path=tried_path,
    )
    assert h is not None
    # largest |w| in fixture is w_remaining_size = -3.6818
    assert h.item_path == "parameters.w_remaining_size"
    # -3.6818 - (-3.6818)*0.1 = -3.31362
    assert h.new_value == pytest.approx(-3.3136, abs=1e-3)
    assert "规则 2" in h.reason


def test_rules_returns_none_when_nothing_triggers(
    writable_items, clean_diagnosis, tried_path
):
    """Clean diagnosis + every writable item exhaustively pre-tried →
    rules return None."""
    # Pre-record an attempt for every writable item at the exact value
    # rule-5 would propose, so the dedup check skips them all.
    for it in writable_items:
        cur = it["current"]
        lo, hi = it["range"]
        mid = (lo + hi) / 2.0
        proposed = round(cur + (mid - cur) * 0.1, 4)
        direction = (
            "increase" if proposed > cur else "decrease" if proposed < cur else "set"
        )
        key = memory_mod.AttemptKey.from_value(
            item_path=it["item_path"],
            direction=direction,
            new_value=proposed,
        )
        memory_mod.record_attempt(
            key,
            run_id="seed",
            outcome="accepted",
            path=tried_path,
        )

    h = propose_via_rules(
        writable_items=writable_items,
        diagnosis=clean_diagnosis,
        tried_path=tried_path,
    )

    assert h is None


# --------------------------------------------------------------------------- #
# Pool stats injection (no-label invariant)
# --------------------------------------------------------------------------- #


def test_pool_stats_in_prompt(
    writable_items, clean_diagnosis, tried_path, runs_path, space_path
):
    """When ``pool_stats`` is forwarded to ``propose``, the LLM user
    message must contain the raw-number block — and **must not** contain
    any market-state labels (the framework lets the LLM judge, but
    refuses to hand-feed it priors).
    """
    captured: dict[str, str] = {}

    def fake_call(api_key, system_prompt, user_prompt):
        captured["system"] = system_prompt
        captured["user"] = user_prompt
        # Return a perfectly valid hypothesis so the LLM path succeeds.
        return json.dumps(
            {
                "item_path": "parameters.w_premium_ratio",
                "new_value": -1.0,
                "expected_direction": "oos_sharpe up by >=0.02",
                "reason": "在统计描述里看到累计涨跌为负, 收紧 |w_premium_ratio| 期望 oos_sharpe 上升",
                "confidence": "medium",
            }
        )

    pool_stats = {
        "sample_n": 130,
        "trend_pct": -0.12,
        "slope_per_day": -0.0005,
        "vol_daily": 0.015,
        "range_pct": 0.35,
    }

    h = propose(
        writable_items=writable_items,
        recent_runs=[],
        diagnosis=clean_diagnosis,
        tried_directions=[],
        llm_client=fake_call,
        space_path=space_path,
        runs_path=runs_path,
        tried_path=tried_path,
        pool_stats=pool_stats,
    )

    assert h is not None
    assert h.source == "llm"

    # Required raw-number markers appear in the user prompt.
    user = captured["user"]
    assert "累计涨跌" in user
    assert "日波动率" in user
    assert "区间宽度" in user
    assert "样本数" in user
    # And the prompt visibly includes the actual numbers.
    assert "-12.0%" in user  # trend_pct rendered as percent
    assert "1.50%" in user   # vol_daily rendered as percent
    assert "130" in user     # sample_n

    # No market-state labels in any part of the prompt — the framework
    # contract is "let the LLM judge", not "hand it a label".
    full_prompt = captured["system"] + "\n" + captured["user"]
    forbidden_lower = ("bull", "bear", "ranging", "volatile", "dead")
    forbidden_chinese = ("牛市", "熊市", "震荡")
    low = full_prompt.lower()
    for token in forbidden_lower:
        assert token not in low, (
            f"forbidden label {token!r} leaked into prompt: {captured!r}"
        )
    for token in forbidden_chinese:
        assert token not in full_prompt, (
            f"forbidden label {token!r} leaked into prompt: {captured!r}"
        )


def test_pool_stats_none_omits_section(
    writable_items, clean_diagnosis, tried_path, runs_path, space_path
):
    """When ``pool_stats`` is omitted (cb_redemption case), the user
    message must NOT contain the stats header — backwards-compat for
    strategies without a price loader.
    """
    captured: dict[str, str] = {}

    def fake_call(api_key, system_prompt, user_prompt):
        captured["user"] = user_prompt
        return json.dumps(
            {
                "item_path": "parameters.w_premium_ratio",
                "new_value": -1.0,
                "expected_direction": "oos_sharpe up by >=0.02",
                "reason": "把 |w_premium_ratio| 缩小测试 pool_stats 为 None 时 prompt 不含统计描述",
                "confidence": "low",
            }
        )

    h = propose(
        writable_items=writable_items,
        recent_runs=[],
        diagnosis=clean_diagnosis,
        tried_directions=[],
        llm_client=fake_call,
        space_path=space_path,
        runs_path=runs_path,
        tried_path=tried_path,
        # pool_stats omitted on purpose
    )

    assert h is not None
    user = captured["user"]
    # Header introduced by _format_pool_stats_section must be absent.
    assert "当前数据切片的统计描述" not in user
