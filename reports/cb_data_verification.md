# 可转债数据独立核对报告

生成时间: 2026-05-09 14:46:46
parquet: `data/cb_warehouse/cb_basic.parquet` (1012 条)

## 数据源
- **A**: akshare `bond_zh_cov` 全集 (1012 行, 含 转股价/正股价/规模/评级/上市日)
- **B**: akshare `bond_zh_cov_info` 单只详情 (含 INITIAL_TRANSFER_PRICE / TRANSFER_PRICE / CONVERT_STOCK_PRICE)
- **C**: eastmoney `RPT_BOND_CB_LIST` 单只过滤 (这就是 parquet 的源, 重拉对照)
- **D**: akshare `bond_cb_adj_logs_jsl` 下修历史 (反推真实最新转股价)
- **E**: tushare `bond_basic` — token 已过期 (`您的token已过期, 请联系续费`), **跳过**

## 关键观察 (在比对前)

从 cov_info 接口同时拿到的字段语义:
- `CONVERT_STOCK_PRICE` = **正股价** (用户已踩过坑)
- `INITIAL_TRANSFER_PRICE` = **初始转股价**
- `TRANSFER_PRICE` = **当前最新转股价** (含下修后. 已退市的 CB 这字段为 None)

我们 build_cb_warehouse.py 第 217-222 行的 fallback 链是:
```
CONVERT_STOCK_PRICE -> TRANSFER_PRICE -> INITIAL_TRANSFER_PRICE
```
但 `RPT_BOND_CB_LIST` 接口里 `CONVERT_STOCK_PRICE` 字段对所有 CB 都返回 None (经 110044 验证)
, 所以**实际生效**的是 `TRANSFER_PRICE -> INITIAL_TRANSFER_PRICE`. 隐患:
**当 `TRANSFER_PRICE=None` (已退市) 而 CB 有过下修时, fallback 到 `INITIAL_TRANSFER_PRICE` 等于把转股价**
**冻结在初始值, 错过所有下修.** 这是核弹级数据错误.

## 逐只比对

### 110044.SH 广电转债
_AA, 8亿, 已强赎(2024-03-21), 已退市(2024-06-27)_

#### conv_price (转股价) — 最关键
```
我们 parquet:                6.91           
源 A bond_zh_cov 转股价:     -              
源 B cov_info TRANSFER:      -              
源 B cov_info INIT_TRANSFER: 6.91           
源 B cov_info CONVERT_STOCK: 3.96            (这是正股价!)
源 C RPT_LIST TRANSFER:      -              
源 C RPT_LIST INIT_TRANSFER: 6.91           
源 D 下修历史 (1条):
  生效 2024-06-05: 6.82 -> 4.41
  -> 真实最新转股价 = 4.41

判定: parquet=6.91 vs 真值=4.41 (下修历史最新 2024-06-05) -> BAD diff +56.7%
```

#### 其他字段
```
字段                我们 parquet         A(zh_cov)         B(cov_info)        C(RPT_LIST)       一致?
issue_size          8                   8                 8                 8                 OK
rating              AA                  AA                AA                AA                OK
list_date           2018-07-24          2018-07-24        2018-07-24        2018-07-24        OK
maturity_date       2024-06-27          -                 2024-06-27        2024-06-27        OK
delist_date         2024-06-27          -                 2024-06-27        2024-06-27        OK
par_value           100                 -                 100               -                 OK
issue_price         100                 -                 100               -                 OK
coupon_rate         0.01217             -                 0.01217           0.01217           OK
```

### 110059.SH 浦发转债
_AAA, 500亿, 巨型, 2025-10 到期_

#### conv_price (转股价) — 最关键
```
我们 parquet:                15.05          
源 A bond_zh_cov 转股价:     -              
源 B cov_info TRANSFER:      -              
源 B cov_info INIT_TRANSFER: 15.05          
源 B cov_info CONVERT_STOCK: 9.07            (这是正股价!)
源 C RPT_LIST TRANSFER:      -              
源 C RPT_LIST INIT_TRANSFER: 15.05          
源 D 下修历史:               无下修

判定: parquet=15.05 vs 真值=15.05 (已退市无下修, 取 INITIAL_TRANSFER_PRICE) -> OK
```

