#!/usr/bin/env python3
"""Registered Hermes command adapter for quant AI-provider calls."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import shutil
import subprocess
import sys


def clean_output(text: str) -> str:
    stripped = text.strip()
    fence = re.search(r"```(?:yaml|yml|json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        stripped = fence.group(1).strip()
    lines = [
        line
        for line in stripped.splitlines()
        if not line.strip().lower().startswith("session_id:")
    ]
    return "\n".join(lines).strip()


def hermes_command() -> str:
    found = shutil.which("hermes")
    if found:
        return found
    candidates = [
        Path.home() / ".local/bin/hermes",
        Path.home() / ".hermes/hermes-agent/venv/bin/hermes",
        Path.home() / ".hermes/hermes-agent/hermes",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError("hermes command not found in PATH or known install locations")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Call Hermes once and print cleaned machine-readable output.")
    parser.add_argument("--timeout-seconds", type=int, default=600)
    args = parser.parse_args(argv)
    prompt = sys.stdin.read()
    if not prompt.strip():
        print("empty prompt", file=sys.stderr)
        return 2
    wrapped_prompt = (
        "You are the registered Hermes provider for the quant workflow.\n"
        "Return only the requested raw YAML or JSON object. No Markdown fences. "
        "No prose. Do not use tools. Do not write files. Do not start processes.\n\n"
        f"{prompt}"
    )
    result = subprocess.run(
        [hermes_command(), "-z", wrapped_prompt],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=args.timeout_seconds,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr or result.stdout or "")
        return result.returncode
    output = clean_output(result.stdout or "")
    if not output:
        print("Hermes returned empty output", file=sys.stderr)
        return 3
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
