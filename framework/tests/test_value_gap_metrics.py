from __future__ import annotations

from scripts import evaluate_cb_arb_value_gap_switch as value_gap


def test_value_gap_metrics_emit_annualized_daily_equity_sharpe(monkeypatch):
    monkeypatch.setattr(value_gap, "_index_total_return", lambda *_: 0.0)
    metrics = value_gap._metrics(
        [("20250101", 100.0), ("20250102", 101.0), ("20250103", 100.5), ("20250104", 102.0)],
        [],
        100.0,
    )
    assert isinstance(metrics["sharpe"], float)
    assert metrics["sharpe"] != 0.0


def test_value_gap_metrics_empty_curve_keeps_numeric_sharpe(monkeypatch):
    monkeypatch.setattr(value_gap, "_index_total_return", lambda *_: 0.0)
    assert value_gap._metrics([], [], 100.0)["sharpe"] == 0.0
