"""Compatibility aliases for strategy-agnostic research framework modules.

The implementation still lives in ``strategies.cb_redemption`` for backward
compatibility. New code may import ``framework.<module>`` while old imports
continue to work.
"""

ALIASED_MODULES = (
    "auditor",
    "benchmarks",
    "bootstrap",
    "editor",
    "evaluator",
    "holdout",
    "holdout_splitter",
    "hypothesizer",
    "judge",
    "llm_queue",
    "memory",
    "orchestrator",
    "pool_stats",
    "result_types",
    "sanity_checker",
)

__all__ = ["ALIASED_MODULES"]
