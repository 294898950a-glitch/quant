#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可转债退出方式 —— 纯事实统计 (不建模 / 不回测 / 不提策略建议).

回答:
  1. 历史上退市的可转债怎么退出的? 分几类, 各占多少?
  2. 实际存续时长 / 名义期限 的分布 (分强赎组 / 非强赎组).
  3. 退出方式的年度分布.
  4. 存续中的转债: 多少只, 已存续多久, 占名义期限多少.
  5. 幸存者偏差方向 + 强赎最终占比的上下界.

>>> 关键数据陷阱 (脚本会自己验证并打印证据) <<<
    cb_basic.maturity_date (来自 eastmoney EXPIRE_DATE) 对**已退市**转债而言
    不是合同约定的到期日, 而是被改写成了实际摘牌/兑付日。因此绝不能直接用
    maturity_date - value_date 当名义期限。本脚本改从 interest_rate_explain
    的票面利率条款文本里解析合同年限 (第一年…第六年 -> 6 年), 并用存续中转债
    做交叉校验。

只读数据 (data/cb_warehouse/), 输出全部落在 study/exit_mode/.

用法:
  python study/cb_exit_mode_stats.py
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    HAVE_MPL = True
    _MPL_ERR = ""
except Exception as _exc:  # pragma: no cover
    HAVE_MPL = False
    _MPL_ERR = repr(_exc)

ROOT = Path(__file__).resolve().parent.parent
WAREHOUSE = ROOT / "data" / "cb_warehouse"
OUT = ROOT / "study" / "exit_mode"
OUT.mkdir(parents=True, exist_ok=True)

# 退市日落在 (推定名义到期日 ± TOL) 内 -> 视为 "持有到到期". 30 天是人为选的,
# 报告里给了 10/20/30/60/90 的敏感性 (结论对它极不敏感).
TOL_MATURITY_DAYS = 30
# 提前退出组内, 退市前 20 个交易日最高收盘价 >= 该阈值 -> 判为转股价值驱动的强赎.
# 依据: 强赎触发条件是正股价连续 15/30 日 >= 转股价 130%, 触发后转债价格贴近转股价值.
CALL_PRICE_FLOOR = 115.0
# 退市前最后收盘价 < 该阈值 -> 判为信用事件 (违约 / 正股退市) 而非强赎.
DISTRESS_CLOSE = 90.0

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 80)
pd.set_option("display.max_rows", 400)

_LOG: list[str] = []

LBL_CALL = "强赎(提前赎回)"
LBL_MAT = "到期(持有到名义到期日)"
LBL_DEF = "违约/正股退市"
LBL_EARLY_UNK = "提前退出-无法归类"
LBL_NO_TENOR = "无法分类(名义期限不可得)"
LBL_ANOM = "异常(退市晚于名义到期)"
LBL_ALIVE = "存续中"

EN = {LBL_CALL: "forced call", LBL_MAT: "held to maturity",
      LBL_DEF: "default / stock delisted", LBL_EARLY_UNK: "early exit, unclassified",
      LBL_NO_TENOR: "tenor unknown", LBL_ANOM: "anomaly", LBL_ALIVE: "alive"}

ORDINALS = ["第一年", "第二年", "第三年", "第四年", "第五年",
            "第六年", "第七年", "第八年", "第九年", "第十年"]


def log(msg: object = "") -> None:
    s = str(msg)
    print(s, flush=True)
    _LOG.append(s)


def to_dt(s: pd.Series) -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(s):
        return s
    ss = s.astype("string").str.strip()
    ss = ss.replace({"": pd.NA, "nan": pd.NA, "None": pd.NA, "NaT": pd.NA, "<NA>": pd.NA})
    out = pd.to_datetime(ss, format="%Y%m%d", errors="coerce")
    bad = out.isna() & ss.notna()
    if bad.any():
        out = out.where(~bad, pd.to_datetime(ss.where(bad), errors="coerce"))
    return out


def pct(n: float, d: float) -> str:
    return "n/a" if not d else f"{100.0 * n / d:.1f}%"


def q_table(x: pd.Series, name: str) -> pd.Series:
    x = pd.to_numeric(x, errors="coerce").dropna()
    if x.empty:
        return pd.Series({k: np.nan for k in
                          ["n", "mean", "min", "p05", "p25", "median", "p75", "p95", "max"]},
                         name=name)
    return pd.Series({
        "n": int(x.size), "mean": x.mean(), "min": x.min(),
        "p05": x.quantile(.05), "p25": x.quantile(.25), "median": x.median(),
        "p75": x.quantile(.75), "p95": x.quantile(.95), "max": x.max(),
    }, name=name)


def md_table(df: pd.DataFrame, fmt: str = "{:.3f}", index_name: str | None = None) -> str:
    d = df.copy()
    for c in d.columns:
        if pd.api.types.is_float_dtype(d[c]):
            d[c] = d[c].map(lambda v: "" if pd.isna(v) else fmt.format(v))
        else:
            d[c] = d[c].astype(str)
    head = [str(index_name if index_name is not None else (d.index.name or ""))]
    head += [str(c) for c in d.columns]
    lines = ["| " + " | ".join(head) + " |",
             "| " + " | ".join(["---"] * len(head)) + " |"]
    for idx, row in d.iterrows():
        lines.append("| " + " | ".join([str(idx)] + list(row.values)) + " |")
    return "\n".join(lines)


def parse_tenor(text: object) -> float:
    """从票面利率条款文本解析合同年限: 出现 '第六年' -> 6 年."""
    if not isinstance(text, str):
        return np.nan
    n = np.nan
    for i, o in enumerate(ORDINALS):
        if o in text:
            n = i + 1
    return float(n) if n == n else np.nan


# ===========================================================================
# 0. 载入 + schema 探查
# ===========================================================================
def load_and_probe() -> tuple[pd.DataFrame, pd.DataFrame]:
    basic = pd.read_parquet(WAREHOUSE / "cb_basic.parquet")
    call = pd.read_parquet(WAREHOUSE / "cb_call.parquet")

    log("=" * 82)
    log("SECTION 0  schema 探查")
    log("=" * 82)
    log(f"\ncb_basic : {basic.shape[0]} 行 x {basic.shape[1]} 列")
    log("  columns: " + ", ".join(map(str, basic.columns)))
    log("\ncb_basic 缺失统计:")
    for c, v in basic.isna().sum().items():
        log(f"    {str(c):26s} null={int(v):5d}  ({pct(int(v), len(basic))})")

    log(f"\ncb_call  : {call.shape[0]} 行 x {call.shape[1]} 列")
    log("  columns: " + ", ".join(map(str, call.columns)))
    log("  dtypes : " + ", ".join(f"{c}={t}" for c, t in call.dtypes.items()))
    log("\ncb_call 缺失统计:")
    for c, v in call.isna().sum().items():
        log(f"    {str(c):26s} null={int(v):5d}  ({pct(int(v), len(call))})")
    log("\ncb_call 前 12 行:")
    log(call.head(12).to_string())
    log("\ncb_call 低基数列取值分布:")
    for c in call.columns:
        nu = call[c].nunique(dropna=False)
        if nu <= 25:
            log(f"\n  --- {c} (nunique={nu}) ---")
            for k, v in call[c].value_counts(dropna=False).items():
                log(f"      {str(k)[:56]:58s} {int(v):5d}")
    log(f"\ncb_call 唯一 ts_code = {call['ts_code'].nunique()} / {len(call)} 行 "
        f"(重复 {int(call['ts_code'].duplicated().sum())} 条)")
    log(f"cb_basic 唯一 ts_code = {basic['ts_code'].nunique()} / {len(basic)} 行")
    return basic, call


