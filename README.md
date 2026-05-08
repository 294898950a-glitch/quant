# Quant — 自循环量化策略优化框架

一个**默认无人值守**的量化策略研究框架。把策略参数喂进去,它自己跑回测、看结果、出主意、改参数、再跑——循环不停。1 分钟内不回 telegram 才会自己重置参数继续,从不静默暂停。

## 框架核心(9 个角色)

```
1. 测试器 (verifier)        给参数模拟一次交易,出客观分数
2. 体检员 (sanity_checker)   跑测试前先校验参数+数据兼容(硬规则 + 事件触发 LLM)
3. 诊断员 (judge)            看一次结果,只描述事实(不出建议)
4. 审计员 (auditor)          看 N 轮趋势,判健康/挖数据/恶化(有否决权)
5. 出主意者 (hypothesizer)   LLM 看历史+诊断,出一条具体建议(死格式校验)
6. 编辑器 (editor)           唯一允许写参数文件的入口(范围+理由硬卡)
7. 记忆员 (memory)           每轮存档 + 防止 AI 重复试同一方向
8. 总指挥 (orchestrator)     状态机协调上述 7 个 + 自动恢复 + 控制接口
9. 市场画像员 (pool_stats)   算原始统计(不打标签),给 LLM 做参考
```

代码全在 `strategies/cb_redemption/`(历史命名,实际是框架 lib)。

## 当前在跑的策略

```
strategies/sp500_grid/    博时标普500 ETF (513500.SH) 网格策略
strategies/csi500_grid/   南方中证500 ETF (510500.SH) 网格策略
```

两个并行跑在 tencent-sig-vm 上,各自独立的 systemd 服务、隔离样本袋、telegram 推送。

## 边界(红黄绿)

```
🔴 红线 — 永远人工
   测试器代码、原始价格数据、评分函数、IS/OOS 切分逻辑

🟡 黄线 — 加新条目走 PR
   tunable_space.yaml 的结构(增/删/改字段)

🟢 绿线 — 系统自动
   tunable_space.yaml 内已登记条目的具体值
```

## 一轮循环

```
读参数 → 体检员校验 → 测试器跑回测 → 诊断员描述 → 记忆员存档 →
审计员看趋势 → 决策路由:
  veto → 自动恢复 3 步 → 还不行 → 推 telegram 想停下 → 1 分钟不回自动 shift
  stagnant 10 轮 → 同上
  healthy → 出主意者 LLM 出方向 → 编辑器写 → git commit → 推 outbox →
            cooldown 30 秒 → 下一轮
```

## 真停的 4 个条件

```
1. 隔离样本袋全用完(数据真没了)
2. AI 连续 5 轮想不出建议(参数空间真探完了)
3. SIGTERM / control.signal=stop(你显式停)
4. 进程崩 / 机器关
```

## 部署

服务跑在 tencent-sig-vm:

```
sp500-grid-orchestrator.service     主循环 daemon
sp500-grid-tg-relay.service         outbox.jsonl → telegram (label [sp500-grid])
csi500-grid-orchestrator.service
csi500-grid-tg-relay.service        label [csi500-grid]
```

DeepSeek API key 复用 `/root/.hermes/.env` 里的 `DEEPSEEK_API_KEY`。

## 数据

```
data/sp500_grid/raw/513500_daily.parquet     5+ 年 ETF 日线
data/sp500_grid/sealed_pools.json            隔离样本袋(8 池, OOS 2022-01 起)
data/sp500_grid/state.json                   循环状态机
data/sp500_grid/runs.jsonl                   每轮存档(参数+分数+诊断+改了啥)
data/sp500_grid/outbox.jsonl                 telegram 推送源
data/sp500_grid/tried_directions.jsonl       已尝试方向去重索引

data/csi500_grid/   同上
```

## 文档

```
docs/plans/2026-05-07-self-loop-roadmap.md     总规划 + 三色边界
docs/plans/2026-05-07-verifier-audit.md        前视污染审计(已修)
docs/plans/2026-05-07-holdout-pool-design.md   隔离机制设计
```

## 验证

```bash
.venv/bin/python -m pytest strategies/ -q       # 209+ 测试,新角色加进来一直涨
```
