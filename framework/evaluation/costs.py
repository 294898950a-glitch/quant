"""Cost-model utilities wrapping the existing cb_arb verifier cost logic.

This module exposes a reusable, dataclass-based API while reusing the
implementation in `strategies.cb_arb.verifier.apply_cost_model`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class CostConfig:
    """Configuration for cost and slippage modeling.

    Fields mirror those used by `strategies.cb_arb.verifier.apply_cost_model`.
    """

    cost_model_enabled: bool = True
    fee_pct: float = 0.0
    slippage_pct: float = 0.0
    market_impact_coeff: float = 0.0
    market_impact_cap_pct: float = 0.0
    holding_cost_pct: float = 0.0

    def to_verifier_config(self) -> "_CBArbConfigStub":
        """Return a minimal object compatible with verifier.apply_cost_model."""
        return _CBArbConfigStub(
            cost_model_enabled=self.cost_model_enabled,
            fee_pct=self.fee_pct,
            slippage_pct=self.slippage_pct,
            market_impact_coeff=self.market_impact_coeff,
            market_impact_cap_pct=self.market_impact_cap_pct,
            holding_cost_pct=self.holding_cost_pct,
        )


class _CBArbConfigStub:
    """Minimal attribute container matching verifier.CBArbConfig expectations."""

    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


def apply_costs(
    price: float,
    qty: float,
    side: str,
    config: CostConfig,
    avg_amount_5d: float | None = None,
    holding_days: int | float = 0,
) -> dict[str, float]:
    """Return cash paid/received after fee, slippage, impact and holding cost.

    This is a thin wrapper around `strategies.cb_arb.verifier.apply_cost_model`.
    """
    from strategies.cb_arb.verifier import apply_cost_model

    return apply_cost_model(
        price=price,
        qty=qty,
        side=side,
        cfg=config.to_verifier_config(),
        avg_amount_5d=avg_amount_5d,
        holding_days=holding_days,
    )


def gross_to_net_cash(
    gross: float,
    fee_pct: float = 0.0,
    slippage_pct: float = 0.0,
    side: str = "sell",
) -> dict[str, float]:
    """Simplified cost helper for quick estimates without market impact.

    Returns a dict compatible with `apply_costs` output:
        cash_amount, gross_amount, fee, slippage
    """
    fee = gross * max(0.0, float(fee_pct))
    slippage = gross * max(0.0, float(slippage_pct))
    if side == "sell":
        cash = gross - fee - slippage
    else:
        cash = gross + fee + slippage
    return {
        "cash_amount": max(0.0, cash),
        "gross_amount": gross,
        "fee": fee,
        "slippage": slippage,
    }
