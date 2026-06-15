"""Build cv.pdf from the content in cv_content.py.

  python3 build_cv.py

Personal CV data lives in cv_content.py (gitignored). Copy
cv_content.example.py → cv_content.py and fill it in. In CI, the workflow
materialises cv_content.py from the CV_CONTENT_B64 secret.

For per-application tailoring, `src/cv_tailor.py` imports `build()` from this
module and passes a custom `summary` + `path` to produce a per-job CV PDF.
"""

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer

try:
    from cv_content import (CONTACT, DEFAULT_SUMMARY, EDUCATION, NAME,
                            PROJECTS, SKILLS, WORK)
except ImportError as e:
    raise SystemExit(
        "cv_content.py not found. Copy cv_content.example.py to cv_content.py "
        "and fill in your details (it is gitignored)."
    ) from e


# ---------- layout ----------

styles = getSampleStyleSheet()
name_style = ParagraphStyle(
    "name", parent=styles["Title"], fontSize=20, leading=24,
    spaceAfter=2, alignment=0,
)
contact_style = ParagraphStyle(
    "contact", parent=styles["BodyText"], fontSize=9, leading=12,
    textColor="#555555", spaceAfter=10,
)
section_style = ParagraphStyle(
    "section", parent=styles["Heading2"], fontSize=11, leading=14,
    textColor="#222222", spaceBefore=10, spaceAfter=3,
    borderPadding=0, textTransform="uppercase",
)
role_style = ParagraphStyle(
    "role", parent=styles["BodyText"], fontSize=10.5, leading=13,
    spaceBefore=3, spaceAfter=1,
)
dates_style = ParagraphStyle(
    "dates", parent=styles["BodyText"], fontSize=8.5, leading=11,
    textColor="#666666", spaceAfter=2,
)
body_style = ParagraphStyle(
    "body", parent=styles["BodyText"], fontSize=10, leading=13,
    spaceAfter=4,
)
bullet_style = ParagraphStyle(
    "bullet", parent=styles["BodyText"], fontSize=10, leading=13,
    leftIndent=12, bulletIndent=0, spaceAfter=2,
)


def section(title: str):
    return [Paragraph(f"<b>{title}</b>", section_style)]


def bullets(items: list[str]):
    flow = [Paragraph(f"-&nbsp;&nbsp;{t}", bullet_style) for t in items]
    flow.append(Spacer(1, 4))
    return flow


def role_block(entry: dict):
    flow = [
        Paragraph(f"<b>{entry['company']}</b> — {entry['role']}", role_style),
        Paragraph(entry["dates"], dates_style),
    ]
    if entry.get("bullets"):
        flow += bullets(entry["bullets"])
    if entry.get("detail"):
        flow.append(Paragraph(entry["detail"], body_style))
    return flow


def edu_block(entry: dict):
    flow = [
        Paragraph(f"<b>{entry['school']}</b> — {entry['degree']}", role_style),
        Paragraph(entry["dates"], dates_style),
    ]
    if entry.get("detail"):
        flow.append(Paragraph(entry["detail"], body_style))
    return flow


def build(path: str = "cv.pdf", summary: str | None = None):
    """Render the CV PDF.

    `summary` overrides the top personal-statement paragraph. Pass a
    job-tailored summary from `src/cv_tailor.py` to produce a per-application
    CV; pass None to use the default.
    """
    doc = SimpleDocTemplate(
        path, pagesize=A4,
        leftMargin=1.8 * cm, rightMargin=1.8 * cm,
        topMargin=1.5 * cm, bottomMargin=1.5 * cm,
        title=f"{NAME} — CV", author=NAME,
    )
    flow = [
        Paragraph(NAME, name_style),
        Paragraph(CONTACT, contact_style),
        Paragraph(summary or DEFAULT_SUMMARY, body_style),
    ]
    flow += section("Experience")
    for w in WORK:
        flow.append(KeepTogether(role_block(w)))

    flow += section("Personal projects")
    for p in PROJECTS:
        block = [Paragraph(f"<b>{p['title']}</b>", role_style)]
        block += bullets(p["bullets"])
        flow.append(KeepTogether(block))

    flow += section("Skills")
    skills_block = [Paragraph(f"<b>{label}:</b> {value}", body_style)
                    for label, value in SKILLS]
    flow.append(KeepTogether(skills_block))

    flow += section("Education")
    for e in EDUCATION:
        flow.append(KeepTogether(edu_block(e)))

    doc.build(flow)
    print(f"wrote {path}")


if __name__ == "__main__":
    build()
