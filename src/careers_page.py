"""Resolve a company name to its official careers page URL."""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache

import requests

from . import claude_subprocess as cs

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


def resolve(company: str, direct_url: str | None = None) -> str | None:
    """Pick the best URL for the agent to start on.

    If the aggregator gave us a direct-to-employer URL that resolves, prefer it
    — that's the exact role page, no Phase A search needed. Otherwise fall back
    to guessing the company's careers root (cached per-company).
    """
    if direct_url and _verify(direct_url):
        return direct_url
    return _resolve_company(company)


@lru_cache(maxsize=2048)
def _resolve_company(company: str) -> str | None:
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
        text = cs.complete(f"Company: {company}", system=SYSTEM, model=cs.MODEL_FAST)
    except Exception as e:
        log.warning("careers_page claude call failed for %r: %s", company, e)
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return (json.loads(m.group(0)).get("url") or "").strip() or None
    except Exception as e:
        log.warning("careers_page parse failed for %r: %s", company, e)
        return None


_SOFT_404_MARKERS = (
    "page not found",
    "page you requested was not found",
    "this job is no longer available",
    "this position has been filled",
    "this posting has been closed",
    "no longer accepting applications",
    "0 jobs found",
    "no openings",
    "no results found",
)


def _verify(url: str) -> bool:
    try:
        r = requests.get(url, headers={"User-Agent": UA},
                         timeout=10, allow_redirects=True)
    except requests.RequestException:
        return False
    if not (200 <= r.status_code < 400) or len(r.text) < 500:
        return False
    # Ashby/Greenhouse/Workday all return 200 OK with a "Page not found"
    # body for stale roles — treat that as a miss so we don't waste 60
    # browser turns on a dead page.
    low = r.text.lower()
    return not any(m in low for m in _SOFT_404_MARKERS)
