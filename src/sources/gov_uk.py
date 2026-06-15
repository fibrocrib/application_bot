"""UK Civil Service Jobs discovery (civilservicejobs.service.gov.uk).

No public API. We hit the search endpoint and parse listing cards. The
'company' is set to the recruiting department, since that's what the
careers-page resolver needs to point at."""

from __future__ import annotations

import logging
import re
import urllib.parse

import requests
from bs4 import BeautifulSoup

from ..models import JobLead

log = logging.getLogger(__name__)

BASE = "https://www.civilservicejobs.service.gov.uk"
SEARCH = f"{BASE}/csr/index.cgi"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/127.0 Safari/537.36"


def search(term: str, limit: int = 30) -> list[JobLead]:
    params = {
        "pageclass": "Jobs",
        "pageaction": "searchresults",
        "searchpage": "ResultsSearchType%3D2&SearchString=" + urllib.parse.quote(term),
    }
    r = requests.get(SEARCH, params=params,
                     headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    leads: list[JobLead] = []
    for card in soup.select(".csr-job-link, .search-result"):
        link = card.find("a", href=True)
        if not link:
            continue
        url = link["href"]
        if url.startswith("/"):
            url = BASE + url
        title = link.get_text(strip=True)

        # Department often appears inside the same card as a sibling line.
        dept_el = card.find_next(string=re.compile(r"\w+"))
        department = (dept_el or "UK Civil Service").strip()[:120]

        loc_el = card.select_one(".location, .csr-job-location")
        location = loc_el.get_text(strip=True) if loc_el else ""

        leads.append(JobLead(
            title=title,
            company=department,
            location=location,
            description="",
            source="gov_uk",
            source_url=url,
        ))
        if len(leads) >= limit:
            break
    return leads
