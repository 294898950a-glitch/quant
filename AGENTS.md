# AGENTS.md (跨 AI 协议红线)

本文件是**任何 AI 工具** (Claude Code / Codex / Cursor / Aider / Continue /
GitHub Copilot / 直接 API 调用 / 等) 进入这个 repo 时必读的硬协议. 跟
CLAUDE.md 同构 (Claude Code 专属副本), 但本文件是 cross-AI 标准入口.

按用户 2026-05-17 提出 "换一个 AI 跑不了"批评: framework 不能绑死单一 AI 工具.

---

## Framework 分层硬约束 (4 道关)

1. **OS 文件系统层 (cross-AI 真硬)**:
   - `scripts/framework_watch_daemon.py` 后台 daemon, **监控整个 repo root**
     (用户 2026-05-17 简化建议: 不维护子目录白名单, 整 repo 监控 + 噪音 skip)
   - 跳过的噪音: `.git/`, `.venv/`, `__pycache__/`, `node_modules/`, `logs/`,
     `.claude/`, `archive/`, `.parquet`, `.csv`, `.pyc`, `.log`, `.pid`, etc
   - 任意文件修改 (任何 AI / 任何编辑器 / 任何用户) 都触发 framework_doc_check
   - 错了写到 `logs/framework_watch.log` + desktop notification (每次 validation 只
     notify 一次, 不刷屏)
   - **不依赖任何特定 AI 工具的 hook 机制**
   - 启动: `bash scripts/framework_watch_ctl.sh start`
   - 状态: `bash scripts/framework_watch_ctl.sh status` (异常退出后自动清 stale pidfile)

2. **AI 协议级 (cross-AI)**: 本文件 (AGENTS.md) + CLAUDE.md (Claude Code 副本).
   任何 AI 接入 repo 第一件事必须: 读这个文件, 理解 7 条红线, 然后才动手写代码.

3. **Claude Code 专属 hook (单一 AI 加强)**: `.claude/settings.local.json`
   PostToolUse hook → `scripts/post_tool_use_hook.sh` → framework_doc_check.
   只 work for Claude Code, 但对 Claude Code 用户多一层保护.

4. **Git pre-commit hook (最后关)**: `scripts/install_pre_commit_hook.sh` 装,
   commit 时跑 framework_preflight (5 道闸 + run_manifest + l4_ack + l5 +
   baseline + gatekeeper compliance), 防错的进仓库. **额外**: 当 `reports/*`
   staged 时单独跑 `validate_retro_report.py` (跟 framework_preflight 解耦,
   commit gate 不依赖 daemon freshness — daemon/hook 可能 down 或 bypass).

---

## 7 条红线

### 红线 1: 写文档后会触发实时检查

daemon 监控整 repo root, 任何文件改动都触发 framework_doc_check.py 看是不是
**受管路径**:

**受管路径** (路径推 schema):
- `reports/**/*.md` → retro 结构检查 (H1/日期/引用源)
- `data/<run-id>/spec.yaml` → spec schema + sentinel
- `data/<run-id>/l4_ack.yaml` → ack schema
- `data/<run-id>/diagnostic.yaml` → L5 reverse-diagnostic schema
- `data/research_framework/baseline_registry.yaml` → transition validator
- `data/research_framework/compute_budget_config.json` → budget schema
- `data/research_framework/run_manifests/*.yaml` → run manifest schema
- `docs/research_framework/*.md` → CURRENT.md / HDRF.md 一致性
- 其他路径 → skip

**AI 责任**: 看到 `logs/framework_watch.log` 新增 FATAL / Claude Code stderr
注入 / desktop notification 时, 立即**回到该文件修**, **不要带着错继续做判断**.

**验证 daemon 是否在跑**: `ls .framework-watch.pid && ps -p $(cat .framework-watch.pid)`

### 红线 2: 跑回测前 5 道闸 (before_run_grid)

任何 grid 跑批脚本跑批前必须接 `GateKeeper.before_run_grid(spec_path)`, 跑:
1. validate_spec.py (schema)
2. validate_data_schema.py (data warehouse)
3. validate_compute_budget.py (budget config)
4. research_sanity_checker.py (spec 语义)
5. ✓ 启动 grid

跑批脚本不接 GateKeeper = 违规. `validate_gatekeeper_compliance.py` 静态扫拦.

### 红线 3: 起新研究 batch 走 new_research.py

不要手动 `mkdir data/<run-id>/`. 必须用:

```
python3 scripts/new_research.py <strategy_id> <hypothesis_slug>
```

工具会: 查 ledger 防 STRONG MATCH 重复方向 + 自动填 schema + 写 sentinel.

### 红线 4: 不动 verifier.py / cost_model 核心 (红区)

`strategies/cb_arb/verifier.py` 和 cost_model 计算逻辑是核心策略代码,
**永远人工**. AI / spot / LLM 不动. 改这些必须用户授权 + 人工 review.

### 红线 5: 起 spot 前用户最后批

任何起 spot VM 跑批 (烧钱操作) 必须用户最后批准. AI / 任何外部 service
不自决起 spot.

### 红线 6: outbox 方向

- `claude/outbox.md` = Claude 写给 Codex (Codex 监听这里)
- `codex/outbox.md` = Codex 写给 Claude (Claude 监听这里)

WSL 路径: `/mnt/c/Users/陈教授/Desktop/ai/projects/quant/`. 写错方向 = 黑洞.

### 红线 7: 写报告前查 ledger

写新 retro / diagnostic / claim 之前, 必须跑 `scripts/search_ledger.py
<keywords>` 查相似方向是否已 reject. 漏看 ledger 直接开干 = 违规.

---

## 不同 AI 工具的额外入口文件

- Claude Code: `CLAUDE.md` (本文件同构副本, Claude Code 自动注入)
- Cursor: `.cursorrules` (可选, 跟本文件同步)
- Aider: `CONVENTIONS.md` (可选)
- 直接 API / 用户手写 prompt: 直接读 AGENTS.md (本文件)

**单一源**: 本文件 (AGENTS.md) 是权威源. CLAUDE.md / .cursorrules / 等是
副本, 改本文件后应该同步副本.

---

## 不在硬约束内的部分

工具能防的是**结构层** (50%): 字段存在 / 路径存在 / 引用源存在.
不能防:
- 内容语义错误 (把猫写成狗)
- 跨证据对账错误 (主策略 vs 改进版混淆)
- 方向判断 (要不要 archive cb_arb)

这些靠**人工 review** (Codex 跨证据对账 30% + 用户拍板 20%).
