"""Thin wrapper around `claude -p` for one-shot completions, billing against
the Max plan via CLAUDE_CODE_OAUTH_TOKEN.

Used by matcher, careers_page, writer, cv_tailor, and filters.location_ok —
everything that needs a single completion (with optional WebSearch). The
agentic applier uses claude-agent-sdk instead."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from typing import Iterable

log = logging.getLogger(__name__)

MODEL_FAST = "haiku"
MODEL_SMART = "sonnet"

_CLI = shutil.which("claude") or "claude"

# Per-call timeout (s). Web search loops can take a while.
DEFAULT_TIMEOUT = 300.0

# Retry on 429s / transient subprocess errors with exponential backoff.
RETRY_DELAYS = (2, 6, 16, 40, 90)


def complete(
    prompt: str,
    *,
    system: str | None = None,
    model: str = MODEL_FAST,
    allowed_tools: Iterable[str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Run a one-shot `claude -p` and return the assistant's final text.

    `allowed_tools` is a list of built-in tool names like ['WebSearch'] —
    pass None or [] to disallow all tools (pure text completion).
    """
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        raise RuntimeError(
            "CLAUDE_CODE_OAUTH_TOKEN not set. Generate one with `claude "
            "setup-token` and store as a repo secret."
        )

    args = [_CLI, "-p", prompt, "--model", model, "--output-format", "json"]
    if system is not None:
        args += ["--system-prompt", system]

    # --tools controls which built-in tools the CLI may use. None / empty
    # means "no tools" (text-only completion). Otherwise, restrict to the
    # given list and pre-approve them so the CLI doesn't prompt.
    tool_list = list(allowed_tools or [])
    args += ["--tools", ",".join(tool_list)]
    if tool_list:
        args += ["--allowedTools", ",".join(tool_list)]

    last_err: Exception | None = None
    for attempt, delay in enumerate((0,) + RETRY_DELAYS):
        if delay:
            log.info("claude -p retry in %ds (attempt %d)", delay, attempt)
            time.sleep(delay)
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired as e:
            last_err = e
            continue

        if proc.returncode == 0:
            return _extract_result(proc.stdout)

        # Non-zero exit. If stderr mentions rate-limit, retry; otherwise raise.
        stderr = proc.stderr or ""
        if _is_transient(stderr):
            last_err = RuntimeError(f"claude -p transient error: {stderr[:200]}")
            continue
        raise RuntimeError(
            f"claude -p failed (exit {proc.returncode}): {stderr[:500] or proc.stdout[:500]}"
        )

    raise RuntimeError(f"claude -p exhausted retries: {last_err}")


def _extract_result(stdout: str) -> str:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        # Sometimes the CLI prints stream-style lines before the final JSON.
        # Take the last JSON-looking line.
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    data = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
        else:
            raise RuntimeError(f"claude -p output not JSON: {stdout[:400]}")
    text = (data.get("result") or "").strip()
    if not text:
        raise RuntimeError(f"claude -p returned no result text: {data}")
    return text


def _is_transient(stderr: str) -> bool:
    s = stderr.lower()
    return any(k in s for k in (
        "rate limit", "rate_limit", "429", "overloaded", "503",
        "timeout", "temporarily", "try again",
    ))
