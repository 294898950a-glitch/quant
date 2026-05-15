# Quant — 量化策略研究项目

**AI 协作的量化策略研究系统**: Claude (本地, 架构/复盘) + Codex (云端 sig/spot, 跑回测) 协作, 用户管大方向. 长期目标: 最少钱 + 全自动找出能上实盘的策略.

---

## 新会话从这里开始 (入口顺序)

| 顺序 | 文件 | 看完知道什么 |
|---|---|---|
| 1 | `docs/research_framework/CURRENT.md` | 当前每个策略状态 / 当前成绩 / 下一步等谁 |
| 2 | `docs/INDEX.md` | 所有文档地图 (协议 / 流程 / 角色 / 模板 / 报告) |
| 3 | `data/research_framework/baseline_registry.md` | 历史成绩单档案 |
| 4 | `docs/research_framework/experience_ledger.md` | 经验账本 (已采用 / 已无效 / 未完成 / 未来) |

只看这 4 个文件就知道项目当前状态. 协议红线 / 角色定义等其他文档按需翻 (在 `docs/INDEX.md` 的对应分类下).

---

## 协议核心 (v1.5, 17 条规则)

详见 `docs/research_framework/protocol_redline.md`. 关键铁律:

- **U1**: 结论必须指向数据 (CSV/parquet/报告), 不准编
- **U2**: 重活只能 VM 上跑, 本地只编辑文档
- **U12**: Claude↔Codex 辩论 ≤3 轮无共识, 听 Codex
- **U14**: Codex 任务 >10 分钟必每 10 分钟写 PROGRESS
- **U15**: RESPONSE 改策略真值必同 handoff 更新 `CURRENT.md` + `baseline_registry.md`
- **U16**: 8 状态机 + 16 转换 + 跑批必写 run_manifest

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

## 自动调参框架 (9 个角色)

```
1. 测试器 (verifier)        给参数模拟一次交易,出客观分数
2. 体检员 (sanity_checker)   跑测试前先校验参数+数据兼容
3. 诊断员 (judge)            看一次结果,只描述事实
4. 审计员 (auditor)          看 N 轮趋势,判健康/挖数据/恶化(有否决权)
5. 出主意者 (hypothesizer)   LLM 看历史+诊断,出建议(死格式校验)
6. 编辑器 (editor)           唯一允许写参数文件的入口
7. 记忆员 (memory)           每轮存档 + 防止 AI 重复试同一方向
8. 总指挥 (orchestrator)     状态机协调上述 7 个 + 自动恢复
9. 市场画像员 (pool_stats)   算原始统计(不打标签)
```

框架代码全在 `strategies/cb_redemption/`(历史命名, 实际是通用 framework lib). 跑过 cb_arb 60 iter + cb_redemption 5 iter, 都没找出能用 baseline.

---

## 真值记录系统 (2026-05-16 建立)

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

详见 `docs/INDEX.md`. 协议 / 流程 / 角色 / 模板 / 真值 / 报告 / 工具 全分类索引.

---

## 验证

```bash
.venv/bin/python -m pytest strategies/ -q       # 主测试套件
python3 scripts/framework_preflight.py          # framework 校验
```

---

*更新于 2026-05-16. 上次大改: 加入 v1.5 协议 / 决策契约 / 真值记录系统 / 自动校验工具.*
