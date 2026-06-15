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

    for q in queries:
        term = q["term"]
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

    for q in gov_uk_queries or []:
        term = q["term"]
        try:
            take(gov_uk.search(term, limit=per_query_limit))
        except Exception as e:
            log.warning("gov_uk search failed for %r: %s", term, e)

    for q in gov_eu_queries or []:
        term = q["term"]
        try:
            take(gov_eu.search(term, limit=per_query_limit))
        except Exception as e:
            log.warning("gov_eu search failed for %r: %s", term, e)

    return leads
