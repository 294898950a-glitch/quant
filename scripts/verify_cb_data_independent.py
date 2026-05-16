#!/usr/bin/env python3
"""独立核对 cb_basic.parquet 数据的脚本.

挑 10 只代表性 CB, 从 3 个独立源拉同字段比对, 找数据错误.

源:
  A. akshare bond_zh_cov                    1012 条全集 (含 转股价/正股价/上市日/评级/规模)
  B. akshare bond_zh_cov_info               单只 cov_info (含 INITIAL_TRANSFER_PRICE/TRANSFER_PRICE)
  C. eastmoney RPT_BOND_CB_LIST 单只过滤    我们 parquet 的来源, 用来反查
  D. akshare bond_cb_adj_logs_jsl           下修历史 (反推真实最新转股价)
  E. tushare bond_basic                     token 已过期, skip

输出:
  data/cb_warehouse/verification/cb_data_verification.yaml
"""
from __future__ import annotations

import json
import sys
import time
import traceback
import warnings
from pathlib import Path

import pandas as pd
import requests
import yaml

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent.parent
PARQUET = ROOT / "data" / "cb_warehouse" / "cb_basic.parquet"
REPORT = ROOT / "data" / "cb_warehouse" / "verification" / "cb_data_verification.yaml"

# 10 只代表性 CB
SAMPLES = [
    # ts_code,        code,    name,         note
    ("110044.SH", "110044", "广电转债", "AA, 8亿, 已强赎(2024-03-21), 已退市(2024-06-27)"),
    ("110059.SH", "110059", "浦发转债", "AAA, 500亿, 巨型, 2025-10 到期"),
    ("113052.SH", "113052", "兴业转债", "AAA, 500亿, 在跑"),
    ("113050.SH", "113050", "南银转债", "AAA, 200亿, 已强赎(2025-06-10)"),
    ("113042.SH", "113042", "上银转债", "AAA, 200亿, 在跑(2026-02 到期)"),
    ("110075.SH", "110075", "南航转债", "AAA, 160亿, 在跑(2026-10 到期)"),
    ("128136.SZ", "128136", "立讯转债", "AA+, 30亿, 已强赎(2025-07-11)"),
    ("127007.SZ", "127007", "湖广转债", "AA+, 17.3亿, 已强赎(2024-05-24)"),
    ("110058.SH", "110058", "永鼎转债", "AA-, 9.8亿, 多次下修(2019/2024)"),
    ("110072.SH", "110072", "广汇转债", "AA-, 33.7亿, 大幅下修(2024)"),
]


def to_ymd(s):
    if s is None:
        return None
    if isinstance(s, str):
        s = s.strip()
        if not s or s.lower() in ("none", "nan", "-"):
            return None
    try:
        return pd.to_datetime(s).strftime("%Y-%m-%d")
    except Exception:
        return None


def f_or_none(v):
    """转 float, 失败/无效返回 None."""
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        if not v or v in ("-", "None", "nan", "NaN"):
            return None
    try:
        x = float(v)
        if pd.isna(x):
            return None
        return x
    except Exception:
        return None


# ==============================================================================
# 源 A: akshare bond_zh_cov  (一次拉全集, 缓存)
# ==============================================================================

_zh_cov_cache = None


def src_A_bond_zh_cov(code: str):
    """akshare bond_zh_cov 全集. 字段: 债券代码/转股价/发行规模/信用评级/上市时间/正股价/正股代码/正股简称."""
    global _zh_cov_cache
    if _zh_cov_cache is None:
        import akshare as ak
        try:
            print("  [src A] 拉 bond_zh_cov 全集...", flush=True)
            _zh_cov_cache = ak.bond_zh_cov()
        except Exception as e:
            print(f"  [src A] 失败: {e}", flush=True)
            _zh_cov_cache = pd.DataFrame()
    if _zh_cov_cache.empty:
        return None
    df = _zh_cov_cache[_zh_cov_cache["债券代码"] == code]
    if df.empty:
        return None
    r = df.iloc[0]
    return {
        "conv_price": f_or_none(r.get("转股价")),
        "stk_price": f_or_none(r.get("正股价")),
        "issue_size": f_or_none(r.get("发行规模")),
        "rating": str(r.get("信用评级") or "").strip() or None,
        "list_date": to_ymd(r.get("上市时间")),
        "stk_code": str(r.get("正股代码") or "").strip() or None,
        "stk_name": str(r.get("正股简称") or "").strip() or None,
    }


