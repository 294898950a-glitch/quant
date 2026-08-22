Evaluation foundation
=====================

This package is the strategy-neutral measurement and validation layer.

Run its standard-library test suite with:

    python3 -m unittest discover -s framework/tests -p 'test_evaluation*.py'

The current suite contains 42 tests covering metrics, benchmark alignment,
cost arithmetic, backtest aggregation, plan completeness, evidence typing,
fail-closed gate evaluation, and benchmark provider injection.

`framework/autonomous/evaluation_protocol.py` is a deprecated compatibility
module. New evaluation contracts belong in `contract.py`, evidence wrapping in
`evidence.py`, and decisions in `validation.py`.
