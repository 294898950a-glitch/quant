# 可转债下修 PEAD 验证项目

## 项目定位
验证中国可转债"下修公告"后是否存在 **PEAD（Post-Event Announcement Drift）** 现象——即公告信息被市场缓慢吸收，价格持续漂移，形成可套利窗口。

核心假设：下修越狠（大幅下修），PEAD 效应越显著。

## 数据文件

位于 `~/projects/quant/strategies/cb_redemption/data/`：

### 1. `cb_down_events_with_returns.csv`
112 条下修事件，列说明：

| 列名 | 说明 |
|------|------|
| `bond_id` | 转债代码 (6位) |
| `name` | 转债名称 |
| `meeting_date` | 股东大会日（事件日，非首次公告日） |
| `before_price` | 下修前转股价 |
| `after_price` | 下修后转股价 |
| `event_close` | 事件日收盘价 |
| `T1_ret` ~ `T60_ret` | T+N 交易日累计收益率 (%，相对于事件日收盘价) |

### 2. `cb_pead_series.csv`
112 条事件 × 完整日价格序列，列说明：

| 列名 | 说明 |
|------|------|
| `bond_id` | 转债代码 |
| `name` | 转债名称 |
| `meeting_date` | 股东大会日 |
| `before_price` | 下修前转股价 |
| `after_price` | 下修后转股价 |
| `T+0` ~ `T+60` | 每个交易日的收盘价 |

## 数据来源

- **下修事件：** 集思录 JSL (jisilu.cn) 转股价调整记录 API → `bond_cb_adj_logs_jsl`
- **价格数据：** akshare → 东方财富 → `bond_zh_hs_cov_daily`
- **原始代码扫描范围：** 全部 1012 只转债中的前 400 只（代码顺序）
- **时间范围：** 2023.01 — 2025.10
- **样本量：** 112 次有效下修事件

## 验证方法

### 分类定义
```python
ratio = float(after_price) / float(before_price)
is_deep = ratio <= 0.75  # 大幅下修：砍价 ≥25%
is_shallow = ratio > 0.75  # 小幅下修
```

### 异常收益计算
```
CAR = (P_t / P_0 - 1) * 100%
```
- `P_0` = 事件日（股东大会日）收盘价
- `P_t` = T+N 天收盘价
- 无基准调整（原始 CAR，如需更严谨应减去转债等权指数同期收益）

### 统计检验
- 单组：t-test（CAR / SE）
- 两组差异：Welch's t-test（不等方差）
- 显著性标记：***p<0.01, **p<0.05, *p<0.1

## 已有发现

| 窗口 | 大幅下修 (n=51) | 小幅下修 (n=61) |
|------|:---:|:---:|
| T+1 | +2.46%*** | -0.32% |
| T+20 | +6.24%*** | +0.27% |
| T+60 | +9.15%*** | +0.59% |

**结论：** 大幅下修 PEAD 显著存在，60 天窗口约 9% 超额收益。小幅下修无效应。两组差异在 T+1, T+20, T+60 均显著（t>3.0）。

## 可改进方向

1. **补充完整扫描** — 当前只扫了前 400 只转债，理论上全部 1012 只都有下修历史，可能漏掉了一些事件（主要漏了较早的、已经退市的）。用 JSL API 遍历全部：
   ```python
   import akshare as ak
   df = ak.bond_zh_cov()  # 1012 bonds
   # 对每只调 bond_cb_adj_logs_jsl(code)
   ```
   ⚠️ 注意 JSL 有反爬，每个请求间隔 0.15s，全部跑完约 2.5 分钟。

2. **区分"下修到底"** — JSL 数据有 `下修底价` 字段，只有 `after_price <= 下修底价 * 1.01` 才算真正修到底。当前用 `ratio <= 0.75` 是近似。

3. **加基准收益** — 用 `ak.bond_cb_index_jsl()` 获取转债等权指数，计算超额收益（CAR - benchmark return）。

4. **区分首次公告日 vs 股东大会日** — 董事会提议下修会提前发布公告，真正的事件日应该是首次公告日而不是股东大会日。两者相差约 10-15 天，当前的 CAR 可能低估了提前反应。

5. **分析首次下修 vs 多次下修** — 有些转债多次下修，第二次的效果可能递减。

6. **分行业/评级/剩余期限** — 看看哪些类型的转债 PEAD 更大。

## 可转债代码格式
- 深交所 (12XXXX, 13XXXX)：`sz{code}` → e.g. `sz123234`
- 上交所 (11XXXX, 10XXXX)：`sh{code}` → e.g. `sh113654`

## 相关 akshare 接口
```python
# 所有转债列表
ak.bond_zh_cov()

# 某只转债的下修记录
ak.bond_cb_adj_logs_jsl(symbol="123234")  # 6位代码，不要 sz/sh 前缀

# 日线数据
ak.bond_zh_hs_cov_daily(symbol="sz123234")  # 需要 sz/sh 前缀

# 转债指数
ak.bond_cb_index_jsl()

# 当前实时数据
ak.bond_cb_jsl()
ak.bond_zh_hs_cov_spot()

# 已退市转债
# 浏览器打开 https://www.jisilu.cn/data/cbnew/#re /
