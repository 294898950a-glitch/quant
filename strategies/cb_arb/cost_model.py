"""交易成本模型 — 手续费/滑点/规模冲击/持仓成本.

从 strategies/cb_arb/verifier.py 拆出(见
docs/2026-09-01-cb-arb-verifier-decouple-spec.txt 组 D)。这里的成本假设只服务
于需要精细成本建模的评估脚本(如 scripts/evaluate_cb_arb_value_gap_switch.py);
verifier.py 自己的回测循环用的是内联的简化手续费, 不依赖这个模块。
"""

from __future__ import annotations

from typing import Any


def apply_cost_model(
    price: float,
    qty: float,
    side: str,
    cfg: Any,
    avg_amount_5d: float | None = None,
    holding_days: int | float = 0,
) -> dict[str, float]:
    """Return cash paid/received after fee, slippage and size-based impact."""
    gross = max(0.0, float(price) * float(qty))
    fee = gross * float(getattr(cfg, "fee_pct", 0.0))
    if not getattr(cfg, "cost_model_enabled", False):
        if side == "sell":
            return {"cash_amount": gross - fee, "gross_amount": gross, "fee": fee}
        return {"cash_amount": gross + fee, "gross_amount": gross, "fee": fee}

    slippage = gross * max(0.0, float(getattr(cfg, "slippage_pct", 0.0)))
    avg_amount = float(avg_amount_5d or 0.0)
    impact_pct = 0.0
    if avg_amount > 0 and gross > 0:
        impact_pct = float(getattr(cfg, "market_impact_coeff", 0.0)) * gross / avg_amount
        impact_pct = min(max(0.0, impact_pct), max(0.0, float(getattr(cfg, "market_impact_cap_pct", 0.0))))
    impact = gross * impact_pct
    holding_cost = 0.0
    if side == "sell":
        holding_cost = gross * max(0.0, float(getattr(cfg, "holding_cost_pct", 0.0))) * max(0.0, float(holding_days)) / 365.0
        cash_amount = gross - fee - slippage - impact - holding_cost
    else:
        cash_amount = gross + fee + slippage + impact
    return {
        "cash_amount": max(0.0, cash_amount),
        "gross_amount": gross,
        "fee": fee,
        "slippage": slippage,
        "market_impact": impact,
        "holding_cost": holding_cost,
        "impact_pct": impact_pct,
    }


__all__ = ["apply_cost_model"]
