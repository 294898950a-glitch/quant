from cb_market.contracts import NO_CLAUSE, NO_SOURCE, PARSED, UNPARSED, cn_int, parse_call_clause, parse_coupons, parse_put_clause, parse_terms

CALL = ("(1)到期赎回条款在本次发行的可转债期满后五个交易日内,公司将以本次发行的可转债债券面值的107%(含最后一期年度利息)的价格"
        "向投资者赎回全部未转股的可转债。(2)有条件赎回条款在本次发行可转债的转股期内,如果公司A股股票连续三十个交易日中至少有"
        "十五个交易日的收盘价格不低于当期转股价格的130%(含130%),公司有权赎回。此外,当未转股的票面总金额不足人民币3,000万元时,公司有权赎回。")
PUT = ("在本次发行的可转换公司债券最后两个计息年度,如果公司股票在任何连续三十个交易日的收盘价格低于当期转股价的70%时,"
       "可转换公司债券持有人有权回售。")


def test_cn_int():
    assert [cn_int(x) for x in ("三十", "十五", "二十", "15", "两", "十")] == [30, 15, 20, 15, 2, 10]
    assert cn_int("若干") is None


def test_call_clause_reads_window_days_trigger_and_maturity_price():
    out = parse_call_clause(CALL)
    assert (out["call_window"], out["call_required_days"], out["call_trigger_pct"]) == (30, 15, 130.0)
    assert out["maturity_redemption_price"] == 107.0 and out["call_small_balance_wan"] == 3000.0
    assert out["call_status"] == PARSED


def test_consecutive_days_without_at_least_means_every_day():
    out = parse_call_clause("在转股期内,如发行人A股股票连续20个交易日的收盘价格不低于当期转股价格的125%,发行人有权赎回。")
    assert (out["call_window"], out["call_required_days"], out["call_trigger_pct"]) == (20, 20, 125.0)


def test_maturity_price_is_not_mistaken_for_the_trigger():
    # the first percentage in the clause is the maturity redemption price, not the call trigger
    assert parse_call_clause(CALL)["call_trigger_pct"] != 107.0


def test_unreadable_call_clause_is_unparsed_not_a_default():
    out = parse_call_clause("有条件赎回条款: 具体条件由董事会与主承销商协商确定。")
    assert out["call_status"] == UNPARSED and out["call_trigger_pct"] is None and out["call_window"] is None
    assert parse_call_clause(None)["call_status"] == NO_CLAUSE


def test_put_clause():
    out = parse_put_clause(PUT)
    assert (out["put_window"], out["put_trigger_pct"], out["put_last_years"], out["put_period"]) == (30, 70.0, 2, "last_n_years")
    only_change_of_use = parse_put_clause("若募集资金运用被认定为改变募集资金用途的,持有人享有一次回售的权利。除此之外,可转债不可由持有人主动回售。")
    assert only_change_of_use["put_status"] == NO_CLAUSE and only_change_of_use["has_change_of_use_put"] is True


def test_coupons_and_par_plus_last_coupon():
    assert parse_coupons("第一年0.20%、第二年0.40%、第三年0.70%、第四年1.20%、第五年1.70%、第六年2.00%。")["term_years"] == 6
    assert parse_coupons("票面利率预设区间为0.8%-1.5%。")["coupon_status"] == UNPARSED
    terms = parse_terms("①到期赎回本次发行的可转债到期后5个交易日内,发行人将以票面面值加最后一期利息的价格赎回。", None,
                        "第一年0.5%、第二年0.8%、第三年1.1%、第四年1.4%、第五年1.7%。")
    assert terms["maturity_redemption_price"] == 101.7


def test_down_revision_clause_says_it_has_no_source():
    assert parse_terms(CALL, PUT, None)["down_revision_status"] == NO_SOURCE
