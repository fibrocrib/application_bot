"""Single Anthropic client shared across modules. Reads ANTHROPIC_API_KEY
from env (set as a GitHub Actions secret in CI)."""

from __future__ import annotations

import os
from functools import lru_cache

from anthropic import Anthropic

MODEL_FAST = "claude-haiku-4-5-20251001"
MODEL_SMART = "claude-sonnet-4-6"


@lru_cache(maxsize=1)
def client() -> Anthropic:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    return Anthropic(api_key=key)