# ===========================================================================
# 1. 主表 + cb_call 语义判定
# ===========================================================================
def build_master(basic: pd.DataFrame, call: pd.DataFrame) -> pd.DataFrame:
    b = basic.copy()
    for c in ["value_date", "list_date", "delist_date", "maturity_date",
              "transfer_start_date", "transfer_end_date"]:
        if c in b.columns:
            b[c] = to_dt(b[c])

    c = call.copy()
    for col in ["ann_date", "call_date", "expire_date"]:
        if col in c.columns:
            c[col] = to_dt(c[col])
    c1 = c.drop_duplicates(subset=["ts_code"], keep="first")

    keep = ["ts_code"] + [x for x in ["ann_date", "call_date", "call_price",
                                      "is_call", "call_type", "expire_date"]
                          if x in c1.columns]
    m = b.merge(c1[keep].rename(columns={"expire_date": "call_expire_date"}),
                on="ts_code", how="left")
    m["has_call_record"] = m["ts_code"].isin(set(c1["ts_code"]))
    m["delisted"] = m["delist_date"].notna()

    log("\n" + "=" * 82)
    log("SECTION 0b  cb_call 到底是什么表 (决定能不能拿它当 '强赎标记')")
    log("=" * 82)
    log("\n来源 (scripts/build_cb_warehouse.py, build_cb_basic_and_call, L237-254):")
    log("对 eastmoney RPT_BOND_CB_LIST 中 IS_REDEEM=='是' 的转债写一行, 字段映射为")
    log("  ann_date   <- NOTICE_DATE_HS / NOTICE_DATE_SH   (赎回相关公告日)")
    log("  call_date  <- EXECUTE_START_DATEHS / ...SH      (赎回执行起始日)")
    log("  call_price <- EXECUTE_PRICE_HS / ...SH          (赎回价)")
    log("  is_call    <- 硬编码常量 '公告实施强赎' (不是数据, 是写死的字符串)")
    log("  expire_date<- EXPIRE_DATE")

    ct = pd.crosstab(m["delisted"], m["has_call_record"])
    log("\n[证据1] cb_call 覆盖率 (行=是否已退市, 列=是否有 cb_call 记录):")
    log(ct.to_string())
    log(f"  => 1012 只里有 {int(m['has_call_record'].sum())} 只有记录, "
        f"其中包含全部 {int((~m['delisted']).sum())} 只**存续中**的转债.")
    log("  => IS_REDEEM=='是' 不是 '已被强赎', 更像是 '含赎回条款' (标准转债几乎都有).")
    log("     所以 cb_call 的**存在与否**不能当强赎标记.")

    alive_call = m[(~m["delisted"]) & m["call_date"].notna()]
    log(f"\n[证据2] 存续中却有 call_date 的转债: {len(alive_call)} 只.")
    log("  若 call_date 真是 '强赎执行日', 这些债早该摘牌了. 抽样 (call_date 最早的 8 只):")
    if len(alive_call):
        log(alive_call.sort_values("call_date")[
            ["ts_code", "bond_short_name", "value_date", "maturity_date",
             "ann_date", "call_date", "call_price"]].head(8).to_string(index=False))
    log("  => ann_date / call_date 的语义无法从数据本身唯一确定 (可能混入了回售执行、")
    log("     '满足赎回条件但不行使' 公告等). 本脚本因此**不用 cb_call 做分类**,")
    log("     只把它当旁证打印出来. 这是明确的推断不确定性.")

    if "call_price" in m.columns:
        cp = pd.to_numeric(m["call_price"], errors="coerce")
        log("\n[证据3] call_price 分布 (赎回价 = 面值+当期利息, 强赎/到期赎回/回售都长这样):")
        log(q_table(cp, "call_price").to_string())
        log("  => call_price 也无法区分事件类型.")

    return m


# ===========================================================================
# 2. 名义期限: maturity_date 不可信, 改从票面利率条款解析
# ===========================================================================
def derive_nominal_term(m: pd.DataFrame) -> pd.DataFrame:
    m = m.copy()
    m["tenor_years"] = m["interest_rate_explain"].map(parse_tenor)
    m["nominal_maturity"] = [
        (v + pd.DateOffset(years=int(t))) if (pd.notna(v) and pd.notna(t)) else pd.NaT
        for v, t in zip(m["value_date"], m["tenor_years"])
    ]

    log("\n" + "=" * 82)
    log("SECTION 0c  ⚠ maturity_date 对已退市转债是被改写过的 (本文最重要的数据发现)")
    log("=" * 82)
    raw_nom = (m["maturity_date"] - m["value_date"]).dt.days / 365.25
    log("\n[证据1] 直接用 maturity_date - value_date 当名义期限, 按是否退市分组:")
    g = pd.DataFrame({
        "存续中": q_table(raw_nom[~m["delisted"]], "alive"),
        "已退市": q_table(raw_nom[m["delisted"]], "delisted"),
    }).T
    log(g.to_string(float_format=lambda x: f"{x:.2f}"))
    log("  => 存续中的转债名义期限清一色 ≈6.0 年 (标准合同期限);")
    log("     已退市的却中位数只有 ~2.7 年. 合同期限不可能因为退市而变短.")
    log("     结论: eastmoney 的 EXPIRE_DATE 在转债退出后被改写成了实际兑付日.")
    dg = (m.loc[m["delisted"], "delist_date"] - m.loc[m["delisted"], "maturity_date"]).dt.days
    log(f"\n[证据2] 已退市样本 delist_date - maturity_date (天): "
        f"中位数 {dg.median():.0f}, |diff|<=15 天的占 "
        f"{pct(int((dg.abs() <= 15).sum()), int(dg.notna().sum()))}")
    log("  => 退市日和 maturity_date 几乎重合, 印证 maturity_date 已被改写.")

    log(f"\n[修复] 改从 interest_rate_explain 解析合同年限 (文本里出现 '第六年' -> 6 年):")
    log(m["tenor_years"].value_counts(dropna=False).to_string())
    al = m[~m["delisted"]]
    diff = (al["nominal_maturity"] - al["maturity_date"]).dt.days
    ok = int((diff.abs() <= 3).sum())
    log(f"\n[校验] 用存续中转债 (maturity_date 未被改写) 交叉验证解析结果:")
    log(f"  {ok} / {len(al)} 只的 value_date+tenor 与 maturity_date 相差 <=3 天 "
        f"({pct(ok, len(al))})")
    bad = al[(diff.abs() > 3) | diff.isna()]
    if len(bad):
        log(f"  不一致的 {len(bad)} 只 (多为快照期间刚被赎回、EXPIRE_DATE 已被改写但 "
            f"DELIST_DATE 尚未更新的):")
        log(bad[["ts_code", "bond_short_name", "value_date", "maturity_date",
                 "nominal_maturity", "tenor_years"]].to_string(index=False))
    n_no = int(m["tenor_years"].isna().sum())
    log(f"\n  无法解析合同年限的: {n_no} 只 -> 归入 '{LBL_NO_TENOR}', 不硬猜.")
    if n_no:
        log(m[m["tenor_years"].isna()][
            ["ts_code", "bond_short_name", "value_date", "delist_date",
             "maturity_date", "has_call_record"]].to_string(index=False))
    return m


# ===========================================================================
# 3. 行情交叉信号
# ===========================================================================
def attach_market_signals(m: pd.DataFrame) -> tuple[pd.DataFrame, pd.Timestamp]:
    daily = pd.read_parquet(WAREHOUSE / "cb_daily.parquet",
                            columns=["ts_code", "trade_date", "close"])
    daily["trade_date"] = to_dt(daily["trade_date"])
    daily = daily.sort_values(["ts_code", "trade_date"])
    data_asof = daily["trade_date"].max()

    last = daily.groupby("ts_code").tail(1)[["ts_code", "trade_date", "close"]].rename(
        columns={"trade_date": "cb_last_trade_date", "close": "last_close"})
    agg = daily.groupby("ts_code")["close"].agg(
        max_close="max",
        max_close_last20=lambda s: s.tail(20).max()).reset_index()
    m = m.merge(last, on="ts_code", how="left").merge(agg, on="ts_code", how="left")

    stk = pd.read_parquet(WAREHOUSE / "stk_daily_qfq.parquet",
                          columns=["stk_code", "trade_date"])
    stk["trade_date"] = to_dt(stk["trade_date"])
    slast = stk.groupby("stk_code")["trade_date"].max().rename("stk_last_trade_date").reset_index()
    m["stk_code"] = m["stk_code"].astype(str).str.strip()
    slast["stk_code"] = slast["stk_code"].astype(str).str.strip()
    m = m.merge(slast, on="stk_code", how="left")
    hit = int(m["stk_last_trade_date"].notna().sum())
    log(f"\n[join] 正股行情匹配上的转债: {hit} / {len(m)} ({pct(hit, len(m))}); "
        f"未匹配上的 {len(m) - hit} 只 —— 正股不在股票宇宙里, 通常意味着正股已退市.")
    return m, data_asof