#### 其他字段
```
字段                我们 parquet         A(zh_cov)         B(cov_info)        C(RPT_LIST)       一致?
issue_size          500                 500               500               500               OK
rating              AAA                 AAA               AAA               AAA               OK
list_date           2019-11-15          2019-11-15        2019-11-15        2019-11-15        OK
maturity_date       2025-10-28          -                 2025-10-28        2025-10-28        OK
delist_date         2025-10-28          -                 2025-10-28        2025-10-28        OK
par_value           100                 -                 100               -                 OK
issue_price         100                 -                 100               -                 OK
coupon_rate         0.01967             -                 0.01967           0.01967           OK
```

### 113052.SH 兴业转债
_AAA, 500亿, 在跑_

#### conv_price (转股价) — 最关键
```
我们 parquet:                20.63          
源 A bond_zh_cov 转股价:     20.63          
源 B cov_info TRANSFER:      20.63          
源 B cov_info INIT_TRANSFER: 25.51          
源 B cov_info CONVERT_STOCK: 17.73           (这是正股价!)
源 C RPT_LIST TRANSFER:      -              
源 C RPT_LIST INIT_TRANSFER: 25.51          
源 D 下修历史:               无下修

判定: parquet=20.63 vs 真值=20.63 (cov_info TRANSFER_PRICE (含调整后最新)) -> OK
```

#### 其他字段
```
字段                我们 parquet         A(zh_cov)         B(cov_info)        C(RPT_LIST)       一致?
issue_size          500                 500               500               500               OK
rating              AAA                 AAA               AAA               AAA               OK
list_date           2022-01-14          2022-01-14        2022-01-14        2022-01-14        OK
maturity_date       2027-12-27          -                 2027-12-27        2027-12-27        OK
delist_date         -                   -                 -                 -                 OK
par_value           100                 -                 100               -                 OK
issue_price         100                 -                 100               -                 OK
coupon_rate         0.014               -                 0.014             0.014             OK
```

### 113050.SH 南银转债
_AAA, 200亿, 已强赎(2025-06-10)_

#### conv_price (转股价) — 最关键
```
我们 parquet:                10.1           
源 A bond_zh_cov 转股价:     -              
源 B cov_info TRANSFER:      -              
源 B cov_info INIT_TRANSFER: 10.1           
源 B cov_info CONVERT_STOCK: 11.32           (这是正股价!)
源 C RPT_LIST TRANSFER:      -              
源 C RPT_LIST INIT_TRANSFER: 10.1           
源 D 下修历史:               无下修

判定: parquet=10.1 vs 真值=10.1 (已退市无下修, 取 INITIAL_TRANSFER_PRICE) -> OK
```

#### 其他字段
```
字段                我们 parquet         A(zh_cov)         B(cov_info)        C(RPT_LIST)       一致?
issue_size          200                 200               200               200               OK
rating              AAA                 AAA               AAA               AAA               OK
list_date           2021-07-01          2021-07-01        2021-07-01        2021-07-01        OK
maturity_date       2025-07-18          -                 2025-07-18        2025-07-18        OK
delist_date         2025-07-18          -                 2025-07-18        2025-07-18        OK
par_value           100                 -                 100               -                 OK
issue_price         100                 -                 100               -                 OK
coupon_rate         0.01033             -                 0.01033           0.01033           OK
```

### 113042.SH 上银转债
_AAA, 200亿, 在跑(2026-02 到期)_

#### conv_price (转股价) — 最关键
```
我们 parquet:                8.57           
源 A bond_zh_cov 转股价:     8.57           
源 B cov_info TRANSFER:      8.57           
源 B cov_info INIT_TRANSFER: 11.03          
源 B cov_info CONVERT_STOCK: 9.24            (这是正股价!)
源 C RPT_LIST TRANSFER:      -              
源 C RPT_LIST INIT_TRANSFER: 11.03          
源 D 下修历史:               无下修

判定: parquet=8.57 vs 真值=8.57 (cov_info TRANSFER_PRICE (含调整后最新)) -> OK
```

#### 其他字段
```
字段                我们 parquet         A(zh_cov)         B(cov_info)        C(RPT_LIST)       一致?
issue_size          200                 200               200               200               OK
rating              AAA                 AAA               AAA               AAA               OK
list_date           2021-02-10          2021-02-10        2021-02-10        2021-02-10        OK
maturity_date       2027-01-25          -                 2027-01-25        2027-01-25        OK
delist_date         -                   -                 -                 -                 OK
par_value           100                 -                 100               -                 OK
issue_price         100                 -                 100               -                 OK
coupon_rate         0.0215              -                 0.0215            0.0215            OK
```

