# CLAUDE.md (quant repo 协议红线)

本文件 Claude Code 会自动注入到每个 session. 这里只放**硬协议红线**, 不放
解释 / 教程 / 历史. 解释看 docs/research_framework/HDRF.md.

---

**重要**: framework 是 **cross-AI** (任何 AI 工具) 不是 Claude Code 专属.
实时检查靠 OS 层 daemon (任何 AI / 编辑器 / 用户改文件都触发), 不靠
Claude Code 特定 hook. (2026-05-17 用户指出 Claude Code hook 偏向单一 AI
违反 cross-AI 哲学已删.)

## 红线 1: 写文档后系统自动弹检查 (OS 层 daemon, cross-AI)

按用户 2026-05-17 提出: commit 时查不够, **写完那一刻系统自动弹**; AI 看到错警告就回去修. **不靠 AI 自觉调**.

**机制**:
- 后台 daemon (scripts/framework_watch_daemon.py) 监控整 repo, 1 秒
  poll, 任意文件修改 (任何 AI / 编辑器 / 用户) 都触发
- 自动调 `scripts/framework_doc_check.py`, 路径推 schema 找对应 validator
- 错了写 `logs/framework_watch.log` + desktop notification
- AI 看 log file 知道错了 (或被 notify-send 弹通知)

**受管路径** (自动按路径推 schema 调对应 validator):
- `reports/**/*.md` → 检查 retro 结构 (H1/日期/引用源)
- `data/<run-id>/spec.yaml` → schema + sentinel
- `data/<run-id>/l4_ack.yaml` → ack schema
- `data/<run-id>/diagnostic.yaml` → L5 reverse-diagnostic schema
- `data/research_framework/baseline_registry.yaml` → transition validator
- `data/research_framework/compute_budget_config.json` → budget schema
- `docs/research_framework/*.md` → CURRENT.md / HDRF.md / etc 一致性
- 其他路径 → silent skip

**AI 责任**: 看 `logs/framework_watch.log` 见 FATAL line 立即**回到该文件修**, **不要带着错继续做判断或写新东西**. 这是"AI 自救窗口" - 错了系统当场告诉你 (log + 可选 desktop notification), 你只要不假装没看见就 OK.

**违规判定**: AI 看到 log file FATAL 但忽略 / 继续基于错误内容做判断 = 违规.

**为什么不依赖 commit hook**: commit hook 是进仓库前最后关 (防止错写进仓库), 但 commit 之前 AI 已经基于错误内容做了判断 / 写了多份文档. 实时检查给 AI **自救窗口**: 错了当场知道当场修, 不带着错继续做.

**为什么用 daemon 不用 Claude Code hook**: cross-AI. daemon 任何 AI / 编辑器 / 用户改都触发, Claude Code hook 只对 Claude Code 一种 AI 有效. 不能把 framework 绑死单一 AI.

---

## 红线 2: 跑回测前 5 道闸 (before_run_grid)

任何 grid 跑批脚本 (scripts/run_cb_* / scripts/evaluate_cb_* / scripts/search_cb_*) 跑批前必须接 GateKeeper.before_run_grid(spec_path), 跑 5 道闸:
1. validate_spec.py (schema)
2. validate_data_schema.py (data warehouse)
3. validate_compute_budget.py (budget config)
4. research_sanity_checker.py (spec 语义)
5. (auto) ✓ 启动 grid

跑批脚本不接 GateKeeper = 违规. validate_gatekeeper_compliance.py 静态扫拦.

---

## 红线 3: 起新研究 batch 走 new_research.py

不要手动 mkdir data/<run-id>/ + cp spec_template.yaml. 必须用:

```
python3 scripts/new_research.py <strategy_id> <hypothesis_slug>
```

这个工具会:
- 检查 ledger 是否 STRONG MATCH 过往 reject 方向 (防重复证伪烧算力)
- 自动填 schema_version / run_id / date / status=DRAFT
- 写 <TODO: ...> sentinel 防偷过 validate_spec

---

## 红线 4: 不动 verifier.py / cost_model 核心 (红区)

`strategies/cb_arb/verifier.py` 和 cost_model 计算逻辑是核心策略代码, **永远人工**. AI / spot / LLM 不动. 改这些必须用户授权 + 人工 review.

唯一例外: 用户明示 + 数据支持的方向性改动 (e.g. 把 prototype 思路提升到主策略 yaml 绿区), 但仍需用户 commit by hand.

---

## 红线 5: 起 spot 前用户最后批

任何起 spot VM 跑批 (烧钱操作) 必须用户最后批准. AI / Codex 不自决起 spot. spec DRAFT + 本地 schema/sanity/budget 检查可自主推进, 但 grid / backtest / VM 一行命令都不发.

---

## 红线 6: outbox 方向

- `claude/outbox.md` = Claude 写给 Codex (Codex 监听这里)
- `codex/outbox.md` = Codex 写给 Claude (Claude 监听这里)

写错方向 = 黑洞. 实际路径在 WSL `/mnt/c/Users/陈教授/Desktop/ai/projects/quant/`.

---

## 红线 7: 报告内容写之前查 ledger

写新 retro / diagnostic / claim 之前, 跑 search_ledger 查相似方向是否已 reject. 漏看 ledger 就直接开干 = 违规 (实战已发生过 2 次, 工具拦下来了, 但应该 AI 主动查).
