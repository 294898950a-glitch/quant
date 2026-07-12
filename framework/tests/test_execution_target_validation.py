from __future__ import annotations

from scripts.research_sanity_checker import check_compute_placement_declared


def _spec(target: str, *, spot: int, local: int):
    return {
        "execution_target": target,
        "strategy_id": "cb_arb_value_gap_switch",
        "automation": {"command": ["python3", "scripts/evaluate_cb_arb_example.py"]},
        "compute_estimate": {"spot_minutes": spot, "local_minutes": local},
    }


def test_hzpc_accepts_explicit_local_compute():
    assert check_compute_placement_declared(_spec("hzpc", spot=0, local=30)) == []


def test_hzpc_rejects_mixed_or_empty_compute_budget():
    assert check_compute_placement_declared(_spec("hzpc", spot=10, local=30))[0].rule_id == "hzpc_requires_local_compute"


def test_default_target_keeps_spot_only_contract():
    assert check_compute_placement_declared(_spec("sig_spot", spot=0, local=30))[0].rule_id == "cb_arb_backtest_requires_spot_minutes"


def test_unknown_target_is_rejected_instead_of_falling_back_to_sig_spot():
    assert check_compute_placement_declared(_spec("sig-spot-typo", spot=30, local=0))[0].rule_id == "unsupported_execution_target"
