"""The Tonghuashun pages are keyed by six digits that two different bonds can share."""

from scripts.build_cb_ths_tables import coupons_match, identity, parse_balances, parse_holders, summary_fields

OURS = """<table><tr><td>债券代码：</td><td>113644</td><td>债券简称：</td><td>艾迪转债</td></tr>
<tr><td>债券类型：</td><td>可转债</td><td>交易市场：</td><td>上交所</td></tr>
<tr><td>起息日期：</td><td>2022-04-15</td><td>到期日期：</td><td>2028-04-15</td></tr></table>"""
SOMEONE_ELSE = OURS.replace("艾迪转债", "12拓奇债").replace("可转债", "其它债券").replace("2022-04-15", "2012-06-11")


def _cashflow(rows):
    return "<table>" + "".join(
        f"<tr><td>{d}</td><td>x</td><td>x</td><td>x</td><td>{kind}</td><td>{rate}</td><td>{bal}</td></tr>"
        for d, kind, rate, bal in rows) + "</table>"


def test_a_page_showing_another_bond_is_rejected():
    assert identity(summary_fields(OURS), "艾迪转债", "20220415") is None
    assert identity(summary_fields(SOMEONE_ELSE), "艾迪转债", "20220415") is not None
    assert identity({}, "艾迪转债", "20220415") is not None


def test_a_shared_page_is_ours_only_if_its_coupons_are_ours():
    mixed = _cashflow([("2013-06-11", "兑付", "7.50", "0.00"),            # the other bond
                       ("2023-04-15", "付息", "0.30", "99000.00"), ("2024-04-15", "付息", "0.50", "98000.00")])
    assert coupons_match(mixed, "20220415", [0.3, 0.5, 1.0, 1.5, 1.8, 2.0])
    assert not coupons_match(mixed, "20220415", [0.4, 0.6, 1.0, 1.5, 1.8, 2.0])  # another convertible's schedule
    assert not coupons_match(mixed, "20220415", None)
    assert not coupons_match(_cashflow([("2023-04-15", "付息", "0.30", "1")]), "20220415", [0.3, 0.5])  # one row proves little


def test_balance_rows_are_observations_not_the_future_schedule():
    page = _cashflow([("2023-04-15", "付息", "0.30", "99000.00"), ("2027-04-15", "付息", "1.80", "98000.00"),
                      ("2028-04-15", "兑付", "2.00", "0.00")])
    assert parse_balances(page, "113644.SH", fetched="20261007") == [
        {"ts_code": "113644.SH", "date": "20230415", "balance_wan_yuan": 99000.0, "source": "ths_coupon_date"}]


def test_holders_keep_their_reporting_date_and_rank():
    page = ('<div id="t_2022-12-31"><table><tr><td>甲基金</td><td>基金</td><td>6.80</td><td>0.85</td></tr>'
            '<tr><td>张三</td><td>--</td><td>4.71</td><td>0.59</td></tr></table></div>')
    rows = parse_holders(page, "113644.SH")
    assert [(r["report_date"], r["rank"], r["holder_name"], r["holder_type"], r["hold_pct"]) for r in rows] == [
        ("20221231", 1, "甲基金", "基金", 0.85), ("20221231", 2, "张三", None, 0.59)]
