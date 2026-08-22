"""Crash-safe single-slot admission state for reviewed research inputs."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

import yaml

from framework.evaluation.research_feedback import RESEARCH_INPUT_SCHEMA_VERSION, ResearchInput


class PendingResearchInputStore:
    """One active input, event-log authority, nonce-CAS claim lifecycle.

    State transitions are history-first.  A missing slot after a crash is
    reconstructed from the last nonterminal history snapshot on the next
    locked operation.
    """

    def __init__(self, pending_path: Path | str, history_path: Path | str) -> None:
        self.pending_path = Path(pending_path)
        self.history_path = Path(history_path)
        self.lock_path = self.pending_path.with_suffix(self.pending_path.suffix + ".lock")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def admission_key(research_input: ResearchInput) -> str:
        return f"{research_input.source_review_id}+{research_input.source_run_id}"

    def _read_slot(self) -> dict[str, Any] | None:
        if not self.pending_path.exists():
            return None
        raw = yaml.safe_load(self.pending_path.read_text(encoding="utf-8")) or {}
        return dict(raw) if isinstance(raw, Mapping) else None

    def _atomic_yaml(self, path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                yaml.safe_dump(dict(payload), handle, allow_unicode=True, sort_keys=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path)
            self._fsync_dir(path.parent)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _append_event(self, event: str, slot: Mapping[str, Any] | None, *, reason: str | None = None) -> None:
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        row = {"event": event, "at": int(time.time()), "slot": dict(slot) if slot else None}
        if reason:
            row["reason"] = reason
        with self.history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._fsync_dir(self.history_path.parent)

    def _events(self) -> list[dict[str, Any]]:
        if not self.history_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in self.history_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    def _latest_by_key(self, key: str) -> dict[str, Any] | None:
        for row in reversed(self._events()):
            slot = row.get("slot")
            if isinstance(slot, Mapping) and slot.get("admission_key") == key:
                return row
        return None

    def _reservation_path(self, output_root: Path, proposal_id: str) -> Path:
        return output_root / ".research_input_reservations" / f"{proposal_id}.json"

    def _reserve(self, output_root: Path, proposal_id: str, nonce: str) -> bool:
        marker = self._reservation_path(output_root, proposal_id)
        marker.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"proposal_id": proposal_id, "claim_nonce": nonce}, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        self._fsync_dir(marker.parent)
        return True

    def _remove_reservation(self, output_root: Path, proposal_id: str, nonce: str) -> None:
        marker = self._reservation_path(output_root, proposal_id)
        try:
            raw = json.loads(marker.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return
        if raw.get("claim_nonce") == nonce:
            marker.unlink(missing_ok=True)
            self._fsync_dir(marker.parent)

    def _slot_from_latest_event(self) -> dict[str, Any] | None:
        for row in reversed(self._events()):
            slot = row.get("slot")
            if isinstance(slot, Mapping):
                state = str(slot.get("state") or "")
                if state in {"pending", "claimed"}:
                    return dict(slot)
                if state in {"consumed", "released", "stale_plan_version"}:
                    return None
        return None

    def _reconstruct(self, output_root: Path, ttl_seconds: int) -> dict[str, Any] | None:
        slot = self._read_slot()
        if slot is not None:
            return slot
        slot = self._slot_from_latest_event()
        if slot is None:
            return None
        if slot.get("state") == "claimed":
            age = time.time() - float(slot.get("claimed_at") or 0)
            if age > ttl_seconds:
                self._append_event("released", {**slot, "state": "released"}, reason="reconstruct_ttl_expired")
                # Keep the reservation tombstone: the expired worker may still
                # write its old path, so a later claim must choose a new id.
                return None
            nonce, proposal_id = str(slot.get("claim_nonce") or ""), str(slot.get("proposal_id") or "")
            if nonce and proposal_id:
                self._reserve(output_root, proposal_id, nonce)
        self._atomic_yaml(self.pending_path, slot)
        return slot

    def active(self, output_root: Path | str, ttl_seconds: int) -> dict[str, Any] | None:
        with self._locked():
            return self._reconstruct(Path(output_root), ttl_seconds)

    def admit(self, research_input: ResearchInput, active_plan_version: str, output_root: Path | str, ttl_seconds: int,
              admission_operation_id: str | None = None) -> tuple[bool, str]:
        if research_input.schema_version != RESEARCH_INPUT_SCHEMA_VERSION:
            return False, "RESEARCH_INPUT_SCHEMA_VERSION_INVALID"
        if research_input.plan_version != str(active_plan_version):
            return False, "RESEARCH_INPUT_STALE_PLAN_VERSION"
        output_root = Path(output_root)
        key = self.admission_key(research_input)
        with self._locked():
            slot = self._reconstruct(output_root, ttl_seconds)
            if slot is not None:
                return False, "RESEARCH_INPUT_SLOT_OCCUPIED"
            latest = self._latest_by_key(key)
            if latest is not None:
                state = str((latest.get("slot") or {}).get("state") or "")
                if state not in {"released", "stale_plan_version"}:
                    return False, "RESEARCH_INPUT_REPLAY_REJECTED"
            next_slot = {"state": "pending", "admission_key": key, "research_input": research_input.to_dict(), "admitted_at": int(time.time(),),
                         "admission_operation_id": admission_operation_id or key}
            self._append_event("admitted", next_slot)
            self._atomic_yaml(self.pending_path, next_slot)
            return True, "ADMITTED"

    @staticmethod
    def _base_id(research_input: ResearchInput) -> str:
        raw = f"research_input_{research_input.question.question_id}_{research_input.source_run_id}"
        return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in raw).strip("_") or "research_input"

    def _mark_stale(self, slot: Mapping[str, Any], output_root: Path, reason: str) -> None:
        stale = {**slot, "state": "stale_plan_version", "stale_at": int(time.time())}
        self._append_event("stale_plan_version", stale, reason=reason)
        self.pending_path.unlink(missing_ok=True)
        # Retain a reservation tombstone if there was a claim. A slow worker
        # cannot consume after this transition, and must not collide with a new
        # claimant that receives a fresh proposal id.

    def claim(self, active_plan_version: str, output_root: Path | str, ttl_seconds: int) -> tuple[dict[str, Any] | None, str]:
        output_root = Path(output_root)
        with self._locked():
            slot = self._reconstruct(output_root, ttl_seconds)
            if slot is None:
                return None, "NO_PENDING_RESEARCH_INPUT"
            if slot.get("state") == "claimed":
                return None, "RESEARCH_INPUT_CLAIMED"
            raw_input = slot.get("research_input")
            research_input = ResearchInput.from_dict(raw_input) if isinstance(raw_input, Mapping) else None
            if research_input is None or research_input.plan_version != str(active_plan_version):
                self._mark_stale(slot, output_root, "claim_plan_version_mismatch")
                return None, "RESEARCH_INPUT_STALE_PLAN_VERSION"
            base, nonce = self._base_id(research_input), secrets.token_urlsafe(24)
            proposal_id = base
            for index in range(1000):
                if index:
                    proposal_id = f"{base}_{index}"
                if (output_root / proposal_id).exists():
                    continue
                if not self._reserve(output_root, proposal_id, nonce):
                    continue
                if (output_root / proposal_id).exists():
                    self._remove_reservation(output_root, proposal_id, nonce)
                    continue
                claimed = {**slot, "state": "claimed", "proposal_id": proposal_id,
                           "proposal_path": str(output_root / proposal_id / "proposal.yaml"),
                           "claim_nonce": nonce, "claimed_at": int(time.time())}
                self._append_event("claimed", claimed)
                self._atomic_yaml(self.pending_path, claimed)
                return claimed, "CLAIMED"
            raise RuntimeError("unable to reserve a proposal id for research input")

    def release_claim(self, claim_nonce: str, output_root: Path | str, reason: str) -> bool:
        output_root = Path(output_root)
        with self._locked():
            slot = self._read_slot()
            if not isinstance(slot, Mapping) or slot.get("state") != "claimed" or slot.get("claim_nonce") != claim_nonce:
                return False
            released = {**slot, "state": "released", "released_at": int(time.time())}
            self._append_event("released", released, reason=reason)
            self.pending_path.unlink(missing_ok=True)
            # This is an explicit completion signal from the owning nonce, so
            # that worker cannot subsequently write the reserved proposal.
            # Unlike TTL recovery, it is safe to release the reservation now.
            self._remove_reservation(output_root, str(slot.get("proposal_id") or ""), claim_nonce)
            return True

    def consume(self, claim_nonce: str, output_root: Path | str, proposal_path: Path | str) -> bool:
        output_root = Path(output_root)
        with self._locked():
            slot = self._read_slot()
            if not isinstance(slot, Mapping) or slot.get("state") != "claimed" or slot.get("claim_nonce") != claim_nonce:
                return False
            consumed = {**slot, "state": "consumed", "consumed_at": int(time.time()), "durable_proposal_path": str(proposal_path)}
            self._append_event("consumed", consumed)
            self.pending_path.unlink(missing_ok=True)
            self._remove_reservation(output_root, str(slot.get("proposal_id") or ""), claim_nonce)
            return True

    def recover(self, active_plan_version: str, output_root: Path | str, ttl_seconds: int,
                artifact_valid: Callable[[dict[str, Any]], bool]) -> str:
        output_root = Path(output_root)
        with self._locked():
            slot = self._reconstruct(output_root, ttl_seconds)
            if slot is None:
                return "NO_PENDING_RESEARCH_INPUT"
            raw_input = slot.get("research_input")
            research_input = ResearchInput.from_dict(raw_input) if isinstance(raw_input, Mapping) else None
            if research_input is None or research_input.plan_version != str(active_plan_version):
                self._mark_stale(slot, output_root, "recover_plan_version_mismatch")
                return "RESEARCH_INPUT_STALE_PLAN_VERSION"
            if slot.get("state") != "claimed":
                return "PENDING_RESEARCH_INPUT"
            # Path("") means the current working directory, never a proposal
            # artifact. Treat missing/blank metadata as no durable artifact.
            raw_proposal_path = str(slot.get("proposal_path") or "").strip()
            proposal_path = Path(raw_proposal_path) if raw_proposal_path else None
            # Recovery intentionally does not require a live worker nonce: it
            # is the lock-owning system authority after a crash. It may consume
            # only the currently claimed slot and only after full artifact
            # validation by the caller.
            if proposal_path is not None and proposal_path.exists() and artifact_valid(dict(slot)):
                consumed = {**slot, "state": "consumed", "consumed_at": int(time.time()), "durable_proposal_path": str(proposal_path)}
                self._append_event("consumed", consumed)
                self.pending_path.unlink(missing_ok=True)
                self._remove_reservation(output_root, str(slot.get("proposal_id") or ""), str(slot.get("claim_nonce") or ""))
                return "RECOVERED_CONSUMED"
            if time.time() - float(slot.get("claimed_at") or 0) > ttl_seconds:
                released = {**slot, "state": "released", "released_at": int(time.time())}
                self._append_event("released", released, reason="claim_ttl_expired")
                self.pending_path.unlink(missing_ok=True)
                # Keep the marker as a tombstone; see _mark_stale.
                return "RELEASED_EXPIRED_CLAIM"
            return "RESEARCH_INPUT_CLAIMED"
