"""Boundary tests for apply_cost_model() added in cb_arb_value_gap_switch promotion.

Covers cost_model_enabled toggle, side semantics (buy vs sell), and edge cases:
- zero qty / negative inputs
- impact_pct cap behavior
- holding_cost only on sell side
- avg_amount_5d=None or zero (impact disabled)
- non-negative cash_amount floor
"""

from __future__ import annotations

import pytest

from strategies.cb_arb.verifier import CBArbConfig, apply_cost_model


@pytest.fixture
def cfg_off() -> CBArbConfig:
    """Default config with cost_model disabled."""
    return CBArbConfig()


@pytest.fixture
def cfg_on() -> CBArbConfig:
    """Cost model fully on with realistic params."""
    return CBArbConfig(
        cost_model_enabled=True,
        fee_pct=0.0003,
        slippage_pct=0.0015,
        market_impact_coeff=1.0,
        market_impact_cap_pct=0.01,
        holding_cost_pct=0.02,
    )


# ============ cost_model disabled (default) ============


def test_cost_off_buy_pays_fee_only(cfg_off: CBArbConfig):
    """cost_model_enabled=False, buy → gross + fee, no slippage/impact."""
    cfg_off.fee_pct = 0.001
    r = apply_cost_model(price=100.0, qty=10, side="buy", cfg=cfg_off)
    assert r["gross_amount"] == 1000.0
    assert r["fee"] == 1.0
    assert r["cash_amount"] == 1001.0
    assert "slippage" not in r  # disabled path returns minimal dict


def test_cost_off_sell_receives_minus_fee(cfg_off: CBArbConfig):
    """cost_model_enabled=False, sell → gross - fee."""
    cfg_off.fee_pct = 0.001
    r = apply_cost_model(price=100.0, qty=10, side="sell", cfg=cfg_off)
    assert r["cash_amount"] == 999.0
    assert r["fee"] == 1.0


# ============ cost_model enabled - buy side ============


def test_cost_on_buy_adds_slippage_no_holding(cfg_on: CBArbConfig):
    """Buy with cost-on: cash = gross + fee + slippage + impact, no holding_cost."""
    r = apply_cost_model(price=100.0, qty=10, side="buy", cfg=cfg_on,
                         avg_amount_5d=100000.0)
    gross = 1000.0
    fee = gross * 0.0003  # 0.3
    slippage = gross * 0.0015  # 1.5
    # impact: coeff=1.0 * gross/avg = 1000/100000 = 0.01, capped at 0.01 → 1% impact = 10.0
    impact = gross * 0.01
    assert r["fee"] == pytest.approx(fee)
    assert r["slippage"] == pytest.approx(slippage)
    assert r["market_impact"] == pytest.approx(impact)
    assert r["holding_cost"] == 0.0  # buy side never has holding cost
    assert r["cash_amount"] == pytest.approx(gross + fee + slippage + impact)


def test_cost_on_buy_no_avg_amount_no_impact(cfg_on: CBArbConfig):
    """avg_amount_5d=None → no impact applied (0%)."""
    r = apply_cost_model(price=100.0, qty=10, side="buy", cfg=cfg_on,
                         avg_amount_5d=None)
    assert r["market_impact"] == 0.0
    assert r["impact_pct"] == 0.0


def test_cost_on_buy_zero_avg_amount_no_impact(cfg_on: CBArbConfig):
    """avg_amount_5d=0 → no impact (no division)."""
    r = apply_cost_model(price=100.0, qty=10, side="buy", cfg=cfg_on,
                         avg_amount_5d=0.0)
    assert r["market_impact"] == 0.0


def test_cost_on_buy_impact_caps_at_max(cfg_on: CBArbConfig):
    """Tiny avg_amount → coeff*gross/avg > cap → impact_pct caps at cap."""
    r = apply_cost_model(price=100.0, qty=10, side="buy", cfg=cfg_on,
                         avg_amount_5d=10.0)  # gross/avg = 100 → coeff*100 = 100
    # cap is 0.01, so impact_pct = 0.01
    assert r["impact_pct"] == pytest.approx(cfg_on.market_impact_cap_pct)
    assert r["market_impact"] == pytest.approx(1000.0 * cfg_on.market_impact_cap_pct)


