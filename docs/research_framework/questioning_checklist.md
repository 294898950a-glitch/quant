# HDRF L4 自动质疑清单 — 5 项标准检查

每次 grid 跑完出 `ranked.csv`, Claude 必须跑这 5 项检查再决定是否进入 L5 数据论证. **少一项就有可能错收一个表面通过的假信号**.

来源: 2026-05-14 cb_arb CSI500 市场过滤器研究里, Claude 用 5 个质疑 (Q1-Q5) 暴露了"66/480 漂亮通过"背后的 8 个真策略 × 8 个参数冗余, 以及 medium signal 在 2021 自身就 fail floor 的更根本问题. 5 项抽出来作为标准.

---

## Q1. 硬约束 binding 检查 — Floor 是不是平凡通过?

**问题**: 如果几乎所有候选都过 floor (比如 90% 通过), 说明 floor 太松, 这次 grid 没真正区分好坏.

**检查方法**:
```python
pass_2020_only = (df.replay_2020_excess >= FLOOR_2020) & (df.replay_2021_excess < FLOOR_2021)
pass_2021_only = (df.replay_2020_excess < FLOOR_2020) & (df.replay_2021_excess >= FLOOR_2021)
pass_both = both floors
fail_both = neither
```

**判定**:
- 如果 `pass_both / total > 50%` → floor 太松, 不是有区分力的约束
- 如果 `pass_2020_only` 或 `pass_2021_only` ≈ 0 → 对应 floor 完全冗余, 可以删
- 如果 `pass_2020_only > 0 AND pass_2021_only > 0` → 两个 floor 都 binding, 健康

**实例** (cb_arb csi market filter):
- pass_both = 66
- pass_2020_only fail_2021 = 120 (说明 2021 floor 显著起作用, 不冗余)
- pass_2021_only fail_2020 = 174

---

## Q2. CV 严格性 — 不同 leave 候选集是不是完全一样?

**问题**: 如果 6 个 leave-N-out 出来的"通过候选"集合完全相同, 那 leave-N-out 没起 cross-validation 作用 — 只是用同一硬约束反复套同一文件.

**检查方法**:
```python
hard_pass_sets = {Y: set(top_candidates(leave_Y)) for Y in years}
intersect = reduce(lambda a, b: a & b, hard_pass_sets.values())
union = reduce(lambda a, b: a | b, hard_pass_sets.values())
identity_rate = len(intersect) / len(union)
```

**判定**:
- 如果 `identity_rate = 1.0` → 6 个 leave 完全一样, 不是真 CV, 必须走"真 CV": **用 leave-Y 训练 (排除 Y), 在 Y 评估**
- 如果 `identity_rate > 0.8` → 警告, CV 力度有限

**实例** (cb_arb csi market filter):
- 6 个 leave-N-out 全是 66 个一模一样 → 完全 fail 严格性
- 必须走真 CV: 用每个 leave-Y 的 top1, 看它在 Y 年表现, 结果 2 胜 2 平 2 负

---

## Q3. Top 候选同质性 — N 个 top 是不是 K 个真策略 × M 个参数冗余?

**问题**: 表面上 66 个候选, 但他们的 trade-level / metric tuple 可能只有 8 种真正不同 — 剩下都是参数包装的等价物.

**检查方法**:
```python
key_metrics = ['replay_2020_excess', 'replay_2021_excess', 'replay_2025_excess', 
                'replay_2020_trades', ...]
distinct_tuples = df_hard_pass.groupby(key_metrics).size()
```

**判定**:
- 如果 `distinct_tuples / total ≤ 0.2` → 同质性高, 80% 候选是参数冗余, 实际只有少数真策略
- 报告: 总候选 = K 个真策略 × M 个参数冗余

**实例** (cb_arb csi market filter):
- 66 个 hard-pass → 只有 8 个 distinct metric tuple
- 即"8 个真策略 × 平均 8 个参数冗余"

---

## Q4. 阈值非单调死区 — 阈值越严越严应该单调

**问题**: 一个连续阈值参数 (如 CSI 跌幅门), 通过数应该是单调函数 (越严越少, 或越严越多). 中间出现"死区" (0 通过) 通常是路径依赖伪信号.

**检查方法**:
```python
counts_by_threshold = df_hard_pass.groupby('threshold_col').size()
# 排序看是不是单调
```

**判定**:
- 如果 counts 非单调 → 用单点 trade-level 解释每个阈值的差异
- 如果某个阈值 = 0, 周围非零 → 路径依赖现象, 需调查

**实例** (cb_arb csi market filter):
- CSI -0.5%: 24 通过
- CSI -1.0%: 0 通过 (死区!)
- CSI -1.5%: 6 通过
- CSI -2.0%: 36 通过
- 非单调 → Codex 分析后发现 -1% 阈值下 12 候选过 2020 floor 但 0 过 2021 floor, 路径依赖

---

## Q5. 新 baseline 复制旧 baseline 检查 — 新方案是不是包装旧 baseline?

**问题**: 新方案的核心 metric 跟某个已有 baseline 完全相同 (到 6 位小数) — 多半是 wrap, 没新东西.

**检查方法**:
```python
# 假设新方案 top1 replay_2020_excess = -0.138588
# 比对已有 baseline 行的相应 metric
for baseline in ['medium_opportunity', 'strong_opportunity', 'current_best_no_opportunity']:
    bl_val = summary_csv[summary_csv.kind == baseline].replay_2020_excess.iloc[0]
    if abs(top1_val - bl_val) < 1e-6:
        print(f"WARNING: top1 == {baseline}, 可能是包装")
```

