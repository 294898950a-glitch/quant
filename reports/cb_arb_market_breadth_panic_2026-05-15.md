<!-- l0-entry-id: 2 -->
<!-- protocol-redline-v1.3 -->

# cb_arb market breadth panic detector retro

Date: 2026-05-15

## Decision

Reject/archive first market-breadth panic detector grid.

The signal is more immediate than self-PnL rolling regime switch, but the tested
parameter family does not pass the main floors and true CV is only 3/6.

## Run

- Script: `scripts/evaluate_cb_arb_market_breadth_panic.py`
- Output: `data/cb_arb_market_breadth_panic_2026-05-15/`
- Grid: 162 candidates x 6 years = 972 candidate-year tasks
- Compute: Tencent spot `ins-5lb9zo12`, 16 workers
- Lookahead rule: T risk state uses T-1 breadth

## Results

- Candidates passing all 4 main floors: 0/162
- Best main-floor pass count: 2/4
- Main-floor pass-count distribution: 0 floors = 2, 1 floor = 18, 2 floors = 142
- CV selected top1 for every holdout: `breadth_dropm0p03_ratio0p2_rec3_h0p08_min1`
- CV selected holdout pass count: 3/6, adoption_pass=false

Selected top1 by year:

| Year | Selected excess | Baseline excess | Holdout improved | Floor pass |
| --- | ---: | ---: | --- | --- |
| 2019 | 0.150459 | 0.161312 | no | n/a |
| 2020 | -0.136525 | -0.130604 | no | yes |
| 2021 | -0.037862 | -0.050441 | yes | no |
| 2022 | 0.023088 | 0.014425 | yes | no |
| 2023 | -0.029066 | -0.031027 | yes | no |
| 2024 | 0.014018 | 0.030085 | no | n/a |

Risk days for selected top1:

| Year | Risk days |
| --- | ---: |
| 2019 | 4 |
| 2020 | 13 |
| 2021 | 6 |
| 2022 | 8 |
| 2023 | 0 |
| 2024 | 5 |

## Mechanism Notes

- 2020 timing is improved versus self-PnL regime switch: 2020-01-23 breadth signal becomes effective on 2020-02-03 because of the mandatory T+1 lag.
- The selected family repairs the 2020 floor but still degrades 2020 versus baseline.
- 2021/2022/2023 improvements are not enough to clear the hard floors.
- 2023 has zero selected risk days, so the 2023 result is effectively baseline-like and still below floor.

## Side Findings

- `data/cb_warehouse/cb_daily.parquet` is a usable breadth source for 2019-2024.
- Same-day T close breadth for T action would be lookahead; T+1 effective dating is required.
- Baseline `trades.csv` and `daily_equity.csv` are now exported, resolving the schema gap from the prior L5 review.

## Next Step

Do not adopt this grid. A follow-up spec would need a different signal shape, such as combining breadth with market index/vol/credit context or changing the action mapping, rather than just retuning the same breadth-only thresholds.
