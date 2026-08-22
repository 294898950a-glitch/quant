"""Strategy-neutral cost-model utilities.

The evaluation layer owns this arithmetic directly. It must not import a
strategy verifier, because generic evaluation should not depend on optional
strategy libraries such as scipy.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CostConfig:
    cost_model_enabled: bool = True
    fee_pct: float = 0.0
    slippage_pct: float = 0.0
    market_impact_coeff: float = 0.0
    market_impact_cap_pct: float = 0.0
    holding_cost_pct: float = 0.0


def apply_costs(
    price: float,
    qty: float,
    side: str,
    config: CostConfig,
    avg_amount_5d: float | None = None,
    holding_days: int | float = 0,
) -> dict[str, float]:
    """Return cash paid/received after fee, slippage, impact and holding cost."""
    gross = max(0.0, float(price) * float(qty))
    fee = gross * float(config.fee_pct)
    if not config.cost_model_enabled:
        cash = gross - fee if side == "sell" else gross + fee
        return {"cash_amount": cash, "gross_amount": gross, "fee": fee}
    slippage = gross * max(0.0, float(config.slippage_pct))
    avg_amount = float(avg_amount_5d or 0.0)
    impact_pct = 0.0
    if avg_amount > 0 and gross > 0:
        impact_pct = float(config.market_impact_coeff) * gross / avg_amount
        impact_pct = min(max(0.0, impact_pct), max(0.0, float(config.market_impact_cap_pct)))
    impact = gross * impact_pct
    holding_cost = 0.0
    if side == "sell":
        holding_cost = gross * max(0.0, float(config.holding_cost_pct)) * max(0.0, float(holding_days)) / 365.0
        cash = gross - fee - slippage - impact - holding_cost
    else:
        cash = gross + fee + slippage + impact
    return {
        "cash_amount": max(0.0, cash), "gross_amount": gross, "fee": fee,
        "slippage": slippage, "market_impact": impact,
        "holding_cost": holding_cost, "impact_pct": impact_pct,
    }


def gross_to_net_cash(
    gross: float,
    fee_pct: float = 0.0,
    slippage_pct: float = 0.0,
    side: str = "sell",
) -> dict[str, float]:
    """Simplified cost helper for quick estimates without market impact."""
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
