# 可转债强赎策略 — 基于Tushare本地数据的严格时序重构

## 目标

将回测/优化从"今日快照×历史价格"（前视偏差）改为 "基于 Tushare Parquet 仓库的严格时序回测"，使优化器跑出来的分数可信。

## 架构变更

```
之前: ak.bond_cb_redeem_jsl() [实时快照] → signal_rank → match 历史日线找价格
之后: cb_call[历史强赎公告] + cb_daily[历史行情] → 逐日计算因子 → 生成真实信号
```

## 文件变更清单

### Task 1: 重构 `data.py` → 纯本地 Parquet 读取

- 删除所有 akshare 依赖
- 新增 `from_warehouse()` 统一接口，读取 `~/projects/quant/data/cb_warehouse/*.parquet`
- 保留向下兼容的函数签名（`get_cb_redeem_data()` 返回类似 JSL 格式的 DataFrame）
- 新增 `get_trade_calendar()` 从 cb_daily 提取交易日列表

### Task 2: 新增 `historical_data.py` — 历史数据工厂

新文件，负责：
- 从 cb_call + cb_daily 重建历史强赎快照
- 对每个交易日 t，构造"当时可获取的转债状态快照"
- 时序严格：因子计算只用 t 及之前的数据

### Task 3: 重写 `backtest.py` — 严格时序回测

- 不再用 `get_cb_redeem_data()` 获取今日快照
- 改为：遍历历史交易日，每天计算一次信号
- 因子计算：
  - `redeem_progress`: 从 cb_call 的 `is_call='已满足强赎条件'` 公告倒推
  - `premium_ratio`: 从 cb_daily 的 `cb_over_rate` 字段直接读取
  - `remaining_size`: 从 cb_basic 读取
  - `stock_momentum`: 从 cb_daily 的 pct_chg 滚动计算
  - ~~`market_sentiment`: 全市场等权平均~~ → 缓存化或移除
- 交易模拟：严格按日内逻辑（信号日买入，后续跟踪）

### Task 4: `optimizer.py` — 几乎不变

- 只改导入：`from backtest import run_backtest` 仍保持兼容接口
- 无需改搜索逻辑、基线持久化、TG推送

### Task 5: `snapshot_redeem.py` — 改为从仓库生成当日快照

- 不再调用 ak.bond_cb_redeem_jsl()（网络不稳定）
- 改为从 cb_daily 最新交易日 + cb_basic 构造当日状态
- 保持输出格式一致

## 执行计划

分解为可独立验证的子任务：

### Sub-task 1: data.py 瘦身
- 删除 akshare 导入/回退逻辑
- 新增 `WAREHOUSE_DIR` 指向 `~/projects/quant/data/cb_warehouse`
- 新增 `_load_parquet(name: str) -> pd.DataFrame` 带 LRU 缓存
- 提供 `get_trade_calendar() -> list[str]`
- 保留 `get_cb_redeem_data()` 接口签名，返回从仓库构造的"当日状态快照"
- 验证：`python -c "from strategies.cb_redemption.data import get_cb_redeem_data; df=get_cb_redeem_data(); print(len(df), list(df.columns)[:10])"`

### Sub-task 2: 新增 historical_data.py — 时序数据工厂
- `RebuildHistoricalSnapshot` 类
- 输入：交易日范围（如 2023-01-01 ~ 2026-04-24）
- 对每个交易日 t：
  - 从 cb_daily 取截至 t 的所有数据
  - 从 cb_basic 取转股价、剩余规模
  - 从 cb_call 取公告(唯一值，用作进度推算)
- 输出：DataFrame of (date, ts_code, cb_close, premium_ratio, redeem_progress, remaining_size, stock_momentum)
- 验证：输出 csv 看样例

### Sub-task 3: 重写 backtest.py — 严格时序回测
- 遍历 historical_data 的每一天
- 每日常规计算信号 → 过滤 → 开仓/持仓管理
- 输出 trade records + performance
- 验证：跑一次看 trade 记录是否合理（日期、价格合理）

### Sub-task 4: 验证 optimizer.py 功能完整
- 跑一次 10 次迭代 + baseline
- 确认持久化、TG 推送正常

### Sub-task 5: 创建 cronjob 每 10 分钟优化
- 优化周期跑 15-20 次迭代（因为 10 分钟要出结果）
- 改 THRESHOLD_CANDIDATES 和搜索范围为偏小（跑得快）
- 链接 TG 推送通知

## 数据格式

### get_cb_redeem_data() 返回格式（与 JSL 兼容）

| 列名 | 来源 | 说明 |
|------|------|------|
| `代码` | cb_basic.ts_code | 如 113504.SH |
| `债券简称` | cb_basic.bond_short_name | |
| `现价` | cb_daily.close | 最新交易日 |
| `正股价` | 暂缺（不作要求） | 设为 1.0 |
| `转股价` | cb_basic.conv_price | |
| `正股代码` | cb_basic.stk_code | |
| `强赎天计数` | 推算 | 从 cb_call 距离最近公告 |
| `强赎状态` | cb_call.is_call | 最近一条 |
| `剩余规模` | cb_basic.remain_size | |
| `转股溢价率` | cb_daily.cb_over_rate | 最新交易日 |
| `市值_updated_at` | - | 时间戳 |
