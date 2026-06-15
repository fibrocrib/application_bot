"""Orchestrator. Run once per cron tick (typically daily)."""

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

from . import (applier, careers_page, cv, cv_tailor, discover, filters,
               notifier, state, writer)
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
    filter_workers = int(cfg.get("filter_concurrency", 6))

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
    total = len(leads)
    fresh_leads = [l for l in leads
                   if not state.already_seen(seen, l.company, l.title)]
    log.info("processing %d leads (%d fresh, %d already seen)",
             total, len(fresh_leads), total - len(fresh_leads))

    # -------- Phase 1: parallel filter --------
    log.info("phase 1: salary + fit + relocation, max_workers=%d",
             filter_workers)
    filter_results: list[filters.FilterResult] = []
    with ThreadPoolExecutor(max_workers=filter_workers) as pool:
        futures = {
            pool.submit(filters.evaluate_lead, lead,
                        cv_text=cv_text, salary_floor=salary_floor,
                        threshold=threshold, base_location=base_location): lead
            for lead in fresh_leads
        }
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                fr = fut.result()
            except Exception as e:
                lead = futures[fut]
                log.warning("[%d/%d] evaluate failed for %s/%s: %s",
                            i, len(fresh_leads), lead.company, lead.title, e)
                fr = filters.FilterResult(lead=lead, skip_reason=f"eval error: {e}")
            verdict = "PASS" if fr.passed else f"SKIP — {fr.skip_reason}"
            log.info("[%d/%d] %s — %s @ %s  · %s",
                     i, len(fresh_leads), fr.lead.company, fr.lead.title,
                     fr.lead.location or "?", verdict)
            filter_results.append(fr)

    passes = [fr for fr in filter_results if fr.passed]
    log.info("phase 1 done: %d pass / %d skip", len(passes),
             len(filter_results) - len(passes))

    # Record every skip up-front; passes are recorded after the apply attempt.
    for fr in filter_results:
        if not fr.passed:
            r = ApplicationResult(lead=fr.lead, status="skipped",
                                   reason=fr.skip_reason or "",
                                   fit_score=fr.fit_score)
            results.append(r)
            _record(seen, fr.lead, r, fr.fit_score)

    # -------- Phase 2: sequential apply --------
    log.info("phase 2: careers → writer → tailor → applier (sequential, cap=%d)",
             daily_cap)

    for idx, fr in enumerate(passes, 1):
        if applied_count >= daily_cap:
            log.info("daily cap reached (%d), stopping", daily_cap)
            break

        lead = fr.lead
        verdict = fr.verdict
        log.info("[%d/%d apply] %s — %s @ %s  · fit %.2f",
                 idx, len(passes), lead.company, lead.title,
                 lead.location or "?", verdict.score)

        careers = careers_page.resolve(lead.company)
        if not careers:
            log.info("    careers page: SKIP — could not resolve")
            r = ApplicationResult(lead=lead, status="skipped",
                                   reason="careers page not resolved",
                                   fit_score=verdict.score)
            results.append(r)
            _record(seen, lead, r, verdict.score)
            continue
        log.info("    careers page: %s", careers)

        try:
            log.info("    writing personal statement...")
            statement = writer.write(
                cv_text=cv_text,
                job_title=lead.title,
                company=lead.company,
                job_description=lead.description or lead.title,
                out_dir=STATEMENTS_DIR,
            )
        except Exception as e:
            log.warning("    statement: FAIL — %s", e)
            r = ApplicationResult(lead=lead, status="failed",
                                   reason=f"statement: {e}",
                                   fit_score=verdict.score)
            results.append(r)
            _record(seen, lead, r, verdict.score)
            continue
        log.info("    statement: %d words → %s", len(statement.text.split()), statement.pdf_path)

        try:
            log.info("    tailoring CV summary for this role...")
            _, tailored_cv_path = cv_tailor.tailor(
                cv_text=cv_text,
                job_title=lead.title,
                company=lead.company,
                job_description=lead.description or lead.title,
                out_dir=STATEMENTS_DIR,
            )
            log.info("    tailored CV: %s", tailored_cv_path)
        except Exception as e:
            log.warning("    cv tailor failed (%s) — falling back to default CV", e)
            tailored_cv_path = cv_path

        log.info("    running browser agent...")
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
        log.info("    agent: %s (%d steps) — %s", out.status, out.steps_used, out.note)

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
            log.info("    applied count: %d / %d", applied_count, daily_cap)

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
        fit_score=fit,
        timestamp=dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )


if __name__ == "__main__":
    sys.exit(main())