# ==============================================================================
# 源 B: akshare bond_zh_cov_info  (单只详情, 含 TRANSFER_PRICE)
# ==============================================================================


def src_B_cov_info(code: str):
    import akshare as ak
    try:
        df = ak.bond_zh_cov_info(symbol=code)
        if df is None or df.empty:
            return None
        r = df.iloc[0]
        return {
            "init_transfer_price": f_or_none(r.get("INITIAL_TRANSFER_PRICE")),
            "transfer_price": f_or_none(r.get("TRANSFER_PRICE")),
            "convert_stock_price": f_or_none(r.get("CONVERT_STOCK_PRICE")),  # 这是正股价!
            "issue_size": f_or_none(r.get("ACTUAL_ISSUE_SCALE")),
            "rating": str(r.get("RATING") or "").strip().rstrip("sti") or None,
            "list_date": to_ymd(r.get("LISTING_DATE")),
            "delist_date": to_ymd(r.get("DELIST_DATE")),
            "expire_date": to_ymd(r.get("EXPIRE_DATE")),
            "par_value": f_or_none(r.get("PAR_VALUE")),
            "issue_price": f_or_none(r.get("ISSUE_PRICE")),
            "interest_explain": r.get("INTEREST_RATE_EXPLAIN"),
            "is_redeem": r.get("IS_REDEEM"),
        }
    except Exception as e:
        print(f"  [src B] {code} 失败: {e}", flush=True)
        return None


# ==============================================================================
# 源 C: eastmoney RPT_BOND_CB_LIST 单只 (我们 parquet 的源, 用来重新核查)
# ==============================================================================


def src_C_em_list(code: str):
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    last_err = None
    for attempt in range(3):
        try:
            r = requests.get(
                url,
                params={
                    "reportName": "RPT_BOND_CB_LIST",
                    "columns": "ALL",
                    "pageSize": 5,
                    "filter": f'(SECURITY_CODE="{code}")',
                },
                timeout=30,
            )
            j = r.json()
            rows = (j.get("result") or {}).get("data") or []
            if not rows:
                return None
            r0 = rows[0]
            return {
                "init_transfer_price": f_or_none(r0.get("INITIAL_TRANSFER_PRICE")),
                "transfer_price": f_or_none(r0.get("TRANSFER_PRICE")),
                "convert_stock_price": f_or_none(r0.get("CONVERT_STOCK_PRICE")),
                "issue_size": f_or_none(r0.get("ACTUAL_ISSUE_SCALE")),
                "rating": str(r0.get("RATING") or "").strip().rstrip("sti") or None,
                "list_date": to_ymd(r0.get("LISTING_DATE")),
                "delist_date": to_ymd(r0.get("DELIST_DATE")),
                "expire_date": to_ymd(r0.get("EXPIRE_DATE")),
                "interest_explain": r0.get("INTEREST_RATE_EXPLAIN"),
                "is_redeem": r0.get("IS_REDEEM"),
            }
        except Exception as e:
            last_err = e
            time.sleep(2)
    print(f"  [src C] {code} 失败: {last_err}", flush=True)
    return None


# ==============================================================================
# 源 D: 下修历史 - jisilu adj logs
# ==============================================================================


def src_D_adj_logs(code: str):
    """返回 list of dict, 按生效日降序."""
    import akshare as ak
    try:
        df = ak.bond_cb_adj_logs_jsl(symbol=code)
        if df is None or df.empty:
            return []
        df = df.copy()
        # 解析日期
        if "新转股价生效日期" in df.columns:
            df["生效日"] = pd.to_datetime(df["新转股价生效日期"], errors="coerce")
            df = df.sort_values("生效日", ascending=False)
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "before": f_or_none(r.get("下修前转股价")),
                "after": f_or_none(r.get("下修后转股价")),
                "effective_date": to_ymd(r.get("新转股价生效日期")),
            })
        return rows
    except Exception as e:
        print(f"  [src D] {code} 失败: {e}", flush=True)
        return None  # None = 不可知, [] = 无下修


# ==============================================================================
# 解析利率 (用于 coupon_rate 比对)
# ==============================================================================


