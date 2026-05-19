"""Autonomous research framework compatibility modules.

The active architecture has 5 nodes: state_and_rules, ideation, proposal_gate,
runner, and review_memory. The modules exported here are implementation files
inside those nodes, not separate architecture nodes.
"""

__all__ = [
    "ai_provider_adapter",
    "artifacts",
    "evidence_tool_registry",
    "executor_registry",
    "ideation_cycle",
    "orchestrator",
    "paths",
    "proposal_rewrite_loop",
    "proposal_schema",
    "recent_results_digest",
    "result_reviewer",
    "spec_compiler",
    "strategy_ideator",
    "verification_tool",
]