# ===========================================================================
# 4. 分类
# ===========================================================================
def classify(m: pd.DataFrame, data_asof: pd.Timestamp) -> pd.DataFrame:
    """分类只用: 日期 + 行情. 不用 cb_call (语义不明, 见 SECTION 0b)."""
    m = m.copy()
    m["gap_days"] = (m["nominal_maturity"] - m["delist_date"]).dt.days
    m["stk_missing"] = m["stk_last_trade_date"].isna()
    # 正股"在转债退市前就死了": 正股最后交易日比 min(退市日, 数据截止日) 早 30 天以上.
    # 必须和数据截止日取 min —— 否则退市日在数据截止日之后的转债会被误判.
    ref = m["delist_date"].where(m["delist_date"] < data_asof, data_asof)
    m["stk_dead_before_exit"] = (
        m["stk_last_trade_date"].notna() &
        (m["stk_last_trade_date"] < ref - pd.Timedelta(days=30)))

    def _one(r) -> str:
        if pd.isna(r["delist_date"]):
            return LBL_ALIVE
        if pd.isna(r["gap_days"]):
            return LBL_NO_TENOR
        g = r["gap_days"]
        if g < -TOL_MATURITY_DAYS:
            return LBL_ANOM
        if abs(g) <= TOL_MATURITY_DAYS:
            return LBL_MAT
        # --- 明显提前退出 ---
        lc, mx20 = r["last_close"], r["max_close_last20"]
        if (pd.notna(lc) and lc < DISTRESS_CLOSE) or r["stk_dead_before_exit"]:
            return LBL_DEF
        if pd.notna(mx20) and mx20 >= CALL_PRICE_FLOOR:
            return LBL_CALL
        return LBL_EARLY_UNK

    m["exit_mode"] = m.apply(_one, axis=1)
    return m


