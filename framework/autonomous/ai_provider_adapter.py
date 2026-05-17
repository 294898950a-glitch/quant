"""Provider-agnostic AI adapter shell with retry behavior."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

import yaml


MAX_RETRIES = 3


class ProviderResponse:
    def __init__(self, content: str, provider_id: str, response_hash: str | None = None, retries_used: int = 0):
        self.content = content
        self.provider_id = provider_id
        self.response_hash = response_hash or hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        self.retries_used = retries_used


class TransientProviderError(RuntimeError):
    pass


def load_providers(path: Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError("provider config must be a mapping")
    return data


def _call_provider(provider_id: str, provider_cfg: dict[str, Any], prompt: str, schema: dict[str, Any]) -> str:
    client = provider_cfg.get("client")
    if client is not None:
        return client(prompt=prompt, schema=schema)
    raise RuntimeError(f"provider {provider_id} has no configured client")


def call_active_provider(
    prompt: str,
    schema: dict[str, Any],
    providers: dict[str, Any] | None = None,
    provider_client=None,
) -> ProviderResponse:
    cfg = providers or {"active_provider": "codex", "providers": {"codex": {}}}
    provider_id = cfg.get("active_provider", "codex")
    provider_cfg = dict((cfg.get("providers") or {}).get(provider_id, {}))
    if provider_client is not None:
        provider_cfg["client"] = provider_client

    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            content = _call_provider(provider_id, provider_cfg, prompt, schema)
            return ProviderResponse(str(content), provider_id=provider_id, retries_used=attempt)
        except (TransientProviderError, TimeoutError, ConnectionError) as exc:
            last_error = exc
            if attempt == MAX_RETRIES - 1:
                break
            time.sleep(0.05 * (2**attempt))
    raise RuntimeError(f"provider {provider_id} failed after retry limit") from last_error