### 110075.SH 南航转债
_AAA, 160亿, 在跑(2026-10 到期)_

#### conv_price (转股价) — 最关键
```
我们 parquet:                6.17           
源 A bond_zh_cov 转股价:     6.17           
源 B cov_info TRANSFER:      6.17           
源 B cov_info INIT_TRANSFER: 6.24           
源 B cov_info CONVERT_STOCK: 5.71            (这是正股价!)
源 C RPT_LIST TRANSFER:      -              
源 C RPT_LIST INIT_TRANSFER: 6.24           
源 D 下修历史:               无下修

判定: parquet=6.17 vs 真值=6.17 (cov_info TRANSFER_PRICE (含调整后最新)) -> OK
```

#### 其他字段
```
字段                我们 parquet         A(zh_cov)         B(cov_info)        C(RPT_LIST)       一致?
issue_size          160                 160               160               160               OK
rating              AAA                 AAA               AAA               AAA               OK
list_date           2020-11-03          2020-11-03        2020-11-03        2020-11-03        OK
maturity_date       2026-10-15          -                 2026-10-15        2026-10-15        OK
delist_date         -                   -                 -                 -                 OK
par_value           100                 -                 100               -                 OK
issue_price         100                 -                 100               -                 OK
coupon_rate         0.009167            -                 0.009167          0.009167          OK
```

### 128136.SZ 立讯转债
_AA+, 30亿, 已强赎(2025-07-11)_

#### conv_price (转股价) — 最关键
```
我们 parquet:                55.97          
源 A bond_zh_cov 转股价:     55.97          
源 B cov_info TRANSFER:      55.97          
源 B cov_info INIT_TRANSFER: 58.62          
源 B cov_info CONVERT_STOCK: 71.29           (这是正股价!)
源 C RPT_LIST TRANSFER:      -              
源 C RPT_LIST INIT_TRANSFER: 58.62          
源 D 下修历史:               无下修

判定: parquet=55.97 vs 真值=55.97 (cov_info TRANSFER_PRICE (含调整后最新)) -> OK
```

#### 其他字段
```
字段                我们 parquet         A(zh_cov)         B(cov_info)        C(RPT_LIST)       一致?
issue_size          30                  30                30                30                OK
rating              AA+                 AA+               AA+               AA+               OK
list_date           2020-12-02          2020-12-02        2020-12-02        2020-12-02        OK
maturity_date       2026-11-03          -                 2026-11-03        2026-11-03        OK
delist_date         -                   -                 -                 -                 OK
par_value           100                 -                 100               -                 OK
issue_price         100                 -                 100               -                 OK
coupon_rate         0.009833            -                 0.009833          0.009833          OK
```

### 127007.SZ 湖广转债
_AA+, 17.3亿, 已强赎(2024-05-24)_

#### conv_price (转股价) — 最关键
```
我们 parquet:                10.16          
源 A bond_zh_cov 转股价:     -              
源 B cov_info TRANSFER:      -              
源 B cov_info INIT_TRANSFER: 10.16          
源 B cov_info CONVERT_STOCK: 4.99            (这是正股价!)
源 C RPT_LIST TRANSFER:      -              
源 C RPT_LIST INIT_TRANSFER: 10.16          
源 D 下修历史 (2条):
  生效 2024-06-13: 5.58 -> 3.79
  生效 2019-02-22: 10.16 -> 7.92
  -> 真实最新转股价 = 3.79

判定: parquet=10.16 vs 真值=3.79 (下修历史最新 2024-06-13) -> BAD diff +168.1%
```

#### 其他字段
```
字段                我们 parquet         A(zh_cov)         B(cov_info)        C(RPT_LIST)       一致?
issue_size          17.34               17.34             17.34             17.34             OK
rating              AA+                 AA+               AA+               AA+               OK
list_date           2018-08-01          2018-08-01        2018-08-01        2018-08-01        OK
maturity_date       2024-06-28          -                 2024-06-28        2024-06-28        OK
delist_date         2024-07-01          -                 2024-07-01        2024-07-01        OK
par_value           100                 -                 100               -                 OK
issue_price         100                 -                 100               -                 OK
coupon_rate         0.01267             -                 0.01267           0.01267           OK
```

