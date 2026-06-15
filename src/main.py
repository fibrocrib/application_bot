"""Orchestrator. Run once per cron tick (typically daily)."""

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from pathlib import Path

import yaml

from . import (applier, careers_page, cv, cv_tailor, discover, filters,
               matcher, notifier, state, writer)
from .models import ApplicationResult

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("app_bot")

CONFIG_PATH = Path("config/queries.yaml")
STATEMENTS_DIR = Path("statements")


def main() -> int:
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    queries = cfg["queries"]
    profile_path = Path(cfg.get("profile_path", "config/profile.yaml"))
    if not profile_path.exists():
        raise SystemExit(
            f"{profile_path} not found. Copy config/profile.example.yaml → "
            f"{profile_path} and fill it in (it is gitignored)."
        )
    profile = yaml.safe_load(profile_path.read_text())
    cv_path = cfg["cv_path"]
    daily_cap = int(cfg.get("daily_cap", 10))
    threshold = float(cfg.get("fit_threshold", 0.6))
    salary_floor = int(cfg.get("salary_floor_gbp", 40_000))
    base_location = cfg.get("base_location", "London, UK")

    # DRY_RUN_OVERRIDE is set by manual workflow_dispatch runs ("true"/"false"
    # to force, anything else falls through to the config value).
    override = (os.environ.get("DRY_RUN_OVERRIDE") or "").strip().lower()
    if override == "true":
        dry_run = True
        log.info("dry_run forced TRUE by workflow input")
    elif override == "false":
        dry_run = False
        log.info("dry_run forced FALSE by workflow input")
    else:
        dry_run = bool(cfg.get("dry_run", False))
    if dry_run:
        log.info("DRY RUN — forms will be filled but not submitted")
    log.info("salary floor: £%s    base location: %s", f"{salary_floor:,}", base_location)

    cv_text = cv.load_text(cv_path)
    seen = state.load()

    gov_uk_queries = cfg.get("gov_uk_queries") or []
    gov_eu_queries = cfg.get("gov_eu_queries") or []
    log.info("discovering leads from %d private + %d UK-gov + %d EU-gov queries",
             len(queries), len(gov_uk_queries), len(gov_eu_queries))
    leads = discover.discover(
        queries,
        gov_uk_queries=gov_uk_queries,
        gov_eu_queries=gov_eu_queries,
        per_query_limit=cfg.get("per_query_limit", 40),
    )
    log.info("discovered %d unique leads", len(leads))

    results: list[ApplicationResult] = []
    applied_count = 0

    for lead in leads:
        if applied_count >= daily_cap:
            log.info("daily cap reached (%d), stopping", daily_cap)
            break
        if state.already_seen(seen, lead.company, lead.title):
            continue

        # 1. cheap salary regex
        ok, why = filters.salary_ok(lead.description or "", floor_gbp=salary_floor)
        if not ok:
            r = ApplicationResult(lead=lead, status="skipped",
                                   reason=f"salary: {why}")
            results.append(r)
            _record(seen, lead, r, 0.0)
            continue

        # 2. fit
        verdict = matcher.score(
            cv_text, lead.title, lead.description or lead.title,
            location=lead.location, threshold=threshold,
        )
        if not verdict.should_apply:
            r = ApplicationResult(lead=lead, status="skipped",
                                   reason=f"fit {verdict.score:.2f}: {verdict.reason}")
            results.append(r)
            _record(seen, lead, r, verdict.score)
            continue

        # 3. relocation
        locv = filters.location_ok(
            lead.title, lead.company, lead.location,
            lead.description or "", base_location=base_location,
        )
        if not locv.worth_it:
            r = ApplicationResult(lead=lead, status="skipped",
                                   reason=f"relocation not worth it: {locv.reason}",
                                   fit_score=verdict.score)
            results.append(r)
            _record(seen, lead, r, verdict.score)
            continue

        careers = careers_page.resolve(lead.company)
        if not careers:
            r = ApplicationResult(lead=lead, status="skipped",
                                   reason="careers page not resolved",
                                   fit_score=verdict.score)
            results.append(r)
            _record(seen, lead, r, verdict.score)
            continue

        try:
            statement = writer.write(
                cv_text=cv_text,
                job_title=lead.title,
                company=lead.company,
                job_description=lead.description or lead.title,
                out_dir=STATEMENTS_DIR,
            )
        except Exception as e:
            log.warning("statement write failed for %s/%s: %s", lead.company, lead.title, e)
            r = ApplicationResult(lead=lead, status="failed",
                                   reason=f"statement: {e}",
                                   fit_score=verdict.score)
            results.append(r)
            _record(seen, lead, r, verdict.score)
            continue

        try:
            _, tailored_cv_path = cv_tailor.tailor(
                cv_text=cv_text,
                job_title=lead.title,
                company=lead.company,
                job_description=lead.description or lead.title,
                out_dir=STATEMENTS_DIR,
            )
        except Exception as e:
            log.warning("cv tailor failed for %s/%s: %s — falling back to default cv",
                        lead.company, lead.title, e)
            tailored_cv_path = cv_path

        out = applier.apply(applier.AgentInput(
            careers_url=careers,
            role_title=lead.title,
            company=lead.company,
            profile=profile,
            cv_path=tailored_cv_path,
            statement_text=statement.text,
            statement_pdf_path=statement.pdf_path,
            dry_run=dry_run,
        ))

        r = ApplicationResult(
            lead=lead, status=out.status, reason=out.note,
            role_url=out.role_url, fit_score=verdict.score,
            statement=statement.text, artefacts=out.screenshots,
            dry_run=dry_run,
        )
        results.append(r)
        # Persist only real submissions / verdicts. Dry-run "applied" stays
        # eligible so the same role gets a real attempt once you flip dry_run off.
        if not (dry_run and r.status == "applied"):
            _record(seen, lead, r, verdict.score)
        if r.status == "applied":
            applied_count += 1

    state.save(seen)

    try:
        notifier.send_digest(results)
    except Exception as e:
        log.warning("digest send failed: %s", e)

    log.info("done — applied=%d skipped=%d failed=%d",
             applied_count,
             sum(1 for r in results if r.status == "skipped"),
             sum(1 for r in results if r.status == "failed"))
    return 0


def _record(seen: dict, lead, r: ApplicationResult, fit: float) -> None:
    key = state.make_key(lead.company, lead.title)
    seen[key] = state.Record(
        key=key, company=lead.company, role=lead.title,
        status=r.status, reason=r.reason, role_url=r.role_url,
        fit_score=fit, timestamp=dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
    )


if __name__ == "__main__":
    sys.exit(main())
