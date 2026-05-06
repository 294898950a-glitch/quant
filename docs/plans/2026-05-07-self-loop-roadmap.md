# 自循环回测优化框架 — 分期 Roadmap

> 上游讨论：双层循环、6 角色、边界 = 不能改 verifier、tunable_space.yaml 登记搜索空间、holdout 池防 OOS 污染。
> 现状：verifier 自评 4/10（前视偏差），样本 112 / 深修 51，自由度对 8 维 CMA-ES 严重不足。
> **结论**：P0 不完成，循环开了等于把垃圾分数算精细。

---

## P0 — 验证器重建 + 样本扩展（人工，不开循环）

### P0.1 修 verifier — 沿用已有 `strategies/cb_redemption/PLAN.md`
- Task 1: `data.py` 改纯本地 parquet 读取，删 akshare 实时依赖
- Task 2: 新增 `historical_data.py` — 历史快照工厂（严格时序）
- Task 3: 重写 `backtest.py` — 逐日遍历，因子只用 t 之前的数据
- Task 4: `optimizer.py` 接口保持不变
- Task 5: `snapshot_redeem.py` 改从仓库读

### P0.2 扩样本（`data/CLAUDE.md` 已列）
- 全量扫 1012 只转债（不止前 400）
- 加"首次公告日"为事件起点（vs 股东大会日）
- 加退市债历史

目标：深修事件 51 → 100+。

### P0.3 holdout 池
- OOS 切 N=4 块，每块 ~25 个事件
- 每块带 `read_count`，循环里 Hypothesizer/Judge 只能读 `read_count=0` 的块
- 读完即封（写 `sealed_pools.json`）
- 全部封死 → 暂停循环，等新数据进来

**P0 退出条件**
- backtest 框架评分 ≥7/10（重新跑 review_strategy）
- 深修事件 OOS 样本 ≥100
- holdout 池 4 块就绪 + 读取守卫已实现

---

## P1 — Orchestrator 上线（自动化循环）

### P1.1 `tunable_space.yaml`
搜索空间登记表，**唯一允许自动化修改的对象**：
```yaml
parameters:    # 数值参数 (CMA-ES 的搜索维度)
factors:       # 已注册的因子定义 + prior
rules:         # 可调的硬规则 (止损、持有期等)
```
加新条目走 PR + 人工 review；空间内的具体值由循环改。

### P1.2 6 角色实现
| 角色 | 怎么做 |
|---|---|
| Runner | 把 `backtest.py` 包成纯函数，输入 strategy+params，输出 BacktestResult |
| Tuner | 现 `optimizer.py`，CMA-ES inner loop |
| Judge | 扩 `review_strategy.py`：从读代码改为读 BacktestResult + 历史 baseline，输出 Diagnosis |
| Editor (内) | 只改 `tunable_space.yaml` 里的值 + `optimizer_baseline.json`，不碰 .py |
| Hypothesizer | **P1 阶段人工** — 只推 Telegram 通知，由 Jay 出 hypothesis 写进 yaml |
| Memory | git commit（每轮 message=hypothesis）+ Notion 行 + Telegram |

### P1.3 Orchestrator daemon
- 独立进程（systemd/launchd），跟 hermes 解耦
- Python long-running loop，**不调 LLM**（避开 reasoning_content 400 + V4 余额坑）
- 每轮结束 push 一条 Telegram → @my_clawd_vjrm_bot
- watchdog：连续 3 轮 OOS 无改进 → 暂停 + 告警

**P1 退出条件**
- inner 在 tunable_space 内自动收敛
- inner 收敛后 outer 暂停推 hypothesis 请求给你
- 每轮一个 git commit + 一条 TG 推送

---

## P2 — 半自动 Hypothesizer（可选，P1 跑 ≥3 个月后评估）

仅当：
- P1 已跑 ≥10 轮人工 hypothesis
- 这些 hypothesis 收敛方向有规律
- V4-pro 在重放这些 hypothesis 时能识别共性

才考虑让 LLM 接管。否则保留人工。

---

## P3 — Paper trading 接口（红线跨越点）

**永远人工**：
- 阈值决定（OOS Sharpe / hit rate 多少算"够")
- 切真账户
- 真实成交反馈如何（不）回流 verifier

不在循环内。

---

## 边界总览（一张图）

```
🔴 红线 — 永远人工
   backtest.py / unified_engine.py / 数据真值构建 / score 函数 / IS/OOS 切分

🟡 黄线 — 加新条目走 PR
   tunable_space.yaml 的结构（增/删/改字段）

🟢 绿线 — 全自动
   tunable_space 内的具体数值 + optimizer_baseline.json
```

---

## 起手点
P0.1 Task 1 — `data.py` 改纯 parquet 读取。机械重构，几十行，零争议。做完评估 Task 2-3 实际工作量。
