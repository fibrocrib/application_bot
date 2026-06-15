"""Score how well a job fits the CV using Claude."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from .claude_client import MODEL_FAST, client

log = logging.getLogger(__name__)


@dataclass
class FitVerdict:
    score: float  # 0..1
    reason: str
    should_apply: bool


SYSTEM = """You evaluate whether a candidate should apply to a specific job.
Be honest. Reject roles that are seniority-mismatched, geographically wrong,
require skills the candidate clearly lacks, or look like spam reposts.

Respond with strict JSON, no other text:
{"score": <0..1 float>, "reason": "<one sentence>"}"""


def score(cv_text: str, job_title: str, job_description: str,
          location: str = "", threshold: float = 0.6) -> FitVerdict:
    user = (
        f"=== CV ===\n{cv_text}\n\n"
        f"=== JOB ===\n"
        f"Title: {job_title}\n"
        f"Location: {location}\n\n"
        f"Description:\n{job_description}\n"
    )
    resp = client().messages.create(
        model=MODEL_FAST,
        max_tokens=300,
        system=SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    text = resp.content[0].text.strip()
    try:
        data = json.loads(_extract_json(text))
        s = float(data["score"])
        reason = str(data.get("reason", "")).strip()
    except Exception as e:
        log.warning("matcher failed to parse %r: %s", text[:200], e)
        return FitVerdict(0.0, "parse error", False)
    return FitVerdict(s, reason, s >= threshold)


def _extract_json(s: str) -> str:
    m = re.search(r"\{.*\}", s, re.S)
    return m.group(0) if m else s
