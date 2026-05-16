import importlib

import framework


def test_framework_aliases_keep_old_module_objects():
    checked = []
    for name in framework.ALIASED_MODULES:
        try:
            old_module = importlib.import_module(f"strategies.cb_redemption.{name}")
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.startswith("strategies.cb_redemption"):
                raise
            continue
        new_module = importlib.import_module(f"framework.{name}")
        assert new_module is old_module
        checked.append(name)
    assert checked


def test_representative_symbol_imports_work():
    from framework.evaluator import EvaluationResult
    from framework.result_types import BacktestResult

    assert EvaluationResult.__module__ == "strategies.cb_redemption.evaluator"
    assert BacktestResult.__module__ == "strategies.cb_redemption.result_types"