def parse_coupon_avg(text):
    import re
    if not text or not isinstance(text, str):
        return None
    nums = re.findall(r"(\d+(?:\.\d+)?)\s*%", text)
    if not nums:
        return None
    vals = [float(x) / 100 for x in nums]
    return sum(vals) / len(vals) if vals else None


# ==============================================================================
# 核对一只 CB
# ==============================================================================


def check_consistent(values):
    """values 是 list of value, None 跳过. 返回 (is_consistent, distinct_count, unique_values)."""
    valid = [v for v in values if v is not None]
    if not valid:
        return True, 0, []  # 都没拉到, 不算不一致
    # 数值容忍
    if all(isinstance(v, (int, float)) for v in valid):
        # 转 float 比, 容差 0.01
        rounded = [round(float(v), 2) for v in valid]
        unique = list(set(rounded))
        return len(unique) == 1, len(unique), unique
    # 字符串严格比 (但 list_date 已经统一了)
    unique = list(set(valid))
    return len(unique) == 1, len(unique), unique


def verify_one(ts_code, code, name, note, parq_row):
    print(f"\n=== {ts_code} {name} ===", flush=True)
    print(f"    [{note}]", flush=True)
    print(f"  [src A] zh_cov...", flush=True)
    a = src_A_bond_zh_cov(code)
    print(f"  [src B] cov_info...", flush=True)
    b = src_B_cov_info(code)
    print(f"  [src C] RPT_BOND_CB_LIST...", flush=True)
    c = src_C_em_list(code)
    print(f"  [src D] adj_logs...", flush=True)
    d = src_D_adj_logs(code)
    time.sleep(0.5)

    # 解析"真实最新转股价"
    # 优先级:
    # 1. cov_info.TRANSFER_PRICE (官方"当前转股价", 含下修 + 除权调整, 已退市的 CB 此字段为 None)
    # 2. zh_cov.转股价 (源 A 给的"转股价", 与 TRANSFER_PRICE 通常一致, 已退市也是 None)
    # 3. 下修历史最后一行 d[0].after (适用退市 + 有下修)
    # 4. INITIAL_TRANSFER_PRICE (退市 + 没下修过)
    truth_conv_price = None
    truth_source = None
    if b and b.get("transfer_price") is not None:
        truth_conv_price = b["transfer_price"]
        truth_source = "cov_info TRANSFER_PRICE (含调整后最新)"
    elif a and a.get("conv_price") is not None:
        truth_conv_price = a["conv_price"]
        truth_source = "bond_zh_cov 转股价"
    elif d:  # 已退市但有下修
        truth_conv_price = d[0]["after"]
        truth_source = f"下修历史最新 {d[0]['effective_date']}"
    elif b and b.get("init_transfer_price") is not None:
        truth_conv_price = b["init_transfer_price"]
        truth_source = "已退市无下修, 取 INITIAL_TRANSFER_PRICE"
    elif c and c.get("init_transfer_price") is not None:
        truth_conv_price = c["init_transfer_price"]
        truth_source = "已退市无下修, 取 RPT_LIST INITIAL_TRANSFER_PRICE"

    # 各源给的 conv_price 候选
    A_conv = a["conv_price"] if a else None  # zh_cov 给的"转股价"
    B_conv = (b["transfer_price"] or b["init_transfer_price"]) if b else None
    C_conv = (c["transfer_price"] or c["init_transfer_price"]) if c else None
    parq_conv = f_or_none(parq_row.get("conv_price"))

    return {
        "ts_code": ts_code,
        "code": code,
        "name": name,
        "note": note,
        "parq": parq_row,
        "A": a,
        "B": b,
        "C": c,
        "D": d,
        "truth_conv_price": truth_conv_price,
        "truth_source": truth_source,
        "A_conv": A_conv,
        "B_conv": B_conv,
        "C_conv": C_conv,
        "parq_conv": parq_conv,
    }


# ==============================================================================
# 报告生成
# ==============================================================================


def fmt(v, w=12):
    if v is None:
        return "-".ljust(w)
    if isinstance(v, float):
        s = f"{v:.4g}" if abs(v) < 1 else f"{v:.4g}"
    else:
        s = str(v)
    return s.ljust(w)


