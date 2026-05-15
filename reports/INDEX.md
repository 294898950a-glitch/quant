# 复盘报告索引 (自动生成)

**最后生成**: 2026-05-16 04:26 (由 `scripts/generate_indexes.py` 自动扫描生成)
**触发**: 加新报告后跑 `python3 scripts/generate_indexes.py` 重新生成

报告物理位置仍在 `reports/` 平铺 (避免破坏 35+ 处现有引用). 按策略找请用本索引.

---

## cb_arb 转债套利

### 2026-05-15

- [cb_arb_baseline_trade_diagnostic_2026-05-15.md](cb_arb_baseline_trade_diagnostic_2026-05-15.md)
- [cb_arb_breadth_confirm_ensemble_2026-05-15.md](cb_arb_breadth_confirm_ensemble_2026-05-15.md)
- [cb_arb_breadth_confirm_ensemble_retro_2026-05-15.md](cb_arb_breadth_confirm_ensemble_retro_2026-05-15.md)
- [cb_arb_main_vs_prototype_recalibration_2026-05-15.md](cb_arb_main_vs_prototype_recalibration_2026-05-15.md)
- [cb_arb_market_breadth_panic_2026-05-15.md](cb_arb_market_breadth_panic_2026-05-15.md)
- [cb_arb_market_breadth_panic_retro_2026-05-15.md](cb_arb_market_breadth_panic_retro_2026-05-15.md)
- [cb_arb_panic_diagnostic_2026-05-15.md](cb_arb_panic_diagnostic_2026-05-15.md)
- [cb_arb_regime_switch_retro_2026-05-15.md](cb_arb_regime_switch_retro_2026-05-15.md)
- [cb_arb_round5_retro_2026-05-15.md](cb_arb_round5_retro_2026-05-15.md)
- [cb_arb_two_line_cross_validation_2026-05-15.md](cb_arb_two_line_cross_validation_2026-05-15.md)

### 2026-05-14

- [cb_arb_csi_market_filter_2026-05-14.md](cb_arb_csi_market_filter_2026-05-14.md)

### 2026-05-11

- [cb_arb_cross_eval_retro_2026-05-11.md](cb_arb_cross_eval_retro_2026-05-11.md)
- [cb_arb_framework_retro_2026-05-11.md](cb_arb_framework_retro_2026-05-11.md)

### 2026-05-10

- [cb_arb_evaluation_2026-05-10.md](cb_arb_evaluation_2026-05-10.md)
- [cb_arb_recovery_2026-05-10.md](cb_arb_recovery_2026-05-10.md)
- [cb_arb_rerun_holdout_fixed_20260510_144000_latest.txt](cb_arb_rerun_holdout_fixed_20260510_144000_latest.txt)


## cb_redemption 强赎策略

### 2026-05-15

- [cb_redemption_state_assessment_2026-05-15.md](cb_redemption_state_assessment_2026-05-15.md)


## 每日自治总结

### 2026-05-15

- [autonomous_summary_2026-05-15.md](autonomous_summary_2026-05-15.md)


## 数据验证

- [cb_data_verification.md](cb_data_verification.md)
- [cb_pricer_sanity_check_3.png](cb_pricer_sanity_check_3.png)


---

## arxiv 候选 (在 data/research_framework/paper_candidates/)

- `data/research_framework/paper_candidates/2026-05-15.md`

---

## 自动生成规则

本文件由 `scripts/generate_indexes.py` 扫描 `reports/` 自动生成:
- 按文件名 prefix 分类 (cb_arb_* / cb_redemption_* / autonomous_summary_* / cb_data_* / cb_pricer_*)
- 按文件名内日期 (YYYY-MM-DD 或 YYYYMMDD) 排序倒序 + 分组
- 改分类规则在脚本里的 `classify_report()` + `REPORTS_STRATEGY_PREFIXES`
