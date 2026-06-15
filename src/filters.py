"""Pre-application filters.

Order of use (cheapest first):
  1. salary_ok   — regex sweep of the JD for a posted £ range. Free.
  2. (matcher)   — Claude scores CV fit (already in matcher.py).
  3. location_ok — Claude evaluates whether a non-London role is worth
                   relocating for. One Haiku call, short-circuits when the
                   role is already London or remote."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from . import claude_subprocess as cs
from .matcher import FitVerdict, score as matcher_score
from .models import JobLead

log = logging.getLogger(__name__)


# ---------- salary ----------

# Match £ amounts. We require the £ symbol to avoid false positives on bare
# numbers, and we plausibility-filter to 15k..500k (annual salary range).
# Hourly rates like "£40 per hour" are excluded because 40 < 15000.
# Alternation orders \d{4,6} first so "40000" doesn't get parsed as "400".
_GBP_SINGLE_RE = re.compile(
    r"£\s*(\d{4,6}|\d{1,3}(?:,\d{3})*)\s*([kK]|,?\s*000)?"
)
# Shared "k" suffix range: £50-70k, £85–110k.
_GBP_RANGE_K_RE = re.compile(
    r"£\s*(\d{1,3})\s*[-–—]\s*(\d{1,3})\s*[kK]\b"
)


def extract_salary_range_gbp(text: str) -> tuple[int, int] | None:
    """Return (lo, hi) of plausible annual £ salaries found in text, or None."""
    nums: list[int] = []
    for m in _GBP_RANGE_K_RE.finditer(text or ""):
        for raw in (m.group(1), m.group(2)):
            n = int(raw) * 1000
            if 15_000 <= n <= 500_000:
                nums.append(n)
    for m in _GBP_SINGLE_RE.finditer(text or ""):
        raw = m.group(1).replace(",", "")
        try:
            n = float(raw)
        except ValueError:
            continue
        suffix = (m.group(2) or "").lower().strip().replace(" ", "")
        if "k" in suffix:
            n *= 1000
        if 15_000 <= n <= 500_000:
            nums.append(int(n))
    if not nums:
        return None
    return min(nums), max(nums)


def salary_ok(jd_text: str, floor_gbp: int) -> tuple[bool, str]:
    """True if the JD's posted salary range meets the floor, OR if no salary
    was posted (we defer to the matcher in that case)."""
    r = extract_salary_range_gbp(jd_text)
    if r is None:
        return True, "no posted salary; deferring"
    lo, hi = r
    if hi < floor_gbp:
        return False, f"max £{hi:,} below floor £{floor_gbp:,}"
    return True, f"posted £{lo:,}–£{hi:,}"


# ---------- relocation ----------

@dataclass
class LocationVerdict:
    worth_it: bool
    reason: str


# Cheap local-or-remote shortcut so we never call Claude on the easy cases.
_LOCAL_KEYWORDS = ("london", "greater london", "remote", "hybrid", "work from home", "wfh")


_LOC_SYSTEM = """You are evaluating whether a UK candidate based in London
should bother applying to a role that would require them to relocate.

Rules:
- If the role's location is London, Greater London, or remote: worth_it=true,
  reason "no relocation needed".
- For roles requiring relocation, worth_it=true ONLY if the role offers
  something materially better than the equivalent London option: a clearly
  higher salary (≥ £10k premium), a notably prestigious employer, or a unique
  domain / once-in-a-career opportunity. Otherwise worth_it=false.
- Default to false when uncertain — better to skip than waste an application.

Output strict JSON only: {"worth_it": <bool>, "reason": "<one short sentence>"}.
No other text."""


def location_ok(
    job_title: str,
    company: str,
    location: str,
    jd_text: str,
    base_location: str = "London, UK",
) -> LocationVerdict:
    """Return whether the role's location is fine (or worth relocating for)."""
    loc_lower = (location or "").lower()
    if not loc_lower:
        return LocationVerdict(True, "no location stated; allowing")
    if any(kw in loc_lower for kw in _LOCAL_KEYWORDS):
        return LocationVerdict(True, "London or remote — no relocation needed")

    user = (
        f"Candidate base: {base_location}\n"
        f"Role: {job_title} at {company}\n"
        f"Role location: {location}\n\n"
        f"Description:\n{jd_text[:6000]}\n"
    )
    try:
        text = cs.complete(user, system=_LOC_SYSTEM, model=cs.MODEL_FAST)
    except Exception as e:
        log.warning("location_ok call failed for %s/%s: %s", company, job_title, e)
        # Conservative default on error: skip rather than apply blind.
        return LocationVerdict(False, f"evaluator error: {e}")
    try:
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0)) if m else {}
        return LocationVerdict(
            worth_it=bool(data.get("worth_it", False)),
            reason=str(data.get("reason", "")).strip() or "n/a",
        )
    except Exception as e:
        log.warning("location_ok parse failed: %s — %r", e, text[:200])
        return LocationVerdict(False, "parse error")


# ---------- combined per-lead evaluation (for parallel execution) ----------

@dataclass
class FilterResult:
    """All three filter outputs for one lead. skip_reason is None if the lead
    survives every gate and should proceed to careers_page + applier."""
    lead: JobLead
    verdict: FitVerdict | None = None
    locv: LocationVerdict | None = None
    skip_reason: str | None = None

    @property
    def passed(self) -> bool:
        return self.skip_reason is None

    @property
    def fit_score(self) -> float:
        return self.verdict.score if self.verdict else 0.0


def evaluate_lead(lead: JobLead, *, cv_text: str, salary_floor: int,
                  threshold: float, base_location: str) -> FilterResult:
    """Run the three filter stages for one lead end-to-end. Safe to call from
    many threads concurrently — each underlying Claude call is a separate
    subprocess, so there's no shared SDK state to race."""
    ok, why = salary_ok(lead.description or "", floor_gbp=salary_floor)
    if not ok:
        return FilterResult(lead=lead, skip_reason=f"salary: {why}")

    verdict = matcher_score(
        cv_text, lead.title, lead.description or lead.title,
        location=lead.location, threshold=threshold,
    )
    if not verdict.should_apply:
        return FilterResult(
            lead=lead, verdict=verdict,
            skip_reason=f"fit {verdict.score:.2f}: {verdict.reason}",
        )

    locv = location_ok(
        lead.title, lead.company, lead.location,
        lead.description or "", base_location=base_location,
    )
    if not locv.worth_it:
        return FilterResult(
            lead=lead, verdict=verdict, locv=locv,
            skip_reason=f"relocation not worth it: {locv.reason}",
        )

    return FilterResult(lead=lead, verdict=verdict, locv=locv)