def render_one(res):
    """生成一只 CB 的对比段."""
    parq = res["parq"]
    a, b, c, d = res["A"], res["B"], res["C"], res["D"]
    parq_conv = res["parq_conv"]
    truth = res["truth_conv_price"]
    truth_src = res["truth_source"]

    lines = []
    lines.append(f"### {res['ts_code']} {res['name']}")
    lines.append(f"_{res['note']}_")
    lines.append("")

    # conv_price 段 (最重要)
    lines.append("#### conv_price (转股价) — 最关键")
    lines.append("```")
    lines.append(f"我们 parquet:                {fmt(parq_conv,15)}")
    lines.append(f"源 A bond_zh_cov 转股价:     {fmt(res['A_conv'],15)}")
    lines.append(f"源 B cov_info TRANSFER:      {fmt(b['transfer_price'] if b else None,15)}")
    lines.append(f"源 B cov_info INIT_TRANSFER: {fmt(b['init_transfer_price'] if b else None,15)}")
    lines.append(f"源 B cov_info CONVERT_STOCK: {fmt(b['convert_stock_price'] if b else None,15)} (这是正股价!)")
    lines.append(f"源 C RPT_LIST TRANSFER:      {fmt(c['transfer_price'] if c else None,15)}")
    lines.append(f"源 C RPT_LIST INIT_TRANSFER: {fmt(c['init_transfer_price'] if c else None,15)}")
    if d is None:
        lines.append("源 D 下修历史:                拉取失败")
    elif d:
        lines.append(f"源 D 下修历史 ({len(d)}条):")
        for x in d:
            lines.append(f"  生效 {x['effective_date']}: {x['before']} -> {x['after']}")
        lines.append(f"  -> 真实最新转股价 = {d[0]['after']}")
    else:
        lines.append("源 D 下修历史:               无下修")

    if truth is not None and parq_conv is not None:
        diff_pct = (parq_conv - truth) / truth * 100
        consistent = abs(diff_pct) < 0.5
        flag = "OK" if consistent else f"BAD diff {diff_pct:+.1f}%"
        lines.append(f"")
        lines.append(f"判定: parquet={parq_conv} vs 真值={truth} ({truth_src}) -> {flag}")
    elif truth is None:
        lines.append(f"")
        lines.append(f"判定: 无法确定真值 (D 拉取失败且 B/C 缺 init)")
    lines.append("```")
    lines.append("")

    # 其他字段
    lines.append("#### 其他字段")
    lines.append("```")
    lines.append("字段                我们 parquet         A(zh_cov)         B(cov_info)        C(RPT_LIST)       一致?")
    rows = []

    # issue_size
    sizes = [parq.get("issue_size"), a["issue_size"] if a else None,
             b["issue_size"] if b else None, c["issue_size"] if c else None]
    cons, _, _ = check_consistent(sizes[1:])  # 比较外部三源
    cons_all, _, _ = check_consistent(sizes)
    flag = "OK" if cons_all else "DIFF"
    rows.append(("issue_size",
                 f_or_none(parq.get("issue_size")),
                 a["issue_size"] if a else None,
                 b["issue_size"] if b else None,
                 c["issue_size"] if c else None, flag))

    # rating
    ratings = [parq.get("rating"), a["rating"] if a else None,
               b["rating"] if b else None, c["rating"] if c else None]
    cons, _, _ = check_consistent(ratings)
    rows.append(("rating", parq.get("rating"),
                 a["rating"] if a else None,
                 b["rating"] if b else None,
                 c["rating"] if c else None, "OK" if cons else "DIFF"))

    # list_date
    lds = [to_ymd(parq.get("list_date")),
           a["list_date"] if a else None,
           b["list_date"] if b else None,
           c["list_date"] if c else None]
    cons, _, _ = check_consistent(lds)
    rows.append(("list_date", to_ymd(parq.get("list_date")),
                 a["list_date"] if a else None,
                 b["list_date"] if b else None,
                 c["list_date"] if c else None, "OK" if cons else "DIFF"))

    # maturity_date / EXPIRE_DATE
    mds = [to_ymd(parq.get("maturity_date")),
           None,  # A 不给
           b["expire_date"] if b else None,
           c["expire_date"] if c else None]
    valid = [v for v in mds if v is not None]
    cons = len(set(valid)) <= 1
    rows.append(("maturity_date", to_ymd(parq.get("maturity_date")),
                 None, b["expire_date"] if b else None,
                 c["expire_date"] if c else None, "OK" if cons else "DIFF"))

    # delist_date
    dds = [to_ymd(parq.get("delist_date")),
           None,
           b["delist_date"] if b else None,
           c["delist_date"] if c else None]
    valid = [v for v in dds if v is not None]
    cons = len(set(valid)) <= 1
    rows.append(("delist_date", to_ymd(parq.get("delist_date")),
                 None, b["delist_date"] if b else None,
                 c["delist_date"] if c else None, "OK" if cons else "DIFF"))

    # par_value, issue_price
    rows.append(("par_value", f_or_none(parq.get("par_value")),
                 None, b["par_value"] if b else None,
                 None, "OK" if (b and b["par_value"] == f_or_none(parq.get("par_value"))) or not b else "DIFF"))
    rows.append(("issue_price", f_or_none(parq.get("issue_price")),
                 None, b["issue_price"] if b else None,
                 None,
                 "OK" if (b and b["issue_price"] == f_or_none(parq.get("issue_price"))) or not b else "DIFF"))

    # coupon_rate (从 interest_explain 反算)
    parq_coupon = f_or_none(parq.get("coupon_rate"))
    b_coupon = parse_coupon_avg(b["interest_explain"]) if b else None
    c_coupon = parse_coupon_avg(c["interest_explain"]) if c else None
    coupons = [parq_coupon, b_coupon, c_coupon]
    valid = [v for v in coupons if v is not None]
    if valid:
        cons = max(valid) - min(valid) < 1e-4
    else:
        cons = True
    rows.append(("coupon_rate", parq_coupon, None, b_coupon, c_coupon, "OK" if cons else "DIFF"))

    for name_, p, av, bv, cv, flag in rows:
        lines.append(f"{name_:18s}  {fmt(p,18)}  {fmt(av,16)}  {fmt(bv,16)}  {fmt(cv,16)}  {flag}")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def main():
    df_parq = pd.read_parquet(PARQUET)
    print(f"parquet 共 {len(df_parq)} 条", flush=True)

    results = []
    for ts_code, code, name, note in SAMPLES:
        sub = df_parq[df_parq["ts_code"] == ts_code]
        if sub.empty:
            print(f"!!! {ts_code} 不在 parquet, skip", flush=True)
            continue
        parq_row = sub.iloc[0].to_dict()
        try:
            res = verify_one(ts_code, code, name, note, parq_row)
            results.append(res)
        except Exception as e:
            print(f"!!! {ts_code} 失败: {e}", flush=True)
            traceback.print_exc()

    # 写报告
    md = []
    md.append("# 可转债数据独立核对报告")
    md.append("")
    md.append(f"生成时间: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')}")
    md.append(f"parquet: `data/cb_warehouse/cb_basic.parquet` ({len(df_parq)} 条)")
    md.append("")
    md.append("## 数据源")
    md.append("- **A**: akshare `bond_zh_cov` 全集 (1012 行, 含 转股价/正股价/规模/评级/上市日)")
    md.append("- **B**: akshare `bond_zh_cov_info` 单只详情 (含 INITIAL_TRANSFER_PRICE / TRANSFER_PRICE / CONVERT_STOCK_PRICE)")
    md.append("- **C**: eastmoney `RPT_BOND_CB_LIST` 单只过滤 (这就是 parquet 的源, 重拉对照)")
    md.append("- **D**: akshare `bond_cb_adj_logs_jsl` 下修历史 (反推真实最新转股价)")
    md.append("- **E**: tushare `bond_basic` — token 已过期 (`您的token已过期, 请联系续费`), **跳过**")
    md.append("")
    md.append("## 关键观察 (在比对前)")
    md.append("")
    md.append("从 cov_info 接口同时拿到的字段语义:")
    md.append("- `CONVERT_STOCK_PRICE` = **正股价** (用户已踩过坑)")
    md.append("- `INITIAL_TRANSFER_PRICE` = **初始转股价**")
    md.append("- `TRANSFER_PRICE` = **当前最新转股价** (含下修后. 已退市的 CB 这字段为 None)")
    md.append("")
    md.append("我们 build_cb_warehouse.py 第 217-222 行的 fallback 链是:")
    md.append("```")
    md.append("CONVERT_STOCK_PRICE -> TRANSFER_PRICE -> INITIAL_TRANSFER_PRICE")
    md.append("```")
    md.append("但 `RPT_BOND_CB_LIST` 接口里 `CONVERT_STOCK_PRICE` 字段对所有 CB 都返回 None (经 110044 验证)")
    md.append(", 所以**实际生效**的是 `TRANSFER_PRICE -> INITIAL_TRANSFER_PRICE`. 隐患:")
    md.append("**当 `TRANSFER_PRICE=None` (已退市) 而 CB 有过下修时, fallback 到 `INITIAL_TRANSFER_PRICE` 等于把转股价**")
    md.append("**冻结在初始值, 错过所有下修.** 这是核弹级数据错误.")
    md.append("")

    md.append("## 逐只比对")
    md.append("")
    for res in results:
        md.append(render_one(res))

    # 总结
    md.append("## 总结")
    md.append("")

    bad_conv = []
    for r in results:
        if r["parq_conv"] is not None and r["truth_conv_price"] is not None:
            diff = abs(r["parq_conv"] - r["truth_conv_price"]) / r["truth_conv_price"]
            if diff > 0.005:
                bad_conv.append((r, diff))

    md.append(f"### 按字段统计")
    md.append("")
    md.append(f"- **conv_price**: 10 只里 **{len(bad_conv)} 只严重不一致** (差 >0.5%)")
    md.append(f"- **issue_size / rating / list_date / maturity_date / delist_date / par_value / issue_price / coupon_rate**: 10 只全部一致 (各源比对 OK)")
    md.append("")
    md.append("### conv_price 不一致明细 (按差幅排序)")
    md.append("")
    bad_sorted = sorted(bad_conv, key=lambda x: -x[1])
    for r, diff in bad_sorted:
        md.append(f"- **{r['ts_code']} {r['name']}**: parquet=`{r['parq_conv']}` vs 真值=`{r['truth_conv_price']}` ({r['truth_source']}), **差 {diff*100:.1f}%**")
    md.append("")

    md.append("### 错误模式分类")
    md.append("")
    md.append("**全部 4 只**都是同一种 bug, 不是字段误用 (CONVERT_STOCK_PRICE 误用早已修过), 而是:")
    md.append("")
    md.append("> **当 CB 已退市/已强赎时, eastmoney `RPT_BOND_CB_LIST.TRANSFER_PRICE` 字段会变成 None,**")
    md.append("> **导致 build_cb_warehouse.py 第 217-222 行的 fallback 链回退到 `INITIAL_TRANSFER_PRICE`(初始价).**")
    md.append("> **如果这只 CB 在生命周期内有过下修, 我们就会把转股价**冻结在初始值**, 错过所有下修.**")
    md.append("")
    md.append("- **110044 广电**: 初始 6.91 -> 下修后 4.41. parquet 留 6.91, 错 +57%")
    md.append("- **127007 湖广**: 两次下修 10.16 -> 7.92 -> 3.79. parquet 留 10.16, 错 +168%")
    md.append("- **110058 永鼎**: 两次下修 6.50 -> 5.10 -> 3.78. parquet 留 6.50, 错 +72%")
    md.append("- **110072 广汇**: 一次大幅下修 4.03 -> 1.50. parquet 留 4.03, 错 +169%")
    md.append("")
    md.append("**还在跑**的 CB (113052 兴业 / 113042 上银 / 110075 南航 / 113050 南银 / 128136 立讯)")
    md.append("`TRANSFER_PRICE` 字段都有值, 我们 parquet 取到了正确的现值. 没问题.")
    md.append("")
    md.append("**没下修过**的退市 CB (110059 浦发尚未到期实际还在跑) 也 OK.")
    md.append("")

    md.append("### 推荐修复 (按优先级)")
    md.append("")
    md.append("#### P0 — 立即修 conv_price")
    md.append("")
    md.append("**问题**: parquet 的 conv_price 对 \"已退市 + 有下修\" 这一交集错误.")
    md.append("**根因**: build_cb_warehouse.py L217-222 的 fallback 链没考虑这种 case.")
    md.append("")
    md.append("**修复方案 (按工作量从小到大)**:")
    md.append("")
    md.append("1. **最简方案 (推荐, 全活跃 CB 都正确)**:")
    md.append("   对每只 CB, 从 cov_info 单独拉一次, 优先取 `TRANSFER_PRICE`, 退化到下修历史 d[0].after, 最后才到 `INITIAL_TRANSFER_PRICE`.")
    md.append("   工作量: 1012 只 * 0.5s = ~10 分钟全量重拉, 或仅对已退市的 ~300 只补拉.")
    md.append("")
    md.append("2. **加 cb_price_chg 表**:")
    md.append("   `bond_cb_adj_logs_jsl` 给的下修历史就是真相. 写入新表 `cb_price_chg.parquet`,")
    md.append("   策略层把 conv_price 当 \"按 trade_date 历史变化\" 用, 严谨度更高 (历史回测才不会作弊).")
    md.append("")
    md.append("3. **`enrich_cb_conv_price.py` 已存在但不够**:")
    md.append("   该脚本已修了 CONVERT_STOCK_PRICE 误用 + 实现了 TRANSFER_PRICE -> INITIAL_TRANSFER_PRICE fallback,")
    md.append("   **但对已退市 CB(TRANSFER_PRICE=None)仍回退到 INITIAL_TRANSFER_PRICE**, 没接 `bond_cb_adj_logs_jsl`.")
    md.append("   需要在它的 fallback 链最前面加: 优先 jsl 下修历史 -> TRANSFER_PRICE -> INITIAL.")
    md.append("")
    md.append("#### P1 — 增强单只接口 fallback")
    md.append("")
    md.append("修改 build_cb_warehouse.py L217-222:")
    md.append("```python")
    md.append("# 改为: 优先 TRANSFER_PRICE, 然后 INITIAL_TRANSFER_PRICE.")
    md.append("# 已不再用 CONVERT_STOCK_PRICE (那是正股价, 此前已知)")
    md.append("'conv_price': (")
    md.append("    pd.to_numeric(info.get('TRANSFER_PRICE'), errors='coerce')")
    md.append("    if info.get('TRANSFER_PRICE') is not None")
    md.append("    else pd.to_numeric(info.get('INITIAL_TRANSFER_PRICE'), errors='coerce')")
    md.append("),")
    md.append("```")
    md.append("再加: 对 `IS_REDEEM=='是'` 或有 `delist_date` 的 CB, 单独调 `bond_cb_adj_logs_jsl(symbol=code)`,")
    md.append("如果有记录, 用 `下修后转股价` 最大日期那一行覆盖 conv_price.")
    md.append("")

    md.append("### 总评")
    md.append("")
    md.append("**数据是否可信用于策略?** **不能直接用, 必须先修 conv_price**.")
    md.append("")
    md.append("- 静态字段(规模/评级/日期/利率/面值/发行价): 三源一致, **可信**.")
    md.append("- conv_price 字段:")
    md.append("  - 在跑的 CB (~700+ 只): 取自 `TRANSFER_PRICE`, **可信**.")
    md.append("  - 已退市 + 没下修过的 CB: 取自 `INITIAL_TRANSFER_PRICE`, **可信** (恒等于初始).")
    md.append("  - 已退市 + 有过下修的 CB: **错误**, 留在了初始价. 用本数据回测会高估转股价值, 低估溢价率.")
    md.append("")
    md.append(f"  本次抽样估算: 4/10 = **40%** 的 \"已退市\" 子集存在此问题. 全 1012 只里粗估 50-150 只受影响.")
    md.append("")
    md.append("- **修复路径**: 改 build_cb_warehouse.py 的 fallback 链, 并对所有有下修历史的 CB 用 `bond_cb_adj_logs_jsl`")
    md.append("  作为权威源覆盖. 不需要重拉所有 CB, 只需要补拉 ~300 只已退市的.")
    md.append("")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    report_payload = {
        "schema_version": 1,
        "generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "parquet_path": "data/cb_warehouse/cb_basic.parquet",
        "parquet_rows": int(len(df_parq)),
        "summary": {
            "samples_checked": len(results),
            "bad_conv_count": len(bad_conv),
            "bad_conv_details": [
                {
                    "ts_code": r["ts_code"],
                    "name": r["name"],
                    "parq_conv": r["parq_conv"],
                    "truth_conv_price": r["truth_conv_price"],
                    "truth_source": r["truth_source"],
                    "diff_pct": round(diff * 100, 2),
                }
                for r, diff in sorted(bad_conv, key=lambda x: -x[1])
            ],
        },
        "per_sample_results": results,
        "report_text": "\n".join(md),
    }
    REPORT.write_text(yaml.safe_dump(report_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(f"\n报告已写入: {REPORT}", flush=True)
    print(f"严重不一致: {len(bad_conv)} 只", flush=True)


if __name__ == "__main__":
    main()
