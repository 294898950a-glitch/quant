from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ResearchPaths:
    repo_root: Path = Path(".")
    data_root: Path = Path("data")
    research_framework_dir: Path = Path("data/research_framework")

    @classmethod
    def from_repo_root(cls, repo_root: Path | str = Path(".")) -> "ResearchPaths":
        root = Path(repo_root)
        return cls(
            repo_root=root,
            data_root=root / "data",
            research_framework_dir=root / "data" / "research_framework",
        )

    @property
    def current(self) -> Path:
        return self.research_framework_dir / "current.yaml"

    @property
    def recent_results_digest(self) -> Path:
        return self.research_framework_dir / "recent_results_digest.yaml"

    @property
    def executor_registry(self) -> Path:
        return self.research_framework_dir / "executor_registry.yaml"

    @property
    def mechanics_vocab(self) -> Path:
        return self.research_framework_dir / "mechanics_vocab.yaml"

    @property
    def evidence_tool_registry(self) -> Path:
        return self.research_framework_dir / "evidence_tool_registry.yaml"

    @property
    def ai_providers(self) -> Path:
        return self.research_framework_dir / "ai_providers.yaml"

    @property
    def framework_change_log(self) -> Path:
        return self.research_framework_dir / "framework_change_log.jsonl"

    @property
    def strategy_ideator_config(self) -> Path:
        return self.research_framework_dir / "strategy_ideator.yaml"

    @property
    def ai_prompt_contracts(self) -> Path:
        return self.research_framework_dir / "ai_prompt_contracts.yaml"

    @property
    def data_inventory(self) -> Path:
        return self.research_framework_dir / "data_inventory.yaml"

    @property
    def research_queue(self) -> Path:
        return self.research_framework_dir / "research_queue.yaml"

    @property
    def research_insights(self) -> Path:
        return self.research_framework_dir / "research_insights.yaml"

    @property
    def ideation_policy_state(self) -> Path:
        return self.research_framework_dir / "ideation_policy_state.json"