### 110058.SH 永鼎转债
_AA-, 9.8亿, 多次下修(2019/2024)_

#### conv_price (转股价) — 最关键
```
我们 parquet:                6.5            
源 A bond_zh_cov 转股价:     -              
源 B cov_info TRANSFER:      -              
源 B cov_info INIT_TRANSFER: 6.5            
源 B cov_info CONVERT_STOCK: 49.94           (这是正股价!)
源 C RPT_LIST TRANSFER:      -              
源 C RPT_LIST INIT_TRANSFER: 6.5            
源 D 下修历史 (2条):
  生效 2024-07-09: 5.04 -> 3.78
  生效 2019-12-20: 6.35 -> 5.1
  -> 真实最新转股价 = 3.78

判定: parquet=6.5 vs 真值=3.78 (下修历史最新 2024-07-09) -> BAD diff +72.0%
```

#### 其他字段
```
字段                我们 parquet         A(zh_cov)         B(cov_info)        C(RPT_LIST)       一致?
issue_size          9.8                 9.8               9.8               9.8               OK
rating              AA-                 AA-               AA-               AA-               OK
list_date           2019-05-08          2019-05-08        2019-05-08        2019-05-08        OK
maturity_date       2024-12-20          -                 2024-12-20        2024-12-20        OK
delist_date         2024-12-20          -                 2024-12-20        2024-12-20        OK
par_value           100                 -                 100               -                 OK
issue_price         100                 -                 100               -                 OK
coupon_rate         0.01217             -                 0.01217           0.01217           OK
```

### 110072.SH 广汇转债
_AA-, 33.7亿, 大幅下修(2024)_

#### conv_price (转股价) — 最关键
```
我们 parquet:                4.03           
源 A bond_zh_cov 转股价:     -              
源 B cov_info TRANSFER:      -              
源 B cov_info INIT_TRANSFER: 4.03           
源 B cov_info CONVERT_STOCK: -               (这是正股价!)
源 C RPT_LIST TRANSFER:      -              
源 C RPT_LIST INIT_TRANSFER: 4.03           
源 D 下修历史 (1条):
  生效 2024-05-23: 4.03 -> 1.5
  -> 真实最新转股价 = 1.5

判定: parquet=4.03 vs 真值=1.5 (下修历史最新 2024-05-23) -> BAD diff +168.7%
```

#### 其他字段
```
字段                我们 parquet         A(zh_cov)         B(cov_info)        C(RPT_LIST)       一致?
issue_size          33.7                33.7              33.7              33.7              OK
rating              AA-                 AA-               AA-               AA-               OK
list_date           2020-09-15          2020-09-15        2020-09-15        2020-09-15        OK
maturity_date       2026-08-18          -                 2026-08-18        2026-08-18        OK
delist_date         2024-08-28          -                 2024-08-28        2024-08-28        OK
par_value           100                 -                 100               -                 OK
issue_price         100                 -                 100               -                 OK
coupon_rate         0.01083             -                 0.01083           0.01083           OK
```

## 总结

### 按字段统计

- **conv_price**: 10 只里 **4 只严重不一致** (差 >0.5%)
- **issue_size / rating / list_date / maturity_date / delist_date / par_value / issue_price / coupon_rate**: 10 只全部一致 (各源比对 OK)

### conv_price 不一致明细 (按差幅排序)

- **110072.SH 广汇转债**: parquet=`4.03` vs 真值=`1.5` (下修历史最新 2024-05-23), **差 168.7%**
- **127007.SZ 湖广转债**: parquet=`10.16` vs 真值=`3.79` (下修历史最新 2024-06-13), **差 168.1%**
- **110058.SH 永鼎转债**: parquet=`6.5` vs 真值=`3.78` (下修历史最新 2024-07-09), **差 72.0%**
- **110044.SH 广电转债**: parquet=`6.91` vs 真值=`4.41` (下修历史最新 2024-06-05), **差 56.7%**

### 错误模式分类

**全部 4 只**都是同一种 bug, 不是字段误用 (CONVERT_STOCK_PRICE 误用早已修过), 而是:

> **当 CB 已退市/已强赎时, eastmoney `RPT_BOND_CB_LIST.TRANSFER_PRICE` 字段会变成 None,**
> **导致 build_cb_warehouse.py 第 217-222 行的 fallback 链回退到 `INITIAL_TRANSFER_PRICE`(初始价).**
> **如果这只 CB 在生命周期内有过下修, 我们就会把转股价**冻结在初始值**, 错过所有下修.**

