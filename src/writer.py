"""Write a customised personal statement / short cover letter for a job.

Anthropic's web_search tool is enabled so Claude can look up anything it
needs about the company/team/role rather than leaving a placeholder. The
output is saved as both .txt (for paste-into-textarea fields) and .pdf (for
file-upload fields)."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from . import claude_subprocess as cs

log = logging.getLogger(__name__)


@dataclass
class Statement:
    text: str
    txt_path: str
    pdf_path: str


SYSTEM = """You write short, specific, honest cover letters for job applications.

ABSOLUTE OUTPUT RULES — your reply will be uploaded verbatim to the company:
- Output ONLY the body of the letter. No preamble like "Here is", no "Dear Hiring Manager",
  no sign-off, no signature, no "[Your Name]" — just the body paragraphs.
- NEVER include inline citations, source titles, link text, web-search result
  titles, or any text in square brackets. If you searched the web, synthesise
  what you learned into your own prose — do not quote titles or URLs.
- NEVER include placeholder text in any form: no [Your Name], no [Specific
  Project], no {company}, no "(insert ...)", no TODOs, no "your X here".
  Every sentence must be publishable as-is.
- NEVER ask the user for clarification or extra information. You have access to
  the web_search tool — if you need a specific fact about the company (their
  product, mission, recent funding, a relevant news item, the team's focus),
  search for it. Do not leave a gap.
- NEVER invent CV facts the candidate doesn't have. If the CV doesn't support a
  claim, don't make it.

CONTENT RULES:
- 180-260 words, plain prose, first person, no bullet points, no headings.
- Open with one specific sentence on why this role at this company (use something
  real you learned — from JD or web_search).
- Two short paragraphs of evidence from the CV mapped to the JD's requirements.
- Close with availability/enthusiasm in one sentence — still no signature."""


# Bracket patterns we treat as REAL placeholders (vs. web-search citations).
# Web-search citations are typically multi-word titles like "[FOSDEM 2026 - ...]";
# real placeholders use specific stand-in words.
_PLACEHOLDER_WORDS = (
    r"your |insert |specify |add |name|company|date|project|role|position|"
    r"link|url|email|phone|title|todo|tbd|x\b|here\b"
)
PLACEHOLDER_PATTERNS = [
    rf"(?i)\[\s*(?:{_PLACEHOLDER_WORDS})[^\]\n]{{0,80}}\]",
    r"\{[^}\n]{1,80}\}",
    r"\(insert[^)]{1,80}\)",
    r"\byour [a-z ]{0,30}here\b",
    r"\bTODO\b",
    r"\bTBD\b",
]


def _strip_citation_brackets(text: str) -> str:
    """Drop bracket wrappers around what's obviously a citation/title, keeping
    the inner text so the prose stays readable. Anything that looks like a
    real placeholder is left intact so _assert_clean can still fail it."""
    def replace(m: re.Match) -> str:
        inside = m.group(1)
        for pat in PLACEHOLDER_PATTERNS:
            if re.search(pat, m.group(0)):
                return m.group(0)  # actual placeholder — keep brackets to fail check
        # Looks like a citation/title — keep inner text, drop the brackets.
        return inside
    return re.sub(r"\[([^\]\n]{1,160})\]", replace, text)


def write(cv_text: str, job_title: str, company: str,
          job_description: str, out_dir: str | Path,
          extra_guidance: str = "") -> Statement:
    user = (
        f"=== CV ===\n{cv_text}\n\n"
        f"=== JOB ===\n"
        f"Company: {company}\n"
        f"Role: {job_title}\n\n"
        f"Description:\n{job_description}\n"
    )
    if extra_guidance:
        user += f"\n=== EXTRA GUIDANCE ===\n{extra_guidance}\n"

    text = _generate(user)
    text = _strip_preamble(text)
    _assert_clean(text)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"statement_{int(time.time() * 1000)}"
    txt_path = out / f"{stem}.txt"
    pdf_path = out / f"{stem}.pdf"
    txt_path.write_text(text, encoding="utf-8")
    _to_pdf(text, pdf_path, job_title=job_title, company=company)
    return Statement(text=text, txt_path=str(txt_path), pdf_path=str(pdf_path))


def _generate(user_msg: str, attempts: int = 2) -> str:
    last_err: Exception | None = None
    for _ in range(attempts):
        text = cs.complete(
            user_msg,
            system=SYSTEM,
            model=cs.MODEL_SMART,
            allowed_tools=["WebSearch"],
        )
        text = _strip_citation_brackets(text)
        try:
            _assert_clean(text)
            return text
        except ValueError as e:
            last_err = e
            log.warning("statement had placeholders, regenerating: %s", e)
    raise RuntimeError(f"writer could not produce a clean statement: {last_err}")


def _strip_preamble(text: str) -> str:
    # Drop any "Here is..." / "Sure, here's..." opener that slipped through.
    text = re.sub(r"^(here(?:'s| is)[^\n]*\n+)", "", text, flags=re.I)
    text = re.sub(r"^(sure[^.\n]*[.\n]+)", "", text, flags=re.I)
    return text.strip()


def _assert_clean(text: str) -> None:
    if not text or len(text) < 200:
        raise ValueError("statement too short")
    for pat in PLACEHOLDER_PATTERNS:
        m = re.search(pat, text, re.I)
        if m:
            raise ValueError(f"placeholder found: {m.group(0)!r}")


def _to_pdf(text: str, path: Path, job_title: str, company: str) -> None:
    doc = SimpleDocTemplate(
        str(path), pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
    )
    styles = getSampleStyleSheet()
    body = styles["BodyText"]
    body.fontSize = 11
    body.leading = 15
    flow = []
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        flow.append(Paragraph(para.replace("\n", "<br/>"), body))
        flow.append(Spacer(1, 0.3 * cm))
    doc.build(flow)