**判定**:
- 如果 top1 的核心 metric == 某 baseline (6 位小数级别) → **必须**做 trade-level diff 看是不是真的等价
- 如果 trade set 完全相同 → 是 wrap, 不是新策略
- 如果 trade set 不同但 P&L 抵消刚好相等 → 是巧合, 但**说明新机制没真正改善**

**实例** (cb_arb csi market filter):
- top1 replay_2020 = -0.138588 = medium baseline replay_2020 (exactly)
- 但 trade-level diff 显示 top1 触发日数 = 17, medium = 19 — 差 2 天 (`20200508`, `20200914`), P&L 影响抵消
- 结论: 不是 medium 复制, 是 "medium + CSI 过滤剪了 2 个误伤日" 的精准等价

---

## Q6. 触发时点检查 (有新机制时必跑)

**问题**: 如果新功能改变了 "信号何时判断 / 何时执行" 的逻辑 (e.g. raw signal date vs effective execution date), 错位会带来隐性 bug.

**检查方法**:
```python
# 1. 列出新机制 N 触发日 (raw signal date 列表)
# 2. 每个 raw date 对应的 effective date (lag 后)
# 3. 各种"被剪 / 保留"日期统计
# 4. 用无新机制的 baseline 回放, 计算每个剪日 / 留日的 P&L 贡献
cut_dates_pnl = baseline_pnl_on(cut_dates)        # 被剪日子贡献
kept_dates_pnl = baseline_pnl_on(kept_dates)      # 留下日子贡献
```

**判定**:
- 如果 `cut_dates` 平均赚钱 → 新机制错剪了好日子 (设计有问题)
- 如果 `kept_dates` 平均亏钱 → 新机制留的是 false positive (阈值或规则有问题)
- 两个都不对 → 新机制路径污染, 不是简单"加" 或 "减"

**实例** (cb_arb 2022 反向):
- 被剪 effective dates (1 笔): P&L `+1240.62`, 平均 `+11.06%` (好日子被错剪)
- 保留 effective dates (19 笔): P&L `-8335`, 平均 `-1.67%` (留的是 false positive)
- 结论: CSI 在 2022 同时错剪好日子 + 错留坏日子

---

## Q7. 路径依赖检查 (有新机制时必跑)

**问题**: 新机制不只影响"加哪几笔", 还影响"已有持仓的退出节奏". 单看新增/删除交易看不到全部伤害.

**检查方法**:
```python
common_trades = trades_baseline & trades_new       # 同样进场标的
# 比较他们的退出日 / 退出价 / 退出原因 / 最终 P&L
exit_diff = (trades_baseline.exit_date - trades_new.exit_date)
pnl_diff = trades_new.pnl - trades_baseline.pnl
```

**判定**:
- 共用交易 P&L 差大 (medium 比 baseline 多亏 X 元) → 新机制改变了退出节奏 (粘性 / 换仓 hurdle)
- 退出日延迟 + P&L 变差 → 新机制让退出过慢
- 退出日提前 + P&L 变差 → 新机制让退出过快

**实例** (cb_arb 2022):
- 共用 109 trades, V2 (CSI) 比 V1 (medium) 多亏 `-7524.11`
- 共用中 32 笔退出日变 → 贡献 `-594.70`
- 结论: 主要伤害来自路径污染, 不是单纯多/少进场

---

## 检查流程

每次 L3 grid 完成后, Claude 必须:

1. 跑 Q1-Q5 (每次必跑)
2. **如果新机制改变交易路径** (新触发逻辑 / 新进出场规则 / 新数据源参与决策): 加跑 Q6, Q7
3. 每项给出: ✓ pass / ⚠ warning / ✗ failed
4. 任一 fail → 必须在 L5 做对应的反向诊断
5. 3 项以上 fail → 假设很可能不成立, 直接进 L6 写 "已确认无效" 报告
6. 全 pass → 假设有真实价值, 进入 L5 深度论证

**何时跳过 Q6/Q7**:
- 纯参数调优 (在已有空间内调) → 可跳
- 加新功能 / 加新数据 / 改触发 / 改进出场规则 → **不可跳**

---

## 与 Codex 协作 + 硬化对账

每次 Q1-Q7 (或 Q1-Q5) 检查后, Claude 把疑点列在一个 DIRECT 给 Codex:

```
### Round-N: Q1-Q5 (+Q6/Q7) 质疑结果

Q1 硬约束 binding: ✓/⚠/✗ (具体)
Q2 CV 严格性: ✓/⚠/✗
Q3 Top 同质性: ✓/⚠/✗
Q4 阈值死区: ✓/⚠/✗
Q5 新 baseline 复制: ✓/⚠/✗
[Q6 触发时点: ✓/⚠/✗ — 仅新机制时]
[Q7 路径依赖: ✓/⚠/✗ — 仅新机制时]

请用现有数据回应所有 failed/警告 项.
```

**硬化对账** (per [enforcement_protocol.md](./enforcement_protocol.md)):

- Claude 端: 收到 Codex L3 RESPONSE 时, prompt 自动注入 "现在必须跑 Q1-Q5"
- Codex 端: 收到 Claude L5 DIRECT 时, 检查 DIRECT 是否含 Q1-Q5 各项 ACK; 缺 → 拒答, 退回让 Claude 补齐
- 双向监督, 不允许跳过

---

## 维护

- 这 5+2 项是从 2026-05-14 cb_arb 研究抽出来的. 后续研究里如果发现新的有价值质疑模式, 加 Q8 / Q9 / ...
- 但不要随便加 — 每项必须能在多次研究里复用, 不是 one-off
- 同样, 如果某项检查在多次研究里都没暴露任何东西 → 评估是否降级为可选
