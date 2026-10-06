"""The registry of market facts: one function per fact.

A fact is a statement about the market plus the measurement behind it. Kinds:

  contract   written into each bond at issue; cannot change
  identity   arithmetic or no-arbitrage bound; true by construction, or a count of how often it is violated
  behaviour  what issuers and the market actually do; can change, re-measure
  environment rules, supply, liquidity; changes at dated breaks

A measurement returns Measured(...) or NotMeasured(reason). There is no default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd

from cb_market import events as ev
from cb_market.identities import decompose

KINDS = ("contract", "identity", "behaviour", "environment")


@dataclass
class Measured:
    values: dict[str, Any]
    n: int
    date_range: tuple[str, str]
    detail: pd.DataFrame | None = None
    notes: str = ""


@dataclass
class NotMeasured:
    reason: str


@dataclass
class Fact:
    id: str
    kind: str
    statement: str
    measure: Callable[["Inputs"], Measured | NotMeasured]


@dataclass
class Inputs:
    panel: pd.DataFrame
    events: pd.DataFrame
    terms: pd.DataFrame
    study_start: str = "20190101"
    _cache: dict[str, Any] = field(default_factory=dict)

    @property
    def p(self) -> pd.DataFrame:
        """Panel rows inside the study window."""
        if "p" not in self._cache:
            self._cache["p"] = self.panel[self.panel["trade_date"] >= self.study_start]
        return self._cache["p"]

    @property
    def span(self) -> tuple[str, str]:
        return str(self.p["trade_date"].min()), str(self.p["trade_date"].max())


REGISTRY: list[Fact] = []


def fact(id: str, kind: str, statement: str):
    assert kind in KINDS

    def wrap(fn):
        REGISTRY.append(Fact(id, kind, statement, fn))
        return fn

    return wrap


def _r(x: Any, digits: int = 4) -> Any:
    if isinstance(x, dict):
        return {str(k): _r(v, digits) for k, v in x.items()}
    if isinstance(x, (float, np.floating)):
        return None if not np.isfinite(x) else round(float(x), digits)
    if isinstance(x, (np.integer,)):
        return int(x)
    return x


def _year(p: pd.DataFrame) -> pd.Series:
    return p["trade_date"].str[:4]


# --------------------------------------------------------------------------- contract

@fact("C1_call_terms", "contract", "发行人什么时候有权强赎, 是逐券写在合同里的: 多数券是 30 个交易日里 15 天正股不低于转股价的 130%, 但不是全部。")
def _c1(x: Inputs):
    t = x.terms[x.terms["call_status"] == "parsed"]
    combos = t.groupby(["call_window", "call_required_days", "call_trigger_pct"]).size().sort_values(ascending=False)
    detail = combos.rename("bonds").reset_index()
    return Measured({
        "bonds_parsed": len(t), "bonds_not_parsed": int((x.terms["call_status"] != "parsed").sum()),
        "share_30_15_130": combos.iloc[0] / len(t), "distinct_term_sets": len(combos),
        "bonds_with_other_terms": int(len(t) - combos.iloc[0]),
    }, len(t), ("", ""), detail, "条款原文与解析值都在 cb_contract_terms.parquet; 与集思录在市券逐只核对一致。")


@fact("C2_put_terms", "contract", "持有人的回售权有期限和门槛: 多数券只在最后两个计息年度、正股连续 30 天低于转股价 70% 时才有; 一部分券没有条件回售。")
def _c2(x: Inputs):
    t = x.terms
    parsed = t[t["put_status"] == "parsed"]
    detail = parsed.groupby(["put_trigger_pct", "put_period", "put_last_years"], dropna=False).size().rename("bonds").reset_index()
    return Measured({
        "bonds_with_conditional_put": len(parsed), "bonds_without": int((t["put_status"] == "no_clause").sum()),
        "bonds_not_parsed": int((t["put_status"] == "unparsed").sum()),
        "share_last_two_years_70pct": float(((parsed["put_trigger_pct"] == 70) & (parsed["put_last_years"] == 2)).mean()),
    }, len(parsed), ("", ""), detail)


@fact("C3_maturity_redemption_price", "contract", "到期不转股, 公司按约定价赎回, 这个价不是 100 元面值, 而是 103 到 130 元, 中位 110 元。")
def _c3(x: Inputs):
    v = x.terms["maturity_redemption_price"].dropna()
    return Measured({"median": v.median(), "p5": v.quantile(0.05), "p95": v.quantile(0.95), "min": v.min(), "max": v.max(),
                     "bonds_not_parsed": int((x.terms["maturity_redemption_status"] != "parsed").sum())},
                    len(v), ("", ""), v.value_counts().sort_index().rename("bonds").rename_axis("price").reset_index(),
                    "含最后一期利息。strategies/cb_arb/cb_pricer.py 的债底按面值 100 加票息算, 没有用这个价。")


@fact("C4_down_revision_terms", "contract", "发行人什么时候有权提议下修 (触发比例和天数)。")
def _c4(x: Inputs):
    return NotMeasured("没有数据源给出下修条款: 东财的条款字段只有赎回和回售, 集思录公开接口也没有。需要从募集说明书取。")


# --------------------------------------------------------------------------- identity

@fact("I1_return_decomposition", "identity", "转债的收益恒等于三项之和: 正股涨跌 + 转股价变动 + 溢价变动。")
def _i1(x: Inputs):
    d = decompose(x.p)
    total = d[["r", "r_stock", "r_conv_price", "r_premium"]].sum()
    resid = float((x.p["r"] - x.p["r_stock"] - x.p["r_conv_price"] - x.p["r_premium"]).abs().max())
    big = x.p[x.p["r_conv_price"].abs() > 0.03]
    return Measured({
        "equal_weight_log_return": total["r"], "from_stock": total["r_stock"],
        "from_conversion_price_change": total["r_conv_price"], "from_premium_change": total["r_premium"],
        "identity_max_residual": resid,
        "days_with_conv_price_change_over_3pct_share": len(big) / len(x.p.dropna(subset=["r_conv_price"])),
        "those_days_share_of_conv_price_term": float(big["r_conv_price"].sum() / x.p["r_conv_price"].sum()),
    }, int(d["bond_days"].sum()), x.span, d.reset_index().rename(columns={"trade_date": "year"}),
        "全部在市券等权、每日再平衡的对数收益。下修当天转股价值上跳、溢价同步压缩, 两项此消彼长, 不能把转股价一项单独读成因果。")


@fact("I2_time_value", "identity", "价格高出 max(转股价值, 纯债价值) 的部分是时间价值, 是市场唯一能自由定价的部分; 它偶尔为负, 说明两个下限不是硬的。")
def _i2(x: Inputs):
    p = x.p.dropna(subset=["time_value"])
    g = p.groupby(_year(p))["time_value"]
    detail = pd.DataFrame({"p5": g.quantile(0.05), "median": g.median(), "p95": g.quantile(0.95),
                           "share_below_minus_1": g.apply(lambda s: (s < -1).mean())}).reset_index()
    below_floor = p[(p["close"] < p["bond_value"] - 1) & (p["conv_value"] < p["bond_value"])]
    return Measured({"median": p["time_value"].median(), "share_below_minus_1_yuan": float((p["time_value"] < -1).mean()),
                     "share_price_below_bond_value": len(below_floor) / len(p)},
                    len(p), x.span, detail, "纯债价值是东财给的估值, 不是合同量; 按哪条收益率曲线折现它没说明。")


@fact("I3_negative_premium", "identity", "进入转股期后可以转股套利, 价格不该长期低于转股价值。")
def _i3(x: Inputs):
    p = x.p.dropna(subset=["premium"])
    conv = p[p["in_conversion_period"] & ~p["called"]]
    pre = p[~p["in_conversion_period"]]
    neg = conv["premium"] < -0.01
    run = neg.groupby(conv["ts_code"]).transform(lambda s: s.groupby((~s).cumsum()).cumsum())
    detail = pd.DataFrame({
        "share_below_minus_1pct": conv.groupby(_year(conv))["premium"].apply(lambda s: (s < -0.01).mean()),
        "share_below_minus_3pct": conv.groupby(_year(conv))["premium"].apply(lambda s: (s < -0.03).mean()),
    }).reset_index()
    return Measured({
        "in_conversion_share_below_minus_1pct": float(neg.mean()),
        "in_conversion_share_below_minus_3pct": float((conv["premium"] < -0.03).mean()),
        "before_conversion_share_below_minus_1pct": float((pre["premium"] < -0.01).mean()),
        "longest_run_days_p95": float(run[neg].groupby(conv["ts_code"]).max().quantile(0.95)),
    }, len(conv), x.span, detail, "已公告强赎的券不计入。转股当天卖不出正股, 所以 1% 以内的负溢价不算违反。")


@fact("I4_mean_price_is_not_return", "identity", "\"所有在市券的平均价\"不是持有转债的收益: 赢家被强赎离场、新券按面值附近进场, 平均价被持续拉低。")
def _i4(x: Inputs):
    p = x.p
    level = p.groupby("trade_date")["close"].mean()
    ew = p.groupby("trade_date")["r"].mean()
    detail = pd.DataFrame({"mean_price_log_change": np.log(level).diff().groupby(level.index.str[:4]).sum(),
                           "equal_weight_log_return": ew.groupby(ew.index.str[:4]).sum()}).reset_index()
    detail["gap"] = detail["equal_weight_log_return"] - detail["mean_price_log_change"]
    return Measured({"mean_price_log_change": float(np.log(level.iloc[-1] / level.iloc[0])),
                     "equal_weight_log_return": float(ew.sum())}, len(p), x.span, detail)


# --------------------------------------------------------------------------- behaviour

def _episodes(p: pd.DataFrame, flag: str) -> pd.DataFrame:
    """First day of each run in which `flag` is true, after at least 30 trading days of it being false."""
    f = p[flag].astype(bool)
    prior = f.groupby(p["ts_code"]).transform(lambda s: s.shift(1).rolling(30, min_periods=1).max()).fillna(0)
    return p[f & (prior == 0)][["ts_code", "trade_date"]]


def _next_event(starts: pd.DataFrame, events: pd.DataFrame, types: list[str], days: pd.Index, horizon: int) -> pd.Series:
    """For each start, the type of the first event of `types` within `horizon` trading days after (or 5 before)."""
    pos = {d: i for i, d in enumerate(days)}
    by_bond = {ts: g for ts, g in events[events["event_type"].isin(types)].groupby("ts_code")}
    out = []
    for ts, day in zip(starts["ts_code"], starts["trade_date"]):
        g = by_bond.get(ts)
        found = "none"
        if g is not None:
            i0 = pos[day]
            lo, hi = days[max(0, i0 - 5)], days[min(len(days) - 1, i0 + horizon)]
            hit = g[(g["event_date"] >= lo) & (g["event_date"] <= hi)]
            if len(hit):
                found = hit.sort_values("event_date")["event_type"].iloc[0]
        out.append(found)
    return pd.Series(out, index=starts.index)


@fact("B1_call_decision", "behaviour", "满足强赎条件之后, 发行人并不总是强赎: 一部分公告不赎, 一部分什么都不说。")
def _b1(x: Inputs):
    p = x.p
    starts = _episodes(p[p["call_status"] == "parsed"], "call_eligible")
    days = pd.Index(sorted(x.panel["trade_date"].unique()))
    starts = starts[starts["trade_date"] <= days[-61]]
    starts = starts.assign(outcome=_next_event(starts, x.events, [ev.CALL_ANNOUNCED, ev.NO_CALL], days, 60))
    tab = starts.groupby([starts["trade_date"].str[:4], "outcome"]).size().unstack(fill_value=0)
    share = tab.div(tab.sum(axis=1), axis=0)
    total = starts["outcome"].value_counts(normalize=True)
    return Measured({"episodes": len(starts), **{f"share_{k}": float(v) for k, v in total.items()}},
                    len(starts), (str(starts["trade_date"].min()), str(starts["trade_date"].max())),
                    share.round(4).assign(episodes=tab.sum(axis=1)).reset_index().rename(columns={"trade_date": "year"}),
                    "条件按每只券自己的合同算。一次\"满足\"指此前 30 个交易日都不满足之后的第一天; 结果看此后 60 个交易日 (及此前 5 日) 内最先出现的公告。")


def _event_study(x: Inputs, types: list[str], first_per_bond_gap: int = 0) -> tuple[pd.DataFrame, pd.DataFrame]:
    p = x.panel
    market = p.groupby("trade_date")["r"].mean()
    e = x.events[x.events["event_type"].isin(types) & (x.events["event_date"] >= x.study_start)]
    e = e.sort_values(["ts_code", "event_date"]).drop_duplicates(["ts_code", "event_date"])
    windows = [(-20, -6), (-5, -1), (0, 0), (1, 5), (6, 20)]
    by_bond = {ts: g.reset_index(drop=True) for ts, g in p[["ts_code", "trade_date", "r", "r_stock", "r_conv_price", "r_premium", "close", "premium"]].groupby("ts_code")}
    rows = []
    for ts, day in zip(e["ts_code"], e["event_date"]):
        g = by_bond.get(ts)
        if g is None:
            continue
        i = int(g["trade_date"].searchsorted(day))
        if i < 21 or i >= len(g):
            continue
        row = {"ts_code": ts, "event_date": day, "price_before": g["close"].iloc[i - 1], "premium_before": g["premium"].iloc[i - 1]}
        for a, b in windows:
            seg = g.iloc[i + a: i + b + 1]
            name = f"{a}..{b}"
            if len(seg) < b - a + 1:  # the bond stopped trading inside this window (a called bond delists within weeks)
                continue
            row[f"vs_market {name}"] = seg["r"].sum() - market.reindex(seg["trade_date"]).sum()
            row[f"stock {name}"] = seg["r_stock"].sum()
            row[f"premium+convprice {name}"] = (seg["r_premium"] + seg["r_conv_price"]).sum()
        rows.append(row)
    t = pd.DataFrame(rows)
    cols = [c for c in t.columns if ".." in c]
    summary = pd.DataFrame({"mean": t[cols].mean(), "median": t[cols].median(), "n": t[cols].count(),
                            "t": t[cols].mean() / (t[cols].std() / np.sqrt(t[cols].count()))}).reset_index().rename(columns={"index": "window"})
    return t, summary


def _event_fact(x: Inputs, types: list[str], note: str):
    t, summary = _event_study(x, types)
    if len(t) < 30:
        return NotMeasured(f"事件数只有 {len(t)}, 不足以下结论")
    pick = summary.set_index("window")
    vals = {f"net_of_stock {w}": pick.loc[f"premium+convprice {w}", "mean"] for w in ("-20..-6", "-5..-1", "0..0", "1..5", "6..20")}
    vals.update({f"t {w}": pick.loc[f"premium+convprice {w}", "t"] for w in ("0..0", "1..5", "6..20")})
    vals.update({f"vs_market {w}": pick.loc[f"vs_market {w}", "mean"] for w in ("0..0", "1..5", "6..20")})
    vals.update({f"stock {w}": pick.loc[f"stock {w}", "mean"] for w in ("-20..-6", "1..5", "6..20")})
    vals["n 6..20"] = int(pick.loc["vs_market 6..20", "n"])
    vals.update({"price_before_median": t["price_before"].median(), "premium_before_median": t["premium_before"].median()})
    return Measured(vals, len(t), (str(t["event_date"].min()), str(t["event_date"].max())), summary, note)


_NET_NOTE = ("net_of_stock = 转债对数收益 − 正股对数收益 (溢价变动 + 转股价变动), 相当于按 1:1 对冲正股; "
             "转债对正股的弹性小于 1, 所以正股下跌的窗口里这个数会机械地偏正, 上涨的窗口里偏负, 要和同一窗口的 stock 一起读。"
             "vs_market = 转债收益 − 全市场转债等权收益。")


@fact("B2_call_announcement_reaction", "behaviour", "公告强赎时, 转债相对正股下跌: 时间价值被收走。")
def _b2(x: Inputs):
    return _event_fact(x, [ev.CALL_ANNOUNCED], _NET_NOTE)


@fact("B3_no_call_announcement_reaction", "behaviour", "公告不强赎时, 转债相对正股上涨: 封顶暂时解除。")
def _b3(x: Inputs):
    return _event_fact(x, [ev.NO_CALL], _NET_NOTE)


@fact("B4_revision_decision", "behaviour", "公司提示\"预计触发下修\"之后, 多数时候选择不下修。")
def _b4(x: Inputs):
    e = x.events[x.events["event_date"] >= x.study_start]
    warn = e[e["event_type"] == ev.REVISION_MAY_TRIGGER].sort_values(["ts_code", "event_date"])
    gap = pd.to_datetime(warn["event_date"]).groupby(warn["ts_code"]).diff().dt.days
    starts = warn[gap.isna() | (gap > 45)].rename(columns={"event_date": "trade_date"})[["ts_code", "trade_date"]]
    days = pd.Index(sorted(x.panel["trade_date"].unique()))
    starts = starts[starts["trade_date"].isin(days) & (starts["trade_date"] <= days[-31])]
    starts = starts.assign(outcome=_next_event(starts, e, [ev.REVISION_PROPOSAL, ev.NO_REVISION], days, 30))
    tab = starts.groupby([starts["trade_date"].str[:4], "outcome"]).size().unstack(fill_value=0)
    counts = e[e["event_type"].isin([ev.REVISION_PROPOSAL, ev.NO_REVISION, ev.REVISION_MAY_TRIGGER, ev.REVISION_EFFECTIVE])]
    per_year = counts.groupby([counts["event_date"].str[:4], "event_type"]).size().unstack(fill_value=0)
    meet = e[e["event_type"] == ev.REVISION_MEETING]["detail"].value_counts()
    total = starts["outcome"].value_counts(normalize=True)
    return Measured({"warnings": len(starts), **{f"share_{k}": float(v) for k, v in total.items()},
                     "meetings_approved": int(meet.get("approved", 0)), "meetings_rejected": int(meet.get("rejected", 0))},
                    len(starts), (str(starts["trade_date"].min()), str(starts["trade_date"].max())),
                    per_year.join(tab.add_prefix("after_warning_")).reset_index().rename(columns={"event_date": "year"}),
                    "一次\"提示\"指距同一只券上一条提示超过 45 天的提示公告; 结果看此后 30 个交易日内最先出现的公告。巨潮档案 2018 年起才有量, 止于 2026-08-29。")


@fact("B5_revision_proposal_reaction", "behaviour", "董事会提议下修的公告日转债上涨; 公告之后到股东大会之间 (约 16 天) 还在涨。")
def _b5(x: Inputs):
    return _event_fact(x, [ev.REVISION_PROPOSAL], _NET_NOTE + " 第 0 天是董事会提议公告日。")


@fact("B6_no_revision_reaction", "behaviour", "公告不下修时, 转债相对正股的反应。")
def _b6(x: Inputs):
    return _event_fact(x, [ev.NO_REVISION], _NET_NOTE)


@fact("B7_time_value_by_state", "behaviour", "市场给时间价值定多少, 主要看转股价值离面值多远: 平价附近最高, 两头都低。")
def _b7(x: Inputs):
    p = x.p.dropna(subset=["time_value", "conv_value"])
    p = p[~p["called"]]
    bucket = pd.cut(p["conv_value"], [0, 60, 80, 95, 105, 120, 140, 1e9], labels=["<60", "60-80", "80-95", "95-105", "105-120", "120-140", ">140"])
    tab = p.groupby([_year(p), bucket], observed=True)["time_value"].median().unstack()
    return Measured({str(k): v for k, v in p.groupby(bucket, observed=True)["time_value"].median().items()},
                    len(p), x.span, tab.reset_index().rename(columns={"trade_date": "year"}), "单位: 元。按转股价值分档的时间价值中位数。")


# --------------------------------------------------------------------------- environment

@fact("E1_supply", "environment", "在市转债的数量由新发和退出决定, 2024 年之后在收缩。")
def _e1(x: Inputs):
    p = x.panel
    life = p.groupby("ts_code")["trade_date"].agg(["min", "max"])
    end = x.panel.attrs["end"]
    years = sorted(set(_year(x.p)))
    rows = []
    for y in years:
        last_day = p[p["trade_date"].str[:4] == y]["trade_date"].max()
        rows.append({"year": y, "listed_at_year_end": int((p["trade_date"] == last_day).sum()),
                     "new_listings": int((life["min"].str[:4] == y).sum()),
                     "exits": int(((life["max"].str[:4] == y) & (life["max"] < end)).sum())})
    d = pd.DataFrame(rows)
    return Measured({"listed_latest": int(d["listed_at_year_end"].iloc[-1]), "listed_peak": int(d["listed_at_year_end"].max()),
                     "peak_year": d.loc[d["listed_at_year_end"].idxmax(), "year"]}, len(life), x.span, d,
                    "2017 年之前上市的券, 上市年份记在面板起点。余额 (金额) 没有数据, 见 N2。")


@fact("E2_valuation_level", "environment", "全市场的价格和时间价值有一个共同的水位, 各年差别很大。")
def _e2(x: Inputs):
    p = x.p.dropna(subset=["premium"])
    g = p.groupby(_year(p))
    d = pd.DataFrame({"median_price": g["close"].median(), "median_premium": g["premium"].median(),
                      "median_time_value": g["time_value"].median(),
                      "share_price_below_110": g["close"].apply(lambda s: (s < 110).mean()),
                      "share_price_above_130": g["close"].apply(lambda s: (s > 130).mean())}).reset_index().rename(columns={"trade_date": "year"})
    return Measured({"median_price_first_year": d["median_price"].iloc[0], "median_price_last_year": d["median_price"].iloc[-1],
                     "share_above_130_first_year": d["share_price_above_130"].iloc[0], "share_above_130_last_year": d["share_price_above_130"].iloc[-1]},
                    len(p), x.span, d)


@fact("E3_price_limits", "environment", "2022-08-01 起转债有 ±20% 涨跌幅限制 (上市首日除外); 此前没有。")
def _e3(x: Inputs):
    p = x.panel[(x.panel["listing_day_index"] > 0)].dropna(subset=["r"])
    ret = np.exp(p["r"]) - 1
    before, after = ret[p["trade_date"] < "20220801"], ret[p["trade_date"] >= "20220801"]
    hit = (after.abs() >= 0.195)
    nxt = p.groupby("ts_code")["r"].shift(-1)
    return Measured({"days_over_20pct_before": int((before.abs() > 0.205).sum()), "days_over_20pct_after": int((after.abs() > 0.205).sum()),
                     "limit_hits_after": int(hit.sum()), "limit_hit_share_after": float(hit.mean()),
                     "next_day_return_after_up_limit": float(nxt[(after >= 0.195).reindex(p.index, fill_value=False)].mean())},
                    len(p), (str(p["trade_date"].min()), str(p["trade_date"].max())), None,
                    "生效日是从数据里读出来的: 该日之后单日涨跌超过 20.5% 的券日数应为 0。")


@fact("E4_liquidity", "environment", "多数转债一天的成交额不大, 这决定了任何策略的容量。")
def _e4(x: Inputs):
    p = x.p
    avg = p.groupby("ts_code")["amount"].transform(lambda s: s.rolling(20, min_periods=5).mean())
    g = avg.groupby(_year(p))
    d = pd.DataFrame({"median_yi": g.median() / 1e8, "share_below_1000wan": g.apply(lambda s: (s < 1e7).mean()),
                      "share_below_5000wan": g.apply(lambda s: (s < 5e7).mean())}).reset_index().rename(columns={"trade_date": "year"})
    return Measured({"median_daily_amount_yi_last_year": d["median_yi"].iloc[-1], "share_below_1000wan_last_year": d["share_below_1000wan"].iloc[-1]},
                    int(avg.notna().sum()), x.span, d, "成交额 = 收盘价 × 成交张数, 20 日均值。")


@fact("E5_no_call_notices_begin", "environment", "\"公告不强赎\"从 2020 年起才大量出现; 此前满足条件而不赎, 公司多半不出声。")
def _e5(x: Inputs):
    e = x.events[x.events["event_type"] == ev.NO_CALL]
    per = e.groupby(e["event_date"].str[:4]).size()
    return Measured({str(k): int(v) for k, v in per.items()}, len(e), (str(e["event_date"].min()), str(e["event_date"].max())),
                    per.rename("no_call_notices").reset_index().rename(columns={"event_date": "year"}),
                    "数量来自巨潮档案。对照 B1: 满足条件后既不赎也不公告的比例从 2019-2021 年的约 28% 降到 2024 年后的 5% 上下。"
                    "背后的披露规则及其生效日没有在本仓库内核实。")


# --------------------------------------------------------------------------- no data

@fact("N1_holders", "environment", "谁持有转债、他们受什么约束 (评级下限、回撤线), 被迫卖出时价格怎样。")
def _n1(x: Inputs):
    return NotMeasured("没有持有人数据; 评级只有最新值, 没有调级历史。")


@fact("N2_outstanding_balance", "environment", "每只券还剩多少没转股, 以及转股进度。")
def _n2(x: Inputs):
    return NotMeasured("cb_basic.remain_size 全空; 集思录只给当前值, 没有历史。")


@fact("N3_intraday", "environment", "T+0 之下的日内行为。")
def _n3(x: Inputs):
    return NotMeasured("没有日内数据。")


@fact("N4_no_short_selling", "environment", "转债不能做空, 高估的券没有人能压下来。")
def _n4(x: Inputs):
    return NotMeasured("这是制度事实, 无法直接测量; 它的后果 (高价高溢价券长期跑输) 属于信号层。")
