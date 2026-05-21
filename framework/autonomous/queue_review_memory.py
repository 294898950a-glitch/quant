"""Queue-facing review and memory service."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable


class QueueReviewMemoryService:
    def __init__(
        self,
        *,
        repo_root: Path,
        run: Callable[..., Any],
        save_state: Callable[[dict[str, Any]], None],
        audit: Callable[[str, dict[str, Any] | None], None],
        mark_history: Callable[[dict[str, Any], dict[str, Any], str, str], None],
        rel: Callable[[Path], str],
        now_iso: Callable[[], str],
    ) -> None:
        self.repo_root = repo_root
        self.run = run
        self.save_state = save_state
        self.audit = audit
        self.mark_history = mark_history
        self.rel = rel
        self.now_iso = now_iso

    def review_and_digest_run(self, run_dir: Path) -> dict[str, Any]:
        review_result = self.run([sys.executable, "scripts/review_result.py", self.rel(run_dir)], check=True)
        digest_result = self.run(
            [
                sys.executable,
                "scripts/build_recent_results_digest.py",
                "--limit",
                "10",
                "--update-current",
            ],
            check=True,
        )
        review_path = run_dir / "review.yaml"
        digest_path = self.repo_root / "data" / "research_framework" / "recent_results_digest.yaml"
        current_path = self.repo_root / "data" / "research_framework" / "current.yaml"
        return {
            "review_path": self.rel(review_path),
            "digest_path": self.rel(digest_path),
            "current_path": self.rel(current_path),
            "review_output": (review_result.stdout or "")[-1000:],
            "digest_output": (digest_result.stdout or "")[-1000:],
        }

    def review_pending_items(self, state: dict[str, Any], queue: list[Any]) -> int:
        changed = 0
        for item in queue:
            if not isinstance(item, dict) or item.get("status") != "review_pending":
                continue
            spec_raw = item.get("spec_path")
            if not isinstance(spec_raw, str) or not spec_raw:
                item["status"] = "failed"
                item["failed_at"] = self.now_iso()
                item["failure_reason"] = "review_pending item missing spec_path"
                self.mark_history(state, item, "failed", item["failure_reason"])
                changed += 1
                continue
            spec_path = Path(spec_raw)
            if not spec_path.is_absolute():
                spec_path = self.repo_root / spec_path
            run_dir = spec_path.parent
            try:
                review_payload = self.review_and_digest_run(run_dir)
                item["workflow_stage"] = "digested"
                item["review_path"] = review_payload["review_path"]
                item["digest_path"] = review_payload["digest_path"]
                item["status"] = "complete"
                item["completed_at"] = self.now_iso()
                self.mark_history(state, item, "complete", "remote run reviewed and digest updated")
                self.audit("review", {"item_id": item.get("id"), "run_dir": self.rel(run_dir), **review_payload})
                self.audit("digest", {"item_id": item.get("id"), "run_dir": self.rel(run_dir), "result": "complete", **review_payload})
                changed += 1
            except Exception as exc:
                item["status"] = "failed"
                item["failed_at"] = self.now_iso()
                item["failure_reason"] = f"review_failed: {type(exc).__name__}: {exc}"
                self.mark_history(state, item, "failed", item["failure_reason"])
                changed += 1
        if changed:
            state["queue"] = queue
            self.save_state(state)
        return changed
