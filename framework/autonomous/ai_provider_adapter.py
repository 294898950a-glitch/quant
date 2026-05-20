"""Provider-agnostic AI adapter shell with retry behavior."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


MAX_RETRIES = 3
PROVIDER_DEBUG_DIR = Path("logs/provider_debug")


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
    allowed_values = policy.get("allowed_entrypoints")
    if isinstance(allowed_values, list):
        allowed = {str(value) for value in allowed_values}
    else:
        legacy = str(policy.get("allowed_entrypoint") or "")
        allowed = {legacy} if legacy else set()
    if allowed and entrypoint not in allowed:
        raise ValueError(f"AI provider calls must use one of {sorted(allowed)}, got {entrypoint}")
    active = str(providers.get("active_provider") or "")
    provider_cfg = (providers.get("providers") or {}).get(active) or {}
    provider_allowed_values = provider_cfg.get("allowed_entrypoints")
    if isinstance(provider_allowed_values, list):
        provider_allowed = {str(value) for value in provider_allowed_values}
    else:
        legacy_provider = str(provider_cfg.get("allowed_entrypoint") or "")
        provider_allowed = {legacy_provider} if legacy_provider else allowed
    if provider_allowed and entrypoint not in provider_allowed:
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


def _read_api_key(provider_cfg: dict[str, Any]) -> str:
    env_name = str(provider_cfg.get("api_key_env") or "").strip()
    if env_name and os.environ.get(env_name):
        return str(os.environ[env_name]).strip()
    key_file = str(provider_cfg.get("api_key_file") or "").strip()
    if key_file:
        key = Path(key_file).expanduser().read_text(encoding="utf-8").strip()
        if key:
            return key
    raise RuntimeError("provider api key is not configured")


def _call_openai_chat_provider(provider_id: str, provider_cfg: dict[str, Any], prompt: str) -> str:
    api_key = _read_api_key(provider_cfg)
    endpoint = str(provider_cfg.get("endpoint") or "").strip()
    if not endpoint:
        base_url = str(provider_cfg.get("base_url") or "https://api.deepseek.com").rstrip("/")
        endpoint = f"{base_url}/chat/completions"
    model = str(provider_cfg.get("model") or "").strip()
    if not model:
        raise RuntimeError(f"provider {provider_id} missing model")
    timeout = int(provider_cfg.get("timeout_seconds") or 240)
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "Return only the requested machine-readable object. Do not include Markdown fences.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": float(provider_cfg.get("temperature", 0.2)),
    }
    max_tokens = provider_cfg.get("max_tokens")
    if max_tokens is not None:
        payload["max_tokens"] = int(max_tokens)
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    http_status: int | None = None
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            http_status = getattr(response, "status", None)
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code in {408, 409, 425, 429, 500, 502, 503, 504}:
            raise TransientProviderError(f"provider {provider_id} HTTP {exc.code}: {body}") from exc
        raise RuntimeError(f"provider {provider_id} HTTP {exc.code}: {body}") from exc
    data = json.loads(raw)
    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices:
        raise RuntimeError(f"provider {provider_id} returned no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        diagnostics_path = _write_empty_content_diagnostics(
            provider_id=provider_id,
            model=model,
            http_status=http_status,
            choice=choices[0] if isinstance(choices[0], dict) else None,
            message=message,
            raw=raw,
        )
        raise RuntimeError(f"provider {provider_id} returned empty content; diagnostics={diagnostics_path}")
    return content.strip()


def _write_empty_content_diagnostics(
    *,
    provider_id: str,
    model: str,
    http_status: int | None,
    choice: dict[str, Any] | None,
    message: dict[str, Any] | None,
    raw: str,
) -> str:
    PROVIDER_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = PROVIDER_DEBUG_DIR / f"{ts}_{provider_id}_empty_content.json"
    diagnostic = {
        "provider_id": provider_id,
        "model": model,
        "http_status": http_status,
        "finish_reason": choice.get("finish_reason") if isinstance(choice, dict) else None,
        "choice_keys": sorted(choice.keys()) if isinstance(choice, dict) else [],
        "message_keys": sorted(message.keys()) if isinstance(message, dict) else [],
        "message_content_type": type(message.get("content")).__name__ if isinstance(message, dict) else None,
        "message_content_length": len(message.get("content") or "") if isinstance(message, dict) else 0,
        "has_reasoning_content": bool(message.get("reasoning_content")) if isinstance(message, dict) else False,
        "has_tool_calls": bool(message.get("tool_calls")) if isinstance(message, dict) else False,
        "has_refusal": bool(message.get("refusal")) if isinstance(message, dict) else False,
        "raw_response_truncated": raw[:12000],
    }
    path.write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _call_provider(provider_id: str, provider_cfg: dict[str, Any], prompt: str, schema: dict[str, Any]) -> str:
    client = provider_cfg.get("client")
    if client is not None:
        return client(prompt=prompt, schema=schema)
    if provider_cfg.get("enabled") is not True:
        raise RuntimeError(f"provider {provider_id} is registered but not enabled")
    if str(provider_cfg.get("kind") or "") == "command_adapter":
        return _call_command_provider(provider_id, provider_cfg, prompt)
    if str(provider_cfg.get("kind") or "") in {"openai_chat_completion", "openai_compatible"}:
        return _call_openai_chat_provider(provider_id, provider_cfg, prompt)
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
