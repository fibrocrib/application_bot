"""EU institution jobs via EPSO / EU Careers (eu-careers.europa.eu).

No public API; we hit the public job-opportunities search and parse listings."""

from __future__ import annotations

import logging
import urllib.parse

import requests
from bs4 import BeautifulSoup

from ..models import JobLead

log = logging.getLogger(__name__)

BASE = "https://eu-careers.europa.eu"
SEARCH = f"{BASE}/en/job-opportunities"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/127.0 Safari/537.36"


def search(term: str, limit: int = 30) -> list[JobLead]:
    r = requests.get(
        SEARCH,
        params={"search_api_fulltext": term},
        headers={"User-Agent": UA},
        timeout=30,
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    leads: list[JobLead] = []
    for card in soup.select("article, .view-content .views-row"):
        link = card.find("a", href=True)
        if not link:
            continue
        href = link["href"]
        if href.startswith("/"):
            href = BASE + href
        title = link.get_text(strip=True)

        inst_el = card.select_one(".institution, .field--name-field-institution")
        institution = inst_el.get_text(strip=True) if inst_el else "European Union Institution"

        loc_el = card.select_one(".location, .field--name-field-place-of-employment")
        location = loc_el.get_text(strip=True) if loc_el else ""

        leads.append(JobLead(
            title=title,
            company=institution,
            location=location,
            description="",
            source="gov_eu",
            source_url=href,
        ))
        if len(leads) >= limit:
            break
    return leads
