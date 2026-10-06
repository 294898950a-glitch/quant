"""Per-bond contract terms parsed from the prospectus clause text.

The contract is the rule of the game: when the issuer may call, when the holder
may put, what is paid at maturity. Everything else in this package conditions on
it. Terms are parsed from eastmoney's clause text; a field that cannot be read
is None and the clause's status says why. Nothing falls back to a market-wide
default.
"""

from __future__ import annotations

import re
from typing import Any

_CN = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_NUM = r"[0-9一二两三四五六七八九十]+"
_PCT = r"([0-9]+(?:\.[0-9]+)?)\s*[%％]"

PARSED, NO_CLAUSE, UNPARSED = "parsed", "no_clause", "unparsed"


def cn_int(text: str) -> int | None:
    """'三十' -> 30, '15' -> 15, '二十' -> 20."""
    text = text.strip()
    if text.isdigit():
        return int(text)
    if not text or any(ch not in _CN and ch != "十" for ch in text):
        return None
    if "十" not in text:
        return _CN[text] if len(text) == 1 else None
    tens, _, ones = text.partition("十")
    return (_CN[tens] if tens else 1) * 10 + (_CN[ones] if ones else 0)


def _clean(text: Any) -> str:
    return re.sub(r"\s+", "", text) if isinstance(text, str) else ""


def _window(segment: str) -> tuple[int | None, int | None]:
    """(window, required days). '连续三十个交易日中至少有十五个交易日' -> (30, 15); '连续二十个交易日' -> (20, 20)."""
    m = re.search(rf"连续({_NUM})个?交易日", segment)
    if not m:
        return None, None
    window = cn_int(m.group(1))
    tail = segment[m.end(): m.end() + 20]
    n = re.match(rf"[内中里]?(?:至少|不少于)?有?({_NUM})个?交易日", tail)
    return window, (cn_int(n.group(1)) if n else window)


def parse_call_clause(text: Any) -> dict[str, Any]:
    """Conditional (stock-price) call and redemption price at maturity."""
    out: dict[str, Any] = {
        "call_window": None, "call_required_days": None, "call_trigger_pct": None,
        "call_small_balance_wan": None, "maturity_redemption_price": None,
        "call_status": NO_CLAUSE, "maturity_redemption_status": NO_CLAUSE,
    }
    s = _clean(text)
    if not s:
        return out

    # maturity redemption: '面值的107%' or '面值上浮10%'; only read inside the sentence that says 期满/到期
    m = re.search(r"(?:期满|到期)后.{0,60}?(?:面值|票面金额)的?" + _PCT, s)
    up = re.search(r"(?:期满|到期)后.{0,60}?(?:面值|票面金额)上浮" + _PCT, s)
    if up:
        out["maturity_redemption_price"] = 100.0 + float(up.group(1))
    elif m:
        out["maturity_redemption_price"] = float(m.group(1))
    else:
        yuan = re.search(r"(?:期满|到期)后.{0,40}?以([0-9]+(?:\.[0-9]+)?)元", s)
        if yuan:
            out["maturity_redemption_price"] = float(yuan.group(1))
    out["maturity_redemption_status"] = PARSED if out["maturity_redemption_price"] else UNPARSED
    if not re.search("期满|到期后", s):
        out["maturity_redemption_status"] = NO_CLAUSE
    if out["maturity_redemption_price"] is not None and not 100.0 <= out["maturity_redemption_price"] <= 140.0:
        out["maturity_redemption_price"], out["maturity_redemption_status"] = None, UNPARSED

    # conditional call: the sentence '连续…个交易日…不低于…转股价格的130%'
    m = re.search(
        rf"连续{_NUM}个?交易日.{{0,60}}?(?:不低于|高于|超过|达到).{{0,25}}?转股价格?的(?:\(含)?" + _PCT, s
    )
    if not m:
        out["call_status"] = UNPARSED if re.search("有条件赎回|提前赎回", s) else NO_CLAUSE
        return out
    out["call_window"], out["call_required_days"] = _window(m.group(0))
    out["call_trigger_pct"] = float(m.group(1))
    small = re.search(r"不足(?:人民币)?([0-9,，]+)万元", s)
    if small:
        out["call_small_balance_wan"] = float(small.group(1).replace(",", "").replace("，", ""))
    ok = (
        out["call_window"] and out["call_required_days"]
        and out["call_required_days"] <= out["call_window"] and 100.0 < out["call_trigger_pct"] <= 200.0
    )
    out["call_status"] = PARSED if ok else UNPARSED
    if not ok:
        out["call_window"] = out["call_required_days"] = out["call_trigger_pct"] = None
    return out


