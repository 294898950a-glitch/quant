# cb_arb breadth + pool-mean confirm ensemble

Date: 2026-05-15

## Scope

- Evaluator: `scripts/evaluate_cb_arb_breadth_confirm_ensemble.py`
- Spec: `data/cb_arb_breadth_confirm_ensemble_2026-05-15/spec.md`
- Output: `data/cb_arb_breadth_confirm_ensemble_2026-05-15/`
- Grid: 108 candidates x 6 holdout years = 648 candidate-year tasks
- Hard floors v1.1: 2020 >= -0.130604, 2021 >= -0.050441, 2022 >= 0.014425, 2023 >= -0.031027
- Signal rule: breadth condition AND pool-mean confirm on signal date T, effective action on T+1 trading day

## Result

- VM run completed on `root@100.91.245.108`; local heavy backtest was not used.
- Artifacts produced: `ranked.csv`, `summary.csv`, `selected.csv`, `trades.csv`, `daily_equity.csv`, `trigger_dates.csv`, `breadth_daily.csv`, `pool_mean_daily.csv`, `run_summary.json`, `vm_20260515_1339.log`.
- Candidate floor result: 0 / 108 candidates passed all 4 floors.
- Floor pass-count distribution: 2 floors = 12 candidates, 3 floors = 96 candidates.
- CV selected top1 result: 3 / 6 holdouts improved or equaled baseline, adoption_pass=false.

## Selected CV Holdouts

| holdout | selected candidate | selected excess | baseline excess | improved |
| --- | --- | ---: | ---: | --- |
| 2019 | `breadth_dropm0p03_ratio0p2_rec3_h0p08_min1_confirmm0p005` | 0.150459 | 0.161312 | no |
| 2020 | `breadth_dropm0p03_ratio0p2_rec3_h0p08_min1_confirmm0p005` | -0.136525 | -0.130604 | no |
| 2021 | `breadth_dropm0p03_ratio0p2_rec3_h0p08_min1_confirmm0p005` | -0.037862 | -0.050441 | yes |
| 2022 | `breadth_dropm0p03_ratio0p2_rec3_h0p1_min1_confirmm0p005` | 0.019834 | 0.014425 | yes |
| 2023 | `breadth_dropm0p03_ratio0p2_rec3_h0p08_min1_confirmm0p005` | -0.029066 | -0.031027 | yes |
| 2024 | `breadth_dropm0p03_ratio0p2_rec3_h0p08_min1_confirmm0p005` | 0.014018 | 0.030085 | no |

## Key Diagnostics

- Pool-mean confirm did not rescue the mechanism. The selected CV pass rate remains 3 / 6, same headline pass count as breadth-only.
- 2024 false-positive dates were not filtered by the confirm signal. The selected candidate still triggered on 2024-01-22, 2024-02-05, 2024-02-28, 2024-06-24, and 2024-10-09 because pool mean was also below the loose `-0.005` confirm threshold on all five dates.
- Stricter breadth variants reduced 2024 risk days to 1-2 in the best rows, but they damaged 2020 materially (`-0.178799` or worse), so no candidate all-passed floors.
- The best floor-pass rows reached only 3 / 4 floors and still failed the 2020 floor.

## Recommendation

Do not adopt this breadth + pool-mean confirm ensemble. It does not meet the >= 5/6 holdout adoption gate and has 0 / 108 candidates passing all four recalibrated hard floors. The confirm signal is valid and non-lookahead, but it is not discriminative enough to remove the 2024 false-positive damage without sacrificing 2020.
