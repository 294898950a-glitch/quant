# HDRF L6 复盘报告模板

每次研究 batch 完成 (assumption confirmed / rejected / pending) 后, Claude 必须在 `reports/<run-id>.md` 写一份复盘. 给用户看, 也给未来的 Claude 看.

## 模板

```markdown
# <strategy-name> <feature-name> 复盘

日期: YYYY-MM-DD
对象: 研究 id (e.g. cb_arb_concurrent_supervised_20260511_094500 + 新功能名)

## 研究问题

(L0 假设的扩展版, 一段话讲清楚 "想验证什么 / 为什么)

## 假设 ladder (强→弱)

按"假设强度"由弱到强列出, 后续验证可以打中其中哪些:

1. 弱版本: ...
2. 中版本: ...
3. 强版本: ...

## 数据范围

- 数据源: data/<dataset>
- 参数搜索维度: ...
- 验证年份: ...
- 硬约束: replay_X ≥ floor_X
- 算力: spot (16 核) / sig (2 核)
- 协作: 用户 + Claude (质疑) + Codex (实施)

## 第 N 轮发现 (按时间顺序)

### Round 1 - <Round 主题>

| 指标 | 结果 |
| --- | --- |

(关键发现一句话)

### Round 2 - 5 项质疑

按 [questioning_checklist](../docs/research_framework/questioning_checklist.md) 跑了 Q1-Q5:

- Q1 (...): ✓/⚠/✗ — (具体数据)
- Q2 (...): ✓/⚠/✗
- Q3 (...): ✓/⚠/✗
- Q4 (...): ✓/⚠/✗
- Q5 (...): ✓/⚠/✗

### Round 3+ - 反向诊断 / 真 CV / 修复尝试

(每一轮一个 H2 子节, 关键数据 + 一句话结论)

## 整体判断

**结论**: <采用 / 拒绝 / 需进一步研究>

理由 (3-5 条):
1. ...
2. ...
3. ...

**真正确认的发现** (有研究价值, 留档):
1. ...
2. ...

**已确认无效方向** (留档, 不要再投入算力):
- ...

**未来值得探索方向**:
1. ...
2. ...

## 算力成本

- spot 跑了约 X 小时, ≈ ¥Y
- sig 跑了约 X 小时 (长开,边际成本 ~ 0)
- 单次研究产出 vs 成本: ...

## 后续待办

- [ ] 关 spot VM (如果还开着)
- [ ] 把"已确认无效方向"归入 cb_arb_cross_eval_retro.md
- [ ] 下次研究方向: ...
```

## 必填要点

- ❌ 不能省"假设 ladder" — 没有梯度的假设很难定位 grid 该怎么设计
- ❌ 不能省"已确认无效方向" — 否则未来研究会重复走老路
- ❌ 不能省"算力成本" — 影响未来 spot 起停决策
- ✅ 弹性: Round 数量 / 反向诊断角度 因研究而异

## 写作风格

- **日常中文**, 不写代码, 术语括号翻译 (per [feedback_plain_language](../../.claude/memory/feedback_plain_language.md))
- **结果优先** — 每段开头一句话给数字, 不要先讲架构
- **诚实** — 失败结论也是结论, 不要包装成"还需进一步研究"如果你知道它不行

## 参考案例

- `reports/cb_arb_csi_market_filter_2026-05-14.md` — 第一个完整走完 HDRF 7 层的研究报告. 285 行, 5 个 Round 章节 + 整体判断 + 算力成本 + 待办. 失败结论但诚实.
- `reports/cb_arb_framework_retro_2026-05-11.md` — 框架本身的修复复盘 (不是策略研究), 但报告结构类似.
