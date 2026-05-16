# Quant — 量化策略研究项目

**AI 协作的量化策略研究系统**: Claude (本地, 架构/复盘) + Codex (云端 sig/spot, 跑回测) 协作, 用户管大方向. 长期目标: 最少钱 + 全自动找出能上实盘的策略.

---

## 新会话从这里开始

**只读这 3 个:**

| 顺序 | 文件 | 作用 |
|---|---|---|
| 1 | `docs/research_framework/CURRENT.md` | 当前策略状态 / 当前成绩 / 下一步等谁 |
| 2 | `data/research_framework/baseline_registry.md` | 历史成绩单 |
| 3 | `docs/INDEX.md` | 找其他文档和工具 |

需要查原因时再读 `docs/research_framework/experience_ledger.md`.

除上面 3 个入口文件外, 其他协议、角色、模板、复盘报告都是**非入口文件**, 不作为当前状态判断依据.

---

## 协议核心

详见 `docs/research_framework/protocol_redline.md`. 日常只记 6 条:

- 结论必须有数据来源.
- 重活只能在 VM 跑.
- 新研究任务必须有完整设计.
- 超过 10 分钟必须报进度.
- 改变策略真值必须更新 `CURRENT.md` + `baseline_registry.md`.
- 完成回测必须写 `run_manifest`.

---

## 当前活跃策略

详见 `docs/research_framework/CURRENT.md`. 当前状态 (2026-05-16):

| 策略 | status | 累计 excess (复利) | 备注 |
|---|---|---:|---|
| cb_arb 主策略 (转债套利) | WIP, 不达门槛 | -12.7% (cost-off) / -19.5% (cost-on) | 13 维参数空间大部分没系统调过 |
| cb_arb value-gap switch (评估分支) | WIP 加强版, 不达门槛 | -3.0% / -10.5% (cost-on) | 6 维 hyperparameter 基本没调 |

实盘可上的策略: **0 个**.

---

## 已归档策略

| 策略 | 归档原因 | 归档日期 |
|---|---|---|
| cb_redemption 真强赎 | 历史 framework 审计员 verdict=data_mining | 2026-05-06 |
| 网格策略 6 标的 (sp500/csi500/yzm/工行/神华/长电) | 2022-2026 中美股票市场对蓝筹股全跑不赢 + 股息 | 2026-05-09 |

详见 `EXPERIMENT_LOG.md` (第一次实验封档) + `reports/INDEX.md` 对应策略段.

---

## 历史自动调参框架

旧框架包含测试、体检、诊断、审计、出主意、编辑、记忆、总指挥等角色. 这些角色文档保留作参考, 但**不再是新会话入口**.

当前判断以 `CURRENT.md`、`baseline_registry.md`、`run_manifest` 为准.

---

## 真值记录系统

之前研究产出散在各处, 新会话容易看错策略状态. 现在有:

- `CURRENT.md` — 每策略当前状态, machine-readable YAML front-matter
- `baseline_registry.md` — 历史 baseline 成绩单, immutable-ish
- `run_manifests/` — 每次跑批 YAML 档案 (commit/dirty/config_hash/data_snapshot 都记)
- `experience_ledger.md` — 经验账本 4 分区 (已采用 / 已无效 / 未完成 / 未来)

协议 U15 强制 — RESPONSE 改策略真值必同 handoff 更新.

---

## 自动校验 (硬代码强约束)

提交涉及策略相关代码时, git pre-commit hook 自动跑 `scripts/framework_preflight.py`, 检查:
- CURRENT.md 每段 YAML front-matter 必填字段
- 每个 run_manifest YAML 必填字段
- 数据 warehouse 字段 (cb_daily / cb_call / stk_daily 等)
- 经验账本立项查重 (避免重复跑已 reject 方向)

校验不过 → block commit (--no-verify 是 escape hatch).

详见 `scripts/install_pre_commit_hook.sh` + 各 validator.

---

## 边界 (红黄绿)

```
🔴 红线 — 永远人工
   verifier 代码、原始价格数据、评分函数、IS/OOS 切分逻辑

🟡 黄线 — 加新条目走 PR
   tunable_space.yaml 的结构(增/删/改字段)

🟢 绿线 — 系统自动
   tunable_space.yaml 内已登记条目的具体值
```

---

## 计算资源

- **本地** (用户 WSL): Claude 跑这里, 只编辑文档 / 写小脚本 / 读代码, 不跑量化分析
- **sig VM** (`root@100.91.245.108`, 2 vCPU 长期开): Codex 跑这里, 轻分析 / 单点回测
- **spot VM** (`ins-5lb9zo12`, 16 vCPU SPOTPAID): 跑大批量回测时临时起, 跑完关
- 月预算: ¥90. 5 月 15 日用 ¥8 (3 batch spot + 多次 sig recon).

---

## 数据

主数据 `data/cb_warehouse/`:
- `cb_basic.parquet` 转债基本信息 (~1000 只)
- `cb_daily.parquet` 转债日行情 (~50 万行)
- `cb_call.parquet` 强赎事件 (~1000 次)
- `stk_daily.parquet` / `stk_daily_qfq.parquet` 正股行情 (~270 万行)

来源: tushare + 各 broker. 详见 `docs/data_source_summary.md`.

---

## 文档地图

详见 `docs/INDEX.md`. 它只负责找文件, 不负责判断当前状态. 当前状态只看 `CURRENT.md`.

---

## 验证

```bash
.venv/bin/python -m pytest strategies/ -q       # 主测试套件
python3 scripts/framework_preflight.py          # framework 校验
```

---

*更新于 2026-05-16. 上次大改: 加入 v1.5 协议 / 决策契约 / 真值记录系统 / 自动校验工具.*
