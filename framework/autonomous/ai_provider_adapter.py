"""Provider-agnostic AI adapter shell with retry behavior."""

from __future__ import annotations

import hashlib
import os
import subprocess
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
    active = str(data.get("active_provider") or "")
    providers = data.get("providers") or {}
    if not isinstance(providers, dict) or active not in providers:
        raise ValueError(f"active_provider {active!r} is not registered")
    return data


def validate_entrypoint(providers: dict[str, Any], entrypoint: str) -> None:
    policy = providers.get("policy") or {}
    allowed = str(policy.get("allowed_entrypoint") or "")
    if allowed and entrypoint != allowed:
        raise ValueError(f"AI provider calls must use {allowed}, got {entrypoint}")
    active = str(providers.get("active_provider") or "")
    provider_cfg = (providers.get("providers") or {}).get(active) or {}
    provider_allowed = str(provider_cfg.get("allowed_entrypoint") or allowed)
    if provider_allowed and entrypoint != provider_allowed:
        raise ValueError(f"provider {active} is not allowed from {entrypoint}")
    if provider_cfg.get("enabled") is not True:
        raise ValueError(f"provider {active} is registered but not enabled")


def _call_command_provider(provider_id: str, provider_cfg: dict[str, Any], prompt: str) -> str:
    command = provider_cfg.get("command")
    if not isinstance(command, list) or not command:
        raise RuntimeError(f"provider {provider_id} has no command contract")
    cmd = [str(part) for part in command]
    prompt_mode = str(provider_cfg.get("prompt_mode") or "stdin")
    timeout = int(provider_cfg.get("timeout_seconds") or 240)
    kwargs: dict[str, Any] = {
        "text": True,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "check": False,
        "timeout": timeout,
    }
    if prompt_mode == "stdin":
        kwargs["input"] = prompt
    elif prompt_mode == "last_arg":
        cmd.append(prompt)
    else:
        raise ValueError(f"provider {provider_id} has unsupported prompt_mode={prompt_mode!r}")
    env = os.environ.copy()
    result = subprocess.run(cmd, env=env, **kwargs)
    if result.returncode != 0:
        raise RuntimeError(
            f"provider {provider_id} command failed rc={result.returncode}: {result.stderr or result.stdout}"
        )
    return str(result.stdout or "").strip()


def _call_provider(provider_id: str, provider_cfg: dict[str, Any], prompt: str, schema: dict[str, Any]) -> str:
    client = provider_cfg.get("client")
    if client is not None:
        return client(prompt=prompt, schema=schema)
    if provider_cfg.get("enabled") is not True:
        raise RuntimeError(f"provider {provider_id} is registered but not enabled")
    if str(provider_cfg.get("kind") or "") == "command_adapter":
        return _call_command_provider(provider_id, provider_cfg, prompt)
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


class RegisteredProviderAdapter:
    def __init__(
        self,
        providers_path: Path,
        *,
        repo_root: Path | str = Path("."),
        entrypoint: str = "scripts/run_strategy_ideation_once.py",
    ) -> None:
        self.providers_path = Path(providers_path)
        self.repo_root = Path(repo_root)
        self.entrypoint = entrypoint
        self.providers = load_providers(self.providers_path)
        validate_entrypoint(self.providers, self.entrypoint)

    def call_active_provider(self, prompt: str, schema: dict[str, Any]) -> ProviderResponse:
        return call_active_provider(prompt=prompt, schema=schema, providers=self.providers)
