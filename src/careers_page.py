"""Resolve a company name to its official careers page URL."""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache

import requests

from .claude_client import MODEL_FAST, client

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/127.0 Safari/537.36"

SYSTEM = """Given a company name, return ONLY the most likely URL of their official
job listings / careers page. Prefer the canonical careers index (e.g.
boards.greenhouse.io/<company>, jobs.lever.co/<company>, jobs.ashbyhq.com/<company>,
<company>.com/careers, etc.) — not a specific role. If the company uses a known
ATS, return that ATS URL directly.

If you genuinely don't know, return "unknown".

Respond with strict JSON: {"url": "<url or 'unknown'>"} — no other text."""


@lru_cache(maxsize=2048)
def resolve(company: str) -> str | None:
    """Return a careers URL we could verify, or None."""
    candidates: list[str] = []

    guess = _ask_claude(company)
    if guess and guess != "unknown":
        candidates.append(guess)

    slug = re.sub(r"[^a-z0-9]+", "", company.lower())
    candidates.extend([
        f"https://boards.greenhouse.io/{slug}",
        f"https://jobs.lever.co/{slug}",
        f"https://jobs.ashbyhq.com/{slug}",
        f"https://{slug}.com/careers",
        f"https://www.{slug}.com/careers",
        f"https://{slug}.com/jobs",
        f"https://careers.{slug}.com",
    ])

    seen: set[str] = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        if _verify(url):
            return url
    return None


def _ask_claude(company: str) -> str | None:
    try:
        resp = client().messages.create(
            model=MODEL_FAST,
            max_tokens=200,
            system=SYSTEM,
            messages=[{"role": "user", "content": f"Company: {company}"}],
        )
        text = resp.content[0].text.strip()
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        url = (json.loads(m.group(0)).get("url") or "").strip()
        return url or None
    except Exception as e:
        log.warning("claude careers resolve failed for %r: %s", company, e)
        return None


def _verify(url: str) -> bool:
    try:
        r = requests.get(url, headers={"User-Agent": UA},
                         timeout=10, allow_redirects=True)
        return 200 <= r.status_code < 400 and len(r.text) > 500
    except requests.RequestException:
        return False
