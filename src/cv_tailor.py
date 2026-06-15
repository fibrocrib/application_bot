"""Rewrite the CV's personal statement (the top paragraph) for a specific job,
then build a per-application cv.pdf.

The body of the CV (work history, projects, skills, education) is never
changed — only the summary paragraph is tailored. Same anti-placeholder and
no-extra-info rules as the cover-letter writer apply."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import build_cv  # at project root; importable when run from there

from .claude_client import MODEL_SMART, client

log = logging.getLogger(__name__)


SYSTEM = """You rewrite the personal-statement paragraph that sits at the top
of a CV so it lines up with one specific job. Your output replaces the existing
summary verbatim — it must be ready to render in a PDF.

ABSOLUTE OUTPUT RULES:
- Output ONLY the new summary paragraph. No preamble, no "Here is", no
  bullet points, no headings, no quotes around it, no sign-off.
- NEVER include placeholder text: no [brackets], no {curly braces}, no
  "(insert ...)", no TODOs, no "your specific X here".
- NEVER ask the user for extra information. You have web_search — use it to
  look up the company, product, or team if you need to.
- NEVER invent skills, employers, or achievements that aren't in the CV.
  Re-emphasise the existing facts in the angle the JD cares about.

CONTENT RULES:
- 70-110 words, one paragraph, plain prose, first person.
- Open with a concrete framing tying the candidate's background to this
  company's domain or this role's focus. If the JD names a technology or
  product area the CV genuinely covers, lead with it.
- One or two sentences of evidence drawn from the CV: degrees, prior roles,
  named projects. Pick the angles relevant to this JD.
- One closing sentence on what the candidate is looking for, framed to fit
  the JD."""


PLACEHOLDER_PATTERNS = [
    r"\[[^\]\n]{1,80}\]",
    r"\{[^}\n]{1,80}\}",
    r"\(insert[^)]{1,80}\)",
    r"\byour [a-z ]{0,30}here\b",
    r"\bTODO\b",
    r"\bTBD\b",
]


def tailor(cv_text: str, job_title: str, company: str,
           job_description: str, out_dir: str | Path) -> tuple[str, str]:
    """Generate a job-tailored summary, build a CV PDF using it, and return
    (summary_text, pdf_path)."""
    summary = _generate(cv_text, job_title, company, job_description)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    pdf_path = out / f"cv_{int(time.time() * 1000)}.pdf"
    build_cv.build(path=str(pdf_path), summary=summary)
    return summary, str(pdf_path)


def _generate(cv_text: str, job_title: str, company: str,
              job_description: str, attempts: int = 2) -> str:
    user = (
        f"=== EXISTING CV (do not invent facts beyond this) ===\n{cv_text}\n\n"
        f"=== CURRENT DEFAULT SUMMARY (rewrite this) ===\n"
        f"{build_cv.DEFAULT_SUMMARY}\n\n"
        f"=== JOB ===\n"
        f"Company: {company}\n"
        f"Role: {job_title}\n\n"
        f"Description:\n{job_description}\n"
    )
    last_err: Exception | None = None
    for _ in range(attempts):
        resp = client().messages.create(
            model=MODEL_SMART,
            max_tokens=900,
            system=SYSTEM,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 3,
            }],
            messages=[{"role": "user", "content": user}],
        )
        text_parts = [b.text for b in resp.content
                      if getattr(b, "type", None) == "text" and getattr(b, "text", None)]
        text = _strip_preamble("\n".join(text_parts).strip())
        try:
            _assert_clean(text)
            return text
        except ValueError as e:
            last_err = e
            log.warning("tailored summary rejected, retrying: %s", e)
    raise RuntimeError(f"cv_tailor could not produce a clean summary: {last_err}")


def _strip_preamble(text: str) -> str:
    text = re.sub(r"^(here(?:'s| is)[^\n]*\n+)", "", text, flags=re.I)
    text = re.sub(r"^(sure[^.\n]*[.\n]+)", "", text, flags=re.I)
    return text.strip().strip('"').strip("'")


def _assert_clean(text: str) -> None:
    if not text or len(text) < 120:
        raise ValueError("summary too short")
    if len(text.split()) > 140:
        raise ValueError("summary too long")
    for pat in PLACEHOLDER_PATTERNS:
        m = re.search(pat, text, re.I)
        if m:
            raise ValueError(f"placeholder found: {m.group(0)!r}")
