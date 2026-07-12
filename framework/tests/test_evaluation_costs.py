"""Tests for framework.evaluation.costs using unittest."""

from __future__ import annotations

import unittest

from framework.evaluation import costs


class TestCostConfig(unittest.TestCase):
    def test_default_disabled(self) -> None:
        cfg = costs.CostConfig(cost_model_enabled=False, fee_pct=0.001)
        result = costs.apply_costs(price=100.0, qty=10.0, side="sell", config=cfg)
        self.assertAlmostEqual(result["gross_amount"], 1000.0, places=6)
        self.assertAlmostEqual(result["fee"], 1.0, places=6)
        self.assertAlmostEqual(result["cash_amount"], 999.0, places=6)

    def test_buy_side(self) -> None:
        cfg = costs.CostConfig(cost_model_enabled=False, fee_pct=0.001)
        result = costs.apply_costs(price=100.0, qty=10.0, side="buy", config=cfg)
        self.assertAlmostEqual(result["cash_amount"], 1001.0, places=6)

    def test_enabled_cost_model(self) -> None:
        cfg = costs.CostConfig(
            cost_model_enabled=True,
            fee_pct=0.001,
            slippage_pct=0.001,
            holding_cost_pct=0.05,
        )
        result = costs.apply_costs(
            price=100.0, qty=10.0, side="sell", config=cfg, holding_days=365
        )
        self.assertAlmostEqual(result["gross_amount"], 1000.0, places=6)
        self.assertAlmostEqual(result["fee"], 1.0, places=6)
        self.assertAlmostEqual(result["slippage"], 1.0, places=6)
        self.assertAlmostEqual(result["holding_cost"], 50.0, places=6)
        self.assertAlmostEqual(result["cash_amount"], 948.0, places=6)


class TestGrossToNetCash(unittest.TestCase):
    def test_sell(self) -> None:
        result = costs.gross_to_net_cash(1000.0, fee_pct=0.001, slippage_pct=0.001)
        self.assertAlmostEqual(result["cash_amount"], 998.0, places=6)

    def test_buy(self) -> None:
        result = costs.gross_to_net_cash(
            1000.0, fee_pct=0.001, slippage_pct=0.001, side="buy"
        )
        self.assertAlmostEqual(result["cash_amount"], 1002.0, places=6)


if __name__ == "__main__":
    unittest.main()
