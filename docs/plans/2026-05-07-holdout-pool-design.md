# Holdout 池机制设计

> 配套：`2026-05-07-self-loop-roadmap.md` P0.3 ／ 实现：`strategies/cb_redemption/holdout.py`

## 目标

OOS（样本外）数据是验证策略真实性能的唯一锚点。一旦 Hypothesizer / Judge / 调参循环反复读取同一份 OOS，OOS 在统计意义上就退化为 IS：每多读一次，参数空间就多向它过拟合一点。**Holdout 池机制**把 OOS 切成 N 块互斥子集，每块只允许被读一次，读完即封；当所有池都封死时循环必须暂停，等新数据到位才能继续。

## 数据模型 — `data/cb_redemption/sealed_pools.json`

```json
{
  "version": 1,
  "strategy": "cb_redemption",
  "split_at": "2025-01-01",
  "n_pools": 4,
  "seed": 42,
  "created_at": "2026-05-07T...Z",
  "pools": [
    {
      "id": 0,
      "event_ids": ["110095_2025-03-25", "..."],
      "read_count": 0,
      "first_read_at": null,
      "sealed_at": null
    },
    ...
  ]
}
```

`event_id` 默认采用 `bond_id + "_" + meeting_date`（`cb_pead_series.csv` 中天然唯一）。`split_at` 来自 backtest 现有的 IS/OOS 切分日，holdout 只切其中 ≥ split_at 的事件。

## API（`holdout.py` 暴露 4 个函数 + 3 个异常）

| 函数 | 作用 |
|---|---|
| `slice_oos_into_pools(events, ..., n_pools=4)` | 第一次切池子。`pool_file` 已存在则 raise `PoolFileExistsError`。打散后均匀分块（差不超过 1 个）。|
| `read_pool(pool_id)` | 返回该池的 `event_id` list。`read_count == 0` 时递增 + 写 `first_read_at`；否则 raise `PoolAlreadyReadError`。|
| `seal_pool(pool_id)` | 显式封池，写 `sealed_at`。多次调用幂等。|
| `pools_remaining()` | 返回 `read_count == 0` 的 pool_id list；空 list 表示循环须暂停。|

并发安全：`_load_pools` / `_save_pools` 用 `fcntl.flock` 包文件锁（仓库不在 Windows 跑，POSIX 即可）。

## 生命周期

```
未读 (read_count=0, first_read_at=None, sealed_at=None)
   │ read_pool(id)        — 唯一一次合法读取
   ▼
已读未封 (read_count=1, first_read_at=ts, sealed_at=None)
   │ seal_pool(id)        — 显式封；可幂等
   ▼
封死 (sealed_at=ts)
```

读已读池 → `PoolAlreadyReadError`。`pools_remaining()` 返回空 → 调用方（Orchestrator）必须暂停循环并触发"扩样本"工单。

## 守卫范围（重要）

守卫**只在 `holdout.read_pool()` 这唯一入口生效**。如果调用方绕路直接读 `cb_pead_series.csv` 切 OOS slice，没法机器拦截。约定写在本文档里、写在 `cb_redemption/CLAUDE.md` 里、Orchestrator 必须只通过 `read_pool` 拿 OOS event_ids。绕过 = 自爆，按"红线违例"处理。

## N 的选择 / 重切约束

- **第一版 `n_pools = 4`**。理由：当前深修 OOS 事件 ~30-40，N=4 时每池 ~8-10 个，统计噪声大但够"用一次扔掉"；等 P0.2 扩样本到 200+ 后每池 ~30-40 即可下结论。
- **已有 sealed_pools.json 后不允许重切**：重切会让"已读过的事件"重新分布到不同池里，破坏"该池只被读了 0 / 1 次"的统计承诺。要改 N 只能新建 strategy（或显式废弃 + 加版本号）。

## 跟 `tunable_space.yaml` 的关系

链式泄露陷阱：用 Pool A 的反馈调出新搜索维度，再用 Pool B 测同一维度 → Pool B 实质上仍是 Pool A 信息的延伸，不再独立。

约定：**`tunable_space.yaml` 的结构性改动（增/删字段）必须在读下一个池之前 commit + freeze**。具体数值在 frozen 空间内继续 CMA-ES 搜索不算泄露。Orchestrator 在 `read_pool` 前应校验 `tunable_space.yaml` 的 git hash 与 baseline 一致。

## 不在本期范围

- 真实切池子（等 P0.2 样本扩展完成后人工触发 `slice_oos_into_pools`）
- 与 `backtest.py` 的集成（P1 Orchestrator 的活）
- Pool 复用机制（按设计就是不能复用，封了就是封了）