# ============ cost_model enabled - sell side ============


def test_cost_on_sell_holding_cost_accrues_with_days(cfg_on: CBArbConfig):
    """holding_cost = gross * pct * days / 365 (only on sell)."""
    r = apply_cost_model(price=100.0, qty=10, side="sell", cfg=cfg_on,
                         avg_amount_5d=100000.0, holding_days=365)
    gross = 1000.0
    expected_holding = gross * 0.02 * 1.0  # full year
    assert r["holding_cost"] == pytest.approx(expected_holding)


def test_cost_on_sell_zero_holding_days(cfg_on: CBArbConfig):
    """holding_days=0 → no holding cost."""
    r = apply_cost_model(price=100.0, qty=10, side="sell", cfg=cfg_on,
                         avg_amount_5d=100000.0, holding_days=0)
    assert r["holding_cost"] == 0.0


def test_cost_on_sell_subtracts_all_costs(cfg_on: CBArbConfig):
    """sell cash = gross - fee - slippage - impact - holding_cost."""
    r = apply_cost_model(price=100.0, qty=10, side="sell", cfg=cfg_on,
                         avg_amount_5d=100000.0, holding_days=180)
    gross = 1000.0
    fee = gross * 0.0003
    slippage = gross * 0.0015
    impact = gross * 0.01  # capped
    holding = gross * 0.02 * (180 / 365)
    assert r["cash_amount"] == pytest.approx(gross - fee - slippage - impact - holding)


# ============ edge cases ============


def test_zero_qty_returns_zero(cfg_on: CBArbConfig):
    """qty=0 → gross=0, all costs=0."""
    r = apply_cost_model(price=100.0, qty=0, side="buy", cfg=cfg_on,
                         avg_amount_5d=100000.0)
    assert r["gross_amount"] == 0.0
    assert r["cash_amount"] == 0.0
    assert r["fee"] == 0.0
    assert r["slippage"] == 0.0
    assert r["market_impact"] == 0.0


def test_negative_price_clamped_to_zero(cfg_on: CBArbConfig):
    """Negative price → gross clamped to 0 (max(0.0, price*qty))."""
    r = apply_cost_model(price=-100.0, qty=10, side="buy", cfg=cfg_on,
                         avg_amount_5d=100000.0)
    assert r["gross_amount"] == 0.0
    assert r["cash_amount"] == 0.0


def test_cash_amount_never_negative(cfg_on: CBArbConfig):
    """sell with extreme costs → cash_amount clamped to 0, not negative."""
    extreme_cfg = CBArbConfig(
        cost_model_enabled=True,
        fee_pct=0.5, slippage_pct=0.5,
        market_impact_coeff=10.0, market_impact_cap_pct=0.5,
        holding_cost_pct=10.0,
    )
    r = apply_cost_model(price=100.0, qty=10, side="sell", cfg=extreme_cfg,
                         avg_amount_5d=100.0, holding_days=3650)
    assert r["cash_amount"] >= 0.0


def test_unknown_side_treated_as_buy(cfg_on: CBArbConfig):
    """side='whatever' falls through to else (buy semantics): adds costs."""
    r = apply_cost_model(price=100.0, qty=10, side="hold", cfg=cfg_on,
                         avg_amount_5d=100000.0)
    assert r["holding_cost"] == 0.0
    # Cash amount should be like buy: gross + fee + slippage + impact
    gross = 1000.0
    expected = gross + gross * 0.0003 + gross * 0.0015 + gross * 0.01
    assert r["cash_amount"] == pytest.approx(expected)


# ============ default CBArbConfig has cost_model fields ============


def test_default_config_has_cost_fields():
    """CBArbConfig defaults expose cost_model fields (added in promotion)."""
    cfg = CBArbConfig()
    assert hasattr(cfg, "cost_model_enabled")
    assert cfg.cost_model_enabled is False
    assert hasattr(cfg, "slippage_pct")
    assert hasattr(cfg, "market_impact_coeff")
    assert hasattr(cfg, "market_impact_cap_pct")
    assert hasattr(cfg, "holding_cost_pct")
