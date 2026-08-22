#!/usr/bin/env python3
"""Admit an externally reviewed exploration packet into the next ideation slot."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from framework.autonomous.paths import ResearchPaths  # noqa: E402
from framework.evaluation.pending_research_input import PendingResearchInputStore  # noqa: E402
from framework.evaluation.research_feedback import ClaudeReview, ExplorationPacket, accept_claude_review  # noqa: E402
from framework.evaluation.research_question import ResearchQuestionLedger  # noqa: E402
from framework.evaluation.research_question_shadow import ShadowAdmissionService, admission_operation_id  # noqa: E402
from scripts.quant_access_guard import require_ticket  # noqa: E402


def _load(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    raw = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise ValueError(f"{path} root must be a mapping")
    return raw


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    args = parser.parse_args()
    require_ticket("admit_research_input")
    paths = ResearchPaths.from_repo_root(REPO_ROOT)
    config = _load(paths.strategy_ideator_config)
    lifecycle_mode = str((config.get("research_lifecycle") or {}).get("mode") or "off")
    if lifecycle_mode != "off" and not paths.research_question_policy.exists():
        print(json.dumps({"status": "RESEARCH_LIFECYCLE_POLICY_MISSING"}, ensure_ascii=False))
        return 1
    packet = ExplorationPacket.from_dict(_load(args.packet))
    review = ClaudeReview.from_dict(_load(args.review))
    research_input, errors = accept_claude_review(packet, review)
    if errors or research_input is None:
        print(json.dumps({"status": "RESEARCH_INPUT_NOT_ADMITTED", "errors": errors}, ensure_ascii=False))
        return 1
    store = PendingResearchInputStore(paths.pending_research_input, paths.research_input_admission_history)
    operation_id = admission_operation_id(research_input)
    shadow = ShadowAdmissionService(ResearchQuestionLedger(paths.research_question_events, paths.research_question_policy), store, paths.data_root,
                                    int(config.get("research_input_claim_ttl_seconds") or 900)) if lifecycle_mode != "off" else None
    if shadow:
        shadow.record_attempt(research_input)
    accepted, status = store.admit(
        research_input, str(config.get("research_input_plan_version") or ""), paths.data_root,
        int(config.get("research_input_claim_ttl_seconds") or 900), operation_id,
    )
    if accepted and shadow:
        shadow.reconcile()
    print(json.dumps({"status": status, "source_review_id": research_input.source_review_id,
                      "source_run_id": research_input.source_run_id}, ensure_ascii=False))
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