def parse_put_clause(text: Any) -> dict[str, Any]:
    """Conditional (stock-price) put. A bond with only the change-of-use put has put_status = no_clause."""
    out: dict[str, Any] = {
        "put_window": None, "put_required_days": None, "put_trigger_pct": None,
        "put_last_years": None, "put_period": None, "put_status": NO_CLAUSE, "has_change_of_use_put": None,
    }
    s = _clean(text)
    if not s:
        return out
    out["has_change_of_use_put"] = bool(re.search("改变募集资金用途|募集资金用途", s))
    if re.search("未设置有条件回售|不设置?有条件回售", s):
        return out
    m = re.search(rf"连续{_NUM}个?交易日.{{0,60}}?低于.{{0,25}}?转股价格?的?" + _PCT, s)
    if not m:
        out["put_status"] = UNPARSED if re.search("有条件回售", s) else NO_CLAUSE
        return out
    out["put_window"], out["put_required_days"] = _window(m.group(0))
    out["put_trigger_pct"] = float(m.group(1))
    # when the put is live: the lead-in before the trigger sentence says so
    lead = s[max(0, m.start() - 80): m.start()]
    years = re.search(rf"最后({_NUM})个计息年度", lead) or re.search(rf"最后({_NUM})个计息年度", s)
    if years:
        out["put_last_years"] = cn_int(years.group(1))
        out["put_period"] = "last_n_years"
    elif re.search("到期日?前一年", lead):
        out["put_last_years"], out["put_period"] = 1, "last_n_years"
    elif re.search("转股期", lead):
        out["put_period"] = "conversion_period"
    elif re.search(r"发行之日起|发行结束之日起|起满", lead):
        out["put_period"] = "after_lockup"
    ok = out["put_window"] and 30.0 <= out["put_trigger_pct"] < 100.0 and out["put_period"]
    out["put_status"] = PARSED if ok else UNPARSED
    if not ok:
        for key in ("put_window", "put_required_days", "put_trigger_pct", "put_last_years", "put_period"):
            out[key] = None
    return out


def parse_coupons(text: Any) -> dict[str, Any]:
    """Coupon schedule: '第一年0.20%、…第六年2.00%' -> [0.2, …, 2.0]."""
    s = _clean(text)
    pairs = re.findall(rf"第({_NUM})年(?:的?(?:票面)?(?:年)?(?:利率|息)为?|为)?[:：]?" + _PCT, s)
    by_year = {cn_int(y): float(c) for y, c in pairs if cn_int(y)}
    if not by_year:
        span = re.search(rf"第一年[到至]第({_NUM})年.{{0,12}}?[:：]((?:{_PCT}[、,，]?)+)", s)
        if span:
            rates = [float(x) for x in re.findall(_PCT, span.group(2))]
            if len(rates) == cn_int(span.group(1)):
                by_year = dict(enumerate(rates, start=1))
    years = sorted(by_year)
    if not years or years != list(range(1, len(years) + 1)):
        return {"coupons_pct": None, "term_years": None, "coupon_status": UNPARSED if s else NO_CLAUSE}
    return {"coupons_pct": [by_year[y] for y in years], "term_years": len(years), "coupon_status": PARSED}


NO_SOURCE = "no_source"


def parse_terms(redeem_clause: Any, resale_clause: Any, coupon_text: Any) -> dict[str, Any]:
    """All terms of one bond. The down-revision clause has no data source yet and says so."""
    out = {**parse_call_clause(redeem_clause), **parse_put_clause(resale_clause), **parse_coupons(coupon_text)}
    # '票面面值加最后一期利息' : redemption at par plus the final coupon
    if (
        out["maturity_redemption_status"] == UNPARSED and out["coupons_pct"]
        and re.search(r"(?:期满|到期)后.{0,60}?面值加上?最后一期(?:年度)?利息", _clean(redeem_clause))
    ):
        out["maturity_redemption_price"] = 100.0 + out["coupons_pct"][-1]
        out["maturity_redemption_status"] = PARSED
    out["down_revision_status"] = NO_SOURCE
    return out