- **110044 广电**: 初始 6.91 -> 下修后 4.41. parquet 留 6.91, 错 +57%
- **127007 湖广**: 两次下修 10.16 -> 7.92 -> 3.79. parquet 留 10.16, 错 +168%
- **110058 永鼎**: 两次下修 6.50 -> 5.10 -> 3.78. parquet 留 6.50, 错 +72%
- **110072 广汇**: 一次大幅下修 4.03 -> 1.50. parquet 留 4.03, 错 +169%

**还在跑**的 CB (113052 兴业 / 113042 上银 / 110075 南航 / 113050 南银 / 128136 立讯)
`TRANSFER_PRICE` 字段都有值, 我们 parquet 取到了正确的现值. 没问题.

**没下修过**的退市 CB (110059 浦发尚未到期实际还在跑) 也 OK.

### 推荐修复 (按优先级)

#### P0 — 立即修 conv_price

**问题**: parquet 的 conv_price 对 "已退市 + 有下修" 这一交集错误.
**根因**: build_cb_warehouse.py L217-222 的 fallback 链没考虑这种 case.

**修复方案 (按工作量从小到大)**:

1. **最简方案 (推荐, 全活跃 CB 都正确)**:
   对每只 CB, 从 cov_info 单独拉一次, 优先取 `TRANSFER_PRICE`, 退化到下修历史 d[0].after, 最后才到 `INITIAL_TRANSFER_PRICE`.
   工作量: 1012 只 * 0.5s = ~10 分钟全量重拉, 或仅对已退市的 ~300 只补拉.

2. **加 cb_price_chg 表**:
   `bond_cb_adj_logs_jsl` 给的下修历史就是真相. 写入新表 `cb_price_chg.parquet`,
   策略层把 conv_price 当 "按 trade_date 历史变化" 用, 严谨度更高 (历史回测才不会作弊).

3. **`enrich_cb_conv_price.py` 已存在但不够**:
   该脚本已修了 CONVERT_STOCK_PRICE 误用 + 实现了 TRANSFER_PRICE -> INITIAL_TRANSFER_PRICE fallback,
   **但对已退市 CB(TRANSFER_PRICE=None)仍回退到 INITIAL_TRANSFER_PRICE**, 没接 `bond_cb_adj_logs_jsl`.
   需要在它的 fallback 链最前面加: 优先 jsl 下修历史 -> TRANSFER_PRICE -> INITIAL.

#### P1 — 增强单只接口 fallback

修改 build_cb_warehouse.py L217-222:
```python
# 改为: 优先 TRANSFER_PRICE, 然后 INITIAL_TRANSFER_PRICE.
# 已不再用 CONVERT_STOCK_PRICE (那是正股价, 此前已知)
'conv_price': (
    pd.to_numeric(info.get('TRANSFER_PRICE'), errors='coerce')
    if info.get('TRANSFER_PRICE') is not None
    else pd.to_numeric(info.get('INITIAL_TRANSFER_PRICE'), errors='coerce')
),
```
再加: 对 `IS_REDEEM=='是'` 或有 `delist_date` 的 CB, 单独调 `bond_cb_adj_logs_jsl(symbol=code)`,
如果有记录, 用 `下修后转股价` 最大日期那一行覆盖 conv_price.

### 总评

**数据是否可信用于策略?** **不能直接用, 必须先修 conv_price**.

- 静态字段(规模/评级/日期/利率/面值/发行价): 三源一致, **可信**.
- conv_price 字段:
  - 在跑的 CB (~700+ 只): 取自 `TRANSFER_PRICE`, **可信**.
  - 已退市 + 没下修过的 CB: 取自 `INITIAL_TRANSFER_PRICE`, **可信** (恒等于初始).
  - 已退市 + 有过下修的 CB: **错误**, 留在了初始价. 用本数据回测会高估转股价值, 低估溢价率.

  本次抽样估算: 4/10 = **40%** 的 "已退市" 子集存在此问题. 全 1012 只里粗估 50-150 只受影响.

- **修复路径**: 改 build_cb_warehouse.py 的 fallback 链, 并对所有有下修历史的 CB 用 `bond_cb_adj_logs_jsl`
  作为权威源覆盖. 不需要重拉所有 CB, 只需要补拉 ~300 只已退市的.
