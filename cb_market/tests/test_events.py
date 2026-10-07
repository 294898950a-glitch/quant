"""Announcements must land on the bond they name, and titles must be read for what they say."""

import json

import pandas as pd

from cb_market import events as ev
from scripts.build_cb_call_history import classify_title, load_notices

LIFE = pd.DataFrame({
    "ts_code": ["113525.SH", "113638.SH"],
    "bond_short_name": ["台华转债", "台21转债"],
    "first_trade": ["20190101", "20220121"],
    "last_trade": ["20230110", "20260930"],
})


def _archive(tmp_path, records):
    path = tmp_path / "ann.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    return path


def test_notice_goes_to_the_bond_named_in_the_title_not_by_list_position(tmp_path):
    # the archive stores codes and names as two separately sorted lists: position 0 of one is not position 0
    # of the other. 台华转债 is 113525 (second code) but sorts first among the names.
    rec = {"record_type": "announcement", "keyword": "赎回", "stk_code": "603055",
           "candidate_ts_codes": ["113638.SH", "113525.SH"], "candidate_bond_names": ["台21转债", "台华转债"][::-1],
           "ann_title": "关于提前赎回“台华转债”的提示性公告", "ann_datetime": "2022-12-14", "ann_url": "u"}
    notices = load_notices([_archive(tmp_path, [rec])], LIFE)
    assert notices["ts_code"].tolist() == ["113525.SH"]
    assert bool(notices["named_in_title"].iloc[0]) is True


def test_only_the_full_short_name_counts(tmp_path):
    life = pd.DataFrame({"ts_code": ["127008.SZ", "127021.SZ"], "bond_short_name": ["特发转债", "特发转2"],
                         "first_trade": ["20190101", "20200901"], "last_trade": ["20260930", "20260930"]})
    rec = {"record_type": "announcement", "keyword": "赎回", "candidate_ts_codes": ["127008.SZ", "127021.SZ"],
           "ann_title": "关于“特发转2”赎回结果的公告", "ann_datetime": "2022-01-05", "ann_url": "u"}
    assert load_notices([_archive(tmp_path, [rec])], life)["ts_code"].tolist() == ["127021.SZ"]


def test_incremental_file_adds_without_duplicating(tmp_path):
    rec = {"record_type": "announcement", "keyword": "赎回", "candidate_ts_codes": ["113525.SH"],
           "ann_title": "关于提前赎回“台华转债”的提示性公告", "ann_datetime": "2022-12-14", "ann_url": "u"}
    later = {**rec, "ann_title": "关于实施“台华转债”赎回暨摘牌的公告", "ann_datetime": "2022-12-27 00:00:00"}
    full = _archive(tmp_path, [rec])
    incr = tmp_path / "incr.jsonl"
    incr.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in (rec, later)) + "\n", encoding="utf-8")
    assert len(load_notices([full, incr], LIFE)) == 2


def test_titles_are_classified_by_what_they_announce():
    assert classify_title("关于提前赎回“台华转债”的提示性公告") == "call"
    assert classify_title("关于不提前赎回“韦尔转债”的公告") == "no_call"
    assert classify_title("关于“国微转债”重新计算是否满足赎回条件的提示性公告") == "may_trigger"
    assert classify_title("关于“平银转债”赎回结果的公告") == "call_late"
    assert ev.classify_revision_title("关于董事会提议向下修正“纵横转债”转股价格的公告") == ev.REVISION_PROPOSAL
    assert ev.classify_revision_title("关于不向下修正“韦尔转债”转股价格的公告") == ev.NO_REVISION
    assert ev.classify_revision_title("关于天赐转债预计触发转股价格向下修正条件的提示性公告") == ev.REVISION_MAY_TRIGGER
    assert ev.classify_revision_title("关于向下修正万青转债转股价格的公告") == ev.REVISION_DECIDED


def test_a_rating_cut_is_an_event_and_an_unknown_rating_is_not_guessed():
    ratings = pd.DataFrame({
        "ts_code": ["A"] * 4 + ["B"] * 3 + ["C"] * 2,
        "scope": ["bond", "bond", "bond", "issuer", "bond", "bond", "bond", "issuer", "issuer"],
        "ann_date": ["20200601", "20210601", "20220601", "20220701", "20200101", "20210101", "20220101", "20200101", "20210101"],
        # A: AA -> AA (affirmed) -> AA- (cut); its issuer row is ignored
        # B: AA -> a code outside the scale -> A+: the unknown step breaks the chain, no cut is invented
        # C: only issuer rows, which mix agencies
        "rating": ["AA", "AA", "AA-", "A", "AA", "AApi", "A+", "AA", "A"],
    })
    cuts = ev.rating_downgrades(ratings)
    assert cuts.to_dict("records") == [{"ts_code": "A", "event_date": "20220601", "detail": "AA->AA-"}]
