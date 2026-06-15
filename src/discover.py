"""Multi-source job discovery.

Each source has its own query list because the vocabulary is different:
  - aggregators use private-sector titles ("software engineer")
  - UK Civil Service uses DDaT vocab ("software developer", grades)
  - EU institutions use EPSO vocab ("ICT specialist", "administrator")

The bot only reads from these sources — it never submits applications through
them. The applier always goes to the company's own careers page.
"""

from __future__ import annotations

import logging

from .models import JobLead
from .sources import aggregators, gov_eu, gov_uk

log = logging.getLogger(__name__)

__all__ = ["JobLead", "discover"]


def discover(
    queries: list[dict],
    gov_uk_queries: list[dict] | None = None,
    gov_eu_queries: list[dict] | None = None,
    per_query_limit: int = 30,
) -> list[JobLead]:
    """Run every source for every relevant query and return deduped leads."""
    seen: set[tuple[str, str]] = set()
    leads: list[JobLead] = []

    def take(new_leads):
        for lead in new_leads:
            if lead.dedupe_key() in seen:
                continue
            seen.add(lead.dedupe_key())
            leads.append(lead)

    for i, q in enumerate(queries, 1):
        term = q["term"]
        before = len(leads)
        try:
            take(aggregators.search(
                term,
                q.get("location"),
                country=q.get("country", "uk"),
                sites=q.get("sites"),
                limit=per_query_limit,
            ))
        except Exception as e:
            log.warning("aggregator search failed for %r: %s", term, e)
        log.info("  [%d/%d aggregator] %r @ %s — %d new leads (total %d)",
                 i, len(queries), term, q.get("location") or "*",
                 len(leads) - before, len(leads))

    for i, q in enumerate(gov_uk_queries or [], 1):
        term = q["term"]
        before = len(leads)
        try:
            take(gov_uk.search(term, limit=per_query_limit))
        except Exception as e:
            log.warning("gov_uk search failed for %r: %s", term, e)
        log.info("  [%d/%d gov_uk] %r — %d new leads (total %d)",
                 i, len(gov_uk_queries), term, len(leads) - before, len(leads))

    for i, q in enumerate(gov_eu_queries or [], 1):
        term = q["term"]
        before = len(leads)
        try:
            take(gov_eu.search(term, limit=per_query_limit))
        except Exception as e:
            log.warning("gov_eu search failed for %r: %s", term, e)
        log.info("  [%d/%d gov_eu] %r — %d new leads (total %d)",
                 i, len(gov_eu_queries), term, len(leads) - before, len(leads))

    return leads
