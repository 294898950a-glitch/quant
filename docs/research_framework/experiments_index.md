# cb_arb experiment index

本索引用于标记 `scripts/` 下 cb_arb research experiments 的边界。列入本文件不
代表策略采用、实盘可用或 baseline 真值变更。

## Boundary

- `strategies/cb_arb/verifier.py` 是当前主策略 surface。
- `scripts/evaluate_cb_arb_*.py` 是一次或一组研究假设的评估入口。
- `scripts/search_cb_arb_*.py` 是网格/切分/行为搜索入口, 必须继续通过
  GateKeeper 与 run_manifest 约束。
- 任何实验结果要影响 CURRENT / baseline_registry, 必须另有 spec、manifest、
  report/diagnostic 和用户或协议允许的决策链。

## Value-Gap / Valuation

- `scripts/evaluate_cb_arb_value_gap_switch.py`
- `scripts/evaluate_cb_arb_valuation_switch.py`
- `scripts/evaluate_cb_arb_three_value_gate.py`

## Panic / Breadth

- `scripts/evaluate_cb_arb_panic_bond_anchor.py`
- `scripts/evaluate_cb_arb_panic_option_stop.py`
- `scripts/evaluate_cb_arb_panic_option_weight.py`
- `scripts/evaluate_cb_arb_market_breadth_panic.py`
- `scripts/evaluate_cb_arb_breadth_confirm_ensemble.py`
- `scripts/search_cb_arb_panic_leave_year_out.py`
- `scripts/search_cb_arb_panic_mid_signal.py`

## Stop / Revaluation / Retention

- `scripts/evaluate_cb_arb_stop_revaluation.py`
- `scripts/evaluate_cb_arb_stop_source_stress.py`
- `scripts/evaluate_cb_arb_stop_value_retention.py`

## Regime / Behavior

- `scripts/evaluate_cb_arb_daily_regime_switch.py`
- `scripts/evaluate_cb_arb_normal_vol.py`
- `scripts/evaluate_cb_arb_regime_switch.py`
- `scripts/evaluate_cb_arb_selfpnl_regime_switch.py`
- `scripts/search_cb_arb_behavior_grid.py`
- `scripts/search_cb_arb_behavior_regimes.py`
- `scripts/search_cb_arb_time_split_grid.py`

## Baseline / Cross-Pool

- `scripts/evaluate_cb_arb_legacy.py`
- `scripts/evaluate_cb_arb_cross_pools.py`