# ===========================================================================
# 主流程
# ===========================================================================
def main() -> dict:
    basic, call = load_and_probe()
    m = build_master(basic, call)
    m = derive_nominal_term(m)
    m, data_asof = attach_market_signals(m)
    m = classify(m, data_asof)

    m["nominal_days"] = (m["nominal_maturity"] - m["value_date"]).dt.days
    m["actual_days"] = (m["delist_date"] - m["value_date"]).dt.days
    m["actual_days_list"] = (m["delist_date"] - m["list_date"]).dt.days
    m["nominal_days_list"] = (m["nominal_maturity"] - m["list_date"]).dt.days
    m["ratio"] = m["actual_days"] / m["nominal_days"]
    m["ratio_list"] = m["actual_days_list"] / m["nominal_days_list"]
    m["elapsed_days"] = np.where(m["delisted"], np.nan,
                                 (data_asof - m["value_date"]).dt.days)
    m["elapsed_ratio"] = m["elapsed_days"] / m["nominal_days"]
    m["delist_year"] = m["delist_date"].dt.year
    m["value_year"] = m["value_date"].dt.year

    # ---------------- SECTION 1 ----------------
    log("\n" + "=" * 82)
    log("SECTION 1  样本覆盖与数据质量")
    log("=" * 82)
    n_all, n_del = len(m), int(m["delisted"].sum())
    n_alive = n_all - n_del
    log(f"数据截止日 (cb_daily 最后交易日) : {data_asof.date()}")
    log(f"cb_basic 标的总数                : {n_all}")
    log(f"  已退市 (delist_date 非空)      : {n_del}  ({pct(n_del, n_all)})")
    log(f"  存续中 (delist_date 为空)      : {n_alive}  ({pct(n_alive, n_all)})")
    log(f"\nvalue_date  : {m['value_date'].min().date()}  ~  {m['value_date'].max().date()}")
    log(f"list_date   : {m['list_date'].min().date()}  ~  {m['list_date'].max().date()}")
    log(f"delist_date : {m['delist_date'].min().date()}  ~  {m['delist_date'].max().date()}")
    log("\n关键字段缺失:")
    for c in ["value_date", "list_date", "interest_rate_explain", "tenor_years"]:
        log(f"  {c:24s} 全样本 null={int(m[c].isna().sum()):4d}, "
            f"已退市 null={int(m.loc[m['delisted'], c].isna().sum()):4d}")
    log(f"  remain_size 全部为空 ({int(m['remain_size'].isna().sum())}/{n_all}) —— "
        f"建仓脚本里写死为 None, 无法用余额判断转股/回售进度.")

    stale = m[(~m["delisted"]) & (m["nominal_maturity"] < data_asof)]
    log(f"\n[质量] 推定名义到期日已过但 delist_date 仍为空: {len(stale)} 只")
    if len(stale):
        log(stale[["ts_code", "bond_short_name", "value_date", "nominal_maturity",
                   "cb_last_trade_date", "last_close"]].to_string(index=False))
    stale2 = m[(~m["delisted"]) &
               (m["cb_last_trade_date"] < data_asof - pd.Timedelta(days=90))]
    log(f"\n[质量] 未标退市但 cb_daily 已 >=90 天无行情: {len(stale2)} 只 "
        "(delist_date 疑似漏标; 本统计仍保守地算作 '存续中')")
    if len(stale2):
        log(stale2[["ts_code", "bond_short_name", "nominal_maturity",
                    "cb_last_trade_date", "last_close"]].to_string(index=False))
    nodata = m[(m["delisted"]) & m["cb_last_trade_date"].isna()]
    log(f"\n[质量] 已退市但 cb_daily 完全无行情: {len(nodata)} 只")

    d = m[m["delisted"]].copy()
    bad = d[(d["ratio"].isna()) | (d["ratio"] <= 0) | (d["ratio"] > 1.15)]
    log(f"\n[质量] 已退市样本中 ratio 缺失或越界(<=0 或 >1.15): {len(bad)} 只")
    if len(bad):
        log(bad[["ts_code", "bond_short_name", "value_date", "delist_date",
                 "nominal_maturity", "tenor_years", "ratio"]].to_string(index=False))

    vy = m["value_year"].value_counts().sort_index()
    log("\n按起息年份的标的数:")
    log("  " + "  ".join(f"{int(k)}:{int(v)}" for k, v in vy.items()))

    # ---------------- SECTION 2 ----------------
    log("\n" + "=" * 82)
    log(f"SECTION 2  退出方式分布 (已退市样本 N={len(d)})")
    log("=" * 82)
    ex = d["exit_mode"].value_counts()
    tbl1 = pd.DataFrame({"count": ex, "share_pct": (ex / len(d) * 100).round(2)})
    tbl1.index.name = "exit_mode"
    log(tbl1.to_string())
    tbl1.to_csv(OUT / "exit_mode_distribution.csv", encoding="utf-8-sig")

    log("\n[回售] cb_basic / cb_call 里都没有回售字段 (无 put_date / put_price / "
        "回售登记日 / 回售金额), 也没有 remain_size,")
    log("       因此 '因回售而退市' 在本数据里**无法识别**, 该类目不存在于上表.")
    log(f"       若确有此类个案, 会落进 '{LBL_EARLY_UNK}' 或 '{LBL_DEF}'.")

    log("\n[容差敏感性] 改变 TOL_MATURITY_DAYS:")
    for tol in [10, 20, 30, 60, 90]:
        n_mat = int((d["gap_days"].abs() <= tol).sum())
        n_early = int((d["gap_days"] > tol).sum())
        log(f"  tol={tol:3d}d -> 到期 {n_mat:4d} ({pct(n_mat, len(d))}), "
            f"提前退出 {n_early:4d} ({pct(n_early, len(d))})")
    log("  => 到期/提前 的切分对容差极不敏感, 因为退出点要么贴着到期日, 要么早好几年.")

    log("\n[交叉验证] 各退出方式的退市前行情特征:")
    cv = d.groupby("exit_mode").agg(
        n=("last_close", "size"),
        last_close_p25=("last_close", lambda s: s.quantile(.25)),
        last_close_med=("last_close", "median"),
        last_close_p75=("last_close", lambda s: s.quantile(.75)),
        max20_med=("max_close_last20", "median"),
        stk_missing=("stk_missing", "sum"),
    )
    log(cv.to_string(float_format=lambda x: f"{x:.1f}"))
    cv.to_csv(OUT / "exit_mode_market_signals.csv", encoding="utf-8-sig")
    log("  强赎组退市前最后价远高于面值 (转股价值驱动), 到期组贴近面值,")
    log("  违约/正股退市组明显低于面值 —— 与分类规则相互印证.")

    for lbl in [LBL_DEF, LBL_EARLY_UNK, LBL_NO_TENOR, LBL_ANOM]:
        sub = d[d["exit_mode"] == lbl].copy()
        if len(sub):
            log(f"\n[明细] {lbl} (n={len(sub)}):")
            sub["life_yrs"] = (sub["actual_days"] / 365.25).round(2) \
                if "actual_days" in sub.columns else np.nan
            log(sub[["ts_code", "bond_short_name", "value_date", "delist_date",
                     "nominal_maturity", "last_close", "max_close_last20",
                     "stk_missing", "stk_dead_before_exit", "has_call_record"]]
                .sort_values("delist_date").to_string(index=False))
            if lbl == LBL_NO_TENOR:
                yrs_ = ((sub["delist_date"] - sub["value_date"]).dt.days / 365.25)
                log(f"  这 {len(sub)} 只全部是 2007-2009 年的分离交易可转债 (也是唯一没有 "
                    f"cb_call 记录的一批). 它们的 value->delist 跨度中位数 "
                    f"{yrs_.median():.2f} 年, 且 delist_date 与 maturity_date 完全重合, "
                    f"形态上像是持有到期; 但因为拿不到合同年限、maturity_date 又不可信, "
                    f"本文不给它们下分类结论.")

    # ---------------- SECTION 3 ----------------
    log("\n" + "=" * 82)
    log("SECTION 3  实际存续时长 vs 名义期限  (核心)")
    log("=" * 82)
    log("名义期限 = 合同年限 (从 interest_rate_explain 解析) , 起算点 = value_date 起息日")
    log("实际存续 = delist_date - value_date")
    d2 = d[(d["ratio"].notna()) & (d["ratio"] > 0) & (d["ratio"] <= 1.15)].copy()
    log(f"\n参与比值统计: {len(d2)} / {len(d)} 只已退市 (剔除 {len(d)-len(d2)} 只)")

    is_call = d2["exit_mode"] == LBL_CALL
    rows = {"全部已退市": q_table(d2["ratio"], "all"),
            "强赎组": q_table(d2.loc[is_call, "ratio"], "call"),
            "非强赎组": q_table(d2.loc[~is_call, "ratio"], "noncall")}
    for g_, sub in d2.groupby("exit_mode"):
        rows[f"  └ {g_}"] = q_table(sub["ratio"], g_)
    ratio_tbl = pd.DataFrame(rows).T
    ratio_tbl.index.name = "group"
    log("\nratio = 实际存续 / 名义期限:")
    log(ratio_tbl.to_string(float_format=lambda x: f"{x:.3f}"))
    ratio_tbl.to_csv(OUT / "survival_ratio_by_group.csv", encoding="utf-8-sig")

    yrs = pd.DataFrame({
        "实际存续年数-全部": q_table(d2["actual_days"] / 365.25, "all"),
        "实际存续年数-强赎组": q_table(d2.loc[is_call, "actual_days"] / 365.25, "call"),
        "实际存续年数-非强赎组": q_table(d2.loc[~is_call, "actual_days"] / 365.25, "noncall"),
        "名义期限年数-全部": q_table(d2["nominal_days"] / 365.25, "nominal"),
    }).T
    yrs.index.name = "metric"
    log("\n绝对年数:")
    log(yrs.to_string(float_format=lambda x: f"{x:.2f}"))
    yrs.to_csv(OUT / "survival_years_by_group.csv", encoding="utf-8-sig")

    d3 = d[(d["ratio_list"].notna()) & (d["ratio_list"] > 0) & (d["ratio_list"] <= 1.15)]
    isc3 = d3["exit_mode"] == LBL_CALL
    lr = pd.DataFrame({
        "全部已退市": q_table(d3["ratio_list"], "all"),
        "强赎组": q_table(d3.loc[isc3, "ratio_list"], "call"),
        "非强赎组": q_table(d3.loc[~isc3, "ratio_list"], "noncall"),
    }).T
    lr.index.name = "group"
    log("\n(口径对照) 起算点改用 list_date 上市日:")
    log(lr.to_string(float_format=lambda x: f"{x:.3f}"))
    lr.to_csv(OUT / "survival_ratio_by_group_listdate.csv", encoding="utf-8-sig")

    bins = np.arange(0, 1.05, 0.05)
    hh = pd.DataFrame({
        "bin_left": bins[:-1].round(2), "bin_right": bins[1:].round(2),
        "all": np.histogram(d2["ratio"].clip(0, 1.0), bins=bins)[0],
        "forced_call": np.histogram(d2.loc[is_call, "ratio"].clip(0, 1.0), bins=bins)[0],
        "non_call": np.histogram(d2.loc[~is_call, "ratio"].clip(0, 1.0), bins=bins)[0],
    })
    hh.to_csv(OUT / "survival_ratio_histogram.csv", index=False, encoding="utf-8-sig")
    log("\n直方图 (ratio, 步长 0.05):")
    log(hh.to_string(index=False))

    # ---------------- SECTION 4 ----------------
    log("\n" + "=" * 82)
    log("SECTION 4  退出方式的年度分布 (按 delist_year)")
    log("=" * 82)
    ct = pd.crosstab(d["delist_year"].astype("Int64"), d["exit_mode"])
    ct["合计"] = ct.sum(axis=1)
    log(ct.to_string())
    ct.to_csv(OUT / "exit_mode_by_year.csv", encoding="utf-8-sig")
    ctp = (ct.drop(columns=["合计"]).div(ct["合计"], axis=0) * 100).round(1)
    log("\n各年占比 (%):")
    log(ctp.to_string())
    ctp.to_csv(OUT / "exit_mode_by_year_pct.csv", encoding="utf-8-sig")

    yr_stat = (d2.assign(grp=np.where(is_call, "强赎", "非强赎"))
               .groupby(["delist_year", "grp"])
               .agg(n=("ratio", "size"), ratio_median=("ratio", "median"),
                    years_median=("actual_days", lambda s: s.median() / 365.25))
               .reset_index())
    log("\n各年 ratio 中位数 / 实际存续年数中位数:")
    log(yr_stat.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    yr_stat.to_csv(OUT / "yearly_ratio_stats.csv", index=False, encoding="utf-8-sig")

    # ---------------- SECTION 5 ----------------
    log("\n" + "=" * 82)
    log("SECTION 5  存续中的转债 (右删失样本)")
    log("=" * 82)
    a = m[~m["delisted"]].copy()
    ar = a["elapsed_ratio"].dropna()
    log(f"存续中标的数: {len(a)}")
    alive_tbl = pd.DataFrame({
        "已存续年数": q_table(a["elapsed_days"] / 365.25, "elapsed_years"),
        "已存续/名义(下界)": q_table(a["elapsed_ratio"], "elapsed_ratio"),
        "名义期限年数": q_table(a["nominal_days"] / 365.25, "nominal_years"),
    }).T
    alive_tbl.index.name = "metric"
    log(alive_tbl.to_string(float_format=lambda x: f"{x:.3f}"))
    alive_tbl.to_csv(OUT / "alive_bonds_stats.csv", encoding="utf-8-sig")

    labels = ["<20%", "20-40%", "40-60%", "60-80%", "80-100%", ">100%"]
    ab = pd.cut(ar, bins=[0, .2, .4, .6, .8, 1.0, 99], labels=labels,
                right=False).value_counts().reindex(labels)
    log("\n存续中转债的 已存续/名义 分桶:")
    log(ab.to_string())
    ab.to_frame("count").to_csv(OUT / "alive_elapsed_ratio_buckets.csv", encoding="utf-8-sig")
    log("\n存续中转债按起息年份:")
    log("  " + "  ".join(f"{int(k)}:{int(v)}"
                         for k, v in a["value_year"].value_counts().sort_index().items()))

    # ---------------- SECTION 6 ----------------
    log("\n" + "=" * 82)
    log("SECTION 6  幸存者偏差方向 + 强赎最终占比的上下界")
    log("=" * 82)
    n_call_done = int((m["exit_mode"] == LBL_CALL).sum())
    lower = n_call_done / n_all
    upper = (n_call_done + n_alive) / n_all
    log(f"全样本 N = {n_all} (已退市 {n_del} + 存续中 {n_alive})")
    log(f"  已确认强赎退市 : {n_call_done}")
    log(f"  已退市非强赎   : {n_del - n_call_done}")
    log(f"  存续中(结局未知): {n_alive}")
    log("\n强赎 '终身占比' 的确定性区间 (不做任何外推):")
    log(f"  硬下界 (存续中全部不强赎) = {lower*100:.1f}%")
    log(f"  硬上界 (存续中全部强赎)   = {upper*100:.1f}%")
    log(f"\n只看已退市样本得到的强赎占比 = {n_call_done/n_del*100:.1f}%  <- 有偏(上偏)")

    log("\n[cohort 视角] 按起息年份分组 —— 老 cohort 已基本走完, 不受右删失影响:")
    coh = m.groupby("value_year").agg(
        n=("ts_code", "size"),
        n_delisted=("delisted", "sum"),
        n_call=("exit_mode", lambda s: int((s == LBL_CALL).sum())),
        n_mat=("exit_mode", lambda s: int((s == LBL_MAT).sum())),
        n_def=("exit_mode", lambda s: int((s == LBL_DEF).sum())),
        n_other=("exit_mode", lambda s: int((~s.isin(
            [LBL_CALL, LBL_MAT, LBL_DEF, LBL_ALIVE])).sum())),
    )
    coh["done_pct"] = (coh["n_delisted"] / coh["n"] * 100).round(1)
    coh["call_pct_of_cohort"] = (coh["n_call"] / coh["n"] * 100).round(1)
    coh["call_pct_of_delisted"] = (coh["n_call"] /
                                   coh["n_delisted"].replace(0, np.nan) * 100).round(1)
    log(coh.to_string())
    coh.to_csv(OUT / "cohort_by_issue_year.csv", encoding="utf-8-sig")

    closed = coh[coh["done_pct"] >= 95]
    closed_stat = None
    if len(closed):
        tot = int(closed["n"].sum()); tc = int(closed["n_call"].sum())
        tm = int(closed["n_mat"].sum()); td = int(closed["n_def"].sum())
        closed_stat = dict(years=(int(closed.index.min()), int(closed.index.max())),
                           n=tot, call=tc, mat=tm, dflt=td, other=tot - tc - tm - td)
        log(f"\n已走完 (>=95% 退市) 的起息年份 {closed_stat['years'][0]}-"
            f"{closed_stat['years'][1]}, 合计 N={tot}:")
        log(f"  强赎 {tc} ({pct(tc, tot)}) | 到期 {tm} ({pct(tm, tot)}) | "
            f"违约/正股退市 {td} ({pct(td, tot)}) | 其它 {tot-tc-tm-td}")

    log("\n[偏差方向] 只用已退市样本算 ratio, 会系统性高估 '快速退出' 的比例:")
    log("  退得快的已经进了样本, 退得慢的还活着、没进来 -> 已退市样本的 ratio 向下偏.")
    med_del, med_alive = d2["ratio"].median(), ar.median()
    allr = pd.concat([d2["ratio"], ar])
    log(f"  已退市样本 ratio 中位数            = {med_del:.3f}")
    log(f"  存续中样本 已存续/名义 中位数(下界) = {med_alive:.3f}")
    log(f"  全样本合并(存续中用下界代入) 中位数  = {allr.median():.3f}  <- 全样本 ratio 的下界")

    # ---------------- 导出 ----------------
    cols = [c for c in ["ts_code", "code", "bond_short_name", "stk_code", "rating",
                        "issue_size", "value_date", "list_date", "delist_date",
                        "maturity_date", "tenor_years", "nominal_maturity",
                        "exit_mode", "gap_days", "nominal_days", "actual_days", "ratio",
                        "elapsed_days", "elapsed_ratio", "last_close",
                        "max_close_last20", "max_close", "cb_last_trade_date",
                        "stk_last_trade_date", "has_call_record", "ann_date",
                        "call_date", "call_price"] if c in m.columns]
    m[cols].sort_values(["exit_mode", "delist_date"]).to_csv(
        OUT / "cb_exit_detail.csv", index=False, encoding="utf-8-sig")
    log(f"\n明细导出: study/exit_mode/cb_exit_detail.csv ({len(m)} 行)")

    ctx = dict(m=m, d=d, d2=d2, a=a, ar=ar, ct=ct, ctp=ctp, tbl1=tbl1, cv=cv,
               ratio_tbl=ratio_tbl, yrs=yrs, lr=lr, coh=coh, closed_stat=closed_stat,
               alive_tbl=alive_tbl, ab=ab, hh=hh, yr_stat=yr_stat, vy=vy,
               data_asof=data_asof, n_all=n_all, n_del=n_del, n_alive=n_alive,
               n_call_done=n_call_done, lower=lower, upper=upper, is_call=is_call,
               stale=stale, stale2=stale2, bad=bad, raw_nom_alive_med=None)
    make_plots(ctx)
    write_report(ctx)
    (OUT / "console_log.txt").write_text("\n".join(_LOG), encoding="utf-8")
    return ctx


# ===========================================================================
def make_plots(c: dict) -> None:
    if not HAVE_MPL:
        log(f"\n[warn] matplotlib 不可用 ({_MPL_ERR}) -> 跳过 PNG.")
        return
    d2, is_call, ar, ct, m = c["d2"], c["is_call"], c["ar"], c["ct"], c["m"]
    bins = np.linspace(0, 1.05, 43)

    fig, axes = plt.subplots(1, 2, figsize=(13.5, 4.8))
    axes[0].hist(d2["ratio"], bins=bins, color="#4C78A8", edgecolor="white")
    med = d2["ratio"].median()
    axes[0].axvline(med, color="#E45756", lw=2, label=f"median = {med:.2f}")
    axes[0].set_title("Delisted CBs: actual life / contractual life")
    axes[0].set_xlabel("(delist - value) / contractual tenor")
    axes[0].set_ylabel("number of CBs")
    axes[0].legend()
    axes[1].hist([d2.loc[is_call, "ratio"], d2.loc[~is_call, "ratio"]], bins=bins,
                 stacked=True, color=["#4C78A8", "#F58518"], edgecolor="white",
                 label=[f"forced call (n={int(is_call.sum())})",
                        f"non-call (n={int((~is_call).sum())})"])
    axes[1].set_title("Split by exit mode")
    axes[1].set_xlabel("(delist - value) / contractual tenor")
    axes[1].legend()
    for ax in axes:
        ax.grid(alpha=.25, axis="y")
    fig.tight_layout(); fig.savefig(OUT / "survival_ratio_hist.png", dpi=140); plt.close(fig)

    fig, ax = plt.subplots(figsize=(11.5, 5.2))
    order = [x for x in [LBL_CALL, LBL_MAT, LBL_DEF, LBL_EARLY_UNK, LBL_NO_TENOR, LBL_ANOM]
             if x in ct.columns]
    order += [x for x in ct.columns if x not in order and x != "合计"]
    colors = ["#4C78A8", "#54A24B", "#E45756", "#F58518", "#9D755D", "#BAB0AC"]
    bottom = np.zeros(len(ct)); xs = ct.index.astype(int).astype(str)
    for i, col in enumerate(order):
        v = ct[col].values.astype(float)
        ax.bar(xs, v, bottom=bottom, label=EN.get(col, col), color=colors[i % len(colors)])
        bottom += v
    ax.set_title("CB delistings by exit mode and year")
    ax.set_xlabel("delist year"); ax.set_ylabel("number of CBs")
    ax.legend(fontsize=8); ax.grid(alpha=.25, axis="y")
    fig.tight_layout(); fig.savefig(OUT / "exit_mode_by_year.png", dpi=140); plt.close(fig)

    fig, ax = plt.subplots(figsize=(9.8, 4.8))
    ax.hist([d2["ratio"], ar], bins=bins, stacked=True,
            color=["#4C78A8", "#BAB0AC"], edgecolor="white",
            label=[f"delisted - FINAL ratio (n={len(d2)})",
                   f"alive - elapsed/nominal, LOWER BOUND (n={len(ar)})"])
    ax.set_title("Survivorship bias: delisted (final) vs alive (lower bound)")
    ax.set_xlabel("elapsed / contractual life"); ax.set_ylabel("number of CBs")
    ax.legend(fontsize=8); ax.grid(alpha=.25, axis="y")
    fig.tight_layout(); fig.savefig(OUT / "survivorship_ratio_hist.png", dpi=140); plt.close(fig)

    # maturity_date 被改写的证据图
    raw = (m["maturity_date"] - m["value_date"]).dt.days / 365.25
    fig, ax = plt.subplots(figsize=(9.8, 4.4))
    b2 = np.linspace(0, 8.2, 42)
    ax.hist([raw[~m["delisted"]], raw[m["delisted"]]], bins=b2, stacked=False,
            color=["#54A24B", "#E45756"], alpha=.8,
            label=[f"still alive (n={int((~m['delisted']).sum())})",
                   f"delisted (n={int(m['delisted'].sum())})"])
    ax.set_title("Evidence: maturity_date is overwritten for delisted CBs")
    ax.set_xlabel("maturity_date - value_date  (years, as stored in cb_basic)")
    ax.set_ylabel("number of CBs")
    ax.legend(fontsize=9); ax.grid(alpha=.25, axis="y")
    fig.tight_layout(); fig.savefig(OUT / "maturity_date_corruption.png", dpi=140); plt.close(fig)

    log("\nPNG: survival_ratio_hist.png / exit_mode_by_year.png / "
        "survivorship_ratio_hist.png / maturity_date_corruption.png")


# ===========================================================================
def write_report(c: dict) -> None:
    d, d2, m, a = c["d"], c["d2"], c["m"], c["a"]
    is_call = c["is_call"]
    med_all = d2["ratio"].median()
    med_call = d2.loc[is_call, "ratio"].median()
    med_non = d2.loc[~is_call, "ratio"].median()
    yr_all = (d2["actual_days"] / 365.25).median()
    yr_call = (d2.loc[is_call, "actual_days"] / 365.25).median()
    nom_all = (d2["nominal_days"] / 365.25).median()
    n_call = int((d["exit_mode"] == LBL_CALL).sum())
    n_mat = int((d["exit_mode"] == LBL_MAT).sum())
    n_def = int((d["exit_mode"] == LBL_DEF).sum())
    n_unk = int(d["exit_mode"].isin([LBL_EARLY_UNK, LBL_NO_TENOR, LBL_ANOM]).sum())
    cs = c["closed_stat"]
    n_del, n_alive, n_all = c["n_del"], c["n_alive"], c["n_all"]

    L: list[str] = []
    A = L.append
    A("# 可转债退出方式 —— 纯事实统计")
    A("")
    A(f"数据 `data/cb_warehouse/`，截止 **{c['data_asof'].date()}**；"
      f"全样本 N = {n_all}（已退市 {n_del}，存续中 {n_alive}）  ")
    A("脚本 `study/cb_exit_mode_stats.py`　输出 `study/exit_mode/`　"
      "（只读数据，不写 `data/research_framework/`）")
    A("")
    A("## 直接回答")
    A("")
    A(f"1. **成立。** 已退市的 {n_del} 只转债里，强赎（提前赎回）**{n_call} 只 "
      f"= {pct(n_call, n_del)}**，持有到到期 {n_mat} 只 = {pct(n_mat, n_del)}，"
      f"违约/正股退市 {n_def} 只 = {pct(n_def, n_del)}，无法分类 {n_unk} 只 "
      f"= {pct(n_unk, n_del)}。")
    A(f"2. **实际存续时长中位数 = 名义期限的 {med_all:.0%}**"
      f"（强赎组 {med_call:.0%}，非强赎组 {med_non:.0%}）。绝对值：已退市转债实际"
      f"存续中位数 **{yr_all:.2f} 年**，合同期限中位数 {nom_all:.2f} 年；"
      f"强赎组只活了 **{yr_call:.2f} 年**。")
    A(f"3. 把存续中的 {n_alive} 只算进来后，强赎在**全部转债**里的终身占比落在硬区间 "
      f"**[{c['lower']*100:.0f}%, {c['upper']*100:.0f}%]**；只看已退市样本得到的 "
      f"{n_call/n_del*100:.0f}% 是**上偏**的。")
    if cs:
        A(f"4. 最接近无偏的口径是**已走完的发行 cohort**：{cs['years'][0]}–{cs['years'][1]} 年"
          f"起息、已 ≥95% 退市的 {cs['n']} 只里，强赎 {pct(cs['call'], cs['n'])}、"
          f"到期 {pct(cs['mat'], cs['n'])}、违约/正股退市 {pct(cs['dflt'], cs['n'])}。")
    A(f"5. ⚠ **仓库里的 `maturity_date` 对已退市转债是被数据源改写过的**（改成了实际"
      f"兑付日），直接拿它算名义期限会得到完全相反的结论。本文的名义期限改从票面"
      f"利率条款文本解析合同年限，详见第 1.2 节。")
    A("")
    A("---")
    A("")
    A("## 1. 数据与口径")
    A("")
    A("### 1.1 `cb_call.parquet` 是什么（先说清楚，因为它不能用来分类）")
    A("")
    A("生成逻辑见 `scripts/build_cb_warehouse.py` 的 `build_cb_basic_and_call`（L237–254）："
      "对 eastmoney `RPT_BOND_CB_LIST` 中 `IS_REDEEM == '是'` 的转债写一行。")
    A("")
    A("| cb_call 列 | 来源字段 | 名义含义 |")
    A("| --- | --- | --- |")
    A("| `ann_date` | `NOTICE_DATE_HS` / `NOTICE_DATE_SH` | 赎回相关公告日 |")
    A("| `call_date` | `EXECUTE_START_DATEHS` / `…SH` | 赎回执行起始日 |")
    A("| `call_price` | `EXECUTE_PRICE_HS` / `…SH` | 赎回价 |")
    A("| `is_call` | 硬编码常量 `'公告实施强赎'` | **不是数据**，是脚本里写死的字符串 |")
    A("| `expire_date` | `EXPIRE_DATE` | 到期日 |")
    A("")
    A("**判定结论：这张表不能当强赎标记，理由是数据本身给出的三条反证：**")
    A("")
    A(f"1. 覆盖率反证：1012 只转债里 {int(m['has_call_record'].sum())} 只有 cb_call 记录，"
      f"**包含全部 {n_alive} 只仍在存续的转债**。如果 `IS_REDEEM=='是'` 是"
      f"“已被强赎”，存续中的债不可能全部命中。它更像是“含赎回条款”"
      f"（标准转债几乎都有）。")
    A(f"2. 时间反证：{int(((~m['delisted']) & m['call_date'].notna()).sum())} 只**存续中**"
      f"的转债有非空 `call_date`，其中不少在该日期之后又正常交易了 2–4 年"
      f"（例：晶瑞转2 `call_date`=2022-06-08，2026-05-08 仍以 145 元交易）。"
      f"真正的强赎执行日之后不可能继续交易。")
    A("3. 取值反证：`is_call` 只有一个取值，`call_price` 全部落在 100–105 元"
      "（面值+当期利息），强赎、到期赎回、回售三者的赎回价都长这样，无法区分。")
    A("")
    A("**因此 `ann_date` / `call_date` 的确切事件类型无法从数据本身确定**"
      "（可能混入了回售执行、“满足赎回条件但公告不行使”等记录）。"
      "本文**完全不用 cb_call 做分类**，只在明细 CSV 里保留它作为旁证。"
      "这是本文最大的一处推断不确定性，已如实标出。")
    A("")
    A("### 1.2 ⚠ `maturity_date` 对已退市转债是被改写过的（最重要的数据发现）")
    A("")
    raw = (m["maturity_date"] - m["value_date"]).dt.days / 365.25
    A(f"`cb_basic.maturity_date` 来自 eastmoney `EXPIRE_DATE`。用它算名义期限："
      f"存续中的 {n_alive} 只清一色 **{raw[~m['delisted']].median():.2f} 年**"
      f"（标准 6 年合同），已退市的 {n_del} 只却只有中位数 "
      f"**{raw[m['delisted']].median():.2f} 年**。合同期限不会因为退市而变短。")
    A("")
    dg = (m.loc[m["delisted"], "delist_date"] - m.loc[m["delisted"], "maturity_date"]).dt.days
    A(f"进一步：已退市样本的 `delist_date − maturity_date` 中位数 = {dg.median():.0f} 天，"
      f"{pct(int((dg.abs() <= 15).sum()), int(dg.notna().sum()))} 落在 ±15 天内 —— "
      f"两个字段几乎重合。**结论：转债一旦退出，数据源就把 `EXPIRE_DATE` 改写成了"
      f"实际兑付/摘牌日。** 见 `maturity_date_corruption.png`。")
    A("")
    A("**修复口径**：改从 `interest_rate_explain`（票面利率条款文本，形如"
      "“第一年0.4%、第二年0.6%…第六年2.0%”）解析合同年限 —— 文本里出现“第六年”即 6 年。")
    A("")
    tv = m["tenor_years"].value_counts(dropna=False)
    A("解析结果：" + "，".join(
        [f"{'缺失' if k != k else str(int(k)) + ' 年'} {int(v)} 只" for k, v in tv.items()]))
    A("")
    diff = (a["nominal_maturity"] - a["maturity_date"]).dt.days
    A(f"**交叉校验**：拿 {n_alive} 只存续中转债（`maturity_date` 未被改写）验证，"
      f"`value_date + 合同年限` 与 `maturity_date` 相差 ≤3 天的有 "
      f"{int((diff.abs() <= 3).sum())} / {n_alive} 只 "
      f"（{pct(int((diff.abs() <= 3).sum()), n_alive)}）。剩下 "
      f"{int((diff.abs() > 3).sum())} 只正是快照期间刚被赎回、`EXPIRE_DATE` 已被改写"
      f"但 `DELIST_DATE` 还没更新的 —— 反过来又印证了改写的存在。")
    A("")
    A(f"合同年限解析不出来的 {int(m['tenor_years'].isna().sum())} 只（全部是 2007–2009 年"
      f"的分离交易可转债，也是唯一没有 cb_call 记录的一批），归入 "
      f"`{LBL_NO_TENOR}`，不硬猜。")
    A("")
    A("### 1.3 分类规则")
    A("")
    A("对每只已退市转债（`delist_date` 非空），令 `gap = 推定名义到期日 − delist_date`：")
    A("")
    A(f"1. 合同年限不可得 → **{LBL_NO_TENOR}**")
    A(f"2. `gap < −{TOL_MATURITY_DAYS}` 天（退市晚于名义到期）→ **{LBL_ANOM}**")
    A(f"3. `|gap| ≤ {TOL_MATURITY_DAYS}` 天 → **{LBL_MAT}**")
    A(f"4. `gap > {TOL_MATURITY_DAYS}` 天（明显提前退出）：")
    A(f"   - 退市前最后收盘 < {DISTRESS_CLOSE:.0f} 元，或正股不在股票宇宙里／"
      f"正股在转债退市前就已停止交易 → **{LBL_DEF}**")
    A(f"   - 退市前 20 个交易日最高收盘 ≥ {CALL_PRICE_FLOOR:.0f} 元 → **{LBL_CALL}**"
      f"（强赎触发条件是正股连续 15/30 日 ≥ 转股价 130%，触发后转债价格贴近转股价值）")
    A(f"   - 其余 → **{LBL_EARLY_UNK}**（不硬塞进任何一类）")
    A("")
    A("这套规则**不依赖任何“事件类型”字段**，只用日期和行情，因此不受 1.1 节那个"
      "语义不明的问题影响。")
    A("")
    A("**注意 `到期` 的准确含义是“一直活到名义到期日才退出”，不是“足额兑付”。**"
      "数据里没有兑付状态字段，无法区分到期正常兑付与到期违约/展期。")
    A("")
    A(f"**回售：数据不足。** `cb_basic` / `cb_call` 里没有任何回售字段"
      f"（无 `put_date` / `put_price` / 回售登记日 / 回售金额），`remain_size` 也是"
      f"全空（建仓脚本写死为 `None`），所以“因回售而退市”**无法识别**，"
      f"该类目不存在于下面所有表里。若确有此类个案，会落进 `{LBL_EARLY_UNK}` "
      f"或 `{LBL_DEF}`。")
    A("")
    A("### 1.4 覆盖范围与已知缺陷")
    A("")
    A(f"- `value_date` 范围 {m['value_date'].min().date()} ~ {m['value_date'].max().date()}；"
      f"`delist_date` 范围 {m['delist_date'].min().date()} ~ {m['delist_date'].max().date()}。")
    A(f"- **起点截断**：最早起息日 {m['value_date'].min().date()}，更早发行的转债不在数据里，"
      f"本文覆盖的不是中国转债市场的全部历史。")
    A("- 按起息年份的标的数：" + "，".join(f"{int(k)}:{int(v)}" for k, v in c["vy"].items())
      + "。2017 年起才上规模，2007–2016 年合计仅 "
      + str(int(c["vy"][c["vy"].index <= 2016].sum())) + " 只，早期样本很薄。")
    A(f"- 推定名义到期日已过但 `delist_date` 仍为空：{len(c['stale'])} 只。")
    A(f"- 未标退市但 cb_daily 已 ≥90 天无行情：{len(c['stale2'])} 只（`delist_date` 疑似"
      f"漏标，本统计仍保守地算作“存续中”—— 这会让强赎占比的估计偏保守）。")
    A(f"- 已退市样本中 `ratio` 缺失或越界（≤0 或 >1.15）：**{len(c['bad'])}** 只，"
      f"只在第 3 节的比值统计里剔除，第 2 节的分类计数仍然保留。")
    A("")
    A("---")
    A("")
    A("## 2. 退出方式分布（已退市样本）")
    A("")
    A(md_table(c["tbl1"], "{:.2f}", index_name="退出方式"))
    A("")
    A(f"合计 {n_del} 只。**无法分类合计 {n_unk} 只（{pct(n_unk, n_del)}）** —— "
      f"如实报告，没有塞进上面任何一类。")
    A("")
    A("### 分类的独立交叉验证：各退出方式的退市前行情特征")
    A("")
    A(md_table(c["cv"], "{:.1f}", index_name="退出方式"))
    A("")
    A("强赎组退市前最后收盘价远高于面值（转股价值驱动），到期组贴近面值，"
      "违约/正股退市组明显低于面值。三组在价格维度上完全分开，说明用日期切出来的"
      "分类和市场行为是自洽的。")
    A("")
    A("### 到期容差的敏感性")
    A("")
    sens = pd.DataFrame(
        {"到期": [int((d["gap_days"].abs() <= t).sum()) for t in [10, 20, 30, 60, 90]],
         "提前退出": [int((d["gap_days"] > t).sum()) for t in [10, 20, 30, 60, 90]]},
        index=[f"{t}天" for t in [10, 20, 30, 60, 90]])
    sens.index.name = "容差"
    A(md_table(sens, "{:.0f}", index_name="容差"))
    A("")
    A("切分对容差**极不敏感** —— 因为退出点要么贴着到期日（几天内），"
      "要么早好几年，中间几乎是空的。")
    A("")
    A("---")
    A("")
    A("## 3. 实际存续时长 vs 名义期限（核心数字）")
    A("")
    A("- 名义期限 = 合同年限（从票面利率条款解析），起算点 = `value_date` 起息日")
    A("- 实际存续 = `delist_date − value_date`")
    A("- ratio = 实际 / 名义")
    A("")
    A(f"参与统计：{len(d2)} / {n_del} 只已退市。")
    A("")
    A(md_table(c["ratio_tbl"], "{:.3f}", index_name="分组"))
    A("")
    A("绝对年数：")
    A("")
    A(md_table(c["yrs"], "{:.2f}", index_name="指标"))
    A("")
    A("口径对照（起算点改用 `list_date` 上市日，方向不变）：")
    A("")
    A(md_table(c["lr"], "{:.3f}", index_name="分组"))
    A("")
    A("直方图 `survival_ratio_hist.png`，分桶数据 `survival_ratio_histogram.csv`。")
    A("")
    A(f"**这些数字的直接含义**：已退市样本的 ratio 中位数 {med_all:.3f}，"
      f"即退出时还剩 {(1-med_all)*100:.0f}% 的合同期限没走完；强赎组 {med_call:.3f}。"
      f"换算成倍数：用合同到期日当期限，会把已退市样本的实际存续期高估约 "
      f"**{1/med_all:.1f} 倍**（中位数口径），强赎组约 **{1/med_call:.1f} 倍**。"
      f"这只是对已实现历史的描述，不含任何对未来的推断。")
    A("")
    A("---")
    A("")
    A("## 4. 年度分布")
    A("")
    A(md_table(c["ct"].astype(int), "{:.0f}", index_name="退市年份"))
    A("")
    yr_last = int(c["ct"].index.max())
    A(f"注：{yr_last} 年是**不完整年份**（数据截止 {c['data_asof'].date()}，"
      f"少数 `delist_date` 甚至排到截止日之后），不能和整年直接比。"
      f"2016–2018 年退市数极少（1–3 只/年），是因为 2017 年之前的存量转债本来就很少。")
    A("")
    A("各年占比（%）：")
    A("")
    A(md_table(c["ctp"], "{:.1f}", index_name="退市年份"))
    A("")
    A("图 `exit_mode_by_year.png`；逐年 ratio 中位数见 `yearly_ratio_stats.csv`。")
    A("")
    ys = c["yr_stat"]

    def _yr(y, grp, col):
        r = ys[(ys["delist_year"] == y) & (ys["grp"] == grp)]
        return None if r.empty else r.iloc[0][col]

    ctp_ = c["ctp"]

    def _cell(y, col):
        try:
            return ctp_.loc[y, col]
        except Exception:
            return float("nan")
    A("**2020 炒作年 vs 2024 信用年（题目点名的两年）**")
    A("")
    A(f"- 2020 年退市 {int(c['ct'].loc[2020, '合计'])} 只，"
      f"强赎占 {_cell(2020, LBL_CALL):.0f}%，到期 {_cell(2020, LBL_MAT):.0f}%，"
      f"违约/正股退市 0 只。强赎组 ratio 中位数 {_yr(2020, '强赎', 'ratio_median'):.3f}，"
      f"实际只活了 **{_yr(2020, '强赎', 'years_median'):.2f} 年** —— 全样本里最短的一档。")
    A(f"- 2024 年退市 {int(c['ct'].loc[2024, '合计'])} 只，"
      f"强赎降到 {_cell(2024, LBL_CALL):.0f}%，到期升到 {_cell(2024, LBL_MAT):.0f}%，"
      f"并且首次出现成规模的违约/正股退市（{_cell(2024, LBL_DEF):.1f}%）。"
      f"强赎组 ratio 中位数升到 {_yr(2024, '强赎', 'ratio_median'):.3f}"
      f"（{_yr(2024, '强赎', 'years_median'):.2f} 年）。")
    A(f"- `{LBL_DEF}` 这一类**只出现在 2023–2025 年**，之前 15 年一只都没有。")
    A("")
    A("两年的差别在数字上很清楚：2020 年退出快且清一色靠强赎；"
      "2024 年退出慢了、靠到期的多了、并且开始有信用事件。"
      "本文只报这个事实，不解释成因。")
    A("")
    A("---")
    A("")
    A("## 5. 存续中的转债")
    A("")
    A(f"存续中 **{n_alive}** 只（占全样本 {pct(n_alive, n_all)}）。")
    A("")
    A(md_table(c["alive_tbl"], "{:.3f}", index_name="指标"))
    A("")
    A("已存续 / 名义期限 的分桶（注意：这是**下界**，它们最终的 ratio 只会更大）：")
    A("")
    A(md_table(c["ab"].to_frame("count"), "{:.0f}", index_name="bucket"))
    A("")
    A("---")
    A("")
    A("## 6. 幸存者偏差 —— 方向、大小、修正后的区间")
    A("")
    A("### 6.1 偏差方向")
    A("")
    A("只统计“已退市”的样本会**系统性高估快速退出的比例**：退得快的已经进了样本，"
      "退得慢的还活着、没进来。所以")
    A("")
    A("- 第 2 节的“强赎占比”是**上偏**的；")
    A("- 第 3 节的 ratio 分布是**下偏**的（真实的全样本 ratio 分布比它更靠右）。")
    A("")
    A("### 6.2 不做外推的硬区间")
    A("")
    A(md_table(pd.DataFrame({
        "强赎占比": [f"{n_call/n_del*100:.1f}%", f"{c['lower']*100:.1f}%",
                     f"{c['upper']*100:.1f}%"]},
        index=["只看已退市（有偏，上偏）",
               f"硬下界：存续中 {n_alive} 只全部不强赎",
               f"硬上界：存续中 {n_alive} 只全部强赎"]), index_name="口径"))
    A("")
    A(f"所以 **“绝大多数转债走强赎”在全样本口径下的确定性区间是 "
      f"[{c['lower']*100:.0f}%, {c['upper']*100:.0f}%]**。"
      f"区间下端已经{'超过' if c['lower'] > 0.5 else '不到'} 50%。")
    A("")
    A("### 6.3 cohort 视角（最接近无偏）")
    A("")
    A("按**起息年份**分组：`done_pct` 是该年起息的转债里已退市的比例。"
      "`done_pct` 接近 100% 的年份不再受右删失影响。")
    A("")
    A(md_table(c["coh"], "{:.1f}", index_name="起息年份"))
    A("")
    if cs:
        A(f"已走完（≥95% 退市）的起息年份 {cs['years'][0]}–{cs['years'][1]}，合计 N={cs['n']}："
          f"强赎 {cs['call']}（{pct(cs['call'], cs['n'])}），到期 {cs['mat']}"
          f"（{pct(cs['mat'], cs['n'])}），违约/正股退市 {cs['dflt']}"
          f"（{pct(cs['dflt'], cs['n'])}），其它 {cs['other']}。")
        A("")
        den = cs["n"] - cs["other"]
        if den > 0:
            A(f"这里的“其它 {cs['other']} 只”绝大部分是 2007–2009 年那批合同年限拿不到的"
              f"分离交易可转债（严格说不是标准转债）。把它们剔出分母后，"
              f"该 cohort 的强赎占比是 **{cs['call']}/{den} = {pct(cs['call'], den)}**，"
              f"到期 {pct(cs['mat'], den)}。")
        A("")
        A("这是本文对强赎占比最可靠的估计，因为它不含右删失样本。"
          "但它只覆盖较早的发行年份 —— 近年发行的转债信用环境和条款博弈都不同，"
          "**数据不足以判断两者是否可比**，不要直接外推。")
    else:
        A("**数据不足**：没有任何起息年份的 cohort 已走完 ≥95%，"
          "因此无法给出完全无删失的强赎占比。")
    A("")
    A("### 6.4 ratio 的下界修正")
    A("")
    allr = pd.concat([d2["ratio"], c["ar"]])
    A(f"- 已退市样本 ratio 中位数 = **{med_all:.3f}**")
    A(f"- 存续中样本 已存续/名义 中位数 = **{c['ar'].median():.3f}**（只是下界）")
    A(f"- 全样本合并（存续中用下界代入）中位数 = **{allr.median():.3f}** —— "
      f"这是全样本 ratio 中位数的**下界**，真实值只会更大。")
    A("")
    A("图 `survivorship_ratio_hist.png`（灰色是存续中样本的下界，它们最终只会向右移）。")
    A("")
    A("**所以“绝大多数走强赎”这个结论在校正后依然成立吗？** 成立，"
      f"但要把话说准：不是“绝大多数转债最终会强赎”，而是"
      f"“在已经退出的转债里，{pct(n_call, n_del)} 是强赎退出的”；"
      f"放到全样本上，强赎终身占比只能确定地说落在 "
      f"[{c['lower']*100:.0f}%, {c['upper']*100:.0f}%]，"
      f"已走完的老 cohort 给出的点估计是 "
      + (f"{pct(cs['call'], cs['n'])}。" if cs else "数据不足。"))
    A("")
    A("---")
    A("")
    A("## 7. 数据不足 / 无法回答的部分")
    A("")
    A("- **回售**：无任何字段，完全无法识别（见 1.3）。")
    A("- **到期是否足额兑付**：无兑付状态字段，无法区分到期正常兑付与到期违约/展期。")
    A(f"- **`{LBL_EARLY_UNK}` 的 {int((d['exit_mode']==LBL_EARLY_UNK).sum())} 只**："
      f"提前退出、价格贴近面值、正股也没停止交易。数据不足以判断原因"
      f"（回售？低价强赎？协议回购？）。明细见 `console_log.txt`。")
    A(f"- **`{LBL_NO_TENOR}` 的 {int((d['exit_mode']==LBL_NO_TENOR).sum())} 只**："
      f"合同年限解析不出来，未参与任何比例统计。")
    A("- **cb_call 的事件类型**：如 1.1 节所述，无法确定，本文未使用。")
    A("- **早于数据起点的转债**：不在样本内。")
    A("")
    A("## 8. 产出文件")
    A("")
    for f in sorted(OUT.glob("*")):
        if f.name != "REPORT.txt":
            A(f"- `{f.name}`")
    A("")

    (OUT / "REPORT.txt").write_text("\n".join(L), encoding="utf-8")
    log(f"\n报告已写入: {OUT/'REPORT.txt'}")


if __name__ == "__main__":
    main()
