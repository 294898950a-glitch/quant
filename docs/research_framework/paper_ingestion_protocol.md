# L0 第 4 入口: arxiv 论文导入

> **本文件: 描述性参考, 非机器约束源**.
> 真正的机器强制规则在 `scripts/validate_*.py` + `data/research_framework/*.yaml`.
> 本文件解释"为什么这么做" + 历史背景, 不写"必须如何".

来源: 2026-05-15 用户在架构审查中要求增加, 防止 AI 只在自己经验账本里打转, 拿不到学术新方向.

## 筛选规则 (硬, v3.3 修正: 任何年份硬筛相同, 2025+ 只排序优先)

**核心铁律**: 任何论文必须满足下面**任一**硬条件 (不分年份):
- 原文明确有"已经试盘"段(live trading, paper trading ≥ 6 个月, 含真实 Sharpe / max drawdown)
- 引用数 ≥ 50(Google Scholar 或 Semantic Scholar 计)
- 被顶会 / 顶刊收录(NeurIPS / ICML / KDD / JFQA / RFS / JoF / Quant Finance)
- 被知名机构 / 量化团队 在博客 / GitHub / 公开 repo 复现并 ≥ 100 stars

**年份只影响排序优先级, 不影响硬筛**:
| 发表时间 | 优先级 |
|---|---|
| 2025-01-01 以后 | 高 (同分排序靠前) |
| 2020-01-01 - 2024-12-31 | 中 |
| 2020 前 | 低 (硬筛门槛升级: 引用要 ≥100, 复现 stars ≥300) |

**Codex schema 硬化** (协议红线 U13):
- 候选 markdown 文件里每条必含字段:
  - `passed_rule: 试盘|引用|顶会|复现`
  - `evidence: <具体数据, e.g. "Sharpe 1.8 live 2022-08 to 2024-12" / "Cited 73 (Semantic Scholar)" / "NeurIPS 2024 accepted" / "github.com/X/Y 187 stars">`
  - `priority: 高|中|低` (按发表年份派生)
- 任一字段缺失或值非法 → Codex 自动剔除该条

不收(任一即排除):
- 不满足上面 4 个硬条件任一
- 仅 arxiv 出现 + 引用 < 5 + 发表 ≥ 1 年(冷文)
- 纯刷 benchmark / leaderboard, 无 OOS 数据
- 纯理论推导, 无实证 + 无可验证假设
- 标题党(摘要号称 SOTA 但数据集只跑某个特殊样本)
- 不开源不复现 + 作者机构无信誉(无 LinkedIn / 无 lab page)

## 操作流程

**触发**:
- 用户主动: "去 arxiv 找新想法"
- 自动: 模式 B 启动前(经验账本"未完成线索"剩 ≤ 2 条时), 触发一次论文检索
- 定时: 每 14 天一次(可调)

**关键词**: 维护一份 `data/research_framework/paper_interest_keywords.txt`(Claude+Codex 共同维护), 默认:
- convertible bond arbitrage
- volatility regime switching
- panic detection equity
- credit spread quant strategy
- mean reversion convertible
- quant strategy live trading
(根据当前研究方向滚动调整)

**执行人**: Codex(在 sig VM 上, 用 arxiv API + Semantic Scholar API)

**输出**: `data/research_framework/paper_candidates/<YYYY-MM-DD>.md`, 格式:

```markdown
# arxiv 论文候选 YYYY-MM-DD

## 通过筛选 (≥ 1, ≤ 5 篇)

### Paper 1: <标题>

- arxiv id / DOI: ...
- 发表日期: ...
- 通过哪条规则: 2025+ / 2020-2024 + 试盘 / 2020-2024 + 引用 50+ / ...
- 引用数: ...
- 作者机构: ...
- 试盘证据 (原文摘录): ...
- 一句话假设 (Claude 拟): "X 信号 / 方法能改善 Y 目标"
- 移植到本仓库的难度: 易 / 中 / 难
- 跟我们当前 cb_arb 经验账本的关系: 增强 / 替代 / 正交

### Paper 2: ...

## 未通过筛选 (留痕, 防止重复检索)

- Paper X: 引用 3, 2023 发表, 无试盘 → 不收
- ...
```

**Claude 处理**: 看候选文件 → 挑 0 - 2 篇值得做的 → 把"一句话假设"作为新 L0, 写进经验账本"未完成线索"分区

**用户处理**:
- 模式 A: 用户审 paper_candidates 文件, 拍板做哪一篇
- 模式 B: 可做新论文方向, 但必须服务 `当前策略`, 先写设计, 用 `scripts/estimate_compute_budget.py` 算预算; 预计 ≤ ¥100 可继续, 否则等用户回来.

## 跟其他入口的关系

L0 现在有 4 个入口:

1. 用户主动: 用户一句话假设
2. 用户给方向: 用户描述问题 → Claude+Codex 协作出候选 → 用户挑
3. 经验账本: 模式 B 从"未完成线索"按优先级挑
4. **arxiv 论文 (本入口)**: Codex 检索 → Claude 挑题 → 用户审

入口 1, 2 是 push(用户给), 入口 3, 4 是 pull(AI 找).

## 升级到 L1 的前置

任何入口的 L0 想法, 进 L1 前必须:

- 查经验账本"已确认无效"区 → 重叠则丢弃
- 查"未完成线索"区 → 重叠则合并不另起
- 跟 yaml 黄 / 绿区参数对比 → 重叠则改成参数微调走自动循环
- 论文入口额外: 复现作者 Sharpe 在我方数据(L1.5 数据预检之前再加一步"复现检查")
