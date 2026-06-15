"""Discovery via job aggregators using JobSpy.

We never submit applications via these sources — they're read-only discovery
of (company, role, location, description) which we then resolve to the
company's own careers page.
"""

from __future__ import annotations

import logging

from ..models import JobLead

log = logging.getLogger(__name__)

# Glassdoor's connector rejects country-level locations like "United Kingdom"
# (HTTP 400 "location not parsed"). It needs numeric IDs or specific city
# strings — not worth the maintenance burden given LinkedIn + Indeed already
# cover its listings. Add it back per-query via `sites:` if you want it.
DEFAULT_SITES = ["linkedin", "indeed", "google"]


def search(
    term: str,
    location: str | None,
    country: str = "uk",
    sites: list[str] | None = None,
    limit: int = 30,
    hours_old: int = 48,
) -> list[JobLead]:
    """Pull listings from the configured aggregator sites."""
    from jobspy import scrape_jobs

    sites = sites or DEFAULT_SITES
    df = scrape_jobs(
        site_name=sites,
        search_term=term,
        location=location,
        results_wanted=limit,
        hours_old=hours_old,
        country_indeed=country,
        linkedin_fetch_description=True,
    )
    if df is None or df.empty:
        return []

    out: list[JobLead] = []
    for _, row in df.iterrows():
        company = str(row.get("company") or "").strip()
        title = str(row.get("title") or "").strip()
        if not company or not title:
            continue
        out.append(JobLead(
            title=title,
            company=company,
            location=str(row.get("location") or "").strip(),
            description=str(row.get("description") or "").strip(),
            source=str(row.get("site") or "aggregator"),
            source_url=str(row.get("job_url") or ""),
        ))
    return out
