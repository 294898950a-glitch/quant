# 复盘报告索引 (按策略 + 日期分类)

**最后更新**: 2026-05-16

报告物理位置仍在 `reports/` 平铺 (避免破坏 35+ 处现有引用). 按策略找请看本索引.

---

## cb_arb (转债套利策略)

### 2026-05-10 历史

- [cb_arb_evaluation_2026-05-10.md](cb_arb_evaluation_2026-05-10.md) — 早期评估
- [cb_arb_recovery_2026-05-10.md](cb_arb_recovery_2026-05-10.md) — 恢复研究
- [cb_arb_rerun_holdout_fixed_20260510_144000_latest.txt](cb_arb_rerun_holdout_fixed_20260510_144000_latest.txt) — holdout 修复后重跑

### 2026-05-11 历史

- [cb_arb_cross_eval_retro_2026-05-11.md](cb_arb_cross_eval_retro_2026-05-11.md) — cross evaluation 复盘
- [cb_arb_framework_retro_2026-05-11.md](cb_arb_framework_retro_2026-05-11.md) — framework 复盘

### 2026-05-14 历史

- [cb_arb_csi_market_filter_2026-05-14.md](cb_arb_csi_market_filter_2026-05-14.md) — CSI500 同日大跌过滤 (reject)

### 2026-05-15 (panic detector / cross-validation 集中研究 11 篇)

**Round 5 + 跨年验证**:
- [cb_arb_round5_retro_2026-05-15.md](cb_arb_round5_retro_2026-05-15.md) — Round 5 reject 复盘

**panic detector 3 batch + 诊断**:
- [cb_arb_regime_switch_retro_2026-05-15.md](cb_arb_regime_switch_retro_2026-05-15.md) — self-PnL regime switch (reject)
- [cb_arb_market_breadth_panic_2026-05-15.md](cb_arb_market_breadth_panic_2026-05-15.md) — market breadth panic batch
- [cb_arb_market_breadth_panic_retro_2026-05-15.md](cb_arb_market_breadth_panic_retro_2026-05-15.md) — market breadth 复盘 (reject)
- [cb_arb_breadth_confirm_ensemble_2026-05-15.md](cb_arb_breadth_confirm_ensemble_2026-05-15.md) — breadth + pool-mean ensemble batch
- [cb_arb_breadth_confirm_ensemble_retro_2026-05-15.md](cb_arb_breadth_confirm_ensemble_retro_2026-05-15.md) — ensemble 复盘 (reject)
- [cb_arb_panic_diagnostic_2026-05-15.md](cb_arb_panic_diagnostic_2026-05-15.md) — 2024 真假 panic 诊断 (整子方向无效)

**trade-level + cross-validation**:
- [cb_arb_baseline_trade_diagnostic_2026-05-15.md](cb_arb_baseline_trade_diagnostic_2026-05-15.md) — trade-level 归因 (A 路径否决)
- [cb_arb_two_line_cross_validation_2026-05-15.md](cb_arb_two_line_cross_validation_2026-05-15.md) — HDRF vs 自循环路线 cross-validation
- [cb_arb_main_vs_prototype_recalibration_2026-05-15.md](cb_arb_main_vs_prototype_recalibration_2026-05-15.md) — 主策略 vs 评估版本校准 (cost realism wall)

---

## cb_redemption (强赎策略)

- [cb_redemption_state_assessment_2026-05-15.md](cb_redemption_state_assessment_2026-05-15.md) — 状态评估 (历史 audit data_mining, 不复活)

---

## 数据验证

- [cb_data_verification.md](cb_data_verification.md) — 数据源验证
- [cb_pricer_sanity_check_3.png](cb_pricer_sanity_check_3.png) — 定价器健全性检查 (图)

---

## 每日自治总结

- [autonomous_summary_2026-05-15.md](autonomous_summary_2026-05-15.md) — 2026-05-15 当天总结

---

## arxiv 候选 (在 data/research_framework/paper_candidates/)

- `data/research_framework/paper_candidates/2026-05-15.md` — 18 keyword 第一次检索, 0 篇过硬筛

---

## 维护规则

- 加新报告 → 同时更新本 INDEX.md 对应策略分类
- 报告文件物理位置不动 (`reports/foo.md`), 通过本索引按策略找
- 未来如果实际移动文件到子目录, 引用本索引 + 35+ 处现有引用需要同步更新
