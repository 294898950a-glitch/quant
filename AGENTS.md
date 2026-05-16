# AGENTS.md (跨 AI 协议红线)

本文件是**任何 AI 工具** (Claude Code / Codex / Cursor / Aider / Continue /
GitHub Copilot / 直接 API 调用 / 等) 进入这个 repo 时必读的硬协议. 跟
CLAUDE.md 同构 (Claude Code 专属副本), 但本文件是 cross-AI 标准入口.

按用户 2026-05-17 提出 "换一个 AI 跑不了"批评: framework 不能绑死单一 AI 工具.

---

## Framework 保护层

1. **自动基础设施**:
   - `scripts/framework_watch_daemon.py` 后台 daemon, **监控整个 repo root**
   - 跳过的噪音: `.git/`, `.venv/`, `__pycache__/`, `node_modules/`, `logs/`,
     `.claude/`, `archive/`, `.parquet`, `.csv`, `.pyc`, `.log`, `.pid`, etc
   - 任意文件修改 (任何 AI / 任何编辑器 / 任何用户) 都触发 framework_doc_check
   - 错了写到 `logs/framework_watch.log` + desktop notification (每次 validation 只
     notify 一次, 不刷屏)
   - **不依赖任何特定 AI 工具的 hook 机制**
   - 启动: `bash scripts/framework_watch_ctl.sh start`
   - 状态: `bash scripts/framework_watch_ctl.sh status` (异常退出后自动清 stale pidfile)

2. **AI 协议级 (cross-AI)**: 本文件 (AGENTS.md) + CLAUDE.md (Claude Code 副本).
   任何 AI 接入 repo 第一件事必须: 读这个文件, 理解 6 条红线, 然后才动手写代码.

3. **Git pre-commit hook (最后关)**: `scripts/install_pre_commit_hook.sh` 装,
   commit 时跑 framework_preflight (5 道闸 + run_manifest + l4_ack + l5 +
   baseline + gatekeeper compliance), 防错的进仓库. **额外**: 当 `reports/*`
   staged 时单独跑 `validate_retro_report.py` (跟 framework_preflight 解耦,
   commit gate 不依赖 daemon freshness — daemon 可能 down 或 bypass).

(原方案有"Claude Code PostToolUse hook"层, 2026-05-17 用户指出违反 cross-AI
哲学已删. 别的 AI 工具没有这种 hook 机制, 加它等于偏向单一 AI.)

---

## 自动基础设施

实时检查已经做成自动启动的基础设施, 不再作为 AI 自觉执行的红线条款。

daemon 监控整 repo root, 任何文件改动都触发 framework_doc_check.py 看是不是受管路径。

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

**AI 责任**: 看到 `logs/framework_watch.log` 新增 FATAL line 或 desktop
notification 时, 立即**回到该文件修**, **不要带着错继续做判断**.

安装自动启动: `bash scripts/install_framework_watch_autostart.sh`

状态: `bash scripts/framework_watch_ctl.sh status`

状态文件: `logs/framework_watch_status.json`

## 6 条红线

### 红线 1: 跑回测前 5 道闸 (before_run_grid)

任何 grid 跑批脚本跑批前必须接 `GateKeeper.before_run_grid(spec_path)`, 跑:
1. validate_spec.py (schema)
2. validate_data_schema.py (data warehouse)
3. validate_compute_budget.py (budget config)
4. research_sanity_checker.py (spec 语义)
5. ✓ 启动 grid

跑批脚本不接 GateKeeper = 违规. `validate_gatekeeper_compliance.py` 静态扫拦.

### 红线 2: 起新研究 batch 走 new_research.py

不要手动 `mkdir data/<run-id>/`. 必须用:

```
python3 scripts/new_research.py <strategy_id> <hypothesis_slug>
```

工具会: 查 ledger 防 STRONG MATCH 重复方向 + 自动填 schema + 写 sentinel.

### 红线 3: 研究代码可自动改, 实盘主路径不自动升级

AI 可以自动改研究脚本、评估脚本、新数据处理脚本、原型策略逻辑和研究参数。

不能自动做:
- 把原型提升为当前主策略真值
- 把策略标成可实盘
- 永久归档当前策略
- 改协议红线
- 复活已确认无效方向

生产主路径的核心定价、成本、切分逻辑如果只是研究原型, 可以改; 如果要替换当前主策略或成为实盘依据, 必须用户拍板.

### 红线 4: 预算计算后自动执行

新数据、研究代码、研究参数都不单独卡用户。先用 `scripts/estimate_compute_budget.py` 算预算:

- 预计 ≤ ¥100: 可以自动继续, 包括远端或 spot 执行.
- 预计 > ¥100: 等用户.
- 算不出来: 等用户.

远端或 spot 不是单独红线。是否能跑只看预算计算结果和研究设计是否通过.

### 红线 5: outbox 方向

- `claude/outbox.md` = Claude 写给 Codex (Codex 监听这里)
- `codex/outbox.md` = Codex 写给 Claude (Claude 监听这里)

WSL 路径: `/mnt/c/Users/陈教授/Desktop/ai/projects/quant/`. 写错方向 = 黑洞.

### 红线 6: 写报告前查 ledger

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
