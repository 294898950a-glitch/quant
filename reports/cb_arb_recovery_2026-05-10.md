# cb_arb VM Reflog Recovery

## Scope

- VM repo: `root@100.91.245.108:/root/projects/quant`
- Recovered run: iteration 1 at 2026-05-09 16:22:26 +0800 through iteration 240 at 2026-05-09 19:59:54 +0800
- Commits recovered: 239
- Source: `git reflog --date=iso --all`; original `data/cb_arb/runs.jsonl` remains unavailable.

## Artifacts

- `data/cb_arb/recovered/vm_git_reflog_raw.txt`
- `data/cb_arb/recovered/cb_arb_reflog_iter_1_240.jsonl`
- `data/cb_arb/recovered/cb_arb_recovery_trace_iter_1_240.jsonl`
- `data/cb_arb/recovered/cb_arb_iter_240_params_before_commit.json`
- `data/cb_arb/recovered/cb_arb_iter_240_params_after_commit.json`

## Recovery Quality

- exact: 206
- exact_no_edit: 1
- inferred_with_mismatch: 20
- partial_shrink_message: 3
- unresolved_recovery_attempt: 9

Exact JSON run records were not recoverable because `runs.jsonl` was untracked and overwritten.
Rows marked `unresolved_recovery_attempt` were recovery commits whose messages did not include concrete values.
Rows marked `inferred_with_mismatch` indicate that a later commit message provided a value that did not match the state reconstructed from earlier messages.

## Iteration 240 Parameters

The `before_commit` file is the best proxy for the parameters used by the 240th backtest row.
The `after_commit` file includes the iteration-240 LLM edit itself.

### Before Iteration 240 Commit

```json
{
  "rules": {
    "rating_floor_int": 2,
    "fee_pct": 0.0003,
    "initial_capital": 1000000
  },
  "thresholds": {},
  "weights": [
    50,
    1.3,
    0.05,
    0.8,
    0.015,
    20,
    90,
    -0.08,
    150000000,
    5000000,
    120,
    200
  ],
  "parameters_by_name": {
    "vol_window_days": 50,
    "vol_multiplier": 1.3,
    "rank_buy_pct": 0.05,
    "rank_sell_pct": 0.8,
    "max_position_pct": 0.015,
    "max_holdings": 20,
    "max_holding_days": 90,
    "stop_loss_pct": -0.08,
    "min_remaining_size": 150000000,
    "min_avg_amount": 5000000,
    "credit_spread_aaa_bp": 120,
    "credit_spread_aa_bp": 200
  }
}
```

### After Iteration 240 Commit

```json
{
  "rules": {
    "rating_floor_int": 2,
    "fee_pct": 0.0003,
    "initial_capital": 1000000
  },
  "thresholds": {},
  "weights": [
    50,
    1.3,
    0.05,
    0.8,
    0.015,
    20,
    90,
    -0.06,
    150000000,
    5000000,
    120,
    200
  ],
  "parameters_by_name": {
    "vol_window_days": 50,
    "vol_multiplier": 1.3,
    "rank_buy_pct": 0.05,
    "rank_sell_pct": 0.8,
    "max_position_pct": 0.015,
    "max_holdings": 20,
    "max_holding_days": 90,
    "stop_loss_pct": -0.06,
    "min_remaining_size": 150000000,
    "min_avg_amount": 5000000,
    "credit_spread_aaa_bp": 120,
    "credit_spread_aa_bp": 200
  }
}
```

## Gaps

- Unresolved recovery attempts: 9
- State/value mismatches: 20
