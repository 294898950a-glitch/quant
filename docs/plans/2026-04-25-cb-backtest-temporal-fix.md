# 可转债强赎回测 — 时序对齐修复方案

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task. 也可由主Agent直接逐步完成。

## 问题诊断

当前 `BacktestEngine.run()` 的致命错误：

```
今日快照(2026-04-24) → 选到转债123256, 现价=195.7
                       ↓
_find_entry() 在全量日线(2年)中找close≈195.7的日期
                       ↓
找到2025-09-02 — 跟快照日期完全无关
                       ↓
用2025年9月的价格走势"验证"2026年4月的信号
                       ↓
得分43.36, 胜率100%  ← 虚假成绩
```

**根因**：快照是"当前时刻"的数据，但日线用了全部历史。两者在时间轴上没对齐。

修复思路：
1. 如果只有最新快照 — 只用快照日之后的日线来模拟持有（`_find_entry`只搜索快照日之后的close）
2. 如果有历史快照 — 严格时序回测（更彻底）

### 约束：目前只有最新快照

`get_cb_redeem_data()` 只返回当前时刻的数据，没有历史存储。所以采用方案1最务实：

> _find_entry() 只在"快照日期之后"的日线中搜索，且只使用该日期之后的close数据进行持有模拟。

这样至少保证：**用2026-04-24的信号，只验证2026-04-24之后的价格走势**。

但问题来了：`get_cb_redeem_data()` 不返回日期字段（它是实时抓取，不带时间戳）。需要在 `BacktestEngine.run()` 里显式指定快照日期。

## 决策

**短期修复**（今天的任务）：
1. 给 `BacktestEngine` 增加 `snapshot_date` 参数
2. `_find_entry()` 和 `_simulate_hold()` 只使用快照日期之后的日线
3. 更新 optimizer 兼容新接口
4. 实测验证

**长期升级**（后续任务）：
5. 设计历史快照存储 + 完整时序回测引擎（真正的T+0→T+N验证）

---

## 实施步骤

### 前置：确认日线数据的时间范围

已确认：akshare `bond_zh_hs_cov_daily()` 返回从上市到当天的全部日线。

### Task 1: 修改 BacktestEngine 增加 snapshot_date

**目标**：让引擎知道自己用的是哪天的快照，买入/卖出只使用该日期之后的日线。

**文件**：`~/projects/quant/strategies/cb_redemption/backtest.py`

**改动**：
1. `__init__` 增加 `snapshot_date: str | None = None` 参数（None = 今天）
2. `run()` 中：获取快照时记录日期 → 传入 _find_entry 和 _simulate_hold
3. `_find_entry()` 增加 start_idx 参数，只从快照日之后搜索
4. `_simulate_hold()` 从快照日之后开始计数

### Task 2: 更新 optimizer 适配

**目标**：optimizer 调用的 `BacktestEngine` 能正常初始化，snapshot_date 默认即可。

**文件**：`~/projects/quant/strategies/cb_redemption/optimizer.py`

**改动**：`BacktestEngine()` 调用不需要额外参数，因为默认 snapshot_date=None=今天。

（测试确认没有 breakage）

### Task 3: 实测验证

**目标**：跑一次完整优化，对比新旧结果。

**命令**：
```bash
cd ~/projects/quant && source .venv/bin/activate
python -m strategies.cb_redemption.optimizer --iterations 10
```

**预期**：由于只能使用快照日之后的日线数据，候选转债数可能持平（今天刚过去），但逻辑上正确了。

### Task 4: 更新 cron job 提示

**目标**：同步到记忆系统，确保下次对话时知道这个修复。

