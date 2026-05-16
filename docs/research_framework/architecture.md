# Quant research architecture

本文记录 repo 的工程分层, 只描述代码与文档边界, 不改变任何策略真值、baseline
数字、实盘状态或归档状态。

## Layers

| Layer | Current path | Contract |
|---|---|---|
| Strategy core | `strategies/cb_arb/` | cb_arb 稳定策略核心与 verifier surface. 一次性实验不得因为路径靠近而自动成为主策略真值。 |
| Research experiments | `scripts/evaluate_cb_arb_*.py`, `scripts/search_cb_arb_*.py` | 研究假设验证入口。脚本可以复用主策略底层函数, 但结果必须经过 spec / manifest / report / ledger 才能影响判断。 |
| Framework tools | `framework/` alias shell, compatibility source `strategies/cb_redemption/` | evaluator, judge, memory, holdout, orchestrator 等通用自循环工具。新代码可逐步引用 `framework.*`; 旧路径继续工作。 |
| Truth/history | `docs/research_framework/`, `data/research_framework/` | 当前真值、baseline、ledger、manifest、协议和模板。这里记录研究状态, 不承载交易执行逻辑。 |
| Collaboration shell | `C:/Users/陈教授/Desktop/ai/projects/quant/{claude,codex,state}.md` | Claude/Codex 通信与状态同步。它不是策略模块, 不进入 `strategies/`。 |

## Compatibility Rule

`strategies/cb_redemption/` 历史上同时承载强赎策略和通用研究框架。强赎策略已经
archived, 但目录里的 framework modules 仍被多个策略与脚本 import。

本轮只建立顶层 `framework/` 兼容壳:

- `framework.evaluator` 等模块 alias 到 `strategies.cb_redemption.evaluator` 等旧模块。
- 旧 import path 保持不变。
- 不物理移动文件。
- 不重写现有 evaluator / run_manifest entrypoint。
- 未来只有在所有旧引用稳定迁出后, 才考虑真实 move。

## Research Script Boundary

cb_arb 的研究脚本当前保留在 `scripts/`, 用
`docs/research_framework/experiments_index.md` 标记用途和边界。它们是 research
experiments, 不是 `strategies/cb_arb/verifier.py` 的主策略真值。

## Non-Goals

- 不修改 `strategies/cb_arb/verifier.py` 的交易逻辑。
- 不修改 cost model、baseline registry 或 CURRENT strategy status。
- 不 promote value-gap switch 或任何 prototype。
- 不运行 heavy backtest、spot 或 VM。
