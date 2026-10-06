"""A registered fact may be unanswered, but it may not be missing or silently empty."""

from pathlib import Path

import pandas as pd
import pytest
import yaml

from cb_market import facts as F
from cb_market.identities import COMPONENTS, decompose

FACTS_YAML = Path(__file__).resolve().parents[2] / "data" / "cb_market_facts" / "facts.yaml"


def test_every_registered_fact_is_in_the_table_with_an_honest_status():
    doc = yaml.safe_load(FACTS_YAML.read_text(encoding="utf-8"))
    by_id = {r["id"]: r for r in doc["facts"]}
    assert set(by_id) == {f.id for f in F.REGISTRY}
    for rec in by_id.values():
        assert rec["status"] in ("measured", "not_measured")
        if rec["status"] == "not_measured":
            assert rec["reason"].strip()
        else:
            assert rec["n"] > 0 and rec["values"]
            if rec["detail"]:
                assert (FACTS_YAML.parent / rec["detail"]).exists()
    assert doc["warehouse_end"] and doc["inputs"]


def test_a_measurement_that_raises_becomes_not_measured(monkeypatch, tmp_path):
    import scripts.build_cb_market_facts as build

    def boom(_):
        raise RuntimeError("no data")

    monkeypatch.setattr(F, "REGISTRY", [F.Fact("X1", "identity", "s", boom), F.Fact("X2", "identity", "s", lambda _: F.Measured({}, 0, ("", "")))])
    monkeypatch.setattr(build, "OUT_DIR", tmp_path)
    monkeypatch.setattr(build, "load_panel", lambda: _panel())
    monkeypatch.setattr(build, "load_events", lambda: pd.DataFrame())
    monkeypatch.setattr(pd, "read_parquet", lambda *a, **k: pd.DataFrame())
    doc = build.run()
    assert [r["status"] for r in doc["facts"]] == ["not_measured", "not_measured"]  # raised; empty sample
    assert "RuntimeError" in doc["facts"][0]["reason"]


def _panel() -> pd.DataFrame:
    p = pd.DataFrame({
        "ts_code": ["A", "A", "B", "B"], "trade_date": ["20240102", "20240103"] * 2,
        "r": [0.0, 0.03, 0.0, -0.01], "r_stock": [0.0, 0.01, 0.0, -0.02],
        "r_conv_price": [0.0, 0.00, 0.0, 0.30], "r_premium": [0.0, 0.02, 0.0, -0.29],
    })
    p.attrs.update(inputs={}, end="20240103")
    return p


def test_decomposition_components_add_up_to_the_return():
    d = decompose(_panel())
    assert d["r"].iloc[0] == pytest.approx(d[COMPONENTS[1:]].iloc[0].sum())
    held_a = decompose(_panel(), weights=pd.Series([1, 1, 0, 0]))
    assert held_a["r"].iloc[0] == pytest.approx(0.03) and held_a["r_conv_price"].iloc[0] == 0.0


def _scripts_writing(filename: str) -> set[str]:
    """Scripts that write `filename` into the shared warehouse (run-local copies elsewhere do not count)."""
    import re

    root = Path(__file__).resolve().parents[2]
    hits = set()
    for path in list((root / "scripts").glob("*.py")) + list((root / "cb_market").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if re.search(rf'to_parquet\(\s*WAREHOUSE(?:_DIR)? / "{re.escape(filename)}"', text):
            hits.add(path.name)
    return hits


def test_each_shared_table_has_exactly_one_writer():
    # cb_call.parquet used to be written by build_cb_warehouse.py too, with every notice type as a "call"
    assert _scripts_writing("cb_call.parquet") == {"build_cb_call_history.py"}
    assert _scripts_writing("cb_contract_terms.parquet") == {"build_cb_contract_terms.py"}


def test_point_in_time_conversion_price_has_one_answer():
    # evaluation scripts must not carry their own conversion-value lookup next to verifier.point_in_time_conv_price
    root = Path(__file__).resolve().parents[2]
    own_lookup = [
        p.name for p in (root / "scripts").glob("evaluate_cb_arb_pit_*.py")
        if "100.0 * stock_price /" in p.read_text(encoding="utf-8")
    ]
    assert own_lookup == []


def test_call_eligibility_rule_lives_only_in_call_condition():
    # the panel feeds per-bond terms to call_condition.is_call_eligible; it must not count trigger days itself
    text = (Path(__file__).resolve().parents[1] / "panel.py").read_text(encoding="utf-8")
    assert "is_call_eligible(" in text and ".rolling(" not in text
