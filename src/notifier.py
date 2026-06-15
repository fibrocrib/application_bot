"""Daily email digest of the run's applications.

Uses SMTP creds from env (Gmail app password works fine).
Required env: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, DIGEST_TO."""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

from .models import ApplicationResult

log = logging.getLogger(__name__)


def send_digest(results: list[ApplicationResult]) -> None:
    if not results:
        log.info("no results to digest")
        return

    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    to = os.environ.get("DIGEST_TO")
    if not all([host, user, pw, to]):
        log.warning("SMTP env not fully set; skipping digest")
        return

    applied = [r for r in results if r.status == "applied"]
    skipped = [r for r in results if r.status == "skipped"]
    failed = [r for r in results if r.status == "failed"]

    tag = "[DRY RUN] " if results and all(r.dry_run for r in results) else ""
    subject = f"{tag}Application bot — {len(applied)} applied, {len(skipped)} skipped, {len(failed)} failed"
    body = _render(applied, skipped, failed)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(body)

    with smtplib.SMTP(host, port) as s:
        s.starttls()
        s.login(user, pw)
        s.send_message(msg)
    log.info("digest sent to %s", to)


def _render(applied: list[ApplicationResult], skipped: list[ApplicationResult],
            failed: list[ApplicationResult]) -> str:
    lines = []
    lines.append(f"APPLIED ({len(applied)})")
    lines.append("=" * 60)
    for r in applied:
        lines.append(f"• {r.lead.company} — {r.lead.title}")
        lines.append(f"  fit: {r.fit_score:.2f}  |  {r.role_url}")
        if r.statement:
            preview = r.statement[:240].replace("\n", " ")
            lines.append(f"  statement: {preview}…")
        lines.append("")

    lines.append("")
    lines.append(f"SKIPPED ({len(skipped)})")
    lines.append("=" * 60)
    for r in skipped:
        lines.append(f"• {r.lead.company} — {r.lead.title}  ({r.reason})")

    lines.append("")
    lines.append(f"FAILED ({len(failed)})")
    lines.append("=" * 60)
    for r in failed:
        lines.append(f"• {r.lead.company} — {r.lead.title}  ({r.reason})")
        if r.role_url:
            lines.append(f"  last url: {r.role_url}")
    return "\n".join(lines)
